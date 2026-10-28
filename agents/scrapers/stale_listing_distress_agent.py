"""Stale-listing distress agent: active NJ listings that aren't selling.

Source: homeharvest (Realtor.com listing data; see docs/recon/comps_listings.md — verified live
2026-10-26, terms-of-use caveat applies: low volume, cached, internal use only).

Flags active (not pending/contingent) residential listings in the focus-town ZIPs that show:
  * 90+ days on market (stale)                 → STALE_LISTING
  * a price reduction (where the MLS feed exposes original list price / reduced flag)
  * distress language in the description or tags: as-is, cash only, investor special, needs TLC,
    estate sale, handyman special, short sale, tenant occupied, fixer-upper, bank-owned
Each flagged listing is matched to its NJGIN parcel (for assessment/tenure data) and becomes a
lead whose contact is the listing agent — the play is a below-list cash/as-is offer.

Run standalone: python -m agents.scrapers.stale_listing_distress_agent [--zip 07111 ...]
"""
from __future__ import annotations

import argparse
import logging
import re
from typing import Any

from agents.base import AgentContext, BaseAgent
from agents.scrapers.njgis_client import NJGISClient, feature_to_property
from core.config import settings
from core.text import normalize_street
from db.models import SignalType
from db.repository import ensure_lead, upsert_listing, upsert_property, upsert_signal

log = logging.getLogger(__name__)
SOURCE = "homeharvest"

# ZIPs covering the default focus municipalities (config: stale_listings.zips overrides).
DEFAULT_ZIPS = [
    # Middlesex: Woodbridge twp sections, Edison, New Brunswick, Perth Amboy
    "07095", "07001", "07067", "08830", "08863", "07064", "08817", "08820", "08837", "08901", "08861",
    # Union: Elizabeth, Plainfield, Linden
    "07201", "07202", "07206", "07208", "07060", "07062", "07063", "07036",
    # Essex: Newark, Irvington, East Orange
    "07102", "07103", "07104", "07105", "07106", "07107", "07108", "07112", "07114", "07111", "07017", "07018",
    # Hudson: Jersey City, Bayonne
    "07302", "07304", "07305", "07306", "07307", "07002",
    # Passaic: Paterson, Passaic
    "07501", "07502", "07503", "07504", "07505", "07513", "07514", "07522", "07524", "07055",
    # Bergen: Hackensack, Garfield
    "07601", "07026",
    # Camden: Camden, Pennsauken
    "08102", "08103", "08104", "08105", "08109", "08110",
    # Mercer: Trenton, Hamilton, Ewing
    "08608", "08609", "08610", "08611", "08618", "08619", "08620", "08628", "08629", "08638", "08690", "08691",
    # Monmouth: Asbury Park, Neptune, Long Branch, Freehold
    "07712", "07753", "07740", "07728",
]

RESIDENTIAL = {"single_family", "multi_family", "duplex_triplex", "townhomes", "condo_townhome",
               "condo_townhome_rowhome_coop", "condos", "condo"}

# Keyword -> (regex, weight). Context guards remove common false positives.
KEYWORDS: dict[str, tuple[str, float]] = {
    "as-is": (r"\b(?:sold|being sold|sell(?:ing)?|offered|purchase[d]?)\s+(?:in\s+)?(?:its\s+)?['\"]?as[\s-]is\b|\bas[\s-]is\s+(?:condition|sale|basis)\b|\bas[\s-]is\b", 1.0),
    "cash only": (r"\bcash\s+(?:only|buyers?\s+only|offers?\s+only)\b", 1.5),
    "investor special": (r"\binvestors?\s+(?:special|alert|opportunity|delight|dream)\b|\bcalling all investors\b|\bbring(?:ing)? (?:your|all) investors\b", 1.2),
    "needs tlc": (r"\bneeds?\s+(?:some\s+|a\s+little\s+|lots? of\s+)?(?:tlc|work|updating|renovation|rehab|repairs?)\b|\btlc\b", 1.0),
    "estate sale": (r"\bestate\s+sale\b|\bsold\s+by\s+(?:the\s+)?estate\b|\bexecutor\b", 1.2),
    "handyman special": (r"\bhandy\s?man(?:'s)?\s+(?:special|dream|delight)\b", 1.2),
    "short sale": (r"\bshort\s+sale\b", 1.3),
    "tenant occupied": (r"\btenant[\s-]+occupied\b|\btenants?\s+in\s+place\b", 0.6),
    "fixer upper": (r"\bfixer[\s-]?upper\b|\bgut\s+(?:rehab|renovation)\b|\btotal\s+rehab\b", 1.2),
    "bank owned": (r"\bbank[\s-]owned\b|\breo\b|\bforeclosure\b", 1.0),
    "motivated seller": (r"\bmotivated\s+sellers?\b|\bbring\s+(?:all\s+)?offers\b|\bmust\s+sell\b", 1.0),
}
_KW = {k: (re.compile(p, re.I), w) for k, (p, w) in KEYWORDS.items()}
NEGATIONS = re.compile(r"\b(?:no|not|isn'?t|without|never)\b[^.!?;]{0,20}$", re.I)


def find_keywords(text: str | None, tags: list[str] | None = None) -> list[str]:
    hits: list[str] = []
    if text:
        for name, (rx, _) in _KW.items():
            for m in rx.finditer(text):
                prefix = text[max(0, m.start() - 25):m.start()]
                if NEGATIONS.search(prefix):
                    continue  # "no need for TLC", "not a short sale"
                hits.append(name)
                break
    for tag in tags or []:
        if tag in ("fixer_upper",) and "fixer upper" not in hits:
            hits.append("fixer upper")
    return hits


def distress_score(dom: int | None, reduced: bool | None, keywords: list[str]) -> float:
    """0..1 severity for a listing."""
    s = 0.0
    if dom:
        s += min(dom / 365, 1.0) * 0.45 if dom >= 90 else 0.0
    if reduced:
        s += 0.15
    s += min(sum(_KW[k][1] for k in keywords if k in _KW) * 0.12, 0.45)
    return round(min(s, 1.0), 3)


def qualifies(row: dict, min_dom: int, max_price: float) -> tuple[bool, list[str]]:
    if (row.get("status") or "").lower() in ("pending", "contingent"):
        return False, []
    if (row.get("property_type") or "").lower() not in RESIDENTIAL:
        return False, []
    price = row.get("list_price") or 0
    if not price or price > max_price:
        return False, []
    kws = find_keywords(row.get("description"), (row.get("raw") or {}).get("tags"))
    stale = (row.get("days_on_market") or 0) >= min_dom
    return (stale or bool(kws) or bool(row.get("price_reduced") and (row.get("days_on_market") or 0) >= 45)), kws


class StaleListingDistressAgent(BaseAgent):
    name = "stale_listings"
    description = "Active listings with 90+ DOM, price cuts, or distress language in focus ZIPs."

    def run(self, ctx: AgentContext) -> None:
        from engine import homeharvest_provider as hh  # optional dependency

        cfg = settings().get("stale_listings", {})
        zips = self.options.get("zips") or cfg.get("zips") or DEFAULT_ZIPS
        min_dom = int(cfg.get("min_days_on_market", 90))
        max_price = float(cfg.get("max_list_price", 900000))
        gis = NJGISClient()

        for zip_code in zips:
            try:
                rows = hh.active_listings(zip_code)
            except hh.ProviderBlocked as exc:
                ctx.error(f"listing provider blocked at {zip_code}: {exc}; stopping run")
                break
            except Exception as exc:  # noqa: BLE001
                ctx.error(f"{zip_code}: {type(exc).__name__}: {exc}")
                continue
            ctx.bump("listings_seen", len(rows))
            flagged = 0
            ctx.checkpoint()
            for row in rows:
                ok, kws = qualifies(row, min_dom, max_price)
                self._store_listing(ctx, row, kws)
                if not ok:
                    continue
                flagged += 1
                try:
                    self._store_lead(ctx, gis, row, kws, min_dom)
                    ctx.checkpoint()
                except Exception as exc:  # noqa: BLE001
                    ctx.session.rollback()
                    ctx.error(f"{row.get('address')} {zip_code}: {type(exc).__name__}: {exc}")
            ctx.metrics.setdefault("flagged_by_zip", {})[zip_code] = flagged
            ctx.checkpoint()
        ctx.metrics["njgis_http"] = gis.http.stats_summary()

    def _store_listing(self, ctx: AgentContext, row: dict, kws: list[str]) -> None:
        upsert_listing(
            ctx.session, source=SOURCE, source_ref=str(row.get("property_id") or row.get("url")),
            url=row.get("url"), status=row.get("status"), address=row.get("address"), city=row.get("city"),
            zip_code=row.get("zip"), property_type=row.get("property_type"), beds=row.get("beds"),
            baths=row.get("baths"), sqft=row.get("sqft"), year_built=row.get("year_built"),
            list_price=row.get("list_price"), list_date=_d(row.get("list_date")),
            days_on_market=row.get("days_on_market"), price_reduced=row.get("price_reduced"),
            distress_keywords=kws, description=row.get("description"), lat=row.get("lat"), lon=row.get("lon"),
            raw_json={"original_list_price": row.get("original_list_price"), "mls": row.get("mls"),
                      "estimated_value": row.get("estimated_value")})
        ctx.records_processed += 1

    def _store_lead(self, ctx: AgentContext, gis: NJGISClient, row: dict, kws: list[str], min_dom: int) -> None:
        raw = row.get("raw") or {}
        county = (((raw.get("location") or {}).get("county") or {}).get("name") or "").title()
        street = row.get("address") or ""
        feature = None
        if county and street:
            mun = gis.resolve_municipality(county, row.get("city") or "")
            feature = gis.find_by_address(county, street, mun[1] if mun else None)
            if feature is None and mun:
                feature = gis.find_by_address(county, street, None)
        if feature:
            data = feature_to_property(feature)
        else:
            data = {"parcel_id": f"{(county or 'NJ').upper()}-UNMATCHED-{re.sub(r'[^A-Z0-9]+', '-', normalize_street(street))}-{row.get('zip')}",
                    "county": county or "Unknown", "street_address": street.upper()}
            ctx.bump("parcel_unmatched")
        data.update({"city": row.get("city"), "zip_code": row.get("zip")})
        for key in ("beds", "baths", "year_built"):
            if row.get(key) and not data.get(key):
                data[key] = row[key]
        prop, created = upsert_property(ctx.session, data, source=SOURCE)
        if row.get("sqft") and not prop.sqft:
            prop.sqft, prop.sqft_source = int(row["sqft"]), "listing"
        if created:
            ctx.bump("properties_created")
        agent = next(iter(raw.get("advertisers") or []), {}) or {}
        phones = [p.get("number") for p in (agent.get("phones") or []) if p and p.get("number")]
        dom = row.get("days_on_market")
        meta: dict[str, Any] = {
            "url": row.get("url"), "list_price": row.get("list_price"), "list_date": row.get("list_date"),
            "days_on_market": dom, "price_reduced": row.get("price_reduced"),
            "original_list_price": row.get("original_list_price"), "keywords": kws,
            "listing_agent": agent.get("name"), "listing_broker": (agent.get("broker") or {}).get("name"),
            "listing_agent_phone": phones[0] if phones else None, "listing_agent_email": agent.get("email"),
            "mls": row.get("mls"), "status": row.get("status"),
        }
        _, created_sig = upsert_signal(
            ctx.session, prop, signal_type=SignalType.STALE_LISTING, source=SOURCE,
            source_ref=str(row.get("property_id") or row.get("url")),
            severity_weight=distress_score(dom, row.get("price_reduced"), kws), status=row.get("status"),
            amount_owed=None, source_url=row.get("url"), raw_metadata_json=meta, active=True)
        if created_sig:
            ctx.bump("signals_created")
        _, lead_created = ensure_lead(ctx.session, prop, primary_source="stale_listing",
                                      contact_name=f"{agent.get('name')} (listing agent)" if agent.get("name") else None)
        if lead_created:
            ctx.new_leads_found += 1


def _d(value: str | None):
    from datetime import date
    return date.fromisoformat(value) if value else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Stale/distressed active listings")
    parser.add_argument("--zip", action="append", help="ZIP code (repeatable). Default: focus-town ZIPs.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = StaleListingDistressAgent(zips=args.zip).execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} new_leads={run.new_leads_found} "
          f"errors={run.error_count} flagged={run.metrics_json.get('flagged_by_zip')}")
    for e in (run.errors or [])[:10]:
        print("  error:", e)


if __name__ == "__main__":
    main()
