"""Stream 11: NJ sheriff-sale bid sheets for auction bidders (2026-11-10).

Investors who bid at NJ sheriff sales pull the same facts for every sale before they spend money on a title search:
when the sale is, the sheriff number and docket, what the house is, who is foreclosing, the upset the notice prints,
the liens the notice says the sale is subject to, and how many adjournments the sale has left. Everything is public:
the sheriff's listing on CivilView (agents/scrapers/sheriff_sale_agent.py stores `upset_amount`, `judgment_amount`,
`amount_owed` = the payoff estimate, `prior_liens` and the status history in `raw_metadata_json`), the NJ MOD-IV tax
record and the recorded deed sales behind our comparable-sales data.

The product: one bid sheet per county ("s11_bid_sheet_<county>_<date>.pdf", the county in the file name so
`firm_outbox.delivery_files` finds a subscriber's county), every active sale in the next `window_days` (21) ordered by
sale date. Columns: sale date and status, sheriff # and docket, street and town, MOD-IV class / units / year / living
area, the plaintiff (a business only, never a person; never a defendant or an occupant), the upset / judgment as the
notice prints it (the row says which), the stated prior liens, adjournments used and left of the five N.J.S.A.
2A:17-36 allows (P.L.2019, c.71: 2 at the debtor's request, 2 at the lender's, 1 by agreement; court-ordered ones are
extra), the min-max of recorded sale prices for the same product type in the same town (a range of public facts,
never a value) and the rehab tier our cost formula (engine/rehab.py) gave the house when it has a valuation. Each sheet
carries the surviving-lien checklist (municipal liens and tax sale certificates survive, an association's 6-month
priority lien may, the IRS's 120-day redemption, senior mortgages, the 10-day objection / redemption window, the
sheriff's conditions of sale) and "order a title search from a licensed title producer before bidding". A sale whose
address is under a Daniel's Law / stop request (`address_ok`), or with no street on record, is left out.

Who buys it: investors who bid at sheriff sales, $199 a month plus NJ sales tax for every sale in one county or $39
plus NJ sales tax for one property, flat, with no commission or per-deal fee (research data, never pay tied to a deal:
N.J.S.A. 45:15-3). Targets: FirmTarget kinds "sheriff_bidder" and "flipper" assigned to s11 (seeded from
config/b2b_targets.yaml), plus `sync_targets`: prospects with a business e-mail whose buyer family took title by a
sheriff's deed (SR1A NU code 12 or a grantor naming the sheriff, the test `s3_surplus_overages.sheriff_deeds` uses)
become "sheriff_bidder" targets on s11, pitched the county where the family bought most. An e-mail another stream
already owns is skipped (one FirmTarget row per address).

The 2-step funnel: Touch 1 is two plain sentences with no price, link or attachment: one sale from the sheet (a real
street that passed `address_ok`, a sale at least `feature_lead_days` out), the true count of that county's sales on the
sheet and "Want this week's <County> County bid sheet, free?". The held Touch 2 is a one-page sample starting at that
sale (2 rows in full, the rest with the street and sheriff # hidden) with both prices; the outbox sends the in-thread
nudge and close-the-loop note by itself. FRFPA: these are public auction listings for bidders, never a seller
solicitation, and no owner is contacted; the sheet says so.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from core.text import clean, is_lender, muni_base, postal_title
from db.models import (CashBuyer, DeedTransfer, DistressSignal, FirmOutbox, FirmTarget, Property, Prospect, SignalType,
                       Valuation)
from engine.streams.base import (PRIORITY_SPECIALTY, SAMPLE_FULL_ROWS, Outbound, Stream, StreamContext, StreamResult,
                                 address_ok, entity_like, kmoney, masked_rows, money, operator_footer,
                                 render_table_pdf, sample_path, slug)
from engine.streams.s3_surplus_overages import (CLOSED, SHERIFF_DEED_NU, STATEWIDE, STATUTORY_ADJOURNMENTS,
                                                current_status, parse_amount, sheriff_no)

KEY = "s11"
LABEL = "Sheriff-sale bid sheets"
TARGET_KINDS = ("sheriff_bidder", "flipper")
SYNC_KIND = "sheriff_bidder"
DEBTOR_MAX, LENDER_MAX, AGREED_MAX = 2, 2, 1          # N.J.S.A. 2A:17-36 as amended by P.L.2019, c.71
MIN_SALE_PRICE = 40_000                               # recorded prices under this are nominal / package transfers
RESIDENTIAL = {"2", "4C"}

DEFAULTS: dict[str, Any] = {
    "window_days": 21, "price_monthly": 199, "price_property": 39, "pitches_per_run": 3, "feature_lead_days": 3,
    "min_rows_to_pitch": 3, "sample_rows": 16, "max_stale_days": 14, "comp_months": 36, "min_comp_sales": 3,
    "max_signals": 3000, "sync_lookback_days": 1095,
}

CLASS_LABELS = {"1": "vacant land", "2": "residential", "3A": "farm", "3B": "farm (qualified)", "4A": "commercial",
                "4B": "industrial", "4C": "apartments", "5A": "railroad", "5B": "railroad"}

# A sale the sheriff isn't holding on its date: bankruptcy / stay / hold. "Post bankruptcy sale date" and "bankruptcy
# dismissed" are back on the calendar.
ON_HOLD = re.compile(r"\bBANK\w{0,2}UPT|\bSTAY(?:ED)?\b|\bHOLD\b", re.I)
RESUMED = re.compile(r"\bPOST\b|DISMISS|\bLIFT|RELIEF|RESCHEDUL|NEW SALE DATE", re.I)


# -- adjournments (pure) ------------------------------------------------------------------------------------------
_ADJ = re.compile(r"\badj", re.I)
_NOT_ADJ = re.compile(r"\b(no|not|never)\s+adj", re.I)
_COURT = re.compile(r"\bCOURT\b", re.I)
_AGREED = re.compile(r"JOINT|MUTUAL|AGREE|CONSENT|\bBOTH\b", re.I)
_DEBTOR = re.compile(r"\bDEF|DEBTOR", re.I)
_LENDER = re.compile(r"PLAINT|\bPLTF|\bPLNTF|LENDER", re.I)
_SHERIFF = re.compile(r"SHERIFF", re.I)
_BOTH_AT_ONCE = re.compile(r"\b8\s*-?\s*WEEKS?\b|\bEIGHT\s+WEEKS?\b", re.I)   # "DEFENDANT TOTAL 8 WEEK ADJ" = both


def classify_adjournment(text: str | None) -> tuple[str, int] | None:
    """(category, count) for one status-history entry that is an adjournment, else None. Court-ordered adjournments
    ("Adjourned - Court", "Court Ordered Plaintiff Adjournment") are their own category: the court may order more for
    cause, outside the five. "DEFENDANT TOTAL 8 WEEK ADJ" is the debtor's two adjournments taken at once."""
    s = clean(text)
    if not s or not _ADJ.search(s) or _NOT_ADJ.search(s):
        return None
    if _COURT.search(s):
        return "court", 1
    if _AGREED.search(s):
        return "agreed", 1
    if _DEBTOR.search(s):
        return "debtor", 2 if _BOTH_AT_ONCE.search(s) else 1
    if _LENDER.search(s):
        return "lender", 1
    if _SHERIFF.search(s):
        return "sheriff", 1
    return "other", 1


@dataclass
class Adjournments:
    debtor: int = 0
    lender: int = 0
    agreed: int = 0
    court: int = 0
    sheriff: int = 0
    other: int = 0

    @property
    def used(self) -> int:
        """Statutory adjournments used (each party's share capped at what the statute gives it)."""
        return min(self.debtor, DEBTOR_MAX) + min(self.lender, LENDER_MAX) + min(self.agreed, AGREED_MAX)

    @property
    def left(self) -> int:
        """What each party may still ask for without a court order: 2 - debtor's, 2 - lender's, 1 - agreed (>= 0 each).
        Court-ordered, sheriff's and unlabeled entries are shown but not counted against the five."""
        return STATUTORY_ADJOURNMENTS - self.used

    def cell(self) -> str:
        parts = [f"{w} {n}" for w, n in (("debtor", self.debtor), ("lender", self.lender), ("agreed", self.agreed))
                 if n]
        extra = [f"+{n} {w}" for w, n in (("court", self.court), ("sheriff", self.sheriff), ("other", self.other)) if n]
        used = ", ".join(parts) or "none"
        return f"{used}<br/><b>{self.left} of {STATUTORY_ADJOURNMENTS} left</b>" + (
            f"<br/>{', '.join(extra)}" if extra else "")


def adjournments(meta: dict[str, Any] | None) -> Adjournments:
    """Adjournments from the sheriff's status history ([[status, date], ...]); without a history, the scraper's
    counts (`defendant_adjournments`, `plaintiff_adjournments`, the rest of `adjournments_total` as unlabeled)."""
    meta = meta or {}
    hist = meta.get("status_history")
    out = Adjournments()
    if isinstance(hist, list) and hist:
        for h in hist:
            text = h[0] if isinstance(h, (list, tuple)) and h else h
            got = classify_adjournment(str(text or ""))
            if got:
                setattr(out, got[0], getattr(out, got[0]) + got[1])
        return out

    def n(k: str) -> int:
        try:
            return max(0, int(meta.get(k) or 0))
        except (TypeError, ValueError):
            return 0
    out.debtor, out.lender = n("defendant_adjournments"), n("plaintiff_adjournments")
    out.other = max(0, n("adjournments_total") - out.debtor - out.lender)
    return out


# -- the house and the figures (pure) -----------------------------------------------------------------------------
def county_key(name: str | None) -> str:
    """'ESSEX', 'Essex County', ' essex ' -> 'Essex'."""
    return postal_title(re.sub(r"(?i)\s+county$", "", clean(name or "")))


def class_label(cls: str | None) -> str:
    c = clean(cls).upper()
    if not c:
        return ""
    if c.startswith("15"):
        return "exempt"
    return CLASS_LABELS.get(c, "")


def product_of(cls: str | None, building_desc: str | None, qual: str | None, units: int | None) -> str | None:
    """The product type a recorded-sales range is matched on: condo / townhouse / 1-family / 2-family / 3-4 family /
    5+ units. None for non-residential classes or an unknown unit count (no range is shown then)."""
    from engine.comps import SubjectKind
    c = clean(cls).upper()
    if c not in RESIDENTIAL:
        return None
    kind = SubjectKind.of(building_desc=building_desc, qual=qual, units=units)
    if kind.condo:
        return "condo"
    if kind.townhouse:
        return "townhouse"
    if c == "4C" or (units or 0) >= 5:
        return "5+ units"
    if not units:
        return None
    return {1: "1-family", 2: "2-family"}.get(int(units), "3-4 family")


def comp_product(comp: dict[str, Any]) -> str | None:
    """The same product types for a recorded comparable sale (MOD-IV building description + dwelling count)."""
    from engine.comps import comp_kind
    condo, town = comp_kind(comp.get("property_type"))
    if condo:
        return "condo"
    if town:
        return "townhouse"
    try:
        units = int(comp.get("units") or 0)
    except (TypeError, ValueError):
        units = 0
    if units <= 0:
        return None
    if units >= 5:
        return "5+ units"
    return {1: "1-family", 2: "2-family"}.get(units, "3-4 family")


def notice_figures(sig: DistressSignal) -> tuple[str, bool]:
    """(cell, has_upset): the upset and the judgment as the sheriff's notice prints them, each labeled; with neither
    broken out, the one amount the listing carries ("Amount stated")."""
    m = sig.raw_metadata_json or {}
    upset, judgment = parse_amount(m.get("upset_amount")), parse_amount(m.get("judgment_amount"))
    parts = []
    if upset:
        parts.append(f"Upset {money(upset)}")
    if judgment and (not upset or abs(judgment - upset) >= 1):
        parts.append(f"Judgment {money(judgment)}")
    if not parts:
        stated = parse_amount(sig.amount_owed) or parse_amount(m.get("payoff_estimate"))
        if stated:
            parts.append(f"Amount stated {money(stated)}")
    return ("<br/>".join(parts) or "not stated"), bool(upset)


_SUFFIX_ONLY = re.compile(r"(?i)(?:L\.?L\.?C|INC|CORP|CORPORATION|CO|COMPANY|N\.?A|F\.?S\.?B|L\.?P|L\.?L\.?P|LTD|PLC)\.?")
_DESCRIPTOR = re.compile(r"(?i)^(?:AS|NOT|BUT|SOLELY|FOR|IN|ITS|F/K/A|FKA|D/B/A|DBA|A/K/A|AKA|SUCCESSOR|SERIES|"
                         r"TRUSTEE|THE HOLDERS)\b")


def _business(part: str) -> bool:
    from core.text import is_government
    up = part.upper()
    return bool(_SUFFIX_ONLY.fullmatch(part)) or entity_like(part) or is_lender(up) or is_government(up)


def _person_like(part: str) -> bool:
    """Two to four plain words that read as a person's name ("QUINN Q SAMPLE"), not a business or a trust clause."""
    return (not _business(part) and not re.search(r"\d", part) and 2 <= len(part.split()) <= 4
            and not _DESCRIPTOR.match(part))


def plaintiff_name(name: str | None, n: int = 44) -> str:
    """The foreclosing party for the sheet: a lender, an association or a town by name, a private person never. A
    party line that is all business text is shown whole ("PENNYMAC LOAN SERVICES, LLC", "TOWNSHIP OF IRVINGTON");
    one that names people keeps only its business parts ("QUINN SAMPLE AND SAMPLE LIVE LLC" -> "SAMPLE LIVE LLC");
    none at all is "(private party)". Long trust captions are cut; names stay as the notice prints them."""
    s = clean(name).strip(" ,;")
    if not s:
        return "-"
    parts = [p.strip(" ,;") for p in re.split(r";|&|\bAND\b|,", s, flags=re.I)]
    parts = [p for p in parts if p]
    if any(_business(p) for p in parts) and not any(_person_like(p) for p in parts):
        label = s
    else:
        merged: list[str] = []
        for p in parts:
            if _SUFFIX_ONLY.fullmatch(p) and merged:
                merged[-1] = f"{merged[-1]}, {p}"          # "PENNYMAC LOAN SERVICES" + "LLC"
            else:
                merged.append(p)
        keep = [p for p in merged if _business(p) and not _SUFFIX_ONLY.fullmatch(p) and not _DESCRIPTOR.match(p)]
        if not keep:
            return "(private party)"
        label = ", ".join(keep)
    if len(label) > n:                               # cut at a clause ("..., AS TRUSTEE ..."), else at a word
        head = label[:n]
        cut = head.rfind(", ")
        label = (head[:cut] if cut >= 15 else head.rsplit(" ", 1)[0]).rstrip(" ,;") + "..."
    return label


def liens_cell(sig: DistressSignal) -> str:
    prior = parse_amount((sig.raw_metadata_json or {}).get("prior_liens"))
    return f"~{money(prior)}<br/>(taxes / water / sewer)" if prior else "none stated"


def on_hold(status: str | None, meta: dict[str, Any] | None) -> bool:
    s = clean(status)
    if s and ON_HOLD.search(s) and not RESUMED.search(s):
        return True
    return bool((meta or {}).get("on_hold")) and not (s and RESUMED.search(s))


def has_street(street: str | None) -> bool:
    """A street we can name and check against the suppression list: a house number alone ("569") is no address."""
    return bool(re.search(r"[A-Za-z]{2}", clean(street)))


def span_words(days: int) -> str:
    return {7: "week", 14: "two weeks", 21: "three weeks", 28: "four weeks"}.get(int(days), f"{int(days)} days")


def short_date(d: date) -> str:
    return f"{d:%b} {d.day}"


# -- recorded sales by town and product type ----------------------------------------------------------------------
@dataclass
class SalesRange:
    lo: float
    hi: float
    n: int
    first: date
    last: date
    product: str

    def cell(self) -> str:
        years = (f"{self.first.year}" if self.first.year == self.last.year
                 else f"{self.first.year}-{self.last.year % 100:02d}")
        rng = kmoney(self.lo) if round(self.lo, -3) == round(self.hi, -3) else f"{kmoney(self.lo)}-{kmoney(self.hi)}"
        return f"{rng}<br/>{self.n} sales ({self.product}), {years}"


def _iso(d: Any) -> date | None:
    try:
        return date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return None


def _addr(a: Any) -> str:
    return re.sub(r"\W", "", str(a or "").upper())


def range_from(comps: list[tuple[str, dict[str, Any]]], today: date, months: int, min_sales: int,
               ) -> dict[tuple[str, str, str], SalesRange]:
    """(county, TOWN, product) -> the min-max of recorded deed prices (pure). `comps` holds (county, comp) pairs from
    stored valuations; only public-record comps (NJGIN MOD-IV sales) count, each sale once, priced at $40K+, sold in
    the last `months`. A range needs `min_sales` sales."""
    since = today - timedelta(days=int(months * 30.44))
    seen: set[tuple[str, str]] = set()
    acc: dict[tuple[str, str, str], list[tuple[float, date]]] = {}
    for county, c in comps:
        if not isinstance(c, dict) or c.get("source") != "njgin_public_record":
            continue
        sold, price = _iso(c.get("sold_date")), parse_amount(c.get("sold_price"))
        town, product = muni_base(c.get("municipality")), comp_product(c)
        if not (sold and price and town and product) or price < MIN_SALE_PRICE or not since <= sold <= today:
            continue
        k = (_addr(c.get("address")), sold.isoformat())
        if k in seen:
            continue
        seen.add(k)
        acc.setdefault((county, town, product), []).append((price, sold))
    out = {}
    for k, xs in acc.items():
        if len(xs) >= min_sales:
            prices, dates = [p for p, _ in xs], [d for _, d in xs]
            out[k] = SalesRange(min(prices), max(prices), len(xs), min(dates), max(dates), k[2])
    return out


def sales_ranges(session: Session, counties: set[str], today: date, months: int, min_sales: int,
                 ) -> dict[tuple[str, str, str], SalesRange]:
    """The recorded sales behind every current valuation of a property in these counties."""
    if not counties:
        return {}
    pairs: list[tuple[str, dict[str, Any]]] = []
    for county, comps in session.execute(
            select(Property.county, Valuation.comps_json).join(Valuation, Valuation.property_id == Property.id)
            .where(Valuation.is_current.is_(True), Valuation.comps_json.is_not(None))):
        ck = county_key(county)
        if ck in counties and isinstance(comps, list):
            pairs += [(ck, c) for c in comps]
    return range_from(pairs, today, months, min_sales)


# -- rows ---------------------------------------------------------------------------------------------------------
@dataclass
class BidRow:
    signal_id: int
    property_id: int
    county: str
    sale_date: date
    status: str
    sheriff_no: str
    docket: str
    street: str
    town: str
    town_key: str
    cls: str
    units: int | None
    year: int | None
    sqft: int | None
    product: str | None
    plaintiff: str
    figures: str
    has_upset: bool
    liens: str
    adj: Adjournments
    rehab: str = ""
    has_offer: bool = False
    seen: datetime | None = None
    sales: SalesRange | None = None

    @property
    def residential(self) -> bool:
        return self.cls in RESIDENTIAL

    def facts(self) -> str:
        if not self.cls:
            return "no tax-record match"
        label = class_label(self.cls)
        head = f"Class {self.cls}" + (f" ({label})" if label else "")
        units = self.product or (f"{self.units} unit{'s' if self.units != 1 else ''}" if self.units else "")
        bits = [units, f"built {self.year}" if self.year else "", f"{self.sqft:,} sqft" if self.sqft else ""]
        tail = " · ".join(b for b in bits if b)
        return head + (f"<br/>{tail}" if tail else "")

    def cells(self, i: int) -> list[str]:
        # sheriff # and docket on one line, so a sample's masked rows hide both (either one names the case)
        case = " · ".join(x for x in (self.sheriff_no, self.docket) if x) or "-"
        return [str(i), f"<b>{self.sale_date:%a %b} {self.sale_date.day}</b><br/>{escape(self.status[:30])}",
                escape(case), f"<b>{escape(self.street)}</b><br/>{escape(self.town)}", self.facts(),
                escape(self.plaintiff), self.figures, self.liens, self.adj.cell(),
                self.sales.cell() if self.sales else "-", self.rehab or "-"]


@dataclass
class Collected:
    rows: list[BidRow] = field(default_factory=list)
    stats: Counter[str] = field(default_factory=Counter)


def _rehab_tier(prop: Property) -> str:
    val = prop.current_valuation
    if val is None or not val.rehab_tier:
        return ""
    flags = [str(f) for f in ((val.assumptions_json or {}).get("flags") or [])]
    if any(f.startswith("NON_RESIDENTIAL") for f in flags):
        return ""
    return str(val.rehab_tier).title()


def collect(session: Session, today: date, lim: dict[str, Any]) -> Collected:
    """Every active sheriff sale dated tomorrow through `window_days` out, one row per property and date, with the
    run's skip counts. Sales already closed (sold, cancelled, writ expired), held (bankruptcy / stay), not seen on
    CivilView for `max_stale_days`, with no street on record or under a Daniel's Law / stop request are left out."""
    out = Collected()
    out.stats["sales_seen"] = int(session.scalar(select(func.count(DistressSignal.id)).where(
        DistressSignal.signal_type == SignalType.SHERIFF_SALE, DistressSignal.active.is_(True))) or 0)
    start, end = today + timedelta(days=1), today + timedelta(days=int(lim["window_days"]))
    stale_before = datetime.combine(today, datetime.min.time()) - timedelta(days=int(lim["max_stale_days"]))
    sigs = list(session.scalars(
        select(DistressSignal).where(DistressSignal.signal_type == SignalType.SHERIFF_SALE,
                                     DistressSignal.active.is_(True),
                                     DistressSignal.scheduled_auction_date >= start,
                                     DistressSignal.scheduled_auction_date <= end)
        .options(selectinload(DistressSignal.property).selectinload(Property.valuations),
                 selectinload(DistressSignal.property).selectinload(Property.lead))
        .order_by(DistressSignal.scheduled_auction_date, DistressSignal.id).limit(int(lim["max_signals"]))))
    out.stats["sales_in_window"] = len(sigs)
    seen: set[tuple[int, date]] = set()
    for sig in sigs:
        prop, meta = sig.property, sig.raw_metadata_json or {}
        if prop is None:
            continue
        status = current_status(sig.status, meta.get("status_history")) or "Scheduled"
        if CLOSED.search(status):
            out.stats["skipped_closed"] += 1
            continue
        if on_hold(status, meta):
            out.stats["skipped_on_hold"] += 1
            continue
        if sig.last_seen_at and sig.last_seen_at < stale_before:
            out.stats["skipped_stale"] += 1             # not on the sheriff's list lately: no claim it is still set
            continue
        if not has_street(prop.street_address):
            out.stats["skipped_no_address"] += 1
            continue
        if not address_ok(session, prop):
            out.stats["skipped_suppressed"] += 1
            continue
        k = (prop.id, sig.scheduled_auction_date)
        if k in seen:
            continue
        seen.add(k)
        figures, has_upset = notice_figures(sig)
        cls = clean(prop.property_class).upper()
        town = postal_title(muni_base(prop.municipality)) or postal_title(prop.city) or ""
        out.rows.append(BidRow(
            signal_id=sig.id, property_id=prop.id, county=county_key(meta.get("county") or prop.county) or "Unknown",
            sale_date=sig.scheduled_auction_date, status=status, sheriff_no=sheriff_no(sig),
            docket=clean(sig.case_number), street=postal_title(prop.street_address).replace("&", "and"), town=town,
            town_key=muni_base(prop.municipality or prop.city), cls=cls, units=prop.dwellings, year=prop.year_built,
            sqft=prop.sqft, product=product_of(cls, prop.building_desc, prop.qual, prop.dwellings),
            plaintiff=plaintiff_name(sig.plaintiff_or_lienholder), figures=figures, has_upset=has_upset,
            liens=liens_cell(sig), adj=adjournments(meta), rehab=_rehab_tier(prop),
            has_offer=bool(prop.lead is not None and prop.lead.offer_amount), seen=sig.last_seen_at))
    ranges = sales_ranges(session, {r.county for r in out.rows}, today, int(lim["comp_months"]),
                          int(lim["min_comp_sales"]))
    for r in out.rows:
        if r.product:
            r.sales = ranges.get((r.county, r.town_key, r.product))
    out.rows.sort(key=lambda r: (r.county, r.sale_date, not r.residential, r.town, r.street))
    out.stats["rows"] = len(out.rows)
    out.stats["rows_with_sales_range"] = sum(1 for r in out.rows if r.sales)
    return out


# -- the sheet ----------------------------------------------------------------------------------------------------
# "Sheriff # / docket" and "Property" are the columns a sample masks (base._MASK_HEAD); no other header may use
# those words ("per notice", not "sheriff's notice")
HEAD = ["#", "Sale date / status", "Sheriff # / docket", "Property", "Class / units / built / sqft", "Plaintiff",
        "Upset / judgment (per notice)", "Stated prior liens", "Adjournments used / left",
        "Recorded sale prices, town + type", "Rehab tier*"]
WIDTHS = [0.25, 0.95, 0.95, 1.3, 1.05, 1.3, 1.0, 0.8, 0.95, 1.0, 0.55]
DISCLAIMER = "Public-record data; not an appraisal, BPO or CMA; no opinion of value."
CHECKLIST = (
    "<b>SURVIVING-LIEN CHECKLIST: read before bidding.</b> A sheriff sale does not clear everything. (1) Municipal "
    "property taxes, water and sewer charges and tax sale certificates survive a mortgage foreclosure sale and stay "
    "with the property. (2) A condo or homeowners association's 6-month priority lien for unpaid assessments (N.J.S.A. "
    "46:8B-21) may survive. (3) A recorded federal tax lien: the IRS may redeem the property within 120 days after the "
    "sale (26 U.S.C. 7425(d)), and without the required notice to the IRS its lien survives (7425(b)). (4) A mortgage "
    "senior to the one being foreclosed survives a junior lienholder's foreclosure. (5) For 10 days after the sale, "
    "objections may be filed and the owner may redeem (R. 4:65-5). (6) Each sheriff's conditions of sale apply: a "
    "deposit, typically 20% of the bid in certified funds, at the sale, and the balance and fees on the sheriff's terms. "
    "<b>Order a title search from a licensed title producer before bidding. This sheet is not a title search, title "
    "insurance, legal advice or an appraisal.</b> Owner-occupied homes in foreclosure are protected by the Foreclosure "
    "Rescue Fraud Prevention Act; this sheet is for auction bidders only.")


def sheet_notes(lim: dict[str, Any]) -> list[str]:
    from core.config import settings
    rc = dict(settings().get("rehab") or {})
    tiers = (f"${float(rc.get('light_per_sqft', 30)):,.0f} / ${float(rc.get('medium_per_sqft', 55)):,.0f} / "
             f"${float(rc.get('heavy_per_sqft', 110)):,.0f}")
    return [
        "Upset / judgment: the figures the sheriff's notice prints on CivilView, each labeled (\"Amount stated\" when "
        "the listing gives one amount without saying which). An approximate upset may not be current: confirm it with "
        "the sheriff or the plaintiff's attorney named on the listing before the sale. Stated prior liens: the amount "
        "the notice says the sale is subject to (taxes, water, sewer); \"none stated\" means the notice gives no amount, "
        "not that there are none.",
        f"Adjournments: counted from the sheriff's status history. N.J.S.A. 2A:17-36 (P.L.2019, c.71) allows "
        f"{STATUTORY_ADJOURNMENTS} without a court order: {DEBTOR_MAX} at the debtor's request, {LENDER_MAX} at the "
        f"lender's, {AGREED_MAX} by agreement; \"left\" is what each party may still ask for (an estimate). Court-ordered "
        "(for cause), sheriff's and unlabeled entries are shown but not counted; a court stay may use up the debtor's "
        "(Lazarus v. Tawil, Ch. Div. 2025). Sales are often adjourned, stayed or cancelled: confirm each with the sheriff.",
        f"Recorded sale prices: the lowest and highest recorded deed prices (NJ MOD-IV via NJGIN) of the same product "
        f"type in the same town in our comparable-sales data, last {int(lim['comp_months'])} months, $40K and up; a range "
        "of public facts, not an estimate for any property. Class, units and year from NJ MOD-IV; living area from the "
        "county tax list or a listing where on record.",
        f"*Rehab tier: the tier our cost formula assigns from age, vacancy, foreclosure, ownership length and listing "
        f"words (Light / Medium / Heavy = {tiers} per sq ft base, plus age add-ons); not an inspection. Plaintiffs are "
        "named only when they are businesses; defendants and occupants are never named. Addresses under a Daniel's Law "
        "or stop request are left out.",
        f"Subscription: every sale in one county, refreshed weekly, ${float(lim['price_monthly']):,.0f} a month plus NJ "
        f"sales tax; a single property ${float(lim['price_property']):,.0f} plus NJ sales tax. No commission or per-deal "
        "fee.",
    ]


def sheet_subtitle(rows: list[BidRow], today: date, lim: dict[str, Any], sample: str = "") -> str:
    first, last = rows[0].sale_date, rows[-1].sale_date
    seen = max((r.seen for r in rows if r.seen), default=None)
    checked = f" (checked {seen:%b} {seen.day})" if seen else ""
    n = len(rows)
    return (f"{DISCLAIMER} {n} sale{'s' if n != 1 else ''} scheduled {short_date(first)} to {short_date(last)}, by sale "
            f"date, as the sheriff lists them on CivilView{checked} · Prepared {today:%B} {today.day}, {today.year} · "
            "Sources: sheriff sale listings (CivilView), NJ MOD-IV tax records, recorded deed sales (NJGIN), our rehab "
            "cost formula · For auction bidders: read the surviving-lien checklist below the table." + sample)


def page_count(path: Path) -> int:
    """Pages in a ReportLab PDF (its page objects are written uncompressed), without a PDF library at run time."""
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", path.read_bytes())) or 1


def sheet_path(out_dir: Path, county: str, today: date) -> Path:
    return out_dir / f"s11_bid_sheet_{slug(county).lower()}_{today.isoformat()}.pdf"


def render_sheet(path: Path, county: str, rows: list[BidRow], ctx: StreamContext, lim: dict[str, Any]) -> Path:
    """The county's full bid sheet: every row, as many pages as it takes (the sample is written separately)."""
    sample_path(path).unlink(missing_ok=True)              # never a stale sample from an earlier render
    return render_table_pdf(path, title=f"NJ Sheriff-Sale Bid Sheet: {county} County",
                            subtitle=sheet_subtitle(rows, ctx.now.date(), lim), head=HEAD,
                            rows=[r.cells(i) for i, r in enumerate(rows, 1)], notes=sheet_notes(lim), box=CHECKLIST,
                            footer=operator_footer(ctx.operator), col_widths=WIDTHS, write_sample=False)


@dataclass
class Sample:
    county: str
    path: Path                      # the full sheet
    file: Path                      # what Touch 2 attaches (the one-page sample)
    featured: BidRow
    shown: int                      # rows on the sample page
    total: int                      # rows on the full sheet
    first: date
    last: date                      # the full sheet's last sale date

    @property
    def masked(self) -> bool:
        return self.shown > SAMPLE_FULL_ROWS


def render_sample(path: Path, county: str, rows: list[BidRow], start: int, ctx: StreamContext, lim: dict[str, Any],
                  ) -> tuple[Path, int]:
    """One page starting at row `start` (the sale Touch 1 names): rows 1-2 in full, the rest with the street and
    sheriff # / docket hidden (`masked_rows`). Rows are dropped from the end until it fits one page."""
    out = sample_path(path)
    part = rows[start:start + max(SAMPLE_FULL_ROWS + 1, int(lim["sample_rows"]))]
    k = len(part)
    total = len(rows)
    while True:
        cells = [r.cells(i) for i, r in enumerate(part[:k], start + 1)]
        mrows = masked_rows(HEAD, cells) or cells
        what = (f"{SAMPLE_FULL_ROWS} of {k} rows in full, the rest with the street and sheriff # hidden"
                if k > SAMPLE_FULL_ROWS else f"{k} rows in full")
        note = (f" · Sample: sales from {short_date(part[0].sale_date)}, {what} (the full sheet lists all {total} "
                f"sales through {short_date(rows[-1].sale_date)})")
        render_table_pdf(out, title=f"NJ Sheriff-Sale Bid Sheet: {county} County",
                         subtitle=sheet_subtitle(rows, ctx.now.date(), lim, sample=note), head=HEAD, rows=mrows,
                         notes=sheet_notes(lim), box=CHECKLIST, footer=operator_footer(ctx.operator),
                         col_widths=WIDTHS, write_sample=False)
        if k <= SAMPLE_FULL_ROWS + 1 or page_count(out) <= 1:
            return out, k
        k = k - 1 if k <= 8 else max(SAMPLE_FULL_ROWS + 1, int(k * 0.85))


# -- the copy -----------------------------------------------------------------------------------------------------
def _plain(text: str) -> str:
    """A street / town as a sentence carries it: no period that would read as a sentence end ("Mt. Laurel")."""
    return re.sub(r"\.(?=\s|$)", "", text).strip()


def touch1(row: BidRow, county: str, n: int, window_days: int, hello: str = "Hello,", first: str = "",
           max_words: int = 45) -> tuple[str, str] | None:
    """(subject, body) of the cold first touch: two plain sentences, no price, link or attachment. One sale on the
    sheet (a street that passed `address_ok`), the true count of the county's sales on the sheet and one question.
    None when no wording fits `max_words` (the caller picks another sale)."""
    from engine.human_copy import word_count
    street, town, when = _plain(row.street), _plain(row.town), short_date(row.sale_date)
    span = span_words(window_days)
    for named in (f"{county} County", county):
        for ask in (f"Want this week's {county} County bid sheet, free?", f"Want this week's {county} sheet, free?"):
            said = (f"{street} in {town} is set for sheriff sale on {when}, one of {n} {named} sales in the next "
                    f"{span} on our sheet with the upset, stated liens and adjournments left.")
            if word_count(f"{said} {ask}") <= max_words:
                body = f"{hello}\n\n{said} {ask}" + (f"\n\n{first}" if first else "")
                return f"[{county}] sheriff sale {when}: {street}", body
    return None


def sample_body(s: Sample, lim: dict[str, Any]) -> str:
    """Touch 2, sent with the one-page sample only after their yes: what is attached, both flat prices, how to start."""
    if s.masked:
        what = (f"Attached is a sample of the {s.county} County bid sheet: {s.shown} sales from {short_date(s.first)}, "
                f"the first {SAMPLE_FULL_ROWS} in full and the rest with the street and sheriff # hidden (the full sheet "
                f"lists all {s.total} through {short_date(s.last)}).")
    else:
        what = (f"Attached is a sample of the {s.county} County bid sheet: {s.shown} sales from {short_date(s.first)} "
                f"(the full sheet lists all {s.total} through {short_date(s.last)}).")
    return (f"{what} It's ${float(lim['price_monthly']):,.0f} a month plus NJ sales tax for every sale in one county, "
            f"or ${float(lim['price_property']):,.0f} plus NJ sales tax for a single property, with no commission or "
            "per-deal fee. Reply \"start\" and I'll set it up.")


# -- targets ------------------------------------------------------------------------------------------------------
def _pk(name: str | None) -> str:
    from engine.deal_desk import prospect_key
    return prospect_key(name or "")


def sheriff_deed_rows(session: Session, since: date) -> list[DeedTransfer]:
    """Recorded sheriff's deeds (SR1A NU code 12, or a grantor naming the sheriff: the test s3's `sheriff_deeds`
    uses) dated on or after `since`."""
    return list(session.scalars(select(DeedTransfer).where(
        or_(DeedTransfer.nu_code == SHERIFF_DEED_NU, func.upper(DeedTransfer.grantor).like("%SHERIFF%")),
        or_(DeedTransfer.deed_date.is_(None), DeedTransfer.deed_date >= since))))


def _is_sheriff_purchase(x: dict[str, Any]) -> bool:
    return str(x.get("nu_code") or "").strip() == SHERIFF_DEED_NU or "SHERIFF" in str(x.get("grantor") or "").upper()


def sheriff_families(session: Session, today: date, lookback_days: int) -> dict[str, dict[str, Any]]:
    """Buyer families (cash-buyer records sharing a mailing address) that took title by a sheriff's deed: matched by
    the grantee's name, the grantee's mailing address or the purchase itself (street + deed date), or a purchase on
    their own record whose grantor is the sheriff. Returns {family key: {"deeds": n, "counties": [most bought first],
    "names": {entity keys}}}. A lender grantee (the plaintiff taking the house back) is never a bidder."""
    from agents.scrapers.nj_tax_assessment_agent import address_mailing_key
    from engine.partner_principals import family_key
    fams: dict[str, list[CashBuyer]] = {}
    for b in session.scalars(select(CashBuyer)):
        fams.setdefault(family_key(b) or f"b{b.id}", []).append(b)
    by_name: dict[str, str] = {}
    by_buy: dict[tuple[str, str], str] = {}
    for k, bs in fams.items():
        for b in bs:
            if b.entity_name and not b.entity_name.upper().startswith("OWNER @"):
                by_name.setdefault(_pk(b.entity_name), k)
            for x in b.recent_purchases_json or []:
                if isinstance(x, dict) and x.get("address") and x.get("deed_date"):
                    by_buy.setdefault((_addr(x["address"]), str(x["deed_date"])[:10]), k)
    hits: dict[str, set[tuple[str, str]]] = {}
    deed_county: dict[tuple[str, str], str] = {}
    for d in sheriff_deed_rows(session, today - timedelta(days=lookback_days)):
        if not d.grantee or is_lender(d.grantee):
            continue
        fam = by_name.get(_pk(d.grantee))
        if fam is None and d.grantee_address:
            mk = address_mailing_key(d.grantee_address)
            fam = mk if mk in fams else None
        when = d.deed_date.isoformat() if d.deed_date else ""
        if fam is None and d.property_location and when:
            fam = by_buy.get((_addr(d.property_location), when))
        if fam is None:
            continue
        k = (_addr(d.property_location or d.parcel_id), when)
        hits.setdefault(fam, set()).add(k)
        deed_county[k] = county_key(d.county or d.parcel_id.split("-")[0])
    since = (today - timedelta(days=lookback_days)).isoformat()
    for k, bs in fams.items():
        for b in bs:
            for x in b.recent_purchases_json or []:
                if isinstance(x, dict) and _is_sheriff_purchase(x) and str(x.get("deed_date") or "") >= since:
                    key = (_addr(x.get("address")), str(x.get("deed_date") or "")[:10])
                    hits.setdefault(k, set()).add(key)
                    deed_county.setdefault(key, county_key(x.get("county")))
    out: dict[str, dict[str, Any]] = {}
    for k, keys in hits.items():
        # where the family bought most: each purchase once (a sheriff's deed already on its record isn't counted twice)
        counties: Counter[str] = Counter()
        bought: set[tuple[str, str]] = set()
        for b in fams[k]:
            for x in b.recent_purchases_json or []:
                if not isinstance(x, dict) or not x.get("county"):
                    continue
                key = (_addr(x.get("address")), str(x.get("deed_date") or "")[:10])
                if key not in bought:
                    bought.add(key)
                    counties[county_key(x["county"])] += 1
        for key in keys - bought:
            if deed_county.get(key):
                counties[deed_county[key]] += 1
        counties.pop("", None)
        out[k] = {"deeds": len(keys), "counties": [c for c, _ in counties.most_common()],
                  "names": {_pk(b.entity_name) for b in fams[k] if b.entity_name}}
    return out


def sync_targets(session: Session, today: date, lookback_days: int = 1095, write: bool = True) -> dict[str, int]:
    """Prospects with a business e-mail whose buyer family bought at a sheriff sale become "sheriff_bidder" targets
    on s11 (county = where the family bought most). An e-mail another stream already owns is skipped; an s11 row of
    our own is refreshed. With `write=False` nothing is stored (a dry run reports what it would do)."""
    from engine import firm_outbox as fo
    counts = {"families": 0, "synced": 0, "refreshed": 0, "other_stream": 0}
    fams = sheriff_families(session, today, lookback_days)
    counts["families"] = len(fams)
    if not fams:
        return counts
    prospects = list(session.scalars(select(Prospect).where(Prospect.email.is_not(None),
                                                            Prospect.stage != "LOST")))
    for fk, info in sorted(fams.items(), key=lambda kv: (-kv[1]["deeds"], kv[0])):
        names = info["names"]
        mine = [p for p in prospects if p.family_key == fk or _pk(p.entity_name) in names
                or (p.alias and _pk(p.alias) in names)]
        for p in mine:
            email = fo.norm_email(p.email)
            if not email:
                continue
            row = session.scalar(select(FirmTarget).where(FirmTarget.email == email))
            if row is not None and not (row.stream == KEY and row.kind == SYNC_KIND):
                counts["other_stream"] += 1             # one row per address: the other stream keeps it
                continue
            counts["refreshed" if row is not None else "synced"] += 1
            if not write:
                continue
            fo.upsert_target(session, kind=SYNC_KIND, stream=KEY, email=email, org=p.entity_name, phone=p.phone,
                             website=p.website, county=(info["counties"] or [None])[0], source=p.source,
                             score=float(info["deeds"]) * 5 + float(p.purchases_36m or 0),
                             data={"prospect_key": p.key, "family_key": fk, "sheriff_deeds": info["deeds"],
                                   "counties": info["counties"][:5]})
    return counts


def target_counties(t: FirmTarget) -> list[str]:
    cs = [county_key(t.county)] if t.county and clean(t.county).lower() not in STATEWIDE else []
    extra = (t.data_json or {}).get("counties") if isinstance(t.data_json, dict) else None
    if isinstance(extra, list):
        cs += [county_key(str(c)) for c in extra if c and clean(str(c)).lower() not in STATEWIDE]
    return [c for i, c in enumerate(cs) if c and c not in cs[:i]]


def due_targets(session: Session, now: datetime, kinds: list[str], key_for: Any) -> list[FirmTarget]:
    """s11 targets the outbox would accept now: NEW / CONTACTED, an e-mail on no stop list, never pitched (or a
    finished sequence that rested: `pitch_ok`), outside the pitch gap, this week's key unused, nothing queued for them
    and not inside stream 1's buy-box conversation. Sheriff bidders first, then flippers, best score first."""
    from engine import firm_outbox as fo
    from engine.streams.s2_deal_desk_saas import buyer_note_targets, queued_targets
    c = fo.cfg()
    gap = timedelta(days=float(c.get("pitch_gap_days", 6)))
    busy = queued_targets(session) | buyer_note_targets(session, now, float(c.get("sample_hold_days", 30)))
    used = set(session.scalars(select(FirmOutbox.key).where(FirmOutbox.stream == KEY)))
    rows = session.scalars(select(FirmTarget).where(FirmTarget.stream == KEY, FirmTarget.kind.in_(kinds),
                                                    FirmTarget.stage.in_(("NEW", "CONTACTED")),
                                                    FirmTarget.email.is_not(None))).all()
    out = []
    for t in sorted(rows, key=lambda t: (t.kind != SYNC_KIND, -(t.score or 0), t.id)):
        if key_for(t) in used or t.id in busy or not fo.pitch_ok(t, now):
            continue
        if t.last_contact_at and t.last_contact_at > now - gap:
            continue
        if fo.stop_reason(session, t.email or "", t):
            continue
        out.append(t)
    return out


def week_tag(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


# -- the stream ---------------------------------------------------------------------------------------------------
class SheriffBidSheetStream(Stream):
    key = KEY
    name = LABEL
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        lim = {**DEFAULTS, **{k: v for k, v in (ctx.limits or {}).items() if v is not None}}
        res = StreamResult(stream=self.key)
        today = ctx.now.date()
        got = collect(session, today, lim)
        res.processed, res.found = got.stats["sales_in_window"], len(got.rows)
        by_county: dict[str, list[BidRow]] = {}
        for r in got.rows:
            by_county.setdefault(r.county, []).append(r)
        sheets: dict[str, Path] = {}
        for county in sorted(by_county):
            path = sheet_path(ctx.out_dir, county, today)
            render_sheet(path, county, by_county[county], ctx, lim)
            sheets[county] = path
        res.products = len(sheets)
        synced = sync_targets(session, today, int(lim["sync_lookback_days"]), write=ctx.send)
        made, matched, no_sheet, samples = (self._pitches(session, ctx, lim, by_county, sheets, res)
                                            if ctx.send else (0, 0, 0, 0))
        res.metrics.update({k: int(v) for k, v in got.stats.items()})
        res.metrics.update({
            "counties": len(sheets), "pdfs": len(sheets), "samples": samples, "targets_synced": synced["synced"],
            "targets_refreshed": synced["refreshed"], "sheriff_buyer_families": synced["families"],
            "targets_other_stream": synced["other_stream"], "targets_matched": matched, "targets_no_sheet": no_sheet,
            "pitches": made})
        for k in ("skipped_suppressed", "skipped_no_address", "skipped_closed", "skipped_on_hold", "skipped_stale",
                  "sales_seen", "sales_in_window"):
            res.metrics.setdefault(k, 0)
        res.notes.append(f"{len(sheets)} county bid sheets, {len(got.rows)} sales, {made} pitches proposed")
        return res

    def _pitches(self, session: Session, ctx: StreamContext, lim: dict[str, Any], by_county: dict[str, list[BidRow]],
                 sheets: dict[str, Path], res: StreamResult) -> tuple[int, int, int, int]:
        """Touch 1 for up to `pitches_per_run` due targets; weekdays only, so the sale Touch 1 names is still days
        away when it goes out. Returns (pitches, targets matched to a sheet, targets with no sheet, samples)."""
        if ctx.now.weekday() >= 5:
            res.notes.append("weekend: no pitches")
            return 0, 0, 0, 0
        today = ctx.now.date()
        pitchable = {c: rows for c, rows in by_county.items() if len(rows) >= int(lim["min_rows_to_pitch"])}
        if not pitchable:
            return 0, 0, 0, 0
        biggest = sorted(pitchable, key=lambda c: (-len(pitchable[c]), c))
        tag = week_tag(ctx.now)

        def key_for(t: FirmTarget) -> str:
            return f"{self.key}:pitch:{t.id}:{tag}"

        kinds = [str(k) for k in (lim.get("target_kinds") or TARGET_KINDS)]
        cap = int(lim["pitches_per_run"])
        from engine.streams.s2_deal_desk_saas import greeting
        first = (ctx.operator or {}).get("first", "")
        cache: dict[str, tuple[Sample, tuple[str, str]] | None] = {}
        made = matched = no_sheet = 0
        for t in due_targets(session, ctx.now, kinds, key_for):
            if made >= cap:
                break
            wanted = target_counties(t)
            county = next((c for c in wanted if c in pitchable), None) if wanted else biggest[0]
            if county is None:
                no_sheet += 1
                continue
            matched += 1
            if county not in cache:
                cache[county] = self._sample(county, by_county[county], sheets[county], ctx, lim, today)
            plan = cache[county]
            if plan is None:
                continue
            s = plan[0]
            copy = touch1(s.featured, county, s.total, int(lim["window_days"]), greeting(t.name), first)
            if copy is None:
                continue
            subject, body = copy
            res.outbound.append(Outbound(
                stream=self.key, key=key_for(t), to=t.email or "", subject=subject, body=body,
                priority=PRIORITY_SPECIALTY, kind="pitch", target_id=t.id, attachments=[],
                sample_body=sample_body(s, lim), sample_attachments=[s.file]))
            made += 1
        return made, matched, no_sheet, sum(1 for v in cache.values() if v is not None)

    @staticmethod
    def _sample(county: str, rows: list[BidRow], path: Path, ctx: StreamContext, lim: dict[str, Any],
                today: date) -> tuple[Sample, tuple[str, str]] | None:
        """The sale Touch 1 names (the first on the sheet at least `feature_lead_days` out, a house before other
        classes, an upset on record, no offer of ours on it, wording that fits) and the one-page sample from it."""
        lead = today + timedelta(days=int(lim["feature_lead_days"]))
        order = sorted(range(len(rows)), key=lambda i: (not rows[i].residential, not rows[i].has_upset, i))
        for i in order:
            r = rows[i]
            if r.sale_date < lead or r.has_offer or len(rows) - i < SAMPLE_FULL_ROWS + 1:
                continue
            copy = touch1(r, county, len(rows), int(lim["window_days"]))
            if copy is None:
                continue
            file, shown = render_sample(path, county, rows, i, ctx, lim)
            return Sample(county=county, path=path, file=file, featured=r, shown=shown, total=len(rows),
                          first=r.sale_date, last=rows[-1].sale_date), copy
        return None


STREAM = SheriffBidSheetStream()
