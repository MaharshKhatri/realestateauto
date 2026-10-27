"""Listing-based comps via homeharvest (Realtor.com sold data) with sqft-normalized valuation.

Verified 2026-10-26 (docs/recon/comps_listings.md): radius searches around an NJ address return
recent sales with sqft ~70% and year built ~80% filled. Realtor.com's terms prohibit scraping, so
this provider is low-volume (top leads only), throttled (>=2s between calls), and cached on disk;
we never republish listing text or photos. Failures fall back to public-record comps.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any

from core.config import CACHE_DIR
from core.geo import haversine_miles
from core.text import muni_base
from engine.comps import Comp, CompHygiene, ValueResult, iqr_filter, value_from_metric

log = logging.getLogger(__name__)

SINGLE_TYPES = {"single_family", "townhomes", "condo_townhome", "condo_townhome_rowhome_coop"}
MULTI_TYPES = {"multi_family", "duplex_triplex", "apartment"}
CONDO_TYPES = {"condos", "condo", "condop", "coop"}


def subject_type(units: int | None, building_desc: str | None, listing_type: str | None = None) -> set[str]:
    """Comparable property types. MOD-IV facts win; without them (listing-only parcels) use the MLS type."""
    desc = (building_desc or "").upper()
    lt = (listing_type or "").lower()
    if "CONDO" in desc or (not desc and not units and lt in CONDO_TYPES):
        return CONDO_TYPES | {"townhomes", "condo_townhome"}
    if (units or 1) >= 2 or (not desc and not units and lt in MULTI_TYPES):
        return MULTI_TYPES
    return SINGLE_TYPES


def listing_type(prop) -> str | None:
    """MLS property type from the property's own listing (stale/active listing signal → listings row)."""
    from sqlalchemy import select
    from sqlalchemy.orm import object_session

    from db.models import Listing, SignalType
    session = object_session(prop)
    if session is None:
        return None
    for sig in prop.active_signals:
        if sig.signal_type in (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING) and sig.source_ref:
            kind = session.scalar(select(Listing.property_type).where(Listing.source_ref == sig.source_ref))
            if kind:
                return kind
    return None


class _DiskCache:
    def __init__(self, ttl_hours: float, root: Path = CACHE_DIR / "homeharvest"):
        self.ttl = ttl_hours * 3600
        self.root = root

    def _path(self, key: str) -> Path:
        h = hashlib.sha256(key.encode()).hexdigest()
        return self.root / h[:2] / f"{h}.json"

    def get(self, key: str) -> Any | None:
        p = self._path(key)
        if p.exists() and time.time() - p.stat().st_mtime < self.ttl:
            try:
                return json.loads(p.read_text())
            except ValueError:
                return None
        return None

    def set(self, key: str, value: Any) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, default=str))


class ListingComps:
    source = "homeharvest_listing"

    def __init__(self, cache_ttl_hours: float = 24.0):
        from engine import homeharvest_provider  # imported lazily: optional dependency
        self.hh = homeharvest_provider
        self.cache = _DiskCache(cache_ttl_hours)

    @classmethod
    def from_settings(cls) -> "ListingComps | None":
        try:
            import homeharvest  # noqa: F401
        except ImportError:
            log.info("homeharvest not installed; listing comps disabled (pip install homeharvest)")
            return None
        return cls()

    # -- data access (cached) -----------------------------------------------------------------
    def sold_near(self, location: str, radius: float, past_days: int) -> list[dict]:
        key = f"sold|{location}|{radius}|{past_days}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        rows = self.hh.sold_comps(location, radius_miles=radius, past_days=past_days)
        rows = [{k: v for k, v in r.items() if k != "raw"} for r in rows]
        self.cache.set(key, rows)
        return rows

    def lookup_subject(self, address_line: str) -> dict | None:
        """Exact single-property record (beds/baths/sqft/year) or None if the address didn't resolve."""
        key = f"subject|{address_line}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached or None
        try:
            rows = self.hh._scrape(location=address_line, limit=5)
        except Exception as exc:  # noqa: BLE001
            log.info("subject lookup failed for %s: %s", address_line, exc)
            return None
        record: dict = {}
        if len(rows) == 1:
            n = self.hh._normalize(rows[0], "off_market")
            record = {k: n.get(k) for k in ("address", "zip", "beds", "baths", "sqft", "year_built", "lot_sqft",
                                            "property_type", "last_sale_price", "last_sale_date",
                                            "estimated_value", "lat", "lon", "status")}
        self.cache.set(key, record)
        return record or None

    @staticmethod
    def address_line(prop) -> str:
        town = (prop.city or muni_base(prop.municipality) or "").title()
        return f"{(prop.street_address or '').title()}, {town}, NJ {prop.zip_code or ''}".strip()

    # -- valuation ----------------------------------------------------------------------------
    def value(self, *, prop, sqft: int, cfg: dict, value_hint: float | None = None,
              hygiene: CompHygiene | None = None) -> ValueResult:
        hygiene = hygiene or CompHygiene.from_settings()
        line = self.address_line(prop)
        subject = self.lookup_subject(line)
        if subject:
            # Fill gaps on the property from the listing record (never overwrite public record).
            if subject.get("sqft") and not prop.sqft:
                prop.sqft, prop.sqft_source = int(subject["sqft"]), "realtor_record"
                sqft = prop.sqft
            if subject.get("beds") and not prop.beds:
                prop.beds = subject["beds"]
            if subject.get("baths") and not prop.baths:
                prop.baths = subject["baths"]
        sqft_known = bool(prop.sqft)
        tol = float(cfg.get("comp_sqft_tolerance", 0.25)) * (1 if sqft_known else 1.6)
        allowed = subject_type(prop.dwellings, prop.building_desc,
                               None if (prop.dwellings or prop.building_desc) else listing_type(prop))
        past_days = int(cfg.get("comp_sold_days", 180))
        min_comps = int(cfg.get("min_comps", 3))
        notes: list[str] = []
        if subject:
            notes.append(f"subject record: {subject.get('beds')} bd / {subject.get('baths')} ba / "
                         f"{subject.get('sqft') or '?'} sqft / built {subject.get('year_built') or '?'}")
        for radius in (cfg.get("comp_radius_miles", 0.5), cfg.get("comp_radius_fallback_miles", 1.0)):
            rows = self.sold_near(line, radius, past_days)
            comps: list[Comp] = []
            for r in rows:
                price, area = r.get("sold_price"), r.get("sqft")
                if not price or not area or area < 400:
                    continue
                if not hygiene.price_ok(price, value_hint) or not hygiene.ppsf_ok(price, area):
                    continue        # non-arm's-length / package price or an impossible $/sqft
                if (r.get("property_type") or "").lower() not in allowed:
                    continue
                if not (sqft * (1 - tol) <= area <= sqft * (1 + tol)):
                    continue
                yb = r.get("year_built")
                if yb and yb < 1750:
                    yb = None
                dist = (haversine_miles(prop.lat, prop.lon, r["lat"], r["lon"])
                        if prop.lat and prop.lon and r.get("lat") and r.get("lon") else None)
                sold = date.fromisoformat(r["sold_date"]) if r.get("sold_date") else None
                comps.append(Comp(
                    address=f"{r.get('address')}, {r.get('city')} {r.get('zip')}", municipality=r.get("city"),
                    sold_price=price, sold_date=sold, distance_mi=round(dist, 3) if dist is not None else None,
                    source=self.source + ("_mls" if r.get("mls") else "_record"), sqft=int(area),
                    beds=r.get("beds"), baths=r.get("baths"), year_built=yb, property_type=r.get("property_type"),
                    lat=r.get("lat"), lon=r.get("lon"), price_per_sqft=price / area, url=r.get("url")))
            # MLS-backed closings carry condition info; deed-only rows include non-arm's-length
            # transfers. Use MLS comps alone when there are enough of them.
            mls_comps = [c for c in comps if c.source.endswith("_mls")]
            if len(mls_comps) >= min_comps:
                comps = mls_comps
            if comps:
                lo, hi = iqr_filter([c.price_per_sqft for c in comps])
                med = sorted(c.price_per_sqft for c in comps)[len(comps) // 2]
                comps = [c for c in comps if lo <= c.price_per_sqft <= hi and c.price_per_sqft >= 0.4 * med]
            if len(comps) >= min_comps:
                arv, as_is, n_r, n_d, disp = value_from_metric(
                    comps, "price_per_sqft", sqft, cfg.get("renovated_percentile", 0.70),
                    cfg.get("distressed_percentile", 0.30))
                mls = sum(1 for c in comps if c.source.endswith("_mls"))
                notes.append(f"{len(comps)} sold comps ({mls} MLS) within {radius} mi, last {past_days} days, "
                             f"{'±' + format(tol, '.0%')} sqft of {sqft:,}{'' if sqft_known else ' (estimated)'}")
                return ValueResult(arv=round(arv, -3), as_is=round(as_is, -3), method="listing_price_per_sqft",
                                   comps=sorted(comps, key=lambda c: c.distance_mi or 9), renovated_count=n_r,
                                   distressed_count=n_d, dispersion=disp, radius_used=radius, notes=notes)
        notes.append("not enough matching listing comps")
        return ValueResult(None, None, "listing_price_per_sqft", notes=notes)
