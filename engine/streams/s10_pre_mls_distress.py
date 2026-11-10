"""Stream 10: the pre-MLS distress queue (internal, for the operator) and a docket sheet for listing agents.

Why it exists: a house in foreclosure, with a tax lien or held by an estate is usually sold before a listing agent
ever sees it. The desk already collects those signals (clerk lis pendens, CivilView sheriff sales and their
adjournments, tax-sale certificates / delinquent lists, estate owners); this stream stacks them per property, keeps
only properties with NO active listing signal (pre-MLS), and ranks them for the operator once a day in one internal
one-page PDF: town, signal stack, days to the sheriff sale, adjournments left, a likely-equity formula and the FRFPA
track.

Why no e-mail goes to an owner: soliciting a homeowner in foreclosure is regulated by the NJ Foreclosure Rescue Fraud
Prevention Act (N.J.S.A. 46:10B-53 to -68: 82% of fair market value, a 10-business-day cancellation, statutory notices),
so the desk reaches owners only by letters the operator approves (engine/direct_mail.py), never with a promise to stop
or postpone a sale. The internal queue goes only to the operator's own inbox (`StreamResult.internal`, written in an
`internal/` folder so nothing that delivers a stream's reports to a client can pick it up).

The listing-agent docket sheet (operator 2026-11-10: "weaponize s10"): a flat-fee research product for licensed agents
who list in a town, never anything sent to a homeowner. Per town, the sheet lists the properties with a lis pendens
filed in the last `agent_recent_days` (30) or a sheriff sale in the next 30 days (plus tax-sale / estate records of the
same 30 days) that are on no active MLS listing. "Not on the MLS" is a claim we can prove: the property carries no
active listing signal, matches no live listing in the listings table, and its ZIP code had a successful listing harvest
within `agent_harvest_days` (7); rows in ZIPs we didn't harvest are left out. Columns: property (street, town), filing,
filed or sale date, docket / sheriff #, "Assessed value (public record)" (MOD-IV, never an opinion of value: N.J.S.A.
45:14F); the sheriff's published upset appears on sheriff-sale rows only, labeled as such, and no debt figure appears
for any other filing. No owner, defendant or plaintiff names; every address passes `address_ok` (Daniel's Law). The
footer says to contact homeowners only as NJ law and the agent's brokerage allow (FRFPA; no rescue promises).
Targets: listing agents in our contacts (LISTING_AGENT, a business e-mail that is no shared office inbox, not on a stop
list or bounced: `firm_outbox.stop_reason`) with a live listing in a town that has a sheet, ranked by the flipper-
connected score (`deal_desk.flipper_agents`), else by how many listings they have in that town; never an agent we are
in deal talks with (`s2_deal_desk_saas.in_deal_talks`). One firm target per e-mail across streams: an existing target
is reused as it is (its stream stays), a new one is created as kind "investor_agent", stream "s10". At most
`agent_pitches_per_run` (6) per run, weekdays only, one per agent per `agent_gap_days` (14) under the key
`s10:agent:<email>:<iso-week>`. Touch 1 is two plain sentences (the true count and one street from the town, no debt
figure, no name, no price); the sheet's masked sample (base.sample_path) and the flat price ($250 a month per town,
no referral fee and nothing tied to a listing or sale: N.J.S.A. 45:15-3) wait as the held Touch 2. A second touch is
one sentence in the first touch's thread.

Fresh adjournments: CivilView's status history only carries sale dates, not when an adjournment was entered, so the
stream keeps a snapshot of every active sale's adjournment count in swarm_state `s10_adjournments` ({signal_id:
adjournments_total}). A sale whose count rose since the last run while its status reads "Adjourned ..." just used one
(the owner or lender bought time: a live conversation window); the day it was seen goes in `s10_adjournments_fresh`
and the row stays "fresh" for `fresh_days` (7). The first run only records the baseline. A dry run (ctx.send False)
reads the snapshot and writes nothing. Statute: five adjournments in all, two at the debtor's request, two at the
lender's, one by agreement, more only by court order (N.J.S.A. 2A:17-36 as amended by P.L.2019, c.71), so
"adjournments left" = 5 - adjournments used (never below 0).

Rows the s2 deal digest may also show (engine.deal_desk.deal_row: sheriff sales with room over the upset and
pre-foreclosures) are marked, so the operator knows a buyer may see the same house. No names anywhere (owner,
defendant, plaintiff); every row passes `address_ok` (Daniel's Law / stop suppressions); the public run log gets counts.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from core.text import clean, postal_title, street_key
from db.models import (Contact, ContactType, DistressSignal, FirmOutbox, FirmTarget, Lead, Listing, Property,
                       SignalType, SwarmState)
from engine.streams.base import (PRIORITY_SPECIALTY, SAMPLE_FULL_ROWS, Outbound, Stream, StreamContext, StreamResult,
                                 address_ok, kmoney, money, operator_footer, render_table_pdf, sample_path, slug,
                                 week_of)
from engine.streams.s3_surplus_overages import (CLOSED as SALE_CLOSED, STATUTORY_ADJOURNMENTS, current_status,
                                                parse_amount, sheriff_no, town_ratios, town_of, value_data)

log = logging.getLogger(__name__)
SNAPSHOT_KEY = "s10_adjournments"
FRESH_KEY = "s10_adjournments_fresh"
INPUTS = (SignalType.LIS_PENDENS, SignalType.SHERIFF_SALE, SignalType.TAX_SALE_CERTIFICATE, SignalType.TAX_DELINQUENT,
          SignalType.ESTATE_OWNER)
LISTED = (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)
TAX = (SignalType.TAX_SALE_CERTIFICATE, SignalType.TAX_DELINQUENT)
CLOSED_STAGES = ("DEAD", "DO_NOT_CONTACT", "CLOSED")
RESIDENTIAL = ("2", "4C", "")
ADJOURNED = re.compile(r"\badj", re.I)              # "Adjourned - Defendant", "Defendant Adjournment", "... 8 WEEK ADJ"
NOT_ADJOURNED = re.compile(r"\b(no|not|never)\s+adj", re.I)

DEFAULTS: dict[str, Any] = {"max_signals": 6_000, "max_rows": 16, "fresh_days": 7, "new_filing_days": 7,
                            "min_confidence": 45, "urgent_days": 30,
                            # the listing-agent docket sheet (research_firm.streams.s10 may override any of these)
                            "agent_pitches_per_run": 6, "agent_price_monthly": 250, "agent_gap_days": 14,
                            "agent_recent_days": 30, "agent_min_rows": 3, "agent_sheet_rows": 18,
                            "agent_harvest_days": 7}

# FRFPA tracks (engine/frfpa_classifier.py) in the operator's words
TRACK_LABELS = {
    "EXEMPT_ENTITY": "exempt: entity owner", "EXEMPT_VACANT": "presumed exempt: vacant",
    "EXEMPT_ESTATE": "presumed exempt: estate, not occupied", "EXEMPT_NON_OWNER_OCCUPIED": "presumed exempt: absentee",
    "EXEMPT_CONFIRMED": "exempt: occupancy confirmed", "NOT_IN_FORECLOSURE": "not in foreclosure",
    "OWNER_OCCUPIED": "covered: owner-occupied", "ESTATE_VERIFY_OCCUPANCY": "covered until occupancy is confirmed",
    "CONFIRMED_OWNER_OCCUPIED": "covered: owner-occupied (confirmed)",
}


# -- pure helpers -------------------------------------------------------------------------------------------------
def is_adjourned(status: str | None) -> bool:
    s = status or ""
    return bool(ADJOURNED.search(s)) and not NOT_ADJOURNED.search(s)


def adjournments_left(total: Any) -> int:
    """5 - adjournments used, never below 0 (N.J.S.A. 2A:17-36)."""
    try:
        used = int(total or 0)
    except (TypeError, ValueError):
        used = 0
    return max(0, STATUTORY_ADJOURNMENTS - used)


def fresh_adjournment(prev: int | None, total: int, status: str | None, baseline: bool) -> bool:
    """True when this sale used an adjournment since the last snapshot: the count rose and the status says adjourned.
    No baseline yet (first run) or a sale we hadn't seen before: not fresh (we can't tell when it happened)."""
    return baseline and prev is not None and total > prev and is_adjourned(status)


def frfpa_label(track: str | None, occupancy: str | None) -> str:
    """The lead's FRFPA track, else "presumed covered" while occupancy is owner / heir / unknown."""
    if track:
        return TRACK_LABELS.get(track, track.replace("_", " ").lower())
    occ = (occupancy or "UNKNOWN").upper()
    if occ in ("UNKNOWN", "OWNER_OCCUPIED", "HEIR_OCCUPIED"):
        return "presumed covered"
    return f"exempt: confirmed {occ.lower().replace('_', '-')}"


def rank_score(families: int, fresh: bool, days_to_sale: int | None, adj_left: int | None, equity: float | None,
               new_filing: bool, urgent_days: int = 30) -> float:
    """More signal families, a just-used adjournment, a near sale, few adjournments left, equity and a new filing all
    move a row up (each term is small and explainable); a house under water sinks to the bottom."""
    score = 10.0 * families + (15 if fresh else 0) + (5 if new_filing else 0)
    if days_to_sale is not None and 0 <= days_to_sale <= urgent_days:
        score += 10
    if adj_left is not None and adj_left <= 1:
        score += 5
    if equity is not None:
        # under water (a short sale at best) goes below every row with equity; $375K+ of equity caps at +15
        score += -100.0 if equity < 0 else min(equity / 25_000, 15.0)
    return round(score, 2)


def signed_k(v: float | None) -> str:
    """$K with the sign in front ("-$198K"), for equity that can be negative."""
    if v is None:
        return "-"
    return ("-" if v < 0 else "") + f"${abs(v) / 1000:,.0f}K"


@dataclass
class QueueRow:
    lead_id: int | None
    street: str
    town: str
    county: str
    stack: list[str]
    days_to_sale: int | None
    sale_passed: bool
    adj_left: int | None
    fresh: bool
    equity: float | None
    equity_note: str
    frfpa: str
    s2: bool
    rank: float = 0.0
    families: set[str] = field(default_factory=set)

    def cells(self, i: int) -> list[str]:
        if self.days_to_sale is not None:
            sale = f"{self.days_to_sale} days" if not self.sale_passed else "date passed: check"
        else:
            sale = "-"
        adj = "-" if self.adj_left is None else f"{self.adj_left} of {STATUTORY_ADJOURNMENTS}" + (
            "<br/><b>just adjourned</b>" if self.fresh else "")
        eq = f"<b>{signed_k(self.equity)}</b><br/>{self.equity_note}" if self.equity is not None else self.equity_note
        return [str(i), f"<b>#{self.lead_id}</b> {self.street}<br/>{self.town}, {self.county}", "<br/>".join(self.stack),
                sale, adj, eq, self.frfpa, "yes" if self.s2 else ""]


# -- state --------------------------------------------------------------------------------------------------------
def _state(session: Session, key: str) -> dict[str, Any] | None:
    row = session.get(SwarmState, key)
    return dict(row.value_json or {}) if row is not None else None


def _save(session: Session, key: str, value: dict[str, Any]) -> None:
    row = session.get(SwarmState, key)
    if row is None:
        session.add(SwarmState(key=key, value_json=value))
    else:
        row.value_json = value                           # a new object, so the JSON change is flushed


# -- the queue ----------------------------------------------------------------------------------------------------
def _family(sig: DistressSignal) -> str:
    return {SignalType.LIS_PENDENS: "lis pendens", SignalType.SHERIFF_SALE: "sheriff sale",
            SignalType.TAX_SALE_CERTIFICATE: "tax", SignalType.TAX_DELINQUENT: "tax",
            SignalType.ESTATE_OWNER: "estate"}.get(sig.signal_type, sig.signal_type.lower())


def _stack_lines(prop: Property, fresh_ids: set[int], today: date) -> tuple[list[str], set[str]]:
    lines: list[str] = []
    fams: set[str] = set()
    for s in sorted(prop.active_signals, key=lambda x: x.signal_type):
        t = s.signal_type
        if t == SignalType.LIS_PENDENS:
            filed = f" filed {s.filing_date:%m/%d/%y}" if s.filing_date else ""
            kind = "tax-lien foreclosure" if "tax" in (s.status or "").lower() else "lis pendens"
            lines.append(f"{kind}{filed}")
        elif t == SignalType.SHERIFF_SALE:
            st = current_status(s.status, (s.raw_metadata_json or {}).get("status_history"))
            lines.append("sheriff sale" + (" (adjourned)" if is_adjourned(st) else "")
                         + (" <b>new adj.</b>" if s.id in fresh_ids else ""))
        elif t == SignalType.TAX_SALE_CERTIFICATE:
            lines.append("tax sale certificate" + (f" {kmoney(s.amount_owed)}" if s.amount_owed else ""))
        elif t == SignalType.TAX_DELINQUENT:
            lines.append("tax delinquent" + (f" {kmoney(s.amount_owed)}" if s.amount_owed else ""))
        elif t == SignalType.ESTATE_OWNER:
            lines.append("estate owner")
        elif t == SignalType.VACANT:
            lines.append("vacant")
        elif t in (SignalType.LONG_TERM_ABSENTEE, SignalType.OUT_OF_STATE_OWNER):
            lines.append("absentee owner" if t == SignalType.LONG_TERM_ABSENTEE else "out-of-state owner")
        else:
            continue
        if t in INPUTS:
            fams.add(_family(s))
        elif t == SignalType.VACANT:
            fams.add("vacant")
    seen: set[str] = set()
    out = [x for x in lines if not (x in seen or seen.add(x))]
    return out[:5], fams


def _debt(prop: Property) -> float | None:
    """engine.underwriter.known_debt (payoff + prior liens) plus tax-lien amounts on record."""
    from engine.underwriter import known_debt
    taxes = sum(parse_amount(s.amount_owed) or 0 for s in prop.active_signals if s.signal_type in TAX)
    d = (known_debt(prop) or 0) + taxes
    return d or None


def _s2_shows(session: Session, lead: Lead | None, today: date) -> bool:
    if lead is None:
        return False
    try:
        from engine.deal_desk import deal_row
        return deal_row(session, lead, today) is not None
    except Exception:                                    # noqa: BLE001 - a flag, never a reason to fail the queue
        return False


def build_queue(session: Session, ctx: StreamContext, lim: dict[str, Any]
                ) -> tuple[list[QueueRow], dict[str, int], dict[str, Any] | None]:
    """The ranked queue, run counts, and the new adjournment state to save (None = leave the state as it is)."""
    from engine.equity import estimate_equity
    today = ctx.now.date()
    stats = {k: 0 for k in ("signals_examined", "properties", "listed_skipped", "closed_skipped", "non_residential",
                            "suppressed", "new_filings", "fresh_adjournments", "baseline", "s2_rows")}
    snap = _state(session, SNAPSHOT_KEY)
    baseline = snap is not None
    stats["baseline"] = int(not baseline)
    fresh_seen = _state(session, FRESH_KEY) or {}
    sigs = list(session.scalars(
        select(DistressSignal).where(DistressSignal.signal_type.in_([str(t) for t in INPUTS]),
                                     DistressSignal.active.is_(True))
        .options(selectinload(DistressSignal.property).selectinload(Property.signals),
                 selectinload(DistressSignal.property).selectinload(Property.valuations),
                 selectinload(DistressSignal.property).selectinload(Property.lead))
        .order_by(DistressSignal.id).limit(int(lim["max_signals"]))))
    stats["signals_examined"] = len(sigs)
    new_snap: dict[str, int] = {}
    cutoff = today - timedelta(days=int(lim["fresh_days"]))
    fresh_ids: set[int] = set()
    for s in sigs:
        if s.signal_type != SignalType.SHERIFF_SALE:
            continue
        meta = s.raw_metadata_json or {}
        total = int(meta.get("adjournments_total") or 0)
        new_snap[str(s.id)] = total
        prev = (snap or {}).get(str(s.id))
        status = current_status(s.status, meta.get("status_history"))
        if fresh_adjournment(int(prev) if prev is not None else None, total, status, baseline):
            fresh_seen[str(s.id)] = today.isoformat()
            stats["fresh_adjournments"] += 1
    for sid, day in list(fresh_seen.items()):
        try:
            keep = date.fromisoformat(str(day)) >= cutoff and sid in new_snap
        except ValueError:
            keep = False
        if keep:
            fresh_ids.add(int(sid))
        else:
            fresh_seen.pop(sid)
    new_since = today - timedelta(days=int(lim["new_filing_days"]))
    props: dict[int, Property] = {}
    qualifies: set[int] = set()
    for s in sigs:
        p = s.property
        if p is None:
            continue
        props[p.id] = p
        if s.signal_type == SignalType.SHERIFF_SALE:
            if s.id in fresh_ids:
                qualifies.add(p.id)
        else:
            qualifies.add(p.id)
        if s.signal_type == SignalType.LIS_PENDENS and (
                (s.filing_date and s.filing_date >= new_since)
                or (s.first_seen_at and s.first_seen_at.date() >= new_since)):
            stats["new_filings"] += 1
    need_ratio = {str(props[i].mun_code) for i in qualifies if props[i].mun_code
                  and (props[i].current_valuation is None
                       or not (props[i].current_valuation.assumptions_json or {}).get("subject_equalized"))}
    ratios = town_ratios(session, need_ratio)
    rows: list[QueueRow] = []
    for pid in sorted(qualifies):
        p = props[pid]
        stats["properties"] += 1
        if any(s.signal_type in LISTED for s in p.active_signals):
            stats["listed_skipped"] += 1                 # on the MLS: s1 / the listing-agent desk handles it
            continue
        lead = p.lead
        if lead is not None and lead.status_stage in CLOSED_STAGES:
            stats["closed_skipped"] += 1
            continue
        if (p.property_class or "").strip() not in RESIDENTIAL:
            stats["non_residential"] += 1
            continue
        if not address_ok(session, p):
            stats["suppressed"] += 1
            continue
        stack, fams = _stack_lines(p, fresh_ids, today)
        sheriff = [s for s in p.active_signals if s.signal_type == SignalType.SHERIFF_SALE]
        dates = sorted(s.scheduled_auction_date for s in sheriff if s.scheduled_auction_date)
        upcoming = [d for d in dates if d >= today]
        days = (upcoming[0] - today).days if upcoming else ((dates[-1] - today).days if dates else None)
        passed = bool(dates) and not upcoming
        adj_left = min((adjournments_left((s.raw_metadata_json or {}).get("adjournments_total")) for s in sheriff),
                       default=None)
        fresh = any(s.id in fresh_ids for s in sheriff)
        vd = value_data(p, ratios, float(lim["min_confidence"]))
        debt = _debt(p)
        equity = estimate_equity(vd.value, p.last_sale_price, p.last_sale_date, known_debt=debt) if vd.value else None
        note = (f"{'eq. assessment' if vd.basis == 'equalized assessment' else vd.basis} - "
                + ("payoff / liens" if debt else "est. purchase mortgage")) if vd.value else "no value on record"
        new_filing = any(s.signal_type == SignalType.LIS_PENDENS and s.filing_date and s.filing_date >= new_since
                         for s in p.active_signals)
        s2 = _s2_shows(session, lead, today)
        stats["s2_rows"] += int(s2)
        row = QueueRow(lead_id=lead.id if lead else None, street=postal_title(p.street_address) or "-",
                       town=town_of(p), county=postal_title(p.county or ""), stack=stack or ["-"], days_to_sale=days,
                       sale_passed=passed, adj_left=adj_left, fresh=fresh, equity=equity, equity_note=note,
                       frfpa=frfpa_label(lead.frfpa_track if lead else None, lead.occupancy_status if lead else None),
                       s2=s2, families=fams)
        row.rank = rank_score(len(fams), fresh, None if passed else days, adj_left, equity, new_filing,
                              int(lim["urgent_days"]))
        rows.append(row)
    rows.sort(key=lambda r: (-r.rank, r.lead_id or 0))
    state = {"snapshot": new_snap, "fresh": fresh_seen} if ctx.send else None
    return rows, stats, state


HEAD = ["#", "Lead / property", "Signal stack", "Days to sale", "Adj. left", "Likely equity*", "FRFPA", "s2"]
WIDTHS = [0.3, 2.3, 2.2, 0.85, 0.85, 1.55, 1.55, 0.5]


def render(path: Path, rows: list[QueueRow], ctx: StreamContext, stats: dict[str, int], total: int) -> int:
    """The internal one-page queue; returns how many rows fit (the lowest-ranked go first)."""
    from engine.streams.s3_surplus_overages import page_count
    today = ctx.now.date()
    sub = (f"Internal: acquisitions queue · {today:%B %-d, %Y} · {total} pre-MLS properties (no active listing) · "
           f"{stats['new_filings']} new filing(s) · {stats['fresh_adjournments']} fresh adjournment(s) · top rows by "
           "signal stack, sale date, adjournments left and equity.")
    notes = [
        "*Likely equity = value figure (as-is formula estimate, else ARV, else assessed value x the town's recorded "
        "sale-to-assessment ratio) - the larger of the known payoff + liens + tax liens and an estimated purchase-money "
        "mortgage balance. Data for ranking, not an appraisal; refinances and HELOCs are invisible in public records.",
        f"Adjournments left = {STATUTORY_ADJOURNMENTS} - used (N.J.S.A. 2A:17-36: 2 debtor, 2 lender, 1 agreed; more "
        "only by court order). s2 = the deal digest may show this house to a buyer too. No owner, defendant or "
        "plaintiff names; every address passed the Daniel's Law / stop list.",
    ]
    box = ("<b>Internal only. No e-mail or call script goes to these owners.</b> Soliciting a homeowner in foreclosure "
           "is regulated by the NJ Foreclosure Rescue Fraud Prevention Act (N.J.S.A. 46:10B-53 to -68): owners are "
           "reached only by letters you approve (manage.py mail-merge), never with a promise to stop or postpone a "
           "sale; a covered owner-occupied house needs 82% of fair market value and the 10-business-day cancellation.")
    kept = list(rows)
    while True:
        render_table_pdf(path, title="Pre-MLS distress queue", subtitle=sub, head=HEAD,
                         rows=[r.cells(i) for i, r in enumerate(kept, 1)], notes=notes, box=box,
                         footer=operator_footer(ctx.operator), col_widths=WIDTHS, write_sample=False)
        if len(kept) <= 1 or page_count(path) <= 1:
            return len(kept)
        kept = kept[:-1]


# -- the listing-agent docket sheet -------------------------------------------------------------------------------
AGENT_KIND = "investor_agent"
AGENT_HEAD = ["#", "Property", "Filing", "Filed or sale date", "Docket / sheriff #", "Assessed value (public record)"]
AGENT_WIDTHS = [0.3, 2.2, 2.6, 1.4, 2.0, 1.5]
AGENT_LEGAL = ("Public-record research for licensed agents; contact homeowners only as NJ law and your brokerage allow "
               "(Foreclosure Rescue Fraud Prevention Act N.J.S.A. 46:10B-53; no rescue promises).")
OPEN_STAGES = ("NEW", "CONTACTED")
SUBSCRIBED = ("TRIAL", "PAYING")
# a listing in any other status (for sale, pending, contingent, unknown) is still on the MLS
NOT_ON_MLS = {"sold", "off_market", "expired", "withdrawn", "cancelled", "canceled"}
_FILING_ORDER = {SignalType.SHERIFF_SALE: 0, SignalType.LIS_PENDENS: 1, SignalType.TAX_SALE_CERTIFICATE: 2,
                 SignalType.TAX_DELINQUENT: 3, SignalType.ESTATE_OWNER: 4}
_DATED = (SignalType.LIS_PENDENS, SignalType.TAX_SALE_CERTIFICATE, SignalType.TAX_DELINQUENT, SignalType.ESTATE_OWNER)


def _utc(now: datetime) -> datetime:
    """ctx.now (naive Eastern) as naive UTC, the convention of first_seen_at / created_at."""
    return now.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc).replace(tzinfo=None)


def _dedupe(xs: list[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in xs if x and not (x in seen or seen.add(x))]


def foreclosure_filing(sig: DistressSignal) -> bool:
    """A clerk lis pendens the lis pendens agent recorded as a foreclosure ("foreclosure complaint filed", "tax-lien
    foreclosure filed"): only those count as "foreclosure filings" in a pitch."""
    return sig.signal_type == SignalType.LIS_PENDENS and "foreclos" in (sig.status or "").lower()


@dataclass
class SheetRow:
    street: str
    town: str
    filings: list[str]
    dates: list[str]
    dockets: list[str]
    assessed: float | None
    filing: bool                          # a foreclosure lis pendens filed in the window
    sale: bool                            # a sheriff sale scheduled in the window
    sort_key: tuple[Any, ...] = ()

    @property
    def foreclosure(self) -> bool:
        return self.filing or self.sale

    def cells(self, i: int) -> list[str]:
        # docket and sheriff # share one line, so a sample's masked rows hide both
        return [str(i), f"<b>{self.street}</b><br/>{self.town}", "<br/>".join(self.filings) or "-",
                "<br/>".join(self.dates) or "-", " · ".join(self.dockets) or "-", money(self.assessed)]


@dataclass
class TownSheet:
    key: str                              # the town's mun_code
    town: str
    county: str
    rows: list[SheetRow] = field(default_factory=list)
    path: Path | None = None
    shown: int = 0

    @property
    def foreclosures(self) -> int:
        return sum(1 for r in self.rows if r.foreclosure)

    @property
    def filings(self) -> int:
        return sum(1 for r in self.rows if r.filing)

    @property
    def sales(self) -> int:
        return sum(1 for r in self.rows if r.sale)

    @property
    def lead_row(self) -> SheetRow:
        """The street Touch 1 names: the first row (foreclosures sort first), so the sample shows it in full."""
        return next(r for r in self.rows if r.foreclosure)

    @property
    def masked(self) -> bool:
        return self.path is not None and sample_path(self.path).exists()

    @property
    def sample(self) -> Path | None:
        return sample_path(self.path) if self.masked else self.path


def harvested_zips(session: Session, now_utc: datetime, days: float) -> set[str]:
    """ZIP codes whose listing harvest succeeded within `days` (swarm_state `stale_listing_harvests`): only there can
    we say a property is not on the MLS."""
    from agents.scrapers.stale_listing_distress_agent import HARVESTS_KEY
    row = session.get(SwarmState, HARVESTS_KEY)
    zips = ((row.value_json or {}).get("zips") or {}) if row is not None else {}
    cutoff = now_utc - timedelta(days=days)
    out: set[str] = set()
    for z, stamps in zips.items():
        for st in stamps or []:
            try:
                if datetime.fromisoformat(str(st)) >= cutoff:
                    out.add(str(z)[:5])
                    break
            except ValueError:
                continue
    return out


def live_listings(session: Session, prop_ids: set[int], zips: set[str]) -> tuple[set[int], set[tuple[str, str]]]:
    """(property ids, (street key, ZIP)) of every stored listing still on the MLS: linked or matched by address."""
    on_mls = or_(Listing.status.is_(None), func.lower(Listing.status).notin_(sorted(NOT_ON_MLS)))
    props = {pid for (pid,) in session.execute(select(Listing.property_id).where(
        Listing.property_id.in_(sorted(prop_ids)), on_mls))} if prop_ids else set()
    keys: set[tuple[str, str]] = set()
    if zips:
        for addr, z in session.execute(select(Listing.address, Listing.zip_code).where(
                func.substr(Listing.zip_code, 1, 5).in_(sorted(zips)), Listing.address.is_not(None), on_mls)):
            keys.add((street_key(addr), (z or "")[:5]))
    return props, keys


def sheet_row(prop: Property, fresh_ids: set[int], today: date) -> SheetRow:
    """One property: every active filing on it (the sheriff's published upset on a sheriff sale only, never a debt
    figure on anything else), dates, public case numbers and the assessment. No names."""
    filings: list[str] = []
    dates: list[str] = []
    dockets: list[str] = []
    filing = sale = False
    next_sale: date | None = None
    newest: date | None = None
    for s in sorted((x for x in prop.active_signals if x.signal_type in INPUTS),
                    key=lambda x: (_FILING_ORDER.get(x.signal_type, 9), x.id)):
        t = s.signal_type
        meta = s.raw_metadata_json or {}
        if t == SignalType.SHERIFF_SALE:
            when = s.scheduled_auction_date
            if when is None or when < today or SALE_CLOSED.search(current_status(s.status, meta.get("status_history"))):
                continue
            upset = parse_amount(meta.get("upset_amount"))
            filings.append("sheriff sale" + (f"<br/>Sheriff's published upset: {money(upset)}" if upset else ""))
            dates.append(f"sale {when:%m/%d/%y}")
            dockets += [clean(s.case_number), f"Sh. #{sheriff_no(s)}" if sheriff_no(s) else ""]
            sale = sale or s.id in fresh_ids
            next_sale = min(next_sale, when) if next_sale else when
        elif t == SignalType.LIS_PENDENS:
            tax = "tax" in (s.status or "").lower()
            filings.append("tax-lien foreclosure (lis pendens)" if tax else
                           "foreclosure (lis pendens)" if foreclosure_filing(s) else "lis pendens")
            if s.filing_date:
                dates.append(f"filed {s.filing_date:%m/%d/%y}")
                newest = max(newest, s.filing_date) if newest else s.filing_date
            dockets.append(clean(s.case_number))
            filing = filing or (s.id in fresh_ids and foreclosure_filing(s))
        elif t == SignalType.TAX_SALE_CERTIFICATE:
            filings.append("tax sale certificate")
            if s.filing_date:
                dates.append(f"certificate {s.filing_date:%m/%d/%y}")
        elif t == SignalType.TAX_DELINQUENT:
            filings.append("tax delinquent")
        elif t == SignalType.ESTATE_OWNER:
            filings.append("estate owner")
    street = postal_title(prop.street_address) or "-"
    first = 0 if (filing or sale) else 1
    key = (first, next_sale.toordinal() if sale and next_sale else 10 ** 7,
           -newest.toordinal() if newest else 0, street)
    return SheetRow(street=street, town=town_of(prop) or "-", filings=_dedupe(filings), dates=_dedupe(dates),
                    dockets=_dedupe(dockets), assessed=float(prop.assessed_total) if prop.assessed_total else None,
                    filing=filing, sale=sale, sort_key=key)


def collect_sheets(session: Session, ctx: StreamContext, lim: dict[str, Any]
                   ) -> tuple[dict[str, TownSheet], dict[str, int]]:
    """Per town (mun_code), the properties with a fresh public record and no MLS listing; only towns with at least
    `agent_min_rows` foreclosure filings / sheriff sales get a sheet."""
    today = ctx.now.date()
    days = int(lim["agent_recent_days"])
    since, horizon = today - timedelta(days=days), today + timedelta(days=days)
    since_utc = _utc(datetime.combine(since, time()))
    stats = {k: 0 for k in ("agent_rows", "agent_rows_listed", "agent_rows_unverified", "agent_rows_suppressed")}
    fresh_by_date = or_(DistressSignal.filing_date >= since,
                        and_(DistressSignal.filing_date.is_(None), DistressSignal.first_seen_at >= since_utc))
    sigs = list(session.scalars(
        select(DistressSignal).where(
            DistressSignal.active.is_(True),
            or_(and_(DistressSignal.signal_type == SignalType.SHERIFF_SALE,
                     DistressSignal.scheduled_auction_date >= today, DistressSignal.scheduled_auction_date <= horizon),
                and_(DistressSignal.signal_type.in_([str(t) for t in _DATED]), fresh_by_date)))
        .options(selectinload(DistressSignal.property).selectinload(Property.signals),
                 selectinload(DistressSignal.property).selectinload(Property.lead))
        .order_by(DistressSignal.id).limit(int(lim["max_signals"]))))
    props: dict[int, Property] = {}
    fresh: dict[int, set[int]] = {}
    for s in sigs:
        p = s.property
        if p is None or not p.mun_code:
            continue
        if s.signal_type == SignalType.SHERIFF_SALE and SALE_CLOSED.search(
                current_status(s.status, (s.raw_metadata_json or {}).get("status_history"))):
            continue
        props[p.id] = p
        fresh.setdefault(p.id, set()).add(s.id)
    if not props:
        return {}, stats
    zips = {(p.zip_code or "")[:5] for p in props.values()}
    on_ids, on_keys = live_listings(session, set(props), zips)
    harvested = harvested_zips(session, _utc(ctx.now), float(lim["agent_harvest_days"]))
    sheets: dict[str, TownSheet] = {}
    for pid in sorted(props):
        p = props[pid]
        z = (p.zip_code or "")[:5]
        if any(s.signal_type in LISTED for s in p.active_signals) or pid in on_ids \
                or (street_key(p.street_address), z) in on_keys:
            stats["agent_rows_listed"] += 1
            continue
        if z not in harvested:
            stats["agent_rows_unverified"] += 1         # no recent harvest of this ZIP: "not on the MLS" unproven
            continue
        if p.lead is not None and p.lead.status_stage in CLOSED_STAGES:
            continue
        if (p.property_class or "").strip() not in RESIDENTIAL:
            continue
        if not address_ok(session, p):
            stats["agent_rows_suppressed"] += 1
            continue
        row = sheet_row(p, fresh[pid], today)
        if not row.filings:
            continue
        key = str(p.mun_code)
        sheet = sheets.setdefault(key, TownSheet(key=key, town=row.town, county=postal_title(p.county or "")))
        sheet.rows.append(row)
        stats["agent_rows"] += 1
    for sh in sheets.values():
        sh.rows.sort(key=lambda r: r.sort_key)
    return ({k: v for k, v in sheets.items() if v.town != "-" and v.foreclosures >= int(lim["agent_min_rows"])},
            stats)


def agent_candidates(session: Session, sheets: dict[str, TownSheet], ctx: StreamContext) -> list[dict[str, Any]]:
    """Listing agents with a live listing in a town that has a sheet: a business e-mail (no shared office inbox), on
    no stop list, not bounced, not in deal talks with us."""
    from engine import firm_outbox as fo
    from engine.deal_desk import OFFICE_MAILBOX
    from engine.streams.s2_deal_desk_saas import in_deal_talks
    agents: dict[str, dict[str, Any]] = {}
    for c, p in session.execute(
            select(Contact, Property).join(Lead, Lead.id == Contact.lead_id).join(Property, Property.id == Lead.property_id)
            .where(Contact.contact_type == ContactType.LISTING_AGENT, Contact.email.is_not(None),
                   Property.mun_code.in_(sorted(sheets)))
            .options(selectinload(Property.signals))).all():
        email = (c.email or "").strip().lower()
        if not email or c.dnc_status == "dnc" or (c.confidence_score or 0) <= 0 or OFFICE_MAILBOX.match(email):
            continue
        if not any(s.signal_type in LISTED and s.active for s in p.signals):
            continue                                     # no live listing: they don't list there now
        a = agents.setdefault(email, {"email": email, "name": "", "company": "", "towns": Counter()})
        a["name"] = a["name"] or (postal_title(c.name) if c.name else "")
        a["company"] = a["company"] or (c.company or "").strip()
        a["towns"][str(p.mun_code)] += 1
    if not agents:
        return []
    talks = in_deal_talks(session, ctx.now)
    return [a for e, a in sorted(agents.items()) if e not in talks and fo.stop_reason(session, e) is None]


def flipper_scores(session: Session, today: date) -> dict[str, float]:
    """deal_desk.flipper_agents' score per e-mail ({} when it can't be computed: ranking falls back to listings)."""
    try:
        from engine.deal_desk import flipper_agents
        return {a["email"]: float(a["score"]) for a in flipper_agents(session, 100_000, today=today)}
    except Exception:                                    # noqa: BLE001 - a ranking input, never a reason to fail
        return {}


def last_touch(session: Session, target_id: int) -> FirmOutbox | None:
    """Our newest s10 agent pitch to this target that went out (a follow-up threads onto it)."""
    return session.scalar(select(FirmOutbox).where(
        FirmOutbox.target_id == target_id, FirmOutbox.stream == "s10", FirmOutbox.kind.in_(("pitch", "followup")),
        FirmOutbox.status == "sent", FirmOutbox.message_id.is_not(None),
        FirmOutbox.key.startswith("s10:agent:", autoescape=True))
        .order_by(FirmOutbox.sent_at.desc(), FirmOutbox.id.desc()).limit(1))


def _greeting(name: str | None) -> str:
    from engine.streams.s2_deal_desk_saas import first_name
    fn = first_name(name)
    return f"Hi {fn}," if fn != "there" else "Hello,"


def agent_touch1(sheet: TownSheet, name: str | None, first: str) -> tuple[str, str]:
    """(subject, body): two plain sentences, the true count and one street in the town; no debt figure, no owner,
    no price, no attachment."""
    n = sheet.foreclosures
    what = (f"{n} new foreclosure filings and sheriff sales" if sheet.filings and sheet.sales
            else f"{n} new foreclosure filings" if sheet.filings else f"{n} upcoming sheriff sales")
    street = sheet.lead_row.street
    body = (f"{_greeting(name)}\n\nWe pulled {what} in {sheet.town} this month that aren't on the MLS, including "
            f"{street}. Want the docket sheet, either to talk with the owners about listing or to work with us on the "
            "investor side?" + (f"\n\n{first}" if first else ""))
    return f"{sheet.town} foreclosures not on the MLS: {street} + {n - 1} more", body


def agent_followup(sheet: TownSheet, name: str | None, first: str) -> str:
    return f"{_greeting(name)}\n\nShould I send the {sheet.town} docket sheet over?" + (f"\n\n{first}" if first else "")


def agent_sample_body(sheet: TownSheet, lim: dict[str, Any]) -> str:
    """Touch 2, sent only after their yes: what is attached, how to use it lawfully, the flat price."""
    price = float(lim.get("agent_price_monthly") or 250)
    lines = [
        (f"Here's the {sheet.town} docket sheet, attached: the first {SAMPLE_FULL_ROWS} properties in full, the rest "
         "with the street and docket hidden." if sheet.masked else f"Here's the {sheet.town} docket sheet, attached."),
        "Please contact owners only as NJ law and your brokerage allow, with no rescue promises.",
        f"The full sheet every week is ${price:,.0f} a month per town, flat, no referral fee and nothing tied to a "
        "listing or sale (N.J.S.A. 45:15-3).",
        'Reply "start" and I\'ll set it up.',
    ]
    return " ".join(lines)


def render_agent_sheet(sheet: TownSheet, ctx: StreamContext, lim: dict[str, Any]) -> Path:
    """The town's one-page docket sheet for licensed agents (and its masked sample when it has 3+ rows)."""
    from engine.streams.s3_surplus_overages import page_count
    y, w, _ = ctx.now.isocalendar()
    path = ctx.out_dir / f"s10_agent_sheet_{slug(sheet.county.lower())}_{slug(sheet.town.lower())}_{y}-W{w:02d}.pdf"
    days = int(lim["agent_recent_days"])
    others = len(sheet.rows) - sheet.foreclosures
    price = float(lim.get("agent_price_monthly") or 250)
    notes = [
        "Assessed value (public record) = the municipal tax assessment as recorded in NJ MOD-IV, shown as recorded: not "
        "a market value, an appraisal or an opinion of value (N.J.S.A. 45:14F). Sheriff's published upset = the figure "
        "printed in the sheriff's sale notice; no debt figure is shown for any other filing.",
        "No owner, defendant or plaintiff names. Every address passed the Daniel's Law / stop list (N.J.S.A. "
        "56:8-166.1). Not on the MLS = no active listing in our harvest of the ZIP code this week; a listing can appear "
        "any day. Verify each docket on eCourts and each sale with the sheriff: sales are often adjourned (N.J.S.A. "
        "2A:17-36), stayed or cancelled.",
    ]
    box = (f"<b>Subscription:</b> this sheet every week for each town you choose, ${price:,.0f} a month per town, flat; "
           "no referral fee and nothing tied to a listing or sale (N.J.S.A. 45:15-3).")
    kept = sheet.rows[:max(1, int(lim["agent_sheet_rows"]))]
    while True:
        shown = f" · {len(kept)} of {len(sheet.rows)} shown" if len(kept) < len(sheet.rows) else ""
        sub = (f"For licensed agents · Week of {week_of(ctx.now)} · {sheet.filings} foreclosure filing(s) from the "
               f"last {days} days, {sheet.sales} sheriff sale(s) in the next {days} days"
               + (f", {others} tax-sale / estate record(s) from the last {days} days" if others else "")
               + f" · none on an active MLS listing{shown} · Sources: county clerk lis pendens, sheriff sale notices "
               "(CivilView), municipal tax-sale records, NJ MOD-IV.")
        render_table_pdf(path, title=f"Foreclosures not on the MLS: {sheet.town}, {sheet.county} County", subtitle=sub,
                         head=AGENT_HEAD, rows=[r.cells(i) for i, r in enumerate(kept, 1)], notes=notes, box=box,
                         footer=[*operator_footer(ctx.operator), AGENT_LEGAL], col_widths=AGENT_WIDTHS)
        if len(kept) <= 1 or page_count(path) <= 1:
            break
        kept = kept[:-1]                                 # one page: the last rows (tax / estate first) go
    if len(kept) <= SAMPLE_FULL_ROWS:
        sample_path(path).unlink(missing_ok=True)        # 1-2 rows: no masked sample, never a stale longer one
    sheet.path, sheet.shown = path, len(kept)
    return path


def subscriber_towns(session: Session) -> set[str]:
    """Towns (mun_code) of s10 targets in TRIAL / PAYING: their sheet is written every run, pitched or not."""
    out: set[str] = set()
    for t in session.scalars(select(FirmTarget).where(FirmTarget.stage.in_(SUBSCRIBED))):
        data = t.data_json if isinstance(t.data_json, dict) else {}
        products = data.get("products") if isinstance(data.get("products"), list) else []
        if (t.stream == "s10" or "s10" in products) and data.get("s10_mun"):
            out.add(str(data["s10_mun"]))
    return out


def agent_pitches(session: Session, ctx: StreamContext, lim: dict[str, Any]
                  ) -> tuple[list[Outbound], dict[str, int], list[str]]:
    """The listing-agent side of s10: town sheets, targets, Touch 1 (or the one-sentence follow-up) with the sample
    held for a yes. Counts only in the metrics."""
    from engine import firm_outbox as fo
    metrics = {"agent_towns": 0, "agent_targets": 0, "agent_pitches": 0, "agent_followups": 0, "agent_sheets": 0}
    notes: list[str] = []
    sheets, stats = collect_sheets(session, ctx, lim)
    metrics.update(stats)
    metrics["agent_towns"] = len(sheets)
    if not sheets:
        return [], metrics, notes
    cands = agent_candidates(session, sheets, ctx)
    metrics["agent_targets"] = len(cands)
    if not ctx.send:
        return [], metrics, notes                        # a dry run counts; it writes no target, sheet or e-mail
    for key in sorted(subscriber_towns(session) & set(sheets)):
        render_agent_sheet(sheets[key], ctx, lim)
        metrics["agent_sheets"] += 1
    if not cands:
        return [], metrics, notes
    if ctx.now.weekday() >= 5:
        notes.append("weekend: no agent pitches")
        return [], metrics, notes
    scores = flipper_scores(session, ctx.now.date())
    for a in cands:
        towns = [k for k in a["towns"] if k in sheets]
        a["mun"] = max(towns, key=lambda k: (a["towns"][k], sheets[k].foreclosures, k))
        a["listings"] = a["towns"][a["mun"]]
        a["flip"] = scores.get(a["email"], 0.0)
    cands.sort(key=lambda a: (-a["flip"], -a["listings"], a["email"]))
    st = fo.cfg()
    max_touches = int(st.get("max_touches", 2))
    pitch_gap = timedelta(days=float(st.get("pitch_gap_days", 6)))
    cutoff = _utc(ctx.now) - timedelta(days=float(lim["agent_gap_days"]))
    y, w, _ = ctx.now.isocalendar()
    first = (ctx.operator or {}).get("first", "")
    out: list[Outbound] = []
    for a in cands:
        if len(out) >= int(lim["agent_pitches_per_run"]):
            break
        email = a["email"]
        t = session.scalar(select(FirmTarget).where(FirmTarget.email == email))
        if t is not None and (t.stage not in OPEN_STAGES or int(t.touches or 0) >= max_touches
                              or (t.last_contact_at and t.last_contact_at > ctx.now - pitch_gap)):
            continue
        prefix = f"s10:agent:{email}:"
        key = f"{prefix}{y}-W{w:02d}"
        if session.scalar(select(FirmOutbox.id).where(
                FirmOutbox.key.startswith(prefix, autoescape=True), FirmOutbox.status != "skipped",
                or_(FirmOutbox.created_at >= cutoff, FirmOutbox.sent_at >= cutoff)).limit(1)) \
                or session.scalar(select(FirmOutbox.id).where(FirmOutbox.key == key)):
            continue                                     # one s10 e-mail per agent per `agent_gap_days`
        prev = last_touch(session, t.id) if t is not None and int(t.touches or 0) > 0 else None
        if prev is not None and prev.kind == "followup":
            continue                                     # a pitch and one follow-up, never more from s10
        sheet = sheets[a["mun"]]
        fields = dict(kind=AGENT_KIND, stream="s10", email=email, org=a["company"] or None, name=a["name"] or None,
                      town=sheet.town, county=sheet.county, source="listing data (MLS)",
                      score=a["flip"] or float(a["listings"]), data={"s10_town": sheet.town, "s10_mun": sheet.key})
        if t is None or t.stream == "s10":
            t = fo.upsert_target(session, **fields)      # a new s10 target, or our own row refreshed
        # an existing target of another stream is reused as it is (one row per e-mail; its stream stays)
        if t is None:
            continue
        if sheet.path is None:
            render_agent_sheet(sheet, ctx, lim)
            metrics["agent_sheets"] += 1
        name = a["name"] or t.name
        subject, body = agent_touch1(sheet, name, first)
        kind, in_reply_to = "pitch", None
        if prev is not None:
            kind, in_reply_to, body = "followup", prev.message_id, agent_followup(sheet, name, first)
            subject = "Re: " + re.sub(r"(?i)^\s*re:\s*", "", prev.subject or subject)
        out.append(Outbound(stream="s10", key=key, to=email, subject=subject, body=body, priority=PRIORITY_SPECIALTY,
                            kind=kind, target_id=t.id, in_reply_to=in_reply_to, attachments=[],
                            sample_body=agent_sample_body(sheet, lim),
                            sample_attachments=[sheet.sample] if sheet.sample else []))
    metrics["agent_pitches"] = len(out)
    metrics["agent_followups"] = sum(1 for o in out if o.kind == "followup")
    return out, metrics, notes


class PreMlsDistressStream(Stream):
    key = "s10"
    name = "Pre-MLS distress"
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        lim = {**DEFAULTS, **{k: v for k, v in (ctx.limits or {}).items() if v is not None}}
        res = StreamResult(stream=self.key)
        rows, stats, state = build_queue(session, ctx, lim)
        res.processed = stats["signals_examined"]
        res.found = len(rows)
        shown = 0
        if rows:
            y, w, _ = ctx.now.isocalendar()
            # its own folder: a client delivery that picks up this run's reports never sees the internal queue
            path = ctx.out_dir / "internal" / f"s10_pre_mls_queue_{y}-W{w:02d}.pdf"
            shown = render(path, rows[:int(lim["max_rows"])], ctx, stats, len(rows))
            res.products = 1
            res.internal.append(path)                    # mailed to the operator's own inbox ("[njw-s10] <date>")
        if state is not None:
            _save(session, SNAPSHOT_KEY, state["snapshot"])
            _save(session, FRESH_KEY, state["fresh"])
            session.flush()
        res.metrics.update(stats)
        res.metrics.update({"rows": len(rows), "rows_shown": shown,
                            "fresh_rows": sum(1 for r in rows if r.fresh),
                            "urgent_rows": sum(1 for r in rows if r.days_to_sale is not None and not r.sale_passed
                                               and r.days_to_sale <= int(lim["urgent_days"]))})
        if stats["baseline"]:
            res.notes.append("first run: adjournment counts recorded as the baseline")
        # the queue's owners never get an e-mail (FRFPA; letters only): the only e-mails are B2B pitches to agents
        try:
            with session.begin_nested():
                res.outbound, agent_metrics, agent_notes = agent_pitches(session, ctx, lim)
        except Exception as exc:                         # noqa: BLE001 - the agent side never costs the internal queue
            log.exception("s10: agent pitches failed")
            res.outbound, agent_metrics = [], {"agent_error": 1, "agent_sheets": 0}
            agent_notes = [f"agent pitches skipped: {type(exc).__name__}"]
        res.products += agent_metrics["agent_sheets"]
        res.metrics.update(agent_metrics)
        res.notes.extend(agent_notes)
        return res


STREAM = PreMlsDistressStream()
