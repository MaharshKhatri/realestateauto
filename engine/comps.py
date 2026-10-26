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
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from agents.scrapers.njgis_client import NJGISClient, stories_from_desc
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
              distressed_pct: float) -> ValueResult:
        subject_stats = muni_stats(self.gis, mun_code, municipality)
        if not subject_stats.median_ratio:
            return ValueResult(None, None, "public_record", notes=["no sales data for subject town"])
        subject_equalized = assessed_total * subject_stats.median_ratio
        subject_units = max(int(units or 1), 1)
        subject_stories = stories_from_desc(building_desc)
        result: ValueResult | None = None
        for r in (radius, fallback_radius):
            features = self.gis.nearby_sales(lat, lon, r, months=months + 12)
            comps: list[Comp] = []
            for f in features:
                a = f["attributes"]
                price, assessed = a.get("SALE_PRICE") or 0, a.get("NET_VALUE") or 0
                if price < 40000 or assessed < 20000:
                    continue
                comp_units = max(int(a.get("DWELL") or 1), 1)
                if (subject_units == 1) != (comp_units == 1) or abs(comp_units - subject_units) > 1:
                    continue  # don't value a 1-family off 3-families or vice versa
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
                    distressed_count=n_d, dispersion=disp, radius_used=r,
                    notes=[f"subject equalized value ${subject_equalized:,.0f} (assessed ${assessed_total:,.0f} x "
                           f"town ratio {subject_stats.median_ratio})",
                           f"{len(comps)} recorded sales within {r} mi, time-adjusted at {appreciation:.0%}/yr"])
                break
        return result or ValueResult(None, None, "public_record", notes=["not enough recorded sales nearby"])
