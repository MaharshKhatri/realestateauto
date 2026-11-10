"""S6 Tax-appeal evidence packets: recorded comparable-sales data for NJ property-tax appeal attorneys (flat fee).

Why this sells: a NJ assessment is reviewed against the municipality's *average ratio* of assessed to true value
(Chapter 123 of 1973, N.J.S.A. 54:1-35a / 54:51A-6). The common level range is that ratio +/- 15%; when a
property's assessment divided by its true value sits above the upper limit, the county board or the Tax Court sets
the taxable value at true value x the average ratio. Attorneys who file these appeals need recorded sales near the
property and the Director's ratio; we already hold both (each valuation's stored comparable set, and the Division
of Taxation's certification in config/streams/nj_chapter123_ratios.yaml).

What a packet is, and what it must never be (legal review 2026-11-09): N.J.S.A. 45:14F-2 defines an appraisal as
"an unbiased analysis, opinion or conclusion relating to the nature, quality, value or utility" of real estate and
45:14F-21(c) reserves it to licensees; the Appraiser Board treats a BPO as an appraisal. So a packet holds recorded
facts and arithmetic only: the subject's block / lot, class, assessment and taxes; each listed sale's date, price,
living area, $/sqft and distance; the Director's ratio and common level range; "subject assessment / this sale
price" per sale; and the statute's own formula with V left open. It never states an estimated, indicated or median
value, never says "over-assessed", never shows a tax-savings figure and never ranks sales as "best". The disclaimer
sits at the top in body type. No owner names; every subject and listed-sale address passes the Daniel's Law / stop
list. Attorneys may pay a flat price for data, never per client (NJ RPC 7.2(c); ACPE Joint Opinion 741).

Which properties get a packet is our own screen (not printed, not pitched): time-adjusted sales of similar houses
(same unit count; recorded sales in the same town assessed within 80%-125% of the subject, or MLS sales within
77%-130% of its living area), the higher of the median-price and median-$/sqft indications, and a property is kept
only when assessment / that figure is above the upper limit AND the Chapter 123 reduction would be at least 15% of
the assessment. Class 4A/4C parcels need commercial sales (the stored sets are house sales): counted and skipped.

The pitch is the 2-step funnel (2026-11-10): Touch 1 is two plain sentences with no attachment and no price (what we
ran, how many homes in one town, one public-record street, "Want the comparable-sales packet for it?"); that
property's one-page packet waits as the held sample with the flat price and goes out in the attorney's own thread
when they say yes. A one-property packet is itself the sample (its sales are the evidence: nothing is masked). The
second cold touch is one sentence in the first one's thread.
"""
from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from db.models import FirmOutbox, FirmTarget, Property, Valuation
from engine.streams.base import (
    PRIORITY_SPECIALTY,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    address_ok,
    money,
    operator_footer,
    render_table_pdf,
    slug,
)

KEY = "s6"
CLR_BAND = 0.15                      # N.J.S.A. 54:1-35a: common level range = average ratio +/- 15%
RATIOS_PATH = Path(__file__).resolve().parents[2] / "config" / "streams" / "nj_chapter123_ratios.yaml"
CLASSES = ("2", "4A", "4C")
COMMERCIAL = ("4A", "4C")
TARGET_KIND = "property_tax_appeal_attorney"
JANUARY_COUNTIES = ("burlington", "gloucester", "monmouth")   # assessment demonstration program: January 15 appeals
DISCLAIMER = ("Public-record data; not an appraisal, BPO or CMA; no opinion of value; not prepared by a NJ licensed "
              "or certified appraiser. For review by a licensed appraiser or attorney.")
ESTIMATED_LABEL = "ratio estimated from recorded sales (VERIFY against the Director's table)"
DEFAULTS: dict[str, Any] = {
    "min_taxes": 12000, "packets_per_run": 15, "pitches_per_run": 3, "price_per_packet": 100, "price_monthly": 500,
    "max_candidates": 4000, "min_comps": 3, "max_comps": 10, "comp_months": 30, "annual_appreciation": 0.05,
    "min_gap_pct": 0.15, "min_estimate_sales": 5, "assess_band": 1.25, "sqft_band": 1.3, "max_implied_multiple": 2.0,
}


def _lim(ctx: StreamContext, key: str) -> Any:
    v = (ctx.limits or {}).get(key)
    return DEFAULTS[key] if v is None else v


# -- the Director's ratios ----------------------------------------------------------------------------------------
@lru_cache(maxsize=4)
def load_ratio_table(path: str | None = None) -> dict[str, Any]:
    """The Chapter 123 table (empty when the file is missing: the screen then falls back to the estimated ratio)."""
    import yaml
    p = Path(path) if path else RATIOS_PATH
    if not p.exists():
        return {"source": {}, "ratios": {}}
    data = yaml.safe_load(p.read_text()) or {}
    return {"source": dict(data.get("source") or {}),
            "ratios": {str(k).zfill(4): v for k, v in (data.get("ratios") or {}).items()}}


@dataclass(frozen=True)
class Ratio:
    """A municipality's average ratio as a fraction (capped at 1.0: the certification's own rule)."""
    avg: float
    printed: float                   # as printed / estimated, in percent (may exceed 100)
    official: bool
    basis: str                       # one line for the packet
    tax_year: int | None = None
    source_url: str | None = None

    @property
    def lower(self) -> float:
        return self.avg * (1 - CLR_BAND)

    @property
    def upper(self) -> float:
        return self.avg * (1 + CLR_BAND)


def official_ratio(mun_code: str | None, table: dict[str, Any]) -> Ratio | None:
    row = (table.get("ratios") or {}).get(str(mun_code or "").zfill(4)) if mun_code else None
    if not row or not row.get("avg"):
        return None
    src = table.get("source") or {}
    pct = float(row["avg"])
    year = int(src["tax_year"]) if src.get("tax_year") else None
    basis = (f"NJ Division of Taxation, Chapter 123 average ratio for {row.get('name') or mun_code}, tax year "
             f"{year or '?'} (promulgated {src.get('promulgated') or '?'}; {src.get('status') or 'as certified'})")
    return Ratio(avg=min(pct, 100.0) / 100.0, printed=pct, official=True, basis=basis, tax_year=year,
                 source_url=src.get("url"))


def estimated_ratio(comps: list[Comp], municipality: str | None, min_sales: int = 5) -> Ratio | None:
    """Fallback for a town missing from the table: median of each same-town recorded sale's own assessment / price
    (each town assesses on its own base, so only same-town sales count). Always printed with VERIFY."""
    town = _town_key(municipality)
    xs = [c.assessed / c.sold_price for c in comps
          if c.assessed and c.sold_price and town and _town_key(c.town) == town]
    if len(xs) < min_sales:
        return None
    pct = round(median(xs) * 100, 2)
    return Ratio(avg=min(pct, 100.0) / 100.0, printed=pct, official=False,
                 basis=f"{ESTIMATED_LABEL}: median assessment / sale price of {len(xs)} same-town recorded sales")


def _town_key(name: str | None) -> str:
    from core.text import muni_base
    return re.sub(r"[^A-Z]", "", muni_base(name or "").upper())


# -- listed sales -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Comp:
    address: str
    town: str
    sold_price: float
    sold_date: date | None
    adj_price: float                 # screen only: time-adjusted to the assessing date (never printed)
    sqft: float | None
    distance_mi: float | None
    units: int | None
    kind: str | None                 # MLS property type, when the sale came from listing data
    assessed: float | None           # the sale property's own assessment (screen only: not evidence of value)
    source: str

    @property
    def ppsf(self) -> float | None:
        return self.sold_price / self.sqft if self.sqft else None

    @property
    def adj_ppsf(self) -> float | None:
        return self.adj_price / self.sqft if self.sqft else None


def _d(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def months_before(d: date, n: int) -> date:
    """The first day of the month `n` months before `d`."""
    y, m = divmod(d.year * 12 + d.month - 1 - int(n), 12)
    return date(y, m + 1, 1)


def time_adjust(price: float, sold: date | None, valuation_date: date, annual_rate: float) -> float:
    """Screen only: price x (1 + rate)^(years from the sale to the assessing date); a later sale stays as sold.
    Adjusting older sales upward makes the screen stricter (a higher figure, a lower ratio)."""
    if not sold or sold >= valuation_date:
        return float(price)
    return float(price) * (1 + annual_rate) ** ((valuation_date - sold).days / 365.25)


def parse_comps(comps_json: list[Any] | None, valuation_date: date, *, months: int = 30,
                annual_rate: float = 0.05) -> list[Comp]:
    """The usable sales of a stored comparable set: priced, dated on or after `months` before the assessing date,
    nearest first (distance is the only order: sales are never ranked)."""
    out: list[Comp] = []
    earliest = months_before(valuation_date, months)
    for c in comps_json or []:
        if not isinstance(c, dict):
            continue
        try:
            price = float(c.get("sold_price") or 0)
            sqft = float(c.get("sqft") or 0)
        except (TypeError, ValueError):
            continue
        sold = _d(c.get("sold_date"))
        if price < 40_000 or not c.get("address") or sold is None or sold < earliest:
            continue
        units = c.get("units")
        out.append(Comp(address=str(c["address"]), town=str(c.get("municipality") or ""), sold_price=price,
                        sold_date=sold, adj_price=round(time_adjust(price, sold, valuation_date, annual_rate), -2),
                        sqft=sqft if sqft > 200 else None,
                        distance_mi=float(c["distance_mi"]) if c.get("distance_mi") is not None else None,
                        units=int(units) if str(units or "").isdigit() else None,
                        kind=str(c.get("property_type") or "") or None,
                        assessed=float(c["assessed_total"]) if c.get("assessed_total") else None,
                        source=str(c.get("source") or "")))
    return sorted(out, key=lambda c: (c.distance_mi if c.distance_mi is not None else 99.0,
                                      -(c.sold_date or date.min).toordinal()))


def same_kind(comp: Comp, subject_units: int) -> bool:
    """A listed sale must have the subject's unit count: a 3-family is not compared with 1- and 2-family sales."""
    if comp.units is not None:
        return comp.units == subject_units
    kind = (comp.kind or "").lower()
    if subject_units == 1:
        return kind in ("single_family", "condos", "condo", "townhomes", "townhouse", "")
    return False                     # MLS sales carry no unit count: not used for a multi-family subject


def comparable(comp: Comp, *, assessed: float, sqft: float | None, town: str | None, assess_band: float = 1.25,
               sqft_band: float = 1.3) -> bool:
    """A fixed, printed selection rule (similar house, not just a nearby one): a recorded sale in the subject's
    municipality assessed within 1/band..band of the subject, or an MLS sale whose living area is within
    1/band..band of the subject's. Without it a big house is screened against its smaller neighbors' sales."""
    if comp.assessed:
        same_town = bool(town) and _town_key(comp.town) == _town_key(town)
        return same_town and assessed / assess_band <= comp.assessed <= assessed * assess_band
    if comp.sqft and sqft:
        return sqft / sqft_band <= comp.sqft <= sqft * sqft_band
    return False


# -- the screen (internal: decides which properties get a packet; never printed or pitched) ----------------------
def screen_figure(comps: list[Comp], subject_sqft: float | None) -> float | None:
    """The higher of median time-adjusted price and median time-adjusted $/sqft x living area (3+ areas), so a
    property passes the screen only when even the higher figure leaves its assessment above the range."""
    if not comps:
        return None
    a = float(median(c.adj_price for c in comps))
    areas = [c.adj_ppsf for c in comps if c.adj_ppsf]
    b = float(median(areas)) * subject_sqft if len(areas) >= 3 and subject_sqft else None
    return max(a, b or 0.0)


@dataclass(frozen=True)
class Screen:
    assessed: float
    taxes: float
    figure: float
    avg: float
    upper: float
    implied: float                   # assessed / figure
    revised: float                   # figure x average ratio (N.J.S.A. 54:51A-6(a))
    gap: float                       # assessed - revised
    gap_pct: float
    tax_diff: float                  # gap x taxes / assessed: orders the packets, never printed
    passed: bool


def chapter123_screen(assessed: float, taxes: float, figure: float, avg: float,
                      min_gap_pct: float = 0.15) -> Screen:
    """Strong cases only: implied ratio above the upper limit AND a reduction of at least `min_gap_pct`."""
    upper = avg * (1 + CLR_BAND)
    implied = assessed / figure
    revised = figure * avg
    gap = assessed - revised
    gap_pct = gap / assessed if assessed else 0.0
    rate = taxes / assessed if assessed else 0.0
    return Screen(assessed=assessed, taxes=taxes, figure=figure, avg=avg, upper=upper, implied=implied,
                  revised=revised, gap=gap, gap_pct=gap_pct, tax_diff=max(gap, 0.0) * rate,
                  passed=implied > upper and gap_pct >= min_gap_pct)


# -- deadlines ----------------------------------------------------------------------------------------------------
def next_deadline(today: date, county: str | None = None) -> date:
    """The next county-board appeal deadline (N.J.S.A. 54:3-21: April 1; January 15 in the assessment demonstration
    counties). Its year is the tax year under appeal."""
    month, day = (1, 15) if _county_key(county) in JANUARY_COUNTIES else (4, 1)
    this = date(today.year, month, day)
    return this if today <= this else date(today.year + 1, month, day)


def deadline_line(deadline: date, county: str | None = None) -> str:
    y = deadline.year
    if _county_key(county) in JANUARY_COUNTIES:
        first = (f"Filing deadline for tax year {y} at the {(county or '').title()} County Board of Taxation: "
                 f"January 15, {y} (assessment demonstration program; confirm with the board).")
    else:
        first = (f"Filing deadline for tax year {y}: April 1, {y}, or 45 days after the assessment notices are mailed "
                 f"if later; May 1, {y} in a municipality implementing a district-wide revaluation or reassessment "
                 "(N.J.S.A. 54:3-21). Burlington, Gloucester and Monmouth County boards use January 15.")
    return first + " An assessment over $1,000,000 may be filed directly with the Tax Court of New Jersey."


def _county_key(text: str | None) -> str:
    return re.sub(r"\s+county$", "", (text or "").strip().lower())


# -- cases --------------------------------------------------------------------------------------------------------
@dataclass
class Case:
    prop_id: int
    county: str
    mun_code: str
    town: str
    block: str
    lot: str
    qual: str
    street: str
    city: str
    zip_code: str
    cls: str
    building: str
    units: int
    sqft: float | None
    assessed: float
    taxes: float
    comps: list[Comp]
    ratio: Ratio
    screen: Screen
    deadline: date
    valuation_date: date
    earliest_sale: date
    note: list[str] = field(default_factory=list)


def evaluate(prop: Property, val: Valuation, table: dict[str, Any], *, today: date, params: dict[str, Any],
             comp_ok: Any = None) -> tuple[Case | None, str]:
    """One property -> a Case, or None with the reason (a metric name). Pure apart from `comp_ok` (suppression)."""
    deadline = next_deadline(today, prop.county)
    valuation_date = date(deadline.year - 1, 10, 1)            # the assessing date for that tax year
    cls = (prop.property_class or "").upper()
    if cls in COMMERCIAL:
        return None, "class4_needs_commercial_comps"
    guard = (val.assumptions_json or {}).get("outlier_guard") or {}
    if isinstance(guard, dict) and guard.get("non_residential"):
        return None, "non_residential"
    assessed, taxes = float(prop.assessed_total or 0), float(prop.annual_taxes or 0)
    if assessed <= 0 or taxes <= 0:
        return None, "no_assessment"
    units = int(prop.dwellings or 1)
    sqft = float(prop.sqft) if prop.sqft and prop.sqft > 200 else None
    months = int(params["comp_months"])
    comps = parse_comps(val.comps_json, valuation_date, months=months, annual_rate=float(params["annual_appreciation"]))
    comps = [c for c in comps if same_kind(c, units) and comparable(
        c, assessed=assessed, sqft=sqft, town=prop.municipality, assess_band=float(params["assess_band"]),
        sqft_band=float(params["sqft_band"]))]
    if comp_ok is not None:
        comps = [c for c in comps if comp_ok(c)]
    if len(comps) < int(params["min_comps"]):
        return None, "too_few_comps"
    ratio = official_ratio(prop.mun_code, table) or estimated_ratio(comps, prop.municipality,
                                                                     int(params["min_estimate_sales"]))
    if ratio is None:
        return None, "no_ratio"
    shown = comps[: int(params["max_comps"])]                  # the screen uses exactly the sales the packet lists
    figure = screen_figure(shown, sqft)
    if not figure:
        return None, "no_figure"
    s = chapter123_screen(assessed, taxes, figure, ratio.avg, float(params["min_gap_pct"]))
    if not s.passed:
        return None, "within_range"
    if s.implied > float(params["max_implied_multiple"]) * s.upper:
        return None, "implausible_ratio"                     # a data problem (class, parcel, units), not a case
    note = []
    if ratio.official and ratio.tax_year and ratio.tax_year != deadline.year:
        note.append(f"VERIFY: the ratio table on file is for tax year {ratio.tax_year}; confirm the tax-year "
                    f"{deadline.year} ratio the Director promulgates on October 1.")
    return Case(prop_id=prop.id, county=prop.county or "", mun_code=prop.mun_code or "",
                town=prop.municipality or prop.city or "", block=prop.block or "", lot=prop.lot or "",
                qual=prop.qual or "", street=prop.street_address or "", city=prop.city or prop.municipality or "",
                zip_code=prop.zip_code or "", cls=cls, building=prop.building_desc or "", units=units, sqft=sqft,
                assessed=assessed, taxes=taxes, comps=shown, ratio=ratio, screen=s, deadline=deadline,
                valuation_date=valuation_date, earliest_sale=months_before(valuation_date, months), note=note), "kept"


def _latest_current(session: Session, params: dict[str, Any]) -> list[tuple[Property, Valuation]]:
    rows = session.execute(
        select(Property, Valuation)
        .join(Valuation, and_(Valuation.property_id == Property.id, Valuation.is_current.is_(True)))
        .where(Property.annual_taxes > float(params["min_taxes"]), Property.property_class.in_(CLASSES),
               Property.assessed_total > 0)
        .order_by(Property.annual_taxes.desc(), Valuation.calculated_at.desc())
        .limit(int(params["max_candidates"]) * 2)).all()
    seen: dict[int, tuple[Property, Valuation]] = {}
    for p, v in rows:
        seen.setdefault(p.id, (p, v))
    return list(seen.values())[: int(params["max_candidates"])]


def city_variants(*names: str | None) -> list[str]:
    """The spellings a suppression request may use for one town ("WEST ORANGE TWP", "West Orange")."""
    from core.text import muni_base
    out: list[str] = []
    for n in names:
        for v in (n, muni_base(n)):
            v = re.sub(r"\s*\d{5}(?:-\d{4})?\s*$", "", (v or "").strip())
            if v and v.upper() not in (x.upper() for x in out):
                out.append(v)
    return out or [""]


def place_ok(session: Session, place: Any) -> bool:
    """address_ok under every spelling of the town a suppression request may have used ("ELIZABETH CITY",
    "Elizabeth"): a Daniel's Law request must win however the town was written."""
    for city in city_variants(getattr(place, "city", None), getattr(place, "municipality", None)):
        probe = SimpleNamespace(street_address=getattr(place, "street_address", None), city=city,
                                zip_code=getattr(place, "zip_code", None) or "",
                                owner_mailing_address=getattr(place, "owner_mailing_address", None),
                                owner_city_state_zip=getattr(place, "owner_city_state_zip", None) or "")
        if not address_ok(session, probe):
            return False
    return True


def _comp_suppressed(session: Session, c: Comp) -> bool:
    street, _, rest = c.address.partition(",")
    return any(not address_ok(session, SimpleNamespace(street_address=street.strip(), city=city, zip_code=""))
               for city in city_variants(c.town, rest))


def find_cases(session: Session, today: date, params: dict[str, Any], table: dict[str, Any] | None = None
               ) -> tuple[list[Case], dict[str, int]]:
    """Every property that passes the screen and the suppression list, in screen order (largest first)."""
    table = table if table is not None else load_ratio_table(params.get("ratios_path"))
    where = (Property.annual_taxes > float(params["min_taxes"]), Property.property_class.in_(CLASSES),
             Property.assessed_total > 0)
    metrics: dict[str, int] = {"candidates": int(session.scalar(select(func.count(Property.id)).where(*where)) or 0),
                               "kept": 0, "suppressed": 0, "ratio_official": 0, "ratio_estimated": 0}
    pairs = _latest_current(session, params)
    metrics["no_valuation"] = max(metrics["candidates"] - len(pairs), 0)
    first: list[tuple[Case, Property, Valuation]] = []
    for p, v in pairs:
        case, why = evaluate(p, v, table, today=today, params=params)
        if case is None:
            metrics[why] = metrics.get(why, 0) + 1
        else:
            first.append((case, p, v))
    first.sort(key=lambda t: -t[0].screen.tax_diff)
    out: list[Case] = []
    for case, p, v in first:
        if not place_ok(session, p):
            metrics["suppressed"] += 1
            continue
        # again without suppressed sale addresses: the case must stand on exactly what the packet lists
        again, _ = evaluate(p, v, table, today=today, params=params,
                            comp_ok=lambda c: not _comp_suppressed(session, c))
        if again is None:
            metrics["suppressed"] += 1
            continue
        out.append(again)
        metrics["ratio_official" if again.ratio.official else "ratio_estimated"] += 1
    metrics["kept"] = len(out)
    return out, metrics


# -- the packet ---------------------------------------------------------------------------------------------------
def pdf_pages(path: Path) -> int:
    """Pages in a reportlab PDF (its page objects are written uncompressed), without a PDF-reading dependency."""
    return len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", Path(path).read_bytes()))


def _e(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


def clip(text: str | None, n: int) -> str:
    """One table line per sale: a long address is cut (block / lot and the deed are one search away)."""
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 3].rstrip(" ,.-") + "..."


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _source_label(src: str) -> str:
    if "public" in src or "njgin" in src:
        return "Recorded deed (NJ MOD-IV)"
    if "listing" in src or "mls" in src:
        return "MLS sold (listing data)"
    return "Recorded / listed sale"


def packet_rows(case: Case) -> list[list[str]]:
    """Facts per listed sale plus one division: subject assessment / this sale price."""
    from core.text import muni_base, postal_title
    rows = []
    for i, c in enumerate(case.comps, 1):
        street = postal_title(c.address.split(",")[0])
        raw_town = c.town or (c.address.split(",")[1] if "," in c.address else "")
        town = postal_title(muni_base(raw_town) if c.assessed else raw_town)   # MOD-IV names carry "TWP" / "CITY"
        rows.append([str(i), _e(clip(f"{street}, {town}" if town else street, 40)),
                     c.sold_date.strftime("%m/%d/%Y") if c.sold_date else "-", money(c.sold_price),
                     f"{c.sqft:,.0f}" if c.sqft else "-", f"${c.ppsf:,.0f}" if c.ppsf else "-",
                     f"{c.distance_mi:.2f} mi" if c.distance_mi is not None else "-", _source_label(c.source),
                     f"{money(case.assessed)} / {money(c.sold_price)} = {_pct(case.assessed / c.sold_price)}"])
    return rows


def packet_notes(case: Case, params: dict[str, Any]) -> list[str]:
    r = case.ratio
    sq = float(params["sqft_band"])
    return [
        f"<b>Sales listed</b> (a fixed rule, no adjustments): sales in our stored public-record and MLS sale sets near "
        f"the subject, sold on or after {case.earliest_sale:%m/%d/%Y}, in the same municipality with the subject's "
        f"unit count ({case.units}) and a similar size (MLS living area within {100 / sq:.0f}%-{100 * sq:.0f}% of the "
        f"subject's where recorded); nearest first, up to {int(params['max_comps'])}. Sale prices are the evidence; "
        "other properties' assessments are not (Treasury, Property Tax Appeal guide). Last column = the subject's "
        "assessment divided by that sale's price.",
        f"<b>Director's average ratio</b> R = {r.printed:.2f}%" + (" (ratios over 100% are taken as 100%)"
                                                                  if r.printed > 100 else "")
        + f"; common level range = 0.85 x R to 1.15 x R = {_pct(r.lower)} to {_pct(r.upper)} (N.J.S.A. 54:1-35a). "
        f"Chapter 123 upper limit 1.15 x R = <b>{_pct(r.upper)}</b>.",
        "<b>Chapter 123 (N.J.S.A. 54:51A-6):</b> when assessment / true value is above the upper limit, the taxable "
        "value is set at true value x R; below the lower limit it is raised the same way; within the range it is "
        f"unchanged. If the true value were V, the Chapter 123 assessment would be R x V = {r.printed:.2f}% x V "
        "(true value is for the board or the Tax Court to find).",
        f"Ratio source: {_e(r.basis)}" + (f", {_e(r.source_url)}" if r.source_url else "") + ".",
        "Chapter 123 does not apply in a tax year in which a district-wide revaluation or reassessment takes effect "
        f"(N.J.S.A. 54:51A-6(d)). The assessment shown is the current MOD-IV figure: confirm the tax-year "
        f"{case.deadline.year} assessment on the notice mailed by February 1. Deed book / page: see the county clerk.",
    ] + case.note


def render_packet(case: Case, path: Path, op: dict[str, str], params: dict[str, Any]) -> Path:
    from core.text import postal_title
    title = (f"Comparable Sales Data Packet: Block {_e(case.block)} Lot {_e(case.lot)}"
             + (f" Qual {_e(case.qual)}" if case.qual else "") + f", {_e(postal_title(case.town))}")
    units = f"{case.units}-family" if case.units > 1 else "1-family"
    subtitle = (f"<b>{DISCLAIMER}</b><br/>Subject: {_e(postal_title(case.street))}, {_e(postal_title(case.city))} NJ "
                f"{_e(case.zip_code)} · {_e(case.county)} County (municipal code {_e(case.mun_code)}) · class "
                f"{_e(case.cls)} ({units}{', ' + _e(case.building) if case.building else ''}) · assessment "
                f"{money(case.assessed)} · annual taxes {money(case.taxes)} · living area "
                f"{f'{case.sqft:,.0f} sqft' if case.sqft else 'not on record'}")
    head = ["#", "Sale (recorded deed or MLS sold)", "Sold", "Sale price", "Living area", "$/sqft", "Distance", "Source",
            "Subject assessment / sale price"]
    box = f"<b>{DISCLAIMER}</b> {deadline_line(case.deadline, case.county)}"
    # one property per packet: the packet is its own sample (the listed sales are the evidence, nothing to mask)
    return render_table_pdf(path, title=title, subtitle=subtitle, head=head, rows=packet_rows(case),
                            notes=packet_notes(case, params), box=box, footer=operator_footer(op),
                            col_widths=[0.3, 2.1, 0.75, 0.85, 0.75, 0.65, 0.7, 1.5, 2.45], write_sample=False)


def with_comps(case: Case, comps: list[Comp], params: dict[str, Any]) -> Case | None:
    """The same case listing fewer sales, re-screened on exactly those (None when it no longer passes)."""
    if len(comps) < int(params["min_comps"]):
        return None
    figure = screen_figure(comps, case.sqft)
    if not figure:
        return None
    s = chapter123_screen(case.assessed, case.taxes, figure, case.ratio.avg, float(params["min_gap_pct"]))
    if not s.passed or s.implied > float(params["max_implied_multiple"]) * s.upper:
        return None
    return replace(case, comps=comps, screen=s)


def fit_one_page(case: Case, path: Path, op: dict[str, str], params: dict[str, Any]) -> tuple[Case, Path] | None:
    """A packet is one page: list one sale fewer (and re-screen) until it fits, or produce no packet."""
    current: Case | None = case
    while current is not None:
        render_packet(current, path, op, params)
        if pdf_pages(path) <= 1:
            return current, path
        current = with_comps(current, current.comps[:-1], params)
    path.unlink(missing_ok=True)
    return None


# -- the pitch: the 2-step funnel (2026-11-10) -------------------------------------------------------------------
def taxes_floor(min_taxes: float) -> str:
    """The tax screen in words ("12,000"): rounded down to the thousand, so "paying over X" stays true."""
    v = float(min_taxes or 0)
    return f"{math.floor(v / 1000) * 1000 if v >= 1000 else v:,.0f}"


def town_label(case: Case) -> str:
    """The municipality as people write it ("NEWARK CITY" -> "Newark"): a Chapter 123 ratio is per municipality."""
    from core.text import muni_base, postal_title
    return postal_title(muni_base(case.town)) if case.town else postal_title(case.city)


def street_label(case: Case) -> str:
    from core.text import postal_title
    return postal_title(case.street).strip(" .,")


def pitch_subject(town: str, street: str, n: int) -> str:
    return (f"Chapter 123 ratios: {street} + {n - 1} more in {town}" if n > 1
            else f"Chapter 123 ratios: {street}, {town}")


def pitch_body(town: str, street: str, n: int, min_taxes: float, first: str, *, comps: int = 0,
               ratio: Ratio | None = None, deadline: date | None = None) -> str:
    """Touch 1: two plain sentences of public-record facts only (the playbook's tax-appeal opener, 2026-11-10): the
    street, how many similar recorded sales the packet holds, the town's Chapter 123 ratio, and a yes/no question with
    the filing deadline. No screen result, no "above the range", no value, no "over-assessed", no savings, no "$"."""
    sales = f"{comps} similar recorded sales" if comps > 1 else "similar recorded sales"
    more = "" if n <= 1 else f", one of {n} homes there paying over {taxes_floor(min_taxes)} dollars a year in taxes,"
    if ratio is not None:
        r = f"{ratio.printed:.2f}%".replace(".00%", "%")
        fact = (f"{town}'s {'official' if ratio.official else 'estimated'} Chapter 123 ratio is {r}")
    else:
        fact = f"{town}'s Chapter 123 ratio is in the packet"
    when = f" before the {deadline:%B} {deadline.day} deadline" if deadline else ""
    return (f"Hello,\n\n{street} in {town}{more} has {sales} nearby on record, and {fact}. Want the one-page "
            f"comparable-sales packet for it{when}?\n\n{first}").strip()


def followup_body(street: str, first: str) -> str:
    """The second cold touch: one sentence, no attachment, in the first touch's thread."""
    return f"Hello,\n\nShould I send the comparable-sales packet for {street} over?\n\n{first}".strip()


def sample_body(street: str, deadline: date, price_packet: float, price_monthly: float, official: bool = True) -> str:
    """Touch 2, sent with the packet only after their yes: what is attached, the flat price (never contingent, never
    per client: NJ RPC 7.2(c)) and the filing deadline."""
    ratio = ("the Director's Chapter 123 ratio" if official
             else "an estimated Chapter 123 ratio to check against the Director's table")
    return (f"Here's the comparable-sales packet for {street} (attached): the recorded sales, {ratio} and the "
            f"arithmetic, with no opinion of value. It is ${price_packet:,.0f} a packet or ${price_monthly:,.0f} a "
            "month for a weekly set, plus NJ sales tax, flat research fees, no contingency and nothing tied to any appeal result; the "
            f"{deadline:%B} {deadline.day}, {deadline.year} filing deadline is the clock. "
            "Reply \"start\" and I'll set it up.")


def thread_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def earlier_pitch(session: Session, target_id: int, stream: str) -> FirmOutbox | None:
    """The newest cold touch this stream sent the target: the second touch goes in its thread."""
    return session.scalar(select(FirmOutbox).where(
        FirmOutbox.target_id == target_id, FirmOutbox.stream == stream, FirmOutbox.kind.in_(("pitch", "followup")),
        FirmOutbox.status == "sent", FirmOutbox.message_id.is_not(None))
        .order_by(FirmOutbox.sent_at.desc(), FirmOutbox.id.desc()).limit(1))


def pitch_due(session: Session, t: FirmTarget, key: str, now: datetime) -> bool:
    """The outbox's own gates, checked here first so a target it would skip never takes one of the run's pitch slots
    (a re-run in the same week, cold touches used up, pitched inside the gap, a pitch still queued)."""
    from engine.firm_outbox import cfg
    c = cfg()
    if session.scalar(select(FirmOutbox.id).where(FirmOutbox.key == key)):
        return False
    from engine.firm_outbox import pitch_ok
    if not pitch_ok(t, now):                      # Touch 1 only: a target in a sequence gets the outbox's nudges
        return False
    if t.last_contact_at and t.last_contact_at > now - timedelta(days=float(c.get("pitch_gap_days", 6))):
        return False
    return not session.scalar(select(FirmOutbox.id).where(FirmOutbox.target_id == t.id,
                                                         FirmOutbox.status == "queued").limit(1))


def names_street(text: str | None, street: str) -> bool:
    """`street` appears in `text` as a whole address ("1 Main St" is not in "11 Main St")."""
    return bool(street) and re.search(rf"(?<![\w-]){re.escape(street)}(?!\w)", text or "") is not None


def pitches(session: Session, ctx: StreamContext, packets: list[tuple[Case, Path]],
            cases: list[Case] | None = None) -> list[Outbound]:
    """One cold touch per due attorney whose county has a packet this week. Touch 1 names that county's top property
    (no attachment); its packet is the held sample. A target touched before gets the one-sentence follow-up in that
    thread. Both are kind "pitch", so the outbox's gap and max-touches rules hold for each. One key per target per
    ISO week."""
    if not ctx.send or not packets:
        return []
    by_county: dict[str, list[tuple[Case, Path]]] = {}
    for case, path in packets:                                   # packets arrive in screen order
        by_county.setdefault(_county_key(case.county), []).append((case, path))
    per_town: dict[tuple[str, str], int] = {}
    for c in cases or [case for case, _ in packets]:
        k = (_county_key(c.county), c.mun_code or _town_key(c.town))
        per_town[k] = per_town.get(k, 0) + 1
    iso = ctx.now.isocalendar()
    first = (ctx.operator or {}).get("first", "")
    out: list[Outbound] = []
    targets = session.scalars(select(FirmTarget).where(
        FirmTarget.kind == TARGET_KIND, FirmTarget.stage.in_(("NEW", "CONTACTED")), FirmTarget.email.is_not(None))
        .order_by(FirmTarget.score.desc(), FirmTarget.id)).all()
    for t in targets:
        if len(out) >= int(_lim(ctx, "pitches_per_run")):
            break
        mine = by_county.get(_county_key(t.county))
        key = f"{KEY}:pitch:{t.id}:{iso[0]}-W{iso[1]:02d}"
        if not mine or not pitch_due(session, t, key, ctx.now):
            continue
        earlier = None    # Touch 1 only: the outbox sends the in-thread nudges (firm_outbox.queue_nudges)
        if earlier is not None:
            # the follow-up names the packet the first touch named while it is still in this week's set
            case, path = next(((c, p) for c, p in mine if names_street(earlier.body, street_label(c))), mine[0])
            subject, body = thread_subject(earlier.subject), followup_body(street_label(case), first)
        else:
            case, path = mine[0]
            n = max(per_town.get((_county_key(case.county), case.mun_code or _town_key(case.town)), 0), 1)
            subject = pitch_subject(town_label(case), street_label(case), n)
            body = pitch_body(town_label(case), street_label(case), n, float(_lim(ctx, "min_taxes")), first,
                              comps=len(case.comps), ratio=case.ratio, deadline=case.deadline)
        out.append(Outbound(
            stream=KEY, key=key, to=t.email or "", subject=subject, body=body, priority=PRIORITY_SPECIALTY,
            kind="pitch", target_id=t.id, in_reply_to=earlier.message_id if earlier is not None else None,
            sample_body=sample_body(street_label(case), case.deadline, float(_lim(ctx, "price_per_packet")),
                                    float(_lim(ctx, "price_monthly")), case.ratio.official),
            sample_attachments=[path]))
    return out


# -- the stream ---------------------------------------------------------------------------------------------------
class TaxAppealStream(Stream):
    key = KEY
    name = "Tax-appeal evidence"
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        res = StreamResult(stream=self.key)
        params = {k: _lim(ctx, k) for k in DEFAULTS}
        params["ratios_path"] = (ctx.limits or {}).get("ratios_path")
        today = ctx.now.date() if isinstance(ctx.now, datetime) else date.today()
        cases, metrics = find_cases(session, today, params)
        res.processed, res.found = metrics["candidates"], len(cases)
        packets: list[tuple[Case, Path]] = []
        for case in cases:
            if len(packets) >= int(params["packets_per_run"]):
                break
            name = f"{KEY}_{slug(case.county, 12)}_{case.mun_code}_B{slug(case.block, 12)}_L{slug(case.lot, 12)}.pdf"
            fitted = fit_one_page(case, ctx.out_dir / name, ctx.operator or {}, params)
            if fitted is not None:
                packets.append(fitted)
        res.products = len(packets)
        res.outbound = pitches(session, ctx, packets, cases)
        res.metrics = {**metrics, "packets": len(packets), "pitches": len(res.outbound)}
        res.notes.append(f"{len(cases)} high-tax properties passed the Chapter 123 screen; {len(packets)} packets")
        if metrics.get("ratio_estimated"):
            res.notes.append(f"{metrics['ratio_estimated']} packets use an estimated ratio (VERIFY)")
        return res


STREAM = TaxAppealStream()
