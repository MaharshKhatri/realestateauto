"""Municipality market statistics computed directly from NJGIN MOD-IV recorded sales.

Each NJ municipality assesses on its own base (revaluation year), so assessed values are only
comparable within a town. The sale-to-assessment ratio (median of recent arm's-length sales
price / assessed total) converts any parcel's assessment into a market-value estimate for that
town. This is our own "equalization ratio", measured from the same data we use for comps.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from agents.scrapers.njgis_client import NJGISClient, _month_prefixes
from core.dates import parse_modiv_date

log = logging.getLogger(__name__)


@dataclass
class MuniStats:
    mun_code: str
    municipality: str
    sales_sampled: int
    median_sale_price: float | None
    median_ratio: float | None          # sale price / assessed total
    p25_ratio: float | None
    p75_ratio: float | None
    median_tax_rate: float | None       # annual taxes / sale price for recent buyers ~ effective rate
    pct_pre_1980: float | None
    latest_sale: date | None
    extra: dict[str, Any] = field(default_factory=dict)

    def value_from_assessment(self, assessed_total: float | None) -> float | None:
        if not assessed_total or not self.median_ratio:
            return None
        return round(assessed_total * self.median_ratio, -3)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _trimmed(values: list[float], lo: float = 0.05, hi: float = 0.95) -> list[float]:
    if len(values) < 10:
        return values
    a, b = _pct(values, lo), _pct(values, hi)
    return [v for v in values if a <= v <= b]


_CACHE: dict[str, MuniStats] = {}


def muni_stats(gis: NJGISClient, mun_code: str, municipality: str = "", months: int = 30,
               sample: int = 1500) -> MuniStats:
    if mun_code in _CACHE:
        return _CACHE[mun_code]
    prefixes = _month_prefixes(months)
    date_clause = " OR ".join(f"DEED_DATE LIKE '{p}%'" for p in prefixes)
    where = f"PCL_MUN='{mun_code}' AND PROP_CLASS='2' AND SALE_PRICE>40000 AND NET_VALUE>20000 AND ({date_clause})"
    rows = list(gis.query(where, out_fields=["SALE_PRICE", "NET_VALUE", "LAST_YR_TX", "YR_CONSTR",
                                             "DEED_DATE", "MUN_NAME", "SALES_CODE"],
                          centroid=False, limit=sample))
    ratios, prices, taxrates, years, dates = [], [], [], [], []
    for r in rows:
        a = r["attributes"]
        price, assessed = a.get("SALE_PRICE") or 0, a.get("NET_VALUE") or 0
        if price <= 0 or assessed <= 0:
            continue
        ratios.append(price / assessed)
        prices.append(price)
        if a.get("LAST_YR_TX"):
            taxrates.append(a["LAST_YR_TX"] / price)
        if a.get("YR_CONSTR"):
            years.append(a["YR_CONSTR"])
        d = parse_modiv_date(a.get("DEED_DATE"))
        if d:
            dates.append(d)
        municipality = municipality or a.get("MUN_NAME") or ""
    ratios = _trimmed(ratios)
    stats = MuniStats(
        mun_code=mun_code, municipality=municipality, sales_sampled=len(rows),
        median_sale_price=statistics.median(prices) if prices else None,
        median_ratio=round(statistics.median(ratios), 4) if ratios else None,
        p25_ratio=round(_pct(ratios, 0.25), 4) if ratios else None,
        p75_ratio=round(_pct(ratios, 0.75), 4) if ratios else None,
        median_tax_rate=round(statistics.median(_trimmed(taxrates)), 5) if taxrates else None,
        pct_pre_1980=round(sum(1 for y in years if y < 1980) / len(years), 3) if years else None,
        latest_sale=max(dates) if dates else None,
    )
    _CACHE[mun_code] = stats
    return stats
