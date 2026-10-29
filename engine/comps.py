"""Comparable-sales providers and the valuation math that turns comps into ARV / as-is value.

Two methods:
  * Listing comps (homeharvest / Realtor.com sold data): sqft is known, so value = $/sqft
    percentile x subject sqft. Renovated comps = top 30% $/sqft (ARV), distressed = bottom 30%
    (as-is).
  * Public-record comps (NJGIN MOD-IV recorded sales): no living area, so each comp's price is
    normalized by its *equalized* assessment (assessed x its town's sale/assessment ratio). The
    subject's equalized assessment x the 70th / 30th percentile normalized ratio gives ARV /
    as-is. Older sales are time-adjusted to today.
"""
from __future__ import annotations

import logging
import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from agents.scrapers.njgis_client import NJGISClient
from core.dates import parse_modiv_date
from core.geo import haversine_miles
from core.text import clean
from engine.market import muni_stats

log = logging.getLogger(__name__)


@dataclass
class Comp:
    address: str
    municipality: str | None
    sold_price: float
    sold_date: date | None
    distance_mi: float | None
    source: str
    sqft: int | None = None
    beds: float | None = None
    baths: float | None = None
    year_built: int | None = None
    units: int | None = None
    property_type: str | None = None
    lat: float | None = None
    lon: float | None = None
    assessed_total: float | None = None
    time_adjusted_price: float | None = None
    normalized_ratio: float | None = None     # public-record method
    price_per_sqft: float | None = None       # listing method
    bucket: str | None = None                 # renovated / middle / distressed
    url: str | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["sold_date"] = self.sold_date.isoformat() if self.sold_date else None
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items() if v is not None}


@dataclass
class ValueResult:
    arv: float | None
    as_is: float | None
    method: str
    comps: list[Comp] = field(default_factory=list)
    renovated_count: int = 0
    distressed_count: int = 0
    dispersion: float | None = None           # IQR / median of the valuation metric
    radius_used: float | None = None
    notes: list[str] = field(default_factory=list)
    subject_equalized: float | None = None    # assessed x town sale/assessment ratio (public-record method)


@dataclass
class CompHygiene:
    """Residential comp filters (NJ class 2). Package deeds, non-arm's-length prices and $/sqft or
    sale-to-assessment extremes never reach the ARV percentile."""
    min_price: float = 40_000
    max_price: float = 2_500_000
    high_value_subject: float = 1_200_000     # above this (assessed equalized value or ask) the price cap is lifted
    ppsf_min: float = 60
    ppsf_max: float = 850
    ratio_min: float = 0.5                    # comp sale / its equalized assessment; above ~2x is
    ratio_max: float = 2.0                    # usually new construction on a teardown, not a fixer comp

    @classmethod
    def from_settings(cls) -> "CompHygiene":
        from core.config import settings
        g = dict(settings().deal.get("outlier_guard") or {})
        keys = {"comp_min_price": "min_price", "comp_max_price": "max_price", "high_value_subject": "high_value_subject",
                "comp_ppsf_min": "ppsf_min", "comp_ppsf_max": "ppsf_max", "comp_ratio_min": "ratio_min",
                "comp_ratio_max": "ratio_max"}
        return cls(**{attr: float(g[k]) for k, attr in keys.items() if k in g})

    def price_ok(self, price: float, subject_value: float | None = None) -> bool:
        cap = math.inf if subject_value and subject_value > self.high_value_subject else self.max_price
        return self.min_price <= price <= cap

    def ppsf_ok(self, price: float, sqft: float | None) -> bool:
        return not sqft or self.ppsf_min <= price / sqft <= self.ppsf_max

    def ratio_ok(self, ratio: float) -> bool:
        return self.ratio_min <= ratio <= self.ratio_max


# -- property-type matching (learned on the Day-1 pre-flight, 2026-10-29) ---------------------------------------
# A 1890 Jersey City house was being valued off condo units and 2023 new construction; a Long Branch townhouse
# off a Monmouth Beach single-family; a 720 sqft Freehold condo off detached houses 2-3 miles away.
MLS_CONDO = {"condos", "condo", "condop", "coop"}
MLS_TOWN = {"townhomes", "condo_townhome", "condo_townhome_rowhome_coop"}
DESC_CONDO = re.compile(r"COND|\bCO-?OP\b", re.I)
DESC_TOWN = re.compile(r"\bTH\b|TOWN\s?H|\bROW\b", re.I)
NEW_BUILD_YEARS = 4            # a comp built in the last 4 years is new construction ...
OLD_SUBJECT_YEARS = 30         # ... and is no renovated-ARV comp for a house older than 30 years


@dataclass
class SubjectKind:
    condo: bool = False
    townhouse: bool = False
    year_built: int | None = None
    units: int = 1

    @classmethod
    def of(cls, *, building_desc: str | None, qual: str | None = None, units: int | None = None,
           year_built: int | None = None, mls_type: str | None = None) -> "SubjectKind":
        desc, lt = building_desc or "", (mls_type or "").lower()
        condo = (bool(DESC_CONDO.search(desc)) or bool(TALL_BUILDING.match(desc)) or str(qual or "").upper().startswith("C")
                 or lt in MLS_CONDO)
        town = not condo and (bool(DESC_TOWN.search(desc)) or lt in MLS_TOWN)
        return cls(condo=condo, townhouse=town, year_built=year_built, units=max(int(units or 1), 1))

    @property
    def attached(self) -> bool:
        return self.condo or self.townhouse


TALL_BUILDING = re.compile(r"^\s*(?:[4-9]|\d{2})\s*S", re.I)   # '6SCB', '5S-14U-11P-COND': 4+ stories


def attached_addresses(comps: list[dict[str, Any]]) -> set[str]:
    """Comps that are units in one building or complex even when MOD-IV doesn't say CONDO: the same address sold
    more than once in the window, or 3+ sales on one street sharing an identical assessment (a townhouse row)."""
    by_addr: dict[str, set[str]] = {}
    by_street_assessed: dict[tuple[str, Any], set[str]] = {}
    for c in comps:
        addr = re.sub(r"[^A-Z0-9 ]", "", str(c.get("address") or "").upper()).strip()
        if not addr:
            continue
        by_addr.setdefault(addr, set()).add(str(c.get("sold_date")))
        street = re.sub(r"^\d+[A-Z]?(?:-\d+[A-Z]?)?\s+", "", addr)
        if c.get("assessed_total"):
            by_street_assessed.setdefault((street, c["assessed_total"]), set()).add(addr)
    out = {a for a, dates in by_addr.items() if len(dates) >= 2}
    for addrs in by_street_assessed.values():
        if len(addrs) >= 3:
            out |= addrs
    return out


def comp_kind(property_type: str | None, qual: str | None = None) -> tuple[bool, bool]:
    """(condo, townhouse) for a comp: MLS type names or MOD-IV building descriptions."""
    t = str(property_type or "")
    if TALL_BUILDING.match(t):
        return True, False
    lt = t.lower()
    if lt in MLS_CONDO:
        return True, False
    if lt in MLS_TOWN:
        return False, True
    if lt in ("single_family", "multi_family", "duplex_triplex", "apartment", "land", "mobile", "farm"):
        return False, False
    condo = bool(DESC_CONDO.search(t)) or str(qual or "").upper().startswith("C")
    return condo, (not condo and bool(DESC_TOWN.search(t)))


def kind_mismatch(subject: SubjectKind | None, property_type: str | None, year_built: int | None,
                  qual: str | None = None, today: date | None = None, attached: bool = False) -> str | None:
    """Why a comp doesn't fit the subject ('property type' / 'new construction'), or None."""
    if subject is None:
        return None
    condo, town = comp_kind(property_type, qual)
    town = town or (attached and not condo)
    if subject.attached != (condo or town):
        return "property type"                       # detached house vs condo / townhouse, either way
    year = (today or date.today()).year
    if (year_built and subject.year_built and year_built >= year - NEW_BUILD_YEARS
            and subject.year_built <= year - OLD_SUBJECT_YEARS):
        return "new construction"
    return None


def ppsf_outliers(values: list[float], high: float = 1.6, low: float = 0.6) -> tuple[float, float]:
    """Band around the median $/sqft for small comp sets, where an IQR can't see one luxury sale."""
    if len(values) < 3:
        return 0.0, math.inf
    med = statistics.median(values)
    return low * med, high * med


def package_deed_keys(features: list[dict]) -> set[tuple[Any, Any]]:
    """(price, deed date) pairs recorded on 2+ different parcels: one bulk deed (a building of condo units,
    a portfolio) whose total price is attached to every parcel. None of those rows is a usable comp."""
    parcels: dict[tuple[Any, Any], set[str]] = {}
    for f in features:
        a = f.get("attributes") or {}
        if a.get("SALE_PRICE") and a.get("DEED_DATE"):
            pin = a.get("PAMS_PIN") or f"{a.get('PCL_MUN')}-{a.get('PCLBLOCK')}-{a.get('PCLLOT')}-{a.get('PCLQCODE')}"
            parcels.setdefault((a["SALE_PRICE"], a["DEED_DATE"]), set()).add(str(pin))
    return {k for k, pins in parcels.items() if len(pins) >= 2}


SQFT_IN_DESC = re.compile(r"(\d{1,2},?\d{3})\s*(?:SF|SQ\.?\s*FT)\b", re.I)


def explicit_sqft(desc: str | None) -> int | None:
    """Living area only when MOD-IV states it ('1,884 SF.', '2775 SQ FT'); never an estimate."""
    m = SQFT_IN_DESC.search(desc or "")
    if not m:
        return None
    n = int(m.group(1).replace(",", ""))
    return n if 300 <= n <= 15000 else None


def percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def iqr_filter(values: list[float], k: float = 1.5) -> tuple[float, float]:
    if len(values) < 5:
        return (min(values), max(values)) if values else (0, 0)
    q1, q3 = percentile(values, 0.25), percentile(values, 0.75)
    return q1 - k * (q3 - q1), q3 + k * (q3 - q1)


def time_adjust(price: float, sold: date | None, annual_rate: float, today: date | None = None) -> float:
    if not sold:
        return price
    years = max(((today or date.today()) - sold).days / 365.25, 0)
    return price * (1 + annual_rate) ** years


def value_from_metric(comps: list[Comp], metric: str, subject_base: float, renovated_pct: float,
                      distressed_pct: float) -> tuple[float, float, int, int, float]:
    """Split comps by a metric into renovated (top) / distressed (bottom) groups and value the subject."""
    vals = [getattr(c, metric) for c in comps]
    hi_cut, lo_cut = percentile(vals, renovated_pct), percentile(vals, distressed_pct)
    renovated = [c for c in comps if getattr(c, metric) >= hi_cut]
    distressed = [c for c in comps if getattr(c, metric) <= lo_cut]
    for c in comps:
        c.bucket = "renovated" if c in renovated else "distressed" if c in distressed else "middle"
    arv = statistics.median([getattr(c, metric) for c in renovated]) * subject_base
    as_is = statistics.median([getattr(c, metric) for c in distressed]) * subject_base
    med = statistics.median(vals)
    dispersion = (percentile(vals, 0.75) - percentile(vals, 0.25)) / med if med else None
    return arv, as_is, len(renovated), len(distressed), dispersion


# -- public-record provider -----------------------------------------------------------------------
class PublicRecordComps:
    """NJGIN MOD-IV recorded sales near the subject."""
    source = "njgin_public_record"

    def __init__(self, gis: NJGISClient | None = None):
        self.gis = gis or NJGISClient()

    def value(self, *, lat: float, lon: float, assessed_total: float, mun_code: str, municipality: str,
              units: int | None, building_desc: str | None, radius: float, fallback_radius: float,
              months: int, appreciation: float, min_comps: int, renovated_pct: float,
              distressed_pct: float, value_hint: float | None = None,
              hygiene: CompHygiene | None = None, subject: SubjectKind | None = None) -> ValueResult:
        hygiene = hygiene or CompHygiene.from_settings()
        subject_stats = muni_stats(self.gis, mun_code, municipality)
        if not subject_stats.median_ratio:
            return ValueResult(None, None, "public_record", notes=["no sales data for subject town"])
        subject_equalized = assessed_total * subject_stats.median_ratio
        subject_units = max(int(units or 1), 1)
        high_value = max(subject_equalized, value_hint or 0)
        result: ValueResult | None = None
        for r in (radius, fallback_radius):
            features = self.gis.nearby_sales(lat, lon, r, months=months + 12)
            packages = package_deed_keys(features)
            dropped = {"package deed": 0, "price band": 0, "$/sqft band": 0, "sale/assessment band": 0,
                       "property type": 0, "new construction": 0}
            comps: list[Comp] = []
            for f in features:
                a = f["attributes"]
                price, assessed = a.get("SALE_PRICE") or 0, a.get("NET_VALUE") or 0
                if assessed < 20000:
                    continue
                if (price, a.get("DEED_DATE")) in packages:
                    dropped["package deed"] += 1
                    continue
                if not hygiene.price_ok(price, high_value):
                    dropped["price band"] += 1
                    continue
                if not hygiene.ppsf_ok(price, explicit_sqft(a.get("BLDG_DESC"))):
                    dropped["$/sqft band"] += 1
                    continue
                comp_units = max(int(a.get("DWELL") or 1), 1)
                if (subject_units == 1) != (comp_units == 1) or abs(comp_units - subject_units) > 1:
                    continue  # don't value a 1-family off 3-families or vice versa
                why = kind_mismatch(subject, a.get("BLDG_DESC"), a.get("YR_CONSTR") or None, a.get("PCLQCODE"))
                if why:
                    dropped[why] += 1
                    continue
                comp_stats = muni_stats(self.gis, a["PCL_MUN"], a.get("MUN_NAME") or "")
                if not comp_stats.median_ratio:
                    continue
                sold = parse_modiv_date(a.get("DEED_DATE"))
                adj = time_adjust(price, sold, appreciation)
                c = f.get("centroid")
                comps.append(Comp(
                    address=clean(a.get("PROP_LOC")), municipality=a.get("MUN_NAME"), sold_price=price,
                    sold_date=sold, distance_mi=round(haversine_miles(lat, lon, c[1], c[0]), 3) if c else None,
                    source=self.source, year_built=a.get("YR_CONSTR") or None, units=comp_units,
                    property_type=a.get("BLDG_DESC"), lat=c[1] if c else None, lon=c[0] if c else None,
                    assessed_total=assessed, time_adjusted_price=round(adj, -2),
                    normalized_ratio=adj / (assessed * comp_stats.median_ratio)))
            if subject is not None and not subject.attached:
                cluster = attached_addresses([{"address": c.address, "sold_date": c.sold_date,
                                               "assessed_total": c.assessed_total} for c in comps])
                kept = [c for c in comps if re.sub(r"[^A-Z0-9 ]", "", c.address.upper()).strip() not in cluster]
                dropped["property type"] += len(comps) - len(kept)
                comps = kept
            kept = [c for c in comps if hygiene.ratio_ok(c.normalized_ratio)]
            dropped["sale/assessment band"] += len(comps) - len(kept)
            comps = kept
            if comps:
                lo, hi = iqr_filter([c.normalized_ratio for c in comps])
                comps = [c for c in comps if lo <= c.normalized_ratio <= hi]
            # Keep the most relevant: same town first, then nearest.
            comps.sort(key=lambda c: (c.municipality != municipality, c.distance_mi or 9))
            comps = comps[:60]
            if len(comps) >= min_comps:
                arv_mult, as_is_mult, n_r, n_d, disp = value_from_metric(
                    comps, "normalized_ratio", 1.0, renovated_pct, distressed_pct)
                result = ValueResult(
                    arv=round(arv_mult * subject_equalized, -3), as_is=round(as_is_mult * subject_equalized, -3),
                    method="public_record_equalized_ratio", comps=comps, renovated_count=n_r,
                    distressed_count=n_d, dispersion=disp, radius_used=r, subject_equalized=round(subject_equalized, -2),
                    notes=[f"subject equalized value ${subject_equalized:,.0f} (assessed ${assessed_total:,.0f} x "
                           f"town ratio {subject_stats.median_ratio})",
                           f"{len(comps)} recorded sales within {r} mi, time-adjusted at {appreciation:.0%}/yr"])
                if any(dropped.values()):
                    result.notes.append("comp hygiene dropped " + ", ".join(
                        f"{n} {why}" for why, n in dropped.items() if n))
                break
        return result or ValueResult(None, None, "public_record", subject_equalized=round(subject_equalized, -2),
                                     notes=["not enough clean recorded sales nearby"])
