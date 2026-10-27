"""Deal-to-cash-buyer matching: the top 5 recorded buyers for each of the top underwritten deals.

Buyers come from recorded deeds and mailing clusters (agents/scrapers/owner_records_agent.py and
nj_tax_assessment_agent.CashBuyerAgent): each has the towns/counties it bought in, its purchase-price
range, unit counts, how heavy a rehab it takes on and when it last bought. A match score (0-100) is
the sum of explainable parts:

  geography   35  bought in the same town (35), a town within 3 miles (24), the same county (14)
  price       25  the price we'd sell the contract at sits inside the buyer's recorded range
  product     12  same unit count (1-family vs 2-4 family) as their purchases
  rehab       10  the buyer has taken on at least this rehab tier
  recency     10  bought in the last 12 months (10) / 24 months (5)
  volume       8  number of recorded purchases (and flips)

Matches are stored in buyer_matches (top 5 per lead) with the reasons, so the cockpit, deal sheet and
briefing can show *why* a buyer fits. Buyers are only ever pitched the deal after the seller contract is
signed and attorney review has ended (engine/scripts.marketable).

Run: python -m engine.buyer_matcher [--top 50]
"""
from __future__ import annotations

import argparse
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from agents.base import AgentContext, BaseAgent
from core.geo import haversine_miles
from core.text import muni_base
from db.models import BuyerMatch, CashBuyer, Lead, Property, Valuation

log = logging.getLogger(__name__)

DEAL_VERDICTS = ("STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT")
REHAB_ORDER = {"LIGHT": 1, "MEDIUM": 2, "HEAVY": 3}
NEARBY_MILES = 3.0
MATCHES_PER_DEAL = 5


@dataclass
class DealProfile:
    lead_id: int
    town: str               # muni_base form, e.g. NEWARK
    county: str             # upper case
    lat: float | None
    lon: float | None
    units: int | None
    rehab_tier: str | None
    arv: float | None
    buyer_price: float | None  # what an end buyer pays for the contract (our price + fee)


@dataclass
class BuyerProfile:
    buyer_id: int
    towns: set[str]
    counties: set[str]
    min_price: float | None
    max_price: float | None
    avg_price: float | None
    units: set[int]
    max_rehab: str | None
    last_purchase: date | None
    purchases: int
    flips: int
    buyer_type: str | None
    town_points: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Match:
    buyer_id: int
    score: float
    reasons: list[str]


def unit_bucket(units: int | None) -> str | None:
    if not units:
        return None
    return "1" if units == 1 else "2-4" if units <= 4 else "5+"


def deal_profile(lead: Lead) -> DealProfile | None:
    p = lead.property
    v = p.current_valuation
    if v is None or v.verdict not in DEAL_VERDICTS:
        return None
    a = v.assumptions_json or {}
    if v.verdict == "WORKABLE_82_PCT_FLOOR":
        price = a.get("track_buyer_price")
    else:
        price = v.mao_aggressive
    return DealProfile(lead_id=lead.id, town=muni_base(p.municipality or p.city), county=(p.county or "").upper(),
                       lat=p.lat, lon=p.lon, units=p.dwellings, rehab_tier=v.rehab_tier, arv=v.arv_estimate,
                       buyer_price=price)


def buyer_profile(b: CashBuyer, town_centroids: dict[str, tuple[float, float]]) -> BuyerProfile:
    purchases = [x for x in (b.recent_purchases_json or []) if isinstance(x, dict)]
    towns = {muni_base(t) for t in (b.target_municipalities or [])} | {muni_base(x.get("municipality")) for x in purchases}
    towns.discard("")
    return BuyerProfile(
        buyer_id=b.id, towns=towns,
        counties={str(c).upper() for c in (b.target_counties or [])} | {str(x.get("county") or "").upper() for x in purchases} - {""},
        min_price=b.min_arv, max_price=b.max_arv, avg_price=b.avg_purchase_price,
        units={int(x["units"]) for x in purchases if x.get("units")}, max_rehab=b.max_rehab,
        last_purchase=b.last_purchase_date, purchases=b.cash_purchases_count or 0, flips=b.flips_count or 0,
        buyer_type=b.buyer_type, town_points=[town_centroids[t] for t in towns if t in town_centroids])


def price_fit(deal_price: float | None, bp: BuyerProfile) -> tuple[float, str | None]:
    """25 points when the contract price sits inside the buyer's recorded price range, tapering outside it."""
    if not deal_price or not (bp.min_price or bp.max_price):
        return 0.0, None
    lo, hi = bp.min_price or 0, bp.max_price or bp.min_price
    if lo <= deal_price <= hi:
        centre = bp.avg_price or (lo + hi) / 2
        closeness = 1 - min(abs(math.log(deal_price / centre)), 1.0) * 0.3 if centre else 1
        return 25 * closeness, f"price ${deal_price:,.0f} inside their ${lo:,.0f}–${hi:,.0f} range"
    edge = lo if deal_price < lo else hi
    off = abs(math.log(deal_price / edge)) if edge else 9
    if off <= 0.35:  # within ~30-40% of their range
        return 25 * (1 - off / 0.35) * 0.6, f"price ${deal_price:,.0f} near their ${lo:,.0f}–${hi:,.0f} range"
    return 0.0, None


def score_match(deal: DealProfile, bp: BuyerProfile, today: date | None = None) -> Match | None:
    today = today or date.today()
    reasons: list[str] = []
    geo = 0.0
    if deal.town and deal.town in bp.towns:
        geo, why = 35, f"bought in {deal.town.title()}"
    else:
        near = min((haversine_miles(deal.lat, deal.lon, la, lo) for la, lo in bp.town_points),
                   default=None) if deal.lat and deal.lon else None
        if near is not None and near <= NEARBY_MILES:
            geo, why = 24, f"buys {near:.1f} mi away"
        elif deal.county and deal.county in bp.counties:
            geo, why = 14, f"buys in {deal.county.title()} County"
        else:
            why = None
    if not geo:
        return None  # out-of-area buyers are noise for a deal blast
    reasons.append(why)
    pts, why = price_fit(deal.buyer_price, bp)
    if why:
        reasons.append(why)
    score = geo + pts
    bucket = unit_bucket(deal.units)
    if bucket and bp.units and bucket in {unit_bucket(u) for u in bp.units}:
        score += 12
        reasons.append(f"buys {bucket}-family")
    if deal.rehab_tier and bp.max_rehab and REHAB_ORDER.get(bp.max_rehab, 0) >= REHAB_ORDER.get(deal.rehab_tier, 9):
        score += 10
        reasons.append(f"takes {bp.max_rehab.lower()} rehabs")
    if bp.last_purchase:
        months = (today - bp.last_purchase).days / 30.4
        if months <= 12:
            score += 10
            reasons.append(f"active: last bought {bp.last_purchase:%b %Y}")
        elif months <= 24:
            score += 5
    score += min(8.0, 2 * math.log2(1 + bp.purchases) + 2 * bp.flips)
    if bp.purchases:
        reasons.append(f"{bp.purchases} recorded purchases" + (f", {bp.flips} flips" if bp.flips else ""))
    return Match(buyer_id=bp.buyer_id, score=round(min(score, 100.0), 1), reasons=reasons)


def top_deals(session: Session, top: int = 50) -> list[Lead]:
    stmt = (select(Lead).join(Property, Lead.property_id == Property.id)
            .join(Valuation, (Valuation.property_id == Property.id) & Valuation.is_current.is_(True))
            .where(Valuation.verdict.in_(DEAL_VERDICTS))
            .order_by(Lead.priority_rank_score.desc().nulls_last()).limit(top * 2))
    out: list[Lead] = []
    for lead in session.scalars(stmt).unique():
        if deal_profile(lead) and lead not in out:
            out.append(lead)
        if len(out) >= top:
            break
    return out


def town_centroids(session: Session) -> dict[str, tuple[float, float]]:
    rows = session.execute(select(Property.municipality, func.avg(Property.lat), func.avg(Property.lon))
                           .where(Property.lat.is_not(None)).group_by(Property.municipality)).all()
    out: dict[str, tuple[float, float]] = {}
    for muni, lat, lon in rows:
        if muni and lat and lon:
            out.setdefault(muni_base(muni), (lat, lon))
    return out


def match_lead(lead: Lead, buyers: list[BuyerProfile], limit: int = MATCHES_PER_DEAL) -> list[Match]:
    deal = deal_profile(lead)
    if deal is None:
        return []
    scored = [m for bp in buyers if (m := score_match(deal, bp))]
    return sorted(scored, key=lambda m: -m.score)[:limit]


def run_matching(session: Session, top: int = 50) -> dict[str, int]:
    centroids = town_centroids(session)
    buyers = [buyer_profile(b, centroids) for b in session.scalars(select(CashBuyer))]
    leads = top_deals(session, top)
    stats: dict[str, int] = defaultdict(int)
    stats["buyers"] = len(buyers)
    for lead in leads:
        matches = match_lead(lead, buyers)
        session.execute(delete(BuyerMatch).where(BuyerMatch.lead_id == lead.id))
        for rank, m in enumerate(matches, 1):
            session.add(BuyerMatch(lead_id=lead.id, buyer_id=m.buyer_id, rank=rank, score=m.score, reasons=m.reasons))
        stats["deals"] += 1
        stats["matches"] += len(matches)
        stats["deals_with_5"] += len(matches) >= MATCHES_PER_DEAL
        stats["deals_unmatched"] += not matches
    session.flush()
    session.expire_all()
    return dict(stats)


class BuyerMatcherAgent(BaseAgent):
    name = "buyer_matcher"
    description = "Match the top underwritten deals to the 5 best-fitting recorded cash buyers."

    def run(self, ctx: AgentContext) -> None:
        stats = run_matching(ctx.session, int(self.options.get("top", 50)))
        ctx.records_processed = stats.get("deals", 0)
        for k, v in stats.items():
            ctx.metrics[k] = v
        ctx.checkpoint()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = BuyerMatcherAgent(top=args.top).execute()
    print(f"{run.agent_name}: {run.status} processed={run.records_processed} {run.metrics_json}")


if __name__ == "__main__":
    main()
