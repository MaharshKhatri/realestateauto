"""Stream 10: the pre-MLS distress queue, an internal acquisitions list (no outbound e-mail at all).

Why it exists: a house in foreclosure, with a tax lien or held by an estate is usually sold before a listing agent
ever sees it. The desk already collects those signals (clerk lis pendens, CivilView sheriff sales and their
adjournments, tax-sale certificates / delinquent lists, estate owners); this stream stacks them per property, keeps
only properties with NO active listing signal (pre-MLS), and ranks them for the operator once a day in one internal
one-page PDF: town, signal stack, days to the sheriff sale, adjournments left, a likely-equity formula and the FRFPA
track.

Why nothing goes out: soliciting a homeowner in foreclosure is regulated by the NJ Foreclosure Rescue Fraud Prevention
Act (N.J.S.A. 46:10B-53 to -68: 82% of fair market value, a 10-business-day cancellation, statutory notices), so the
desk reaches owners only by letters the operator approves (engine/direct_mail.py), never with a promise to stop or
postpone a sale. `StreamResult.outbound` is always empty.

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

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from core.text import postal_title
from db.models import DistressSignal, Lead, Property, SignalType, SwarmState
from engine.streams.base import (PRIORITY_SPECIALTY, Stream, StreamContext, StreamResult, address_ok, kmoney,
                                 operator_footer, render_table_pdf)
from engine.streams.s3_surplus_overages import (STATUTORY_ADJOURNMENTS, current_status, parse_amount, town_ratios,
                                                town_of, value_data)

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
                            "min_confidence": 45, "urgent_days": 30}

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
                         footer=operator_footer(ctx.operator), col_widths=WIDTHS)
        if len(kept) <= 1 or page_count(path) <= 1:
            return len(kept)
        kept = kept[:-1]


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
            path = ctx.out_dir / f"s10_pre_mls_queue_{y}-W{w:02d}.pdf"
            shown = render(path, rows[:int(lim["max_rows"])], ctx, stats, len(rows))
            res.products = 1
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
        res.outbound = []                                # internal queue: never an e-mail (FRFPA; letters only)
        return res


STREAM = PreMlsDistressStream()
