"""Stream 12: monthly town market reports for listing agents who farm a town (2026-11-10).

Who buys it: a listing agent who farms one town sends a market update to their sphere every month. This stream builds
that update from public records only, one page per town (mun_code), with a "Ready-to-send e-mail text" box at the end
the agent can paste: $79 a month per town, or $149 a month to be the only brokerage getting that town (research
verdict, 2026-11-10), plus NJ sales tax, flat, no commission or per-deal fee.

What is in a report (aggregates only, never a single property):
- recorded home sales: the last 3 and the last 12 complete months on record (fewer when the town's deed file is
  shorter, labeled with the real span): homes sold, median price, median price per square foot where the living area
  is on record, and the share bought by companies (an LLC / corporation on the deed: a common proxy for investors);
- the same for the long window by property type: 1-4 family houses, condo units (MOD-IV qualifier C), and 1-family /
  2-4 family when MOD-IV unit counts are on file for most of the sales;
- the foreclosure pipeline, counts only: sheriff sales scheduled in the next 90 days (where the county's CivilView list
  is current; "at least N" when some of the county's notices don't make the town clear) and foreclosure lis pendens
  filed in the last 90 days (only where the clerk index covers the window and ties every filing to a town), else the
  reason. Touch 1 states a pipeline count only when it is exact;
- property taxes: the rate implied by MOD-IV (median of the last annual tax bill / assessed value), the Director's
  Chapter 123 average ratio (config/streams/nj_chapter123_ratios.yaml) and their product;
- in towns that s7's daily high-value listing pass covers, the count of for-sale home listings priced inside a graduated
  transfer-fee dead zone (s7's solver; the only line built from listings, labeled as such).

Why the numbers are true (Consumer Fraud Act): "homes sold" are arm's-length recorded deeds (SR1A non-usable code
blank, $30,000+: owner_records_agent.is_arms_length) of Class 2 parcels in the county deed exports we hold
(deed_transfers). A town's file must cover whole calendar months: the first month counts only when the file starts in
its first week, a month counts only when it ended `record_lag_days` (60) before the newest recorded date in the file
(deeds post weeks after closing, so the newest months are incomplete), every month in the window needs a normal deed
volume, and a file older than `max_data_age_days` makes no report. Any figure built from fewer than 5 recorded sales is
suppressed ("fewer than 5 recorded sales"); medians are rounded to $1,000.

Why it stays legal for an unlicensed operator: counts, medians and price per square foot of recorded sales describe a
town, not a property. A property-specific value would be a comparative market analysis (licensed brokerage work, NJ
Real Estate Commission Bulletin 13-05) or an appraisal (N.J.S.A. 45:14F), so the report never values a property, never
lists a sale, never names a buyer or seller, holds no address, and uses no MLS or listing-site sale statistics. It is a
flat research fee, never pay tied to a listing or sale (N.J.S.A. 45:15-3).

Who it's pitched to: listing agents in our contacts (LISTING_AGENT, a business e-mail that is no shared office inbox,
on no stop list, not bounced) with `min_listings` (3) active or recent listings in one target-county town that has a
report, ranked by that count; never an agent we are in deal talks with (s2.in_deal_talks), never an e-mail that
already belongs to another stream's target (one FirmTarget per e-mail). New targets are kind "farm_agent", stream
"s12". At most `pitches_per_run` (2) a weekday run, one per town per run, one per agent per month; nothing goes into a
town an exclusive subscriber holds (data_json "s12_exclusive"). Touch 1 is two plain sentences (the town's count of
homes sold, its median and one pipeline count, no attachment, no other "$"); the report itself is the held sample
(it holds no addresses, so nothing is masked), released with the price when they say yes.

Subscribers (TRIAL / PAYING, data_json "s12_mun"): each gets their own town's report once a calendar month,
co-branded with their name and brokerage, proposed here as a "delivery" in their name (the outbox's weekly county
delivery would send other towns: nothing is written at out_dir's root, so it finds no file for s12).
Counts only in the metrics and notes (the cloud run log is public).
"""
from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.text import clean, is_government, is_lender, muni_base, postal_title
from db.models import (
    ClerkFiling,
    Contact,
    ContactType,
    DeedTransfer,
    DistressSignal,
    FirmOutbox,
    FirmTarget,
    Lead,
    Listing,
    Property,
    SignalType,
    SwarmState,
    utcnow,
)
from engine.streams.base import (
    PRIORITY_REPLY,
    PRIORITY_SPECIALTY,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    money,
    operator_footer,
    slug,
)

log = logging.getLogger(__name__)

KEY = "s12"
TARGET_KIND = "farm_agent"
MIN_CELL = 5                                  # any figure built from fewer recorded sales is suppressed
SUPPRESSED = "fewer than 5 recorded sales"
MIN_SQFT = 300                                # a stated living area under this is a data error, not a house
LISTED = (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)
OPEN_STAGES = ("NEW", "CONTACTED")
FORECLOSURE_DOC = re.compile(r"FORECLOS|IN REM", re.I)
DEFAULTS: dict[str, Any] = {
    "price_monthly": 79, "price_exclusive": 149, "pitches_per_run": 2, "min_listings": 3, "listing_recent_days": 90,
    "record_lag_days": 60, "max_data_age_days": 150, "min_months": 6, "min_sales": 10, "pipeline_days": 90,
    "max_reports": 40, "sheriff_fresh_days": 7, "clerk_fresh_days": 14, "split_known_share": 0.8, "max_words": 45,
    "months_back": 30,
}
DISCLAIMER = "Public-record data; not an appraisal, BPO or CMA; no opinion of value."
LEGAL = ("Aggregates of recorded sales only. Counts, medians and price per square foot of recorded deeds describe the "
         "town, not any one property: a property-specific value is a comparative market analysis (licensed brokerage "
         "work, NJ Real Estate Commission Bulletin 13-05) or an appraisal (N.J.S.A. 45:14F), and this report is "
         "neither. No single sale is listed and no buyer, seller or address appears; any figure built from fewer than "
         "5 recorded sales is suppressed. Sales figures come from recorded deeds, never from MLS statistics.")


# -- months (pure) --------------------------------------------------------------------------------------------------
YM = tuple[int, int]


def ym(d: date) -> YM:
    return d.year, d.month


def next_month(m: YM) -> YM:
    return (m[0] + 1, 1) if m[1] == 12 else (m[0], m[1] + 1)


def prev_month(m: YM) -> YM:
    return (m[0] - 1, 12) if m[1] == 1 else (m[0], m[1] - 1)


def month_end(m: YM) -> date:
    return date(*next_month(m), 1) - timedelta(days=1)


def month_name(m: YM, year: bool = True, short: bool = False) -> str:
    d = date(m[0], m[1], 1)
    return d.strftime(("%b" if short else "%B") + (" %Y" if year else ""))


def span_label(months: list[YM]) -> str:
    """'Jul 2025-Jun 2026'; 'Apr-Jun 2026' inside one year."""
    a, b = months[0], months[-1]
    if a == b:
        return month_name(a, short=True)
    if a[0] == b[0]:
        return f"{month_name(a, year=False, short=True)}-{month_name(b, short=True)}"
    return f"{month_name(a, short=True)}-{month_name(b, short=True)}"


def covered_months(counts: dict[YM, int], first: date, as_of: date, lag_days: int, *, ratio: float = 0.6,
                   min_median: int = 10) -> list[YM]:
    """The complete calendar months a town's deed file covers, oldest first (pure).

    - the first month counts only when the file starts in its first week (an export from mid-month leaves it partial);
    - the last month is the newest one that ended `lag_days` before `as_of` (the newest recorded date in the file):
      deeds post weeks after closing, so later months are still filling in;
    - every month needs a deed and, in a town with a normal monthly volume of `min_median`+, at least `ratio` of the
      median month (a thin month is a gap in the export); the run of healthy months ending at the last one is kept."""
    if first > as_of:
        return []
    start = ym(first) if first.day <= 7 else next_month(ym(first))
    cutoff = as_of - timedelta(days=lag_days)
    end = ym(cutoff) if month_end(ym(cutoff)) <= cutoff else prev_month(ym(cutoff))
    months: list[YM] = []
    m = start
    while m <= end:
        months.append(m)
        m = next_month(m)
    if not months:
        return []
    med = statistics.median([counts.get(x, 0) for x in months])
    run: list[YM] = []
    for x in reversed(months):
        c = counts.get(x, 0)
        if c < 1 or (med >= min_median and c < ratio * med):
            break
        run.append(x)
    return list(reversed(run))


# -- sales and their statistics (pure) ------------------------------------------------------------------------------
@dataclass
class Sale:
    parcel: str
    deed_date: date
    price: float
    sqft: float | None = None
    company: bool | None = None            # None: no buyer name on the record
    product: str = "house"                 # condo / 1-family / 2-4 family / house (unit count not on file)
    package: bool = False                  # one price on 2+ parcels: left out of the medians


@dataclass
class Stats:
    n: int
    median_price: float | None
    median_ppsf: float | None
    ppsf_n: int
    company_pct: float | None
    company_n: int


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def stats(sales: list[Sale]) -> Stats:
    """Counts include package deeds (each parcel is a home sold); prices leave them out (one price covers several)."""
    priced = [s for s in sales if not s.package]
    ppsf = [s.price / s.sqft for s in priced if s.sqft and s.sqft >= MIN_SQFT]
    named = [s for s in sales if s.company is not None]
    med = _median([s.price for s in priced]) if len(priced) >= MIN_CELL else None
    per_sqft = _median(ppsf) if len(ppsf) >= MIN_CELL else None
    share = 100.0 * sum(1 for s in named if s.company) / len(named) if len(named) >= MIN_CELL else None
    return Stats(n=len(sales), median_price=round(med, -3) if med else None,
                 median_ppsf=float(round(per_sqft)) if per_sqft else None, ppsf_n=len(ppsf), company_pct=share,
                 company_n=len(named))


def mark_packages(sales: list[Sale]) -> None:
    """One price on one date for 2+ parcels is a package deed (NJGIN / SR1A data can't be trusted to flag it)."""
    dup = Counter((s.price, s.deed_date) for s in sales)
    for s in sales:
        s.package = dup[(s.price, s.deed_date)] > 1


def company_buyer(grantee: str | None) -> bool | None:
    """An LLC / corporation / partnership on the deed (a company, never a family trust or an estate: s4.company_like);
    banks and government are left out of "companies". None when the deed names no buyer."""
    from engine.streams.s4_contractor_leads import company_like
    g = clean(grantee or "")
    if not g:
        return None
    return company_like(g) and not is_government(g) and not is_lender(g)


def product_of(parcel_id: str, prop: Property | None) -> str:
    qual = parcel_id.split("-")[4] if parcel_id.count("-") >= 4 else ""
    desc = (prop.building_desc if prop is not None else "") or ""
    if qual.upper().startswith("C") or re.search(r"COND|\bCO-?OP\b", desc, re.I):
        return "condo"
    units = prop.dwellings if prop is not None else None
    if units:
        return "1-family" if units == 1 else "2-4 family" if units <= 4 else "house"
    return "house"


# -- the town file --------------------------------------------------------------------------------------------------
@dataclass
class TownReport:
    mun: str
    town: str
    county: str
    months: list[YM]
    data_as_of: date
    sales: list[Sale]                      # every arm's-length Class 2 sale inside the long window
    sheriff: int | None = None             # None: not shown (reason in sheriff_note)
    sheriff_unplaced: int = 0              # open sales in the county whose town is unclear (the count is a floor)
    sheriff_note: str = ""
    lis_pendens: int | None = None
    lp_note: str = ""
    tax_rate: float | None = None          # annual taxes / assessed value (a fraction)
    tax_n: int = 0
    tax_note: str = ""
    ratio: dict[str, Any] | None = None
    ratio_year: int | None = None
    dead_zone: int | None = None           # None: not shown
    zones: list[tuple[float, float]] = field(default_factory=list)
    path: Path | None = None
    suppressed: int = 0

    @property
    def long_months(self) -> list[YM]:
        return self.months[-12:]

    @property
    def short_months(self) -> list[YM]:
        return self.months[-3:]

    def window(self, months: list[YM]) -> list[Sale]:
        a, b = date(*months[0], 1), month_end(months[-1])
        return [s for s in self.sales if a <= s.deed_date <= b]

    @property
    def long(self) -> Stats:
        return stats(self.window(self.long_months))

    @property
    def short(self) -> Stats:
        return stats(self.window(self.short_months))

    @property
    def long_label(self) -> str:
        n = len(self.long_months)
        return f"Last {n} months on record ({span_label(self.long_months)})"

    @property
    def short_label(self) -> str:
        return f"Last 3 months on record ({span_label(self.short_months)})"


def county_key(name: str | None) -> str:
    return re.sub(r"\s+county$", "", clean(name or ""), flags=re.I).strip().title()


def _iso(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _mun_of(parcel_id: str) -> str:
    bits = (parcel_id or "").split("-")
    return bits[1] if len(bits) >= 4 and re.fullmatch(r"\d{4}", bits[1] or "") else ""


def town_names(session: Session, table: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """mun_code -> (town, county): the Chapter 123 table first (all target towns), then our parcels."""
    out: dict[str, tuple[str, str]] = {}
    for code, row in (table.get("ratios") or {}).items():
        if row.get("name"):
            out[str(code)] = (town_label(row["name"]), county_key(row.get("county")))
    for code, name, county in session.execute(select(Property.mun_code, Property.municipality, Property.county).where(
            Property.mun_code.is_not(None), Property.municipality.is_not(None)).distinct()):
        if str(code) not in out and name:
            out[str(code)] = (town_label(name), county_key(county))
    return out


def collect_towns(session: Session, today: date, lim: dict[str, Any], names: dict[str, tuple[str, str]],
                  counties: set[str], stats_out: Counter[str]) -> dict[str, TownReport]:
    """Per town: the covered months and the arm's-length Class 2 sales in them (no report when the file is too short,
    too old or too thin)."""
    from agents.scrapers.owner_records_agent import is_arms_length
    since = today - timedelta(days=int(lim["months_back"]) * 31)
    by_mun: dict[str, list[DeedTransfer]] = {}
    for d in session.scalars(select(DeedTransfer).where(DeedTransfer.deed_date >= since,
                                                        DeedTransfer.deed_date <= today)):
        stats_out["deeds_examined"] += 1
        mun = _mun_of(d.parcel_id)
        if mun:
            by_mun.setdefault(mun, []).append(d)
    out: dict[str, TownReport] = {}
    for mun, deeds in sorted(by_mun.items()):
        town, county = names.get(mun, ("", ""))
        county = county or county_key(deeds[0].county or deeds[0].parcel_id.split("-")[0])
        if not town or (counties and county not in counties):
            continue
        recorded = [r for r in (_iso((d.raw_json or {}).get("recorded_date")) for d in deeds) if r and r <= today]
        as_of = max(recorded) if recorded else max(d.deed_date for d in deeds)
        if as_of < today - timedelta(days=int(lim["max_data_age_days"])):
            stats_out["towns_stale"] += 1
            continue
        counts = Counter(ym(d.deed_date) for d in deeds)
        months = covered_months(counts, min(d.deed_date for d in deeds), as_of, int(lim["record_lag_days"]))
        if len(months) < max(3, int(lim["min_months"])):
            stats_out["towns_short"] += 1
            continue
        a, b = date(*months[-12:][0], 1), month_end(months[-1])
        seen: set[tuple[str, date]] = set()
        picked: list[DeedTransfer] = []
        for d in sorted(deeds, key=lambda x: x.id):
            if not (a <= d.deed_date <= b) or (d.property_class or "").strip().upper() != "2":
                continue
            if not is_arms_length({"price": d.sale_price, "nu_code": (d.nu_code or "").strip() or None}):
                continue
            k = (d.parcel_id, d.deed_date)
            if k in seen:
                continue                                 # the same deed under two buyer spellings counts once
            seen.add(k)
            picked.append(d)
        if len(picked) < int(lim["min_sales"]):
            stats_out["towns_thin"] += 1
            continue
        props: dict[str, Property] = {}
        ids = sorted({d.parcel_id for d in picked})
        for i in range(0, len(ids), 500):
            for p in session.scalars(select(Property).where(Property.parcel_id.in_(ids[i:i + 500]))):
                props[p.parcel_id] = p
        sales = []
        for d in picked:
            p = props.get(d.parcel_id)
            sqft = (d.raw_json or {}).get("sqft") or (p.sqft if p is not None else None)
            try:
                sqft = float(sqft) if sqft else None
            except (TypeError, ValueError):
                sqft = None
            sales.append(Sale(parcel=d.parcel_id, deed_date=d.deed_date, price=float(d.sale_price), sqft=sqft,
                              company=company_buyer(d.grantee), product=product_of(d.parcel_id, p)))
        mark_packages(sales)
        out[mun] = TownReport(mun=mun, town=town, county=county, months=months, data_as_of=as_of, sales=sales)
    return out


# -- the pipeline, taxes, the dead zones ----------------------------------------------------------------------------
def town_label(name: str | None) -> str:
    """'E RUTHERFORD BORO' -> 'East Rutherford', 'HASBROUCK HGHTS BORO' -> 'Hasbrouck Heights' (the Director's
    table abbreviates a few names; reports and e-mails spell them out)."""
    t = muni_base(name)
    t = re.sub(r"^([EWNS])\s+", lambda m: {"E": "EAST ", "W": "WEST ", "N": "NORTH ", "S": "SOUTH "}[m.group(1)], t)
    t = re.sub(r"\b(HGHTS|HTS)\b", "HEIGHTS", t)
    return postal_title(t)


def _town_index(names: dict[str, tuple[str, str]]) -> dict[tuple[str, str], str]:
    """(county, MUNI BASE) -> mun_code, for records that carry a town name but no parcel ("NEWARK", "Freehold Twp")."""
    idx: dict[tuple[str, str], str] = {}
    for code, (town, county) in names.items():
        idx.setdefault((county, muni_base(town)), code)
    return idx


def sheriff_counts(session: Session, reports: dict[str, TownReport], today: date, lim: dict[str, Any],
                   names: dict[str, tuple[str, str]]) -> None:
    """Sheriff sales scheduled in the next `pipeline_days` per town, only for counties whose CivilView list the desk
    harvested in the last `sheriff_fresh_days` (Mercer is not on CivilView: never counted)."""
    from engine.streams.s3_surplus_overages import CLOSED, current_status
    days = int(lim["pipeline_days"])
    fresh = utcnow() - timedelta(days=int(lim["sheriff_fresh_days"]))
    covered = {county_key(c) for (c,) in session.execute(
        select(Property.county).join(DistressSignal, DistressSignal.property_id == Property.id).where(
            DistressSignal.signal_type == SignalType.SHERIFF_SALE, DistressSignal.last_seen_at >= fresh).distinct())}
    by_county: dict[str, list[tuple[str, str]]] = {}
    for code, (town, county) in names.items():
        by_county.setdefault(county, []).append((muni_base(town), code))
    found: dict[str, set[int]] = {}
    unplaced: Counter[str] = Counter()
    for sig, prop in session.execute(
            select(DistressSignal, Property).join(Property, Property.id == DistressSignal.property_id).where(
                DistressSignal.signal_type == SignalType.SHERIFF_SALE, DistressSignal.active.is_(True),
                DistressSignal.scheduled_auction_date >= today,
                DistressSignal.scheduled_auction_date <= today + timedelta(days=days))):
        if CLOSED.search(current_status(sig.status, (sig.raw_metadata_json or {}).get("status_history"))):
            continue
        if (prop.property_class or "").strip() not in ("", "2"):
            continue                                     # homes: Class 2 (an unmatched parcel has no class)
        mun = prop.mun_code or place_unmatched(f"{prop.street_address or ''} {prop.city or ''}",
                                               by_county.get(county_key(prop.county), []))
        if mun:
            found.setdefault(str(mun), set()).add(prop.id)
        else:
            unplaced[county_key(prop.county)] += 1      # its town is unclear: no count in that county is exact
    for rep in reports.values():
        if rep.county in covered:
            rep.sheriff, rep.sheriff_unplaced = len(found.get(rep.mun, set())), int(unplaced.get(rep.county, 0))
        else:
            rep.sheriff_note = f"not tracked for {rep.county} County"


def place_unmatched(text: str, towns: list[tuple[str, str]]) -> str:
    """The town of a sheriff sale CivilView couldn't match to a parcel, only when its address text ENDS with one of the
    county's town names ("Wyoming Avenue South Orange" -> South Orange, never Orange); a village name ("Hunt Avenue
    Vauxhall") or a typo stays unplaced (pure)."""
    t = re.sub(r"[^A-Z ]+", " ", (text or "").upper())
    t = re.sub(r"\s+(NJ|NEW JERSEY)\s*$", "", " ".join(t.split()))
    hits: list[tuple[int, str]] = []
    for base, code in towns:
        for name in {base, re.sub(r"\s+VILLAGE$", "", base or "")}:   # "SOUTH ORANGE VILLAGE" is "South Orange"
            if not name or not (t == name or t.endswith(" " + name)):
                continue
            before = t[:len(t) - len(name)].split()
            if before and before[-1] in _COMPOUND:
                continue                                 # "... South Orange" is never Orange
            hits.append((len(name), code))
    return max(hits)[1] if hits else ""


# a town name after one of these words is part of a longer name ("West Orange", "New Milford", "Glen Rock")
_COMPOUND = {"EAST", "WEST", "NORTH", "SOUTH", "NEW", "OLD", "UPPER", "LOWER", "LITTLE", "GREAT", "MOUNT", "MT", "PORT",
             "LAKE", "GLEN", "FORT", "CAPE", "POINT", "PT", "SADDLE", "RIVER", "HIGHLAND", "BERKELEY", "PERTH"}


def lis_pendens_counts(session: Session, reports: dict[str, TownReport], today: date, lim: dict[str, Any],
                       names: dict[str, tuple[str, str]]) -> None:
    """Foreclosure lis pendens recorded in the last `pipeline_days`, per town, only where the county's clerk index we
    hold covers the whole window, is current, and ties every filing in it to a town (Union's index gives no town)."""
    days = int(lim["pipeline_days"])
    start = today - timedelta(days=days)
    idx = _town_index(names)
    by_county: dict[str, list[ClerkFiling]] = {}
    span: dict[str, tuple[date, date]] = {}
    for f in session.scalars(select(ClerkFiling).where(ClerkFiling.recorded_date.is_not(None))):
        c = county_key(f.county)
        lo, hi = span.get(c, (f.recorded_date, f.recorded_date))
        span[c] = (min(lo, f.recorded_date), max(hi, f.recorded_date))
        if start <= f.recorded_date <= today and not f.is_discharge and FORECLOSURE_DOC.search(f.doc_type or ""):
            by_county.setdefault(c, []).append(f)
    props = {p.id: p for p in session.scalars(select(Property).where(Property.id.in_(sorted(
        {f.property_id for fs in by_county.values() for f in fs if f.property_id}))))} if by_county else {}
    per_county: dict[str, Counter[str] | str] = {}
    for c in {r.county for r in reports.values()}:
        if c not in span:
            per_county[c] = f"not collected for {c} County yet"
            continue
        lo, hi = span[c]
        if lo > start:
            per_county[c] = f"our {c} County clerk index starts {lo:%b} {lo.day}, under {days} days ago"
            continue
        if hi < today - timedelta(days=int(lim["clerk_fresh_days"])):
            per_county[c] = f"our {c} County clerk index is not current"
            continue
        counts: Counter[str] = Counter()
        ok = True
        for f in by_county.get(c, []):
            p = props.get(f.property_id) if f.property_id else None
            mun = (p.mun_code if p is not None else None) or idx.get((c, muni_base(f.town)))
            if not mun:
                ok = False
                break
            counts[str(mun)] += 1
        per_county[c] = counts if ok else f"the {c} County clerk index doesn't give the town for every filing"
    for rep in reports.values():
        v = per_county.get(rep.county)
        if isinstance(v, Counter):
            rep.lis_pendens = int(v.get(rep.mun, 0))
        else:
            rep.lp_note = str(v or "not collected")


def tax_lines(session: Session, reports: dict[str, TownReport], table: dict[str, Any]) -> None:
    """The rate MOD-IV implies (median annual taxes / assessed value of the town's Class 2 parcels on file) and the
    Director's average ratio. A town whose ratio moved more than 20% in a year (a revaluation) gets no implied rate:
    last year's tax over this year's assessment would mislead."""
    src = table.get("source") or {}
    year = int(src["tax_year"]) if src.get("tax_year") else None
    rates: dict[str, list[float]] = {}
    for mun, taxes, assessed in session.execute(select(Property.mun_code, Property.annual_taxes,
                                                       Property.assessed_total).where(
            Property.mun_code.in_(sorted(reports)), Property.property_class == "2", Property.annual_taxes > 0,
            Property.assessed_total > 0)):
        rates.setdefault(str(mun), []).append(float(taxes) / float(assessed))
    for rep in reports.values():
        row = (table.get("ratios") or {}).get(rep.mun)
        if row and row.get("avg"):
            rep.ratio, rep.ratio_year = row, year
        v = rates.get(rep.mun, [])
        rep.tax_n = len(v)
        moved = bool(row and row.get("avg") and row.get("avg_2026")
                     and abs(float(row["avg"]) - float(row["avg_2026"])) / float(row["avg_2026"]) > 0.20)
        if moved:
            rep.tax_note = "not computed: the assessment ratio moved more than 20% in a year (revaluation)"
        elif len(v) < MIN_CELL:
            rep.tax_note = "fewer than 5 parcels with a tax bill on file"
        else:
            rep.tax_rate = statistics.median(v)


def dead_zone_counts(session: Session, reports: dict[str, TownReport], fresh_days: float = 3.0) -> None:
    """For-sale listings inside a graduated-fee dead zone, only in towns every planned band of s7's daily high-value
    pass answered for in the last `fresh_days` (elsewhere the listing harvest stops at $900K: a 0 would be unproven)."""
    try:
        from engine.streams import s7_highvalue_harvest as hv
        from engine.streams import s7_mansion_tax_cliff as s7
        zones = s7.dead_zones()
        ranges = hv.query_ranges(hv.bands())
    except Exception:                                    # noqa: BLE001 - an optional line, never a reason to fail
        return
    row = session.get(SwarmState, hv.HISTORY_KEY)
    hist = ((row.value_json or {}).get("zips") or {}) if row is not None else {}
    cutoff = utcnow() - timedelta(days=fresh_days)

    def answered(qkey: str) -> bool:
        for st in hist.get(qkey) or []:
            try:
                if datetime.fromisoformat(str(st)) >= cutoff:
                    return True
            except ValueError:
                continue
        return False

    towns = {t.split(",")[0].strip().upper(): t for t in hv.TOWNS}
    for rep in reports.values():
        loc = towns.get(rep.town.upper())
        if not loc or not ranges or not all(answered(hv.Query(loc, lo, hi).key) for lo, hi in ranges):
            continue
        n = 0
        for price, ptype, pclass in session.execute(
                select(Listing.list_price, Listing.property_type, Property.property_class)
                .outerjoin(Property, Property.id == Listing.property_id)
                .where(Listing.status == "for_sale", Listing.last_seen_at >= cutoff,
                       or_(Property.mun_code == rep.mun, func.upper(Listing.city) == rep.town.upper()))):
            # the graduated fee applies to Class 2 homes only (a listing with no parcel: a 1-4 family listing type)
            n += int(s7.cliff(price) is not None and s7.residential(pclass, ptype))
        rep.dead_zone, rep.zones = n, zones


# -- the report (one page) ------------------------------------------------------------------------------------------
def _usd(v: float | None) -> str:
    return money(v) if v else "-"


def stat_cells(st: Stats) -> tuple[list[str], int]:
    """[homes sold, median price, median $/sq ft, bought by companies] with the suppression text; and how many cells
    were suppressed."""
    out, n = [], 0
    if st.n < MIN_CELL:
        return [SUPPRESSED] * 4, 4
    out.append(f"{st.n:,}")
    for v, fmt in ((st.median_price, _usd), (st.median_ppsf, lambda x: f"${x:,.0f}"),
                   (st.company_pct, lambda x: f"{x:.0f}%")):
        if v is None:
            out.append(SUPPRESSED)
            n += 1
        else:
            out.append(fmt(v))
    return out, n


def product_rows(rep: TownReport, known_share: float) -> tuple[list[list[str]], int, str]:
    """By property type over the long window: houses vs condo units; 1-family / 2-4 family only when MOD-IV unit
    counts are on file for `known_share` of the house sales."""
    sales = rep.window(rep.long_months)
    houses = [s for s in sales if s.product != "condo"]
    condos = [s for s in sales if s.product == "condo"]
    known = [s for s in houses if s.product in ("1-family", "2-4 family")]
    rows: list[list[str]] = []
    hidden = 0
    groups: list[tuple[str, list[Sale]]] = [("1-4 family houses", houses)]
    split = bool(houses) and len(known) / len(houses) >= known_share
    if split:
        groups += [("&nbsp;&nbsp;1-family", [s for s in houses if s.product == "1-family"]),
                   ("&nbsp;&nbsp;2-4 family", [s for s in houses if s.product == "2-4 family"])]
    groups.append(("Condo units (MOD-IV qualifier C)", condos))
    for label, group in groups:
        cells, n = stat_cells(stats(group))
        rows.append([label, *cells])
        hidden += n
    note = ("" if split else f"1-family vs 2-4 family is shown when MOD-IV unit counts are on file for "
            f"{known_share:.0%} of the house sales ({len(known)} of {len(houses)} here).")
    return rows, hidden, note


def operator_company(op: dict[str, str]) -> str:
    return (op.get("company") or op.get("name") or "our research desk").strip()


def email_box(rep: TownReport, company: str) -> list[str]:
    """The 4 plain sentences an agent can paste to their sphere: counts and medians only, and the source."""
    lg, sh = rep.long, rep.short
    a, b = rep.long_months[0], rep.long_months[-1]
    s1 = f"Here's a quick look at home sales in {rep.town} by public record, through {month_name(b)}."
    if lg.median_price:
        s2 = (f"{lg.n:,} homes sold from {month_name(a)} to {month_name(b)}, at a median price of "
              f"{_usd(lg.median_price)}" + (f", about ${lg.median_ppsf:,.0f} per square foot where the size is on record"
                                           if lg.median_ppsf else "") + ".")
    else:
        s2 = f"{lg.n:,} homes sold from {month_name(a)} to {month_name(b)}."
    if sh.n >= MIN_CELL and sh.median_price:
        s3 = (f"In the last three months on record ({span_label(rep.short_months)}), {sh.n:,} homes sold at a median "
              f"of {_usd(sh.median_price)}.")
    elif lg.company_pct is not None:
        s3 = f"Companies such as LLCs bought {lg.company_pct:.0f}% of those homes."
    else:
        s3 = "Deeds post a few weeks after closing, so the newest weeks are not counted yet."
    return [s1, s2, s3, f"Source: NJ public records via {company}."]


def _pct(x: float) -> str:
    return f"{x:.2f}%"


def render_report(path: Path, rep: TownReport, ctx: StreamContext, lim: dict[str, Any], brand: str = "") -> int:
    """The town's one-page report (dropping optional notes until it fits one page); returns the suppressed cells."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    from engine.contracts import _pdf_safe
    from engine.streams.s4_contractor_leads import page_count
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=8.4, leading=10.2)
    small = ParagraphStyle("s", parent=body, fontSize=6.8, leading=8.2)
    cell = ParagraphStyle("c", parent=body, fontSize=7.6, leading=9)
    h1 = ParagraphStyle("h", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=15, leading=18, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=9.6, leading=12, spaceBefore=6,
                        spaceAfter=2)
    boxed = ParagraphStyle("x", parent=body, backColor=colors.HexColor("#eef6ff"), borderPadding=6,
                           borderColor=colors.HexColor("#1d4ed8"), borderWidth=0.6, spaceBefore=6, leading=11)

    def p(t: str, st: ParagraphStyle = cell) -> Paragraph:
        return Paragraph(_pdf_safe(t), st)

    def table(head: list[str], rows: list[list[str]], widths: list[float]) -> Table:
        t = Table([[p(f"<b>{h}</b>") for h in head]] + [[p(str(c)) for c in r] for r in rows],
                  colWidths=[w * inch for w in widths], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#9ca3af")),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return t

    company = operator_company(ctx.operator)
    head = ["Window", "Homes sold", "Median price", "Median price per sq ft*", "Bought by companies**"]
    s_cells, s_hidden = stat_cells(rep.short)
    l_cells, l_hidden = stat_cells(rep.long)
    p_rows, p_hidden, p_note = product_rows(rep, float(lim["split_known_share"]))
    days = int(lim["pipeline_days"])
    pipe = [[f"Sheriff foreclosure sales scheduled in the next {days} days (sheriff's notices)",
             rep.sheriff_note if rep.sheriff is None else f"{rep.sheriff:,}" if not rep.sheriff_unplaced else
             f"at least {rep.sheriff:,} ({rep.sheriff_unplaced} more in {rep.county} County have a notice that doesn't "
             "make the town clear)"],
            [f"Foreclosure lis pendens filed in the last {days} days (county clerk index)",
             f"{rep.lis_pendens:,}" if rep.lis_pendens is not None else rep.lp_note]]
    taxes: list[list[str]] = [[f"Tax rate implied by MOD-IV (median of the last annual tax bill / assessed value, "
                               f"{rep.tax_n} parcels on file)",
                               f"${rep.tax_rate * 100:.3f} per $100 of assessed value" if rep.tax_rate
                               else rep.tax_note]]
    if rep.ratio:
        r = rep.ratio
        taxes.append([f"Chapter 123 average ratio of assessed to true value, tax year {rep.ratio_year or '?'} "
                      "(NJ Division of Taxation)",
                      f"{float(r['avg']):.2f}% (common level range {float(r['lower']):.2f}%-{float(r['upper']):.2f}%)"])
        if rep.tax_rate:
            eff = rep.tax_rate * min(float(r["avg"]), 100.0)
            taxes.append(["Effective rate (implied tax rate x average ratio)", f"about {_pct(eff)} of true value a year"])
    box = ("<b>Ready-to-send e-mail text</b> (paste it to your sphere as is):<br/>"
           + " ".join(escape(s) for s in email_box(rep, company)))
    a_l = rep.long_months
    notes = [
        "Homes sold = arm's-length recorded deeds of residential (Class 2: 1-4 family and condo) parcels: SR1A "
        "non-usable code blank and a price of $30,000 or more. One deed price covering 2+ parcels counts each home but "
        "stays out of the medians. Medians are rounded to $1,000.",
        f"Months are complete calendar months. Deeds post weeks after closing, so months after {month_name(a_l[-1])} "
        f"are still filling in and are left out (record current to {rep.data_as_of:%B} {rep.data_as_of.day}, "
        f"{rep.data_as_of.year}).",
        f"*Living area as stated on the deed or assessment record ({rep.long.ppsf_n} of {rep.long.n} sales in the long "
        "window). **An LLC, corporation or partnership named as buyer on the deed (banks, government, trusts and estates "
        "left out), a common proxy for investor purchases.",
    ]
    if p_note:
        notes.append(p_note)
    zone_line = None
    if rep.dead_zone is not None and rep.zones:
        ranges = ", ".join(f"{money(t)}-{money(e)}" for t, e in rep.zones)
        zone_line = (f"<b>Graduated transfer-fee dead zones</b> ({ranges}: the seller nets less after the statutory "
                     f"transfer fees than at the line, N.J.S.A. 46:15-7, P.L.2025, c.69): {rep.dead_zone} for-sale home "
                     f"listing{'' if rep.dead_zone == 1 else 's'} in {rep.town} priced inside one, from our daily read "
                     "of public for-sale listings (the only figure here not from recorded deeds).")
    sources = ("Sources: county deed records (SR1A sale data from the county tax board), NJ MOD-IV assessment records, "
               "sheriff sale notices (CivilView), county clerk lis pendens indexes, NJ Division of Taxation Chapter 123 "
               "certification" + (", public for-sale listings (dead-zone count only)" if zone_line else "") + ".")
    sub = (f"{rep.county} County, NJ · {ctx.now:%B %Y} edition · recorded sales through {month_name(a_l[-1])} · "
           f"prepared {ctx.now:%B} {ctx.now.day}, {ctx.now.year} by {escape(company)}")
    hidden = s_hidden + l_hidden + p_hidden

    def story(keep_zone: bool, keep_notes: int) -> list[Any]:
        st: list[Any] = [p(f"{escape(rep.town)} Housing Market Report", h1), p(sub, body)]
        if brand:
            st.append(p(f"<b>Presented by {escape(brand)}</b>", body))
        st += [Spacer(1, 3), p(DISCLAIMER, body), p("Recorded home sales", h2),
               table(head, [[rep.short_label, *s_cells], [rep.long_label, *l_cells]], [2.35, 1.05, 1.15, 1.35, 1.4]),
               p(f"By property type, {rep.long_label[0].lower()}{rep.long_label[1:]}", h2),
               table(["Type", "Homes sold", "Median price", "Median price per sq ft*", "Bought by companies**"],
                     p_rows, [2.35, 1.05, 1.15, 1.35, 1.4]),
               p("Foreclosure pipeline (counts only, no addresses)", h2), table(["Record", "Count"], pipe, [4.6, 2.7]),
               p("Property taxes", h2), table(["Measure", "Figure"], taxes, [4.6, 2.7])]
        if zone_line and keep_zone:
            st += [Spacer(1, 4), p(zone_line, body)]
        st += [Spacer(1, 4), p(box, boxed), Spacer(1, 5)]
        st += [p(n, small) for n in notes[:keep_notes]]
        st += [p(sources, small), p(LEGAL, small)]
        st += [p(f, small) for f in operator_footer(ctx.operator)]
        return st

    path.parent.mkdir(parents=True, exist_ok=True)
    for keep_zone, keep_notes in ((True, len(notes)), (False, len(notes)), (False, 3), (False, 2)):
        SimpleDocTemplate(str(path), pagesize=LETTER, title=f"{rep.town} Housing Market Report",
                          leftMargin=0.55 * inch, rightMargin=0.55 * inch, topMargin=0.45 * inch,
                          bottomMargin=0.4 * inch).build(story(keep_zone, keep_notes))
        if page_count(path) <= 1:
            break
    return hidden


def report_path(base: Path, rep: TownReport, now: datetime) -> Path:
    return base / f"s12_market_{slug(rep.county).lower()}_{slug(rep.town).lower()}_{now:%Y-%m}.pdf"


# -- Touch 1, Touch 2 and the delivery note -------------------------------------------------------------------------
def _words(text: str) -> int:
    from engine import human_copy as hc
    return hc.word_count(text)


def pipeline_clause(rep: TownReport, today: date, days: int) -> str:
    """The one pipeline count Touch 1 may state: filings when the clerk index is complete for the town, else scheduled
    sheriff sales when the county's CivilView list is current, else nothing (a count we can't prove is never said)."""
    if rep.lis_pendens:
        since = today - timedelta(days=days)
        return (f", and {rep.lis_pendens} foreclosure case{'s were' if rep.lis_pendens != 1 else ' was'} filed there "
                f"since {since:%B} {since.day}")
    if rep.sheriff and not rep.sheriff_unplaced:
        until = today + timedelta(days=days)
        return (f", and {rep.sheriff} sheriff sale{'s are' if rep.sheriff != 1 else ' is'} set there before "
                f"{until:%B} {until.day}")
    return ""


def touch1(rep: TownReport, name: str | None, now: datetime, first: str = "", days: int = 90,
           max_words: int = 45) -> tuple[str, str] | None:
    """(subject, body): two plain sentences, no attachment, no link, no "$" but the recorded median (a public aggregate).
    The longest true version that fits `max_words` is used; None when even the shortest doesn't (or the count is
    suppressed: nothing to say)."""
    from engine.streams.s2_deal_desk_saas import greeting
    lg = rep.long
    if lg.n < MIN_CELL:
        return None
    a, b = rep.long_months[0], rep.long_months[-1]
    median = f", with a median of {_usd(lg.median_price)}" if lg.median_price else ""
    clause = pipeline_clause(rep, now.date(), days)
    month = f"{now:%B}'s"
    for cl, town_in_s2 in ((clause, True), (clause, False), ("", True), ("", False)):
        s1 = f"{lg.n:,} homes sold in {rep.town} from {month_name(a, year=False)} to {month_name(b, year=False)} by " \
             f"public record{median}{cl}."
        s2 = (f"I put that into a 1-page monthly {rep.town + ' ' if town_in_s2 else ''}report agents can send to their "
              f"sphere; want {month}, free?")
        if _words(f"{s1} {s2}") <= max_words:
            body = f"{greeting(name)}\n\n{s1} {s2}" + (f"\n\n{first}" if first else "")
            return f"[{rep.town}] home sales by public record", body
    return None


def sample_body(rep: TownReport, lim: dict[str, Any]) -> str:
    """Touch 2, sent with the full report only after their yes: what is attached, the flat prices, how to start."""
    monthly, exclusive = float(lim["price_monthly"]), float(lim["price_exclusive"])
    return (f"Attached is the {rep.town} report (the e-mail box at the end is ready to paste). It's ${monthly:,.0f} a "
            f"month plus NJ sales tax per town, or ${exclusive:,.0f} a month plus NJ sales tax to be the only brokerage "
            "getting it there, with no commission or per-deal fee. Reply \"start\" and I'll set it up.")


def delivery_body(rep: TownReport, name: str | None, now: datetime, first: str = "") -> str:
    from engine.streams.s2_deal_desk_saas import greeting
    return (f"{greeting(name)}\n\nHere's the {rep.town} market report for {now:%B %Y}, attached; the e-mail text at the "
            "end is ready to paste to your sphere. Reply with any questions or another town you'd like."
            + (f"\n\n{first}" if first else ""))


def brand_line(t: FirmTarget) -> str:
    """'Pat Agent, Sample Realty' on a subscriber's own copy: their name when it reads as a person, and the brokerage
    (NJ advertising rules want the brokerage named)."""
    from engine.streams.s2_deal_desk_saas import first_name
    person = postal_title(t.name) if t.name and first_name(t.name) != "there" else ""
    return ", ".join(x for x in (person, clean(t.org or "")) if x)


# -- targets --------------------------------------------------------------------------------------------------------
def agent_candidates(session: Session, reports: dict[str, TownReport], ctx: StreamContext, lim: dict[str, Any],
                     counties: set[str]) -> tuple[list[dict[str, Any]], int]:
    """(agents with `min_listings` active or recent listings in one target-county town that has a report, minus office
    inboxes, stop lists, bounces and deal talks; the number of agents with that many listings in any target town)."""
    from engine import firm_outbox as fo
    from engine.deal_desk import OFFICE_MAILBOX
    from engine.streams.s2_deal_desk_saas import in_deal_talks
    recent = utcnow() - timedelta(days=int(lim["listing_recent_days"]))
    live = {pid for (pid,) in session.execute(select(DistressSignal.property_id).where(
        DistressSignal.signal_type.in_([str(t) for t in LISTED]), DistressSignal.active.is_(True)))}
    live |= {pid for (pid,) in session.execute(select(Listing.property_id).where(
        Listing.property_id.is_not(None), Listing.last_seen_at >= recent))}
    agents: dict[str, dict[str, Any]] = {}
    stopped: set[str] = set()
    for c, prop in session.execute(
            select(Contact, Property).join(Lead, Lead.id == Contact.lead_id).join(Property, Property.id == Lead.property_id)
            .where(Contact.contact_type == ContactType.LISTING_AGENT, Contact.email.is_not(None),
                   Property.mun_code.is_not(None))):
        email = (c.email or "").strip().lower()
        if not email:
            continue
        if c.dnc_status == "dnc" or (c.confidence_score or 0) <= 0:
            stopped.add(email)
            continue
        if prop.id not in live or (counties and county_key(prop.county) not in counties):
            continue
        a = agents.setdefault(email, {"email": email, "name": "", "company": "", "towns": {}})
        a["name"] = a["name"] or (postal_title(c.name) if c.name else "")
        a["company"] = a["company"] or clean(c.company or "")
        a["towns"].setdefault(str(prop.mun_code), set()).add(prop.id)
    need = int(lim["min_listings"])
    seen = {e: a for e, a in agents.items() if any(len(v) >= need for v in a["towns"].values())}
    talks = in_deal_talks(session, ctx.now)
    out = []
    for email, a in sorted(seen.items()):
        if email in stopped or OFFICE_MAILBOX.match(email) or email in talks or fo.stop_reason(session, email):
            continue
        towns = [(len(v), m) for m, v in a["towns"].items() if len(v) >= need and m in reports]
        if towns:
            a["options"] = sorted(towns, key=lambda x: (-x[0], x[1]))
            out.append(a)
    out.sort(key=lambda a: (-a["options"][0][0], a["email"]))
    return out, len(seen)


def exclusive_towns(subs: list[tuple[FirmTarget, str]]) -> dict[str, FirmTarget]:
    return {mun: t for t, mun in subs if isinstance(t.data_json, dict) and t.data_json.get("s12_exclusive")}


def subscribers(session: Session, reports: dict[str, TownReport], names: dict[str, tuple[str, str]]
                ) -> list[tuple[FirmTarget, str]]:
    """(target, mun_code) for s12 subscribers in TRIAL / PAYING: data_json "s12_mun", else their town by name."""
    from engine import firm_outbox as fo
    by_name = {(county_key(c), muni_base(t)): m for m, (t, c) in names.items()}
    out = []
    for t in fo.subscribers(session, KEY):
        data = t.data_json if isinstance(t.data_json, dict) else {}
        mun = str(data.get("s12_mun") or "") or by_name.get((county_key(t.county), muni_base(t.town)), "")
        if not mun and t.town:
            hits = {m for (c, base), m in by_name.items() if base == muni_base(t.town)}
            mun = hits.pop() if len(hits) == 1 else ""
        if mun:
            out.append((t, mun))
    return out


# -- the stream -----------------------------------------------------------------------------------------------------
class TownMarketReportStream(Stream):
    key = KEY
    name = "Town market reports"
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        from core.config import settings
        from engine.streams.s6_tax_appeals import load_ratio_table
        lim = {**DEFAULTS, **{k: v for k, v in (ctx.limits or {}).items() if v is not None}}
        res = StreamResult(stream=self.key)
        today = ctx.now.date()
        counts: Counter[str] = Counter()
        table = load_ratio_table()
        names = town_names(session, table)
        counties = {county_key(c) for c in (settings().get("counties") or [])}
        reports = collect_towns(session, today, lim, names, counties, counts)
        res.processed = counts["deeds_examined"]
        if reports:
            sheriff_counts(session, reports, today, lim, names)
            lis_pendens_counts(session, reports, today, lim, names)
            tax_lines(session, reports, table)
            dead_zone_counts(session, reports)
        res.found = len(reports)
        # every PDF in a subfolder: the outbox's weekly county delivery (out_dir's root) never picks a town report
        towns_dir = ctx.out_dir / "towns"
        suppressed = 0
        ranked = sorted(reports.values(), key=lambda r: (-r.long.n, r.mun))[:int(lim["max_reports"])]
        for rep in ranked:
            rep.path = report_path(towns_dir, rep, ctx.now)
            rep.suppressed = render_report(rep.path, rep, ctx, lim)
            suppressed += rep.suppressed
            res.products += 1
        rendered = {r.mun: r for r in ranked}
        subs = subscribers(session, reports, names)
        excl = exclusive_towns(subs)
        deliveries = self._deliveries(session, ctx, lim, rendered, subs, excl, res) if ctx.send else 0
        pitches, agents_seen, synced = self._pitches(session, ctx, lim, rendered, excl, counties, res)
        res.metrics.update({k: int(v) for k, v in counts.items()})
        res.metrics.update(towns_with_data=len(reports), reports=res.products, agents_seen=agents_seen,
                           targets_synced=synced, pitches=pitches, cells_suppressed=suppressed,
                           deliveries=deliveries, towns_exclusive=len(excl))
        res.notes.append(f"{len(reports)} towns with recorded sales, {res.products} reports, {pitches} pitches proposed")
        return res

    def _deliveries(self, session: Session, ctx: StreamContext, lim: dict[str, Any], rendered: dict[str, TownReport],
                    subs: list[tuple[FirmTarget, str]], excl: dict[str, FirmTarget], res: StreamResult) -> int:
        """Each subscriber's own town, co-branded, once a calendar month (key s12:deliver:<target>:<YYYY-MM>)."""
        first = (ctx.operator or {}).get("first", "")
        n = 0
        for t, mun in subs:
            rep = rendered.get(mun)
            if rep is None or not t.email:
                continue
            holder = excl.get(mun)
            if holder is not None and holder.id != t.id and clean(holder.org or "").lower() != clean(t.org or "").lower():
                res.metrics["deliveries_held_exclusive"] = int(res.metrics.get("deliveries_held_exclusive", 0)) + 1
                continue                                 # another brokerage holds the town
            key = f"{self.key}:deliver:{t.id}:{ctx.now:%Y-%m}"
            if session.scalar(select(FirmOutbox.id).where(FirmOutbox.key == key)):
                continue
            path = report_path(ctx.out_dir / "clients" / str(t.id), rep, ctx.now)
            render_report(path, rep, ctx, lim, brand=brand_line(t))
            res.products += 1
            res.outbound.append(Outbound(
                stream=self.key, key=key, to=t.email, subject=f"{rep.town} market report: {ctx.now:%B %Y}",
                body=delivery_body(rep, t.name, ctx.now, first), priority=PRIORITY_REPLY, kind="delivery",
                attachments=[path], target_id=t.id, commercial=False))
            n += 1
        return n

    def _pitches(self, session: Session, ctx: StreamContext, lim: dict[str, Any], rendered: dict[str, TownReport],
                 excl: dict[str, FirmTarget], counties: set[str], res: StreamResult) -> tuple[int, int, int]:
        from engine import firm_outbox as fo
        from engine.streams.s4_contractor_leads import page_count
        if not rendered:
            return 0, 0, 0
        cands, seen = agent_candidates(session, rendered, ctx, lim, counties)
        if not ctx.send:
            return 0, seen, 0                            # a dry run counts; it writes no target and proposes nothing
        if ctx.now.weekday() >= 5:
            res.notes.append("weekend: no pitches")
            return 0, seen, 0
        gap = timedelta(days=float(fo.cfg().get("pitch_gap_days", 6)))
        cap = int(lim["pitches_per_run"])
        first = (ctx.operator or {}).get("first", "")
        towns_used: set[str] = set()
        made = synced = 0
        for a in cands:
            if made >= cap:
                break
            email = a["email"]
            t = session.scalar(select(FirmTarget).where(FirmTarget.email == email))
            if t is not None and t.stream != self.key:
                continue                                 # one row per e-mail: another stream owns this person
            if t is not None and (t.stage not in OPEN_STAGES or not fo.pitch_ok(t, ctx.now)
                                  or (t.last_contact_at and t.last_contact_at > ctx.now - gap)):
                continue
            key = f"{self.key}:pitch:{email}:{ctx.now:%Y-%m}"
            if session.scalar(select(FirmOutbox.id).where(FirmOutbox.key == key)) or (t is not None and session.scalar(
                    select(FirmOutbox.id).where(FirmOutbox.target_id == t.id, FirmOutbox.status == "queued").limit(1))):
                continue                                 # one pitch per agent per month; nothing while one is queued
            pick = next(((n, m) for n, m in a["options"] if m not in towns_used and m not in excl), None)
            if pick is None:
                continue
            listings, mun = pick
            rep = rendered[mun]
            copy = touch1(rep, a["name"] or (t.name if t is not None else None), ctx.now, first,
                          int(lim["pipeline_days"]), int(lim["max_words"]))
            if copy is None or rep.path is None or page_count(rep.path) != 1:
                continue                                 # Touch 1 says "1-page": never about a longer file
            t = fo.upsert_target(session, kind=TARGET_KIND, stream=self.key, email=email, org=a["company"] or None,
                                 name=a["name"] or None, town=rep.town, county=rep.county,
                                 source="listing data (MLS)", score=float(listings),
                                 data={"s12_mun": rep.mun, "s12_town": rep.town, "s12_listings": listings})
            if t is None:
                continue
            synced += 1
            subject, body = copy
            res.outbound.append(Outbound(
                stream=self.key, key=key, to=email, subject=subject, body=body, priority=PRIORITY_SPECIALTY,
                kind="pitch", attachments=[], target_id=t.id, sample_body=sample_body(rep, lim),
                sample_attachments=[rep.path]))
            towns_used.add(mun)
            made += 1
        return made, seen, synced


STREAM = TownMarketReportStream()
