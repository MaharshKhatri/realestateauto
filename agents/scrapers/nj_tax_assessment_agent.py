"""NJ MOD-IV assessment agents (source: NJGIN statewide parcel + MOD-IV composite, live ArcGIS API).

TaxAssessmentAgent — harvests motivated-owner leads in the focus municipalities:
  * Out-of-state owners (mailing state != NJ), any tenure → OUT_OF_STATE_OWNER
  * In-state absentee owners (mailing street != property street) owned 10+ years → LONG_TERM_ABSENTEE
  * Bonus context: pre-1980 housing stock, 2–4 family ("tired landlord"), likely free & clear

CashBuyerAgent — identifies active investors from recorded purchases. NJGIN redacts owner
names, so buyers are clustered by **owner mailing address**: one mailing address that took
title to several residential parcels in the most recent 24 months of deed data is an active
investor (portfolio landlord, flipper, or fund). We store their purchase list, buy box (towns,
price range, building age) and a reliability score. Names are attached later by owner-record
enrichment.

Run standalone:
    python -m agents.scrapers.nj_tax_assessment_agent absentee [--muni "WOODBRIDGE TWP"]
    python -m agents.scrapers.nj_tax_assessment_agent buyers [--county Essex]
"""
from __future__ import annotations

import argparse
import logging
import re
import statistics
from collections import defaultdict
from datetime import date
from typing import Any

from agents.base import AgentContext, BaseAgent
from agents.scrapers.njgis_client import NJGISClient, _month_prefixes, feature_to_property, years_owned
from core.config import settings
from core.dates import parse_modiv_date
from core.text import clean, mailing_state, normalize_street
from core.zcta import ZipResolver
from db.models import SignalType, SwarmState
from db.repository import ensure_lead, upsert_cash_buyer, upsert_property, upsert_signal
from engine.equity import estimate_equity, likely_free_and_clear
from engine.market import muni_stats

log = logging.getLogger(__name__)
SOURCE = "njgin_modiv"


def _sql(v: str) -> str:
    return v.replace("'", "''")


def harvest_rank(data: dict, tenure: float | None, out_of_state: bool, long_absentee: bool, old_stock: int) -> float:
    """Pre-score used to choose which absentee parcels become leads (motivation proxies only)."""
    t = tenure or 0.0
    score = min(t, 40) * 1.5                                   # long tenure -> equity, fatigue
    score += 15 if out_of_state else 0
    score += 10 if long_absentee else 0
    score += 8 if data.get("year_built") and data["year_built"] < old_stock else 0
    score += 6 if 2 <= (data.get("dwellings") or 1) <= 4 else 0  # small multifamily = tired landlord
    score -= 25 if t < 3 else 0                                  # just bought: investor, not motivated
    return score


def is_package_sale(price: float | None, value: float | None) -> bool:
    """A deed covering several parcels records the package price on each lot."""
    return bool(price and value and price > 2.2 * value)


class TaxAssessmentAgent(BaseAgent):
    name = "tax_assessment"
    description = "Harvest high-equity absentee and out-of-state owners from NJ MOD-IV data."

    def run(self, ctx: AgentContext) -> None:
        cfg = settings()
        harvest = cfg.get("harvest", {})
        min_years = float(harvest.get("absentee_min_years_owned", 10))
        cap = int(self.options.get("cap") or harvest.get("max_parcels_per_municipality", 400))
        old_stock = int(harvest.get("old_stock_year_built_max", 1980))
        targets = self.options.get("municipalities") or [
            (m["county"], m["name"]) for m in cfg.markets.get("focus_municipalities", [])]
        if not self.options.get("municipalities"):
            # Towns the self-improvement agent added (alpha discovery).
            auto = ctx.session.get(SwarmState, "auto_expansion")
            for m in (auto.value_json.get("municipalities", []) if auto else []):
                if (m["county"], m["name"]) not in targets:
                    targets.append((m["county"], m["name"]))
        gis = NJGISClient()
        zips = ZipResolver()

        for county, muni_name in targets:
            resolved = gis.resolve_municipality(county, muni_name)
            if not resolved:
                ctx.error(f"Unknown municipality {muni_name} in {county}")
                continue
            mun_name, mun_code = resolved
            stats = muni_stats(gis, mun_code, mun_name)
            base = f"PCL_MUN='{mun_code}' AND PROP_CLASS='2' AND ST_ADDRESS<>''"
            # Highest-value slices first; stop at the cap.
            slices = [
                ("out_of_state", f"{base} AND CITY_STATE NOT LIKE '%NJ%' AND CITY_STATE NOT LIKE '%N.J%'"),
                ("absentee_pre2000", f"{base} AND DEED_DATE >= '500000'"),
                ("absentee_2000_2015", f"{base} AND DEED_DATE < '{date.today().year - 2000 - int(min_years) + 1:02d}0000'"),
            ]
            # Pool candidates from every slice, then keep the most motivated `cap` of them.
            pool: dict[str, dict] = {}
            for slice_name, where in slices:
                n = 0
                for feature in gis.query(where, limit=cap * 4):
                    data = feature_to_property(feature)
                    ctx.records_processed += 1
                    tenure = years_owned(data["last_sale_date"])
                    out_of_state = bool(data.get("is_out_of_state"))
                    long_absentee = bool(data.get("is_absentee")) and tenure is not None and tenure >= min_years
                    if not (out_of_state or long_absentee) or data["parcel_id"] in pool:
                        continue
                    data["_tenure"], data["_oos"], data["_abs"] = tenure, out_of_state, long_absentee
                    data["_rank"] = harvest_rank(data, tenure, out_of_state, long_absentee, old_stock)
                    pool[data["parcel_id"]] = data
                    n += 1
                ctx.metrics.setdefault("slices", {})[f"{mun_name}:{slice_name}"] = n
            chosen = sorted(pool.values(), key=lambda d: d["_rank"], reverse=True)[:cap]
            for i, data in enumerate(chosen, 1):
                self._store(ctx, data, stats, zips, data.pop("_tenure"), data.pop("_oos"), data.pop("_abs"), old_stock)
                data.pop("_rank", None)
                if i % 25 == 0:
                    ctx.checkpoint()
            kept = len(chosen)
            ctx.metrics.setdefault("per_municipality", {})[mun_name] = {
                "kept": kept, "median_ratio": stats.median_ratio, "median_sale_price": stats.median_sale_price,
                "sales_sampled": stats.sales_sampled}
            ctx.note(f"{mun_name}: {kept} absentee/out-of-state leads (sale/assessment ratio {stats.median_ratio})")
            ctx.checkpoint()
        ctx.metrics["njgis_http"] = gis.http.stats_summary()

    def _store(self, ctx: AgentContext, data: dict, stats, zips: ZipResolver, tenure: float | None,
               out_of_state: bool, long_absentee: bool, old_stock: int) -> None:
        if data.get("lat") and data.get("lon"):
            data["zip_code"] = zips.zip_for(data["lon"], data["lat"])
        value = stats.value_from_assessment(data.get("assessed_total"))
        data["estimated_value"] = value
        package = is_package_sale(data.get("last_sale_price"), value)
        data["estimated_equity"] = None if package else estimate_equity(
            value, data.get("last_sale_price"), data.get("last_sale_date"))
        prop, created = upsert_property(ctx.session, data, source=SOURCE)
        if created:
            ctx.bump("properties_created")
        else:
            ctx.records_updated += 1
        meta = {
            "mailing": f"{data.get('owner_mailing_address')}, {data.get('owner_city_state_zip')}",
            "years_owned": round(tenure, 1) if tenure is not None else None,
            "likely_free_and_clear": likely_free_and_clear(data.get("last_sale_date")),
            "old_stock": bool(data.get("year_built") and data["year_built"] < old_stock),
            "units": data.get("dwellings"),
            "estimated_value": value, "estimated_equity": data["estimated_equity"],
            "package_sale": package,
        }
        ref = data["parcel_id"]
        if out_of_state:
            upsert_signal(ctx.session, prop, signal_type=SignalType.OUT_OF_STATE_OWNER, source=SOURCE,
                          source_ref=ref, severity_weight=0.6 + (0.2 if (tenure or 0) >= 10 else 0),
                          raw_metadata_json={**meta, "owner_state": data.get("owner_state")}, active=True)
        if long_absentee:
            upsert_signal(ctx.session, prop, signal_type=SignalType.LONG_TERM_ABSENTEE, source=SOURCE,
                          source_ref=ref, severity_weight=min(0.4 + (tenure or 0) / 50, 0.9),
                          raw_metadata_json=meta, active=True)
        primary = "out_of_state_owner" if out_of_state else "absentee_owner"
        _, lead_created = ensure_lead(ctx.session, prop, primary_source=primary)
        if lead_created:
            ctx.new_leads_found += 1


# -- cash buyers --------------------------------------------------------------------------------------
UNIT_RE = re.compile(r"[,\s]+(?:#|UNIT|STE|SUITE|APT)\s*\S+$")


def mailing_key(street: str | None, city_state: str | None) -> str:
    """Merge variants: '25 ALLIK WAY, #101' / '25 ALLIK WAY UNIT 101' / 'ROAD' vs 'RD'; city with or without
    state and ZIP ('JERSEY CITY, NJ' == 'JERSEY CITY, N.J. 07306' == 'JERSEY CITY')."""
    s = normalize_street(street)
    unit = ""
    m = re.search(r"(?:#|UNIT|STE|SUITE|APT)\s*([A-Z0-9-]+)$", s)
    if m:
        unit = m.group(1)
        s = UNIT_RE.sub("", s)
    city = clean(city_state).upper()
    city = re.sub(r"[\s,]+\d{5}(?:-?\d{4})?\s*$", "", city)          # trailing ZIP
    city = re.sub(r"[\s,]+N\.?\s?J\.?\s*$|[\s,]+[A-Z]{2}\.?\s*$", "", city)  # trailing state
    city = re.sub(r"[^A-Z ]", " ", city)
    return f"{s}|{unit}|{re.sub(r'\s+', ' ', city).strip()}"


def address_mailing_key(mailing_address: str | None) -> str | None:
    """Key for a stored 'STREET, CITY, ST ZIP' string."""
    street, _, rest = (mailing_address or "").partition(", ")
    return mailing_key(street, rest) if street else None


def buyer_reliability(count: int, volume: float, last_purchase: date | None, towns: int) -> float:
    """0–100: repeat volume, recency, and breadth of activity."""
    score = min(count, 20) * 3.0                     # up to 60
    score += min(volume / 1_000_000, 10) * 2.0       # up to 20
    if last_purchase:
        months = (date.today() - last_purchase).days / 30.4
        score += max(0.0, 15 - months * 0.6)         # up to 15, decays over ~2 years
    score += min(towns, 5)                           # up to 5
    return round(min(score, 100), 1)


class CashBuyerAgent(BaseAgent):
    name = "cash_buyers"
    description = "Identify active NJ investors by clustering recent deed transfers on owner mailing address."

    def run(self, ctx: AgentContext) -> None:
        counties = self.options.get("counties") or settings().markets.get("counties", [])
        months = int(self.options.get("months", 24))
        min_purchases = int(self.options.get("min_purchases", 2))
        top_n = int(self.options.get("top_n", 60))
        gis = NJGISClient()

        for county in counties:
            vintage = gis.latest_sale_month(county) or date.today()
            prefixes = _month_prefixes(months, anchor=vintage)
            date_clause = " OR ".join(f"DEED_DATE LIKE '{p}%'" for p in prefixes)
            where = f"COUNTY='{county.upper()}' AND PROP_CLASS IN ('2','4C') AND SALE_PRICE>1000 AND ({date_clause})"
            params = {
                "where": where,
                "outStatistics": ('[{"statisticType":"count","onStatisticField":"OBJECTID","outStatisticFieldName":"n"},'
                                  '{"statisticType":"sum","onStatisticField":"SALE_PRICE","outStatisticFieldName":"vol"}]'),
                "groupByFieldsForStatistics": "ST_ADDRESS,CITY_STATE",
                "havingClause": f"COUNT(OBJECTID)>={min_purchases}",
                "orderByFields": "n DESC", "f": "json",
            }
            data = gis.http.get_json("https://services2.arcgis.com/XVOqAjTOJ5P6ngMu/arcgis/rest/services/"
                                     "Parcels_Composite_NJ_WM/FeatureServer/0/query", params=params)
            if "error" in data:
                ctx.error(f"{county}: grouping query failed: {data['error']}")
                continue
            groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "vol": 0.0, "variants": []})
            for f in data.get("features", []):
                a = f["attributes"]
                if not a.get("ST_ADDRESS"):
                    continue
                key = mailing_key(a["ST_ADDRESS"], a.get("CITY_STATE"))
                g = groups[key]
                g["n"] += a["n"]
                g["vol"] += a.get("vol") or 0
                g["variants"].append((a["ST_ADDRESS"], a.get("CITY_STATE")))
            # Arm's-length investor activity: average price must look like real purchases.
            for key, g in groups.items():
                g["key"] = key
            ranked = sorted((g for g in groups.values() if g["vol"] / max(g["n"], 1) >= 60000),
                            key=lambda g: (g["n"], g["vol"]), reverse=True)[:top_n]
            ctx.note(f"{county}: {len(groups)} repeat-buyer mailing addresses; keeping top {len(ranked)}")
            for g in ranked:
                self._store_buyer(ctx, gis, county, g, date_clause)
                ctx.checkpoint()
        ctx.metrics["njgis_http"] = gis.http.stats_summary()

    def _store_buyer(self, ctx: AgentContext, gis: NJGISClient, county: str, group: dict, date_clause: str) -> None:
        purchases: list[dict[str, Any]] = []
        for street, city_state in group["variants"]:
            where = (f"ST_ADDRESS='{_sql(street)}' AND CITY_STATE='{_sql(city_state or '')}' "
                     f"AND PROP_CLASS IN ('2','4C') AND SALE_PRICE>1000 AND ({date_clause})")
            for feature in gis.query(where, limit=200):
                a = feature["attributes"]
                purchases.append({
                    "address": clean(a.get("PROP_LOC")), "municipality": a.get("MUN_NAME"),
                    "county": (a.get("COUNTY") or county).title(), "price": a.get("SALE_PRICE"),
                    "deed_date": (parse_modiv_date(a.get("DEED_DATE")) or date.min).isoformat(),
                    "year_built": a.get("YR_CONSTR") or None, "units": a.get("DWELL"),
                    "class": a.get("PROP_CLASS"), "block": a.get("PCLBLOCK"), "lot": a.get("PCLLOT"),
                    "mun_code": a.get("PCL_MUN"),
                })
        purchases = [p for p in purchases if (p["price"] or 0) >= 30000]
        if len(purchases) < 2:
            return
        prices = [p["price"] for p in purchases]
        vintage_year = max(int(p["deed_date"][:4]) for p in purchases)
        new_builds = sum(1 for p in purchases if p["year_built"] and p["year_built"] >= vintage_year - 2)
        repeated_price = max(prices.count(x) for x in set(prices))
        if statistics.median(prices) > 1_500_000 or new_builds > len(purchases) / 2 or repeated_price > len(purchases) / 2:
            # Builders/developers and bulk package deeds are not wholesale buyers.
            ctx.bump("buyers_excluded_builder_or_package")
            return
        ctx.records_processed += 1
        purchases.sort(key=lambda p: p["deed_date"], reverse=True)
        prices = [p["price"] for p in purchases]
        dates = [date.fromisoformat(p["deed_date"]) for p in purchases if p["deed_date"] != date.min.isoformat()]
        towns = sorted({p["municipality"] for p in purchases if p["municipality"]})
        years = [p["year_built"] for p in purchases if p["year_built"]]
        multi = sum(1 for p in purchases if (p["units"] or 1) >= 2)
        street, city_state = group["variants"][0]
        state = mailing_state(city_state)
        buyer_type = "portfolio_landlord" if multi >= len(purchases) / 2 else "investor"
        if years and statistics.median(years) < 1960 and statistics.median(prices) < 450000:
            buyer_type = "value_add_investor"
        _, created = upsert_cash_buyer(
            ctx.session, f"Owner @ {street}, {city_state}",
            mailing_address=f"{street}, {city_state}",
            mailing_key=group.get("key") or mailing_key(street, city_state),
            buyer_type=buyer_type,
            target_counties=sorted({p["county"] for p in purchases}),
            target_municipalities=towns,
            min_arv=float(min(prices)), max_arv=float(max(prices)),
            max_rehab="HEAVY" if years and statistics.median(years) < 1950 else "MEDIUM",
            cash_purchases_count=len(purchases),
            total_volume=float(sum(prices)),
            avg_purchase_price=round(statistics.mean(prices), -2),
            first_purchase_date=min(dates) if dates else None,
            last_purchase_date=max(dates) if dates else None,
            recent_purchases_json=purchases[:25],
            reliability_score=buyer_reliability(len(purchases), sum(prices), max(dates) if dates else None, len(towns)),
            source=f"njgin_mailing_cluster{'_out_of_state' if state and state != 'NJ' else ''}",
        )
        if created:
            ctx.bump("buyers_created")
        else:
            ctx.records_updated += 1


def main() -> None:
    parser = argparse.ArgumentParser(description="NJ MOD-IV assessment agents")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("absentee")
    a.add_argument("--muni", action="append", help='MUN_NAME, e.g. "WOODBRIDGE TWP" (needs --county)')
    a.add_argument("--county")
    a.add_argument("--cap", type=int)
    b = sub.add_parser("buyers")
    b.add_argument("--county", action="append")
    b.add_argument("--top-n", type=int, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "absentee":
        munis = [(args.county, m) for m in args.muni] if args.muni else None
        run = TaxAssessmentAgent(municipalities=munis, cap=args.cap).execute()
    else:
        run = CashBuyerAgent(counties=args.county, top_n=args.top_n).execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} new_leads={run.new_leads_found} "
          f"errors={run.error_count}")
    for note in (run.self_improvement_notes or "").splitlines():
        print("  ", note)
    for e in (run.errors or [])[:10]:
        print("  error:", e)


if __name__ == "__main__":
    main()
