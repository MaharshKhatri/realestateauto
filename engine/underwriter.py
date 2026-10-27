"""Wholesale underwriting engine: comps -> ARV / as-is -> NJ tax drag -> rehab -> MAO -> offers.

    MAO (conservative) = ARV x 0.70 - repairs - assignment fee
    MAO (aggressive)   = ARV x 0.75 - repairs - assignment fee
    Opening anchor     = MAO (conservative) x (1 - 12%)

The end buyer pays up to ARV x 0.70 - repairs; our assignment fee is the gap between that and the
contract price we negotiate with the seller. Known debt (sheriff-sale upset amount) is compared to
MAO: if the debt exceeds MAO, the only path is a short sale or the seller bringing cash.

Run: python -m engine.underwriter --top 15
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.base import AgentContext, BaseAgent
from agents.scrapers.njgis_client import NJGISClient, estimate_sqft, years_owned
from agents.scrapers.sheriff_sale_agent import first_defendant
from core.config import settings
from db.models import DistressSignal, Lead, Property, SignalType, Valuation, utcnow
from engine import rehab as rehab_model
from engine.comps import PublicRecordComps, ValueResult
from engine.equity import estimate_equity
from engine.frfpa_classifier import FrfpaDecision, OccupancyFacts, classify, covered_track

log = logging.getLogger(__name__)


@dataclass
class OfferMath:
    arv: float
    arv_before_tax_drag: float
    effective_tax_rate: float | None
    tax_drag_adjustment: float
    repairs: float
    assignment_fee: float
    mao_conservative: float
    mao_aggressive: float
    anchor_offer: float
    buyer_purchase_price: float
    buyer_profit: float
    buyer_roi: float
    debt: float | None
    deal_spread: float | None
    verdict: str
    frfpa_floor: float | None = None
    flags: list[str] = field(default_factory=list)


def tax_drag(arv: float, annual_taxes: float | None, cfg: dict) -> tuple[float, float | None, float]:
    """NJ property taxes cut what buyers will pay. Above the threshold effective rate, haircut ARV."""
    if not annual_taxes or not arv:
        return arv, None, 0.0
    eff = annual_taxes / arv
    threshold = float(cfg.get("high_tax_drag_threshold", 0.025))
    per_pct = float(cfg.get("tax_drag_arv_haircut_per_pct", 0.02))
    if eff <= threshold:
        return arv, round(eff, 4), 0.0
    haircut = min((eff - threshold) * 100 * per_pct, 0.10)
    return round(arv * (1 - haircut), -3), round(eff, 4), round(haircut, 4)


def assignment_fee_for(arv: float, buyer_max: float, debt: float | None, cfg: dict) -> tuple[float, list[str]]:
    """Target fee: ~6% of ARV clamped to $15K–$35K, cut back when known debt leaves a thin spread."""
    lo, hi = float(cfg.get("target_assignment_fee_min", 15000)), float(cfg.get("target_assignment_fee_max", 35000))
    floor = float(cfg.get("min_assignment_fee", 10000))
    fee = min(max(0.06 * arv, lo), hi)
    flags: list[str] = []
    if debt:
        room = buyer_max - debt
        if room < fee + 10000:
            share = float(cfg.get("assignment_fee_pct_of_spread", 0.40))
            fee = max(floor, room * share) if room > 0 else floor
            flags.append("thin spread over known debt — fee reduced")
    return round(fee, -2), flags


FRFPA_FMV_FLOOR = 0.82   # N.J.S.A. 46:10B-64: owner must receive >= 82% of fair market value


def occupancy_facts(prop: Property) -> OccupancyFacts:
    signals = prop.active_signals
    sheriff = next((s for s in signals if s.signal_type == SignalType.SHERIFF_SALE), None)
    lead = prop.lead
    names = [n for n in (prop.owner_name, first_defendant(sheriff.defendant) if sheriff and sheriff.defendant else None) if n]
    confirmed = lead.occupancy_status if lead and lead.occupancy_confirmed_at and lead.occupancy_status else None
    return OccupancyFacts(
        in_foreclosure=any(s.signal_type in (SignalType.SHERIFF_SALE, SignalType.LIS_PENDENS) for s in signals),
        one_to_four=prop.property_class == "2" or (prop.dwellings or 1) <= 4,
        owner_names=names, defendant=sheriff.defendant if sheriff else None,
        vacant_signal=any(s.signal_type == SignalType.VACANT for s in signals),
        estate_signal=bool(prop.is_estate_owned) or any(s.signal_type in (SignalType.ESTATE_OWNER, SignalType.PROBATE)
                                                        for s in signals),
        is_absentee=prop.is_absentee, confirmed_occupancy=confirmed,
        confirmed_source=lead.occupancy_source if lead else None)


def frfpa_decision(prop: Property) -> FrfpaDecision:
    return classify(occupancy_facts(prop))


def frfpa_applies(prop: Property) -> bool:
    """True when the lead must be priced and contracted as FRFPA-covered (see engine/frfpa_classifier.py)."""
    return frfpa_decision(prop).applies


def offer_math(*, arv: float, repairs: float, annual_taxes: float | None, debt: float | None,
               cfg: dict | None = None, frfpa: bool = False, fmv: float | None = None) -> OfferMath:
    cfg = cfg or dict(settings().deal)
    adj_arv, eff, haircut = tax_drag(arv, annual_taxes, cfg)
    cons, aggr = float(cfg.get("mao_pct_conservative", 0.70)), float(cfg.get("mao_pct_aggressive", 0.75))
    buyer_max = adj_arv * cons - repairs
    fee, flags = assignment_fee_for(adj_arv, buyer_max, debt, cfg)
    mao_c = adj_arv * cons - repairs - fee
    mao_a = adj_arv * aggr - repairs - fee
    anchor = mao_c * (1 - float(cfg.get("anchor_discount_pct", 0.12)))
    # End-buyer economics at the conservative price.
    holding = buyer_max * 0.0125 * 6
    resale_costs = adj_arv * 0.07
    buyer_all_in = buyer_max + repairs + holding
    buyer_profit = adj_arv - buyer_all_in - resale_costs
    roi = buyer_profit / buyer_all_in if buyer_all_in > 0 else 0.0
    spread = (mao_c - debt) if debt else None
    if haircut:
        flags.append(f"high NJ tax drag: effective rate {eff:.2%} → ARV haircut {haircut:.1%}")
    floor = round(FRFPA_FMV_FLOOR * fmv, -2) if frfpa and fmv else None
    if floor:
        flags.append(f"FRFPA: owner-occupied foreclosure — offer must be ≥ 82% of FMV (${floor:,.0f}); 10-business-day "
                     "cancellation, notarized 14-pt contract, attorney/title closing; never promise to stop the foreclosure")
        anchor = max(anchor, floor)
    if mao_c <= 0:
        verdict = "PASS"
        flags.append("repairs + fee exceed 70% of ARV")
    elif floor and mao_c < floor:
        verdict = "FRFPA_BLOCKED"
        flags.append(f"MAO ${mao_c:,.0f} is below the FRFPA floor ${floor:,.0f}: no compliant wholesale offer — "
                     "refer to a listing agent, explore a short sale, or pass")
    elif debt and spread is not None and spread < 0:
        verdict = "SHORT_SALE_ONLY"
        flags.append(f"known debt ${debt:,.0f} exceeds MAO by ${-spread:,.0f}")
    elif debt and spread is not None and spread < 15000:
        verdict = "TIGHT"
    elif mao_c / adj_arv >= 0.45:
        verdict = "STRONG"
    else:
        verdict = "WORKABLE"
    return OfferMath(
        arv=round(adj_arv, -3), arv_before_tax_drag=round(arv, -3), effective_tax_rate=eff,
        tax_drag_adjustment=haircut, repairs=round(repairs, -2), assignment_fee=fee,
        mao_conservative=round(mao_c, -2), mao_aggressive=round(mao_a, -2), anchor_offer=round(anchor, -2),
        buyer_purchase_price=round(buyer_max, -2), buyer_profit=round(buyer_profit, -2), buyer_roi=round(roi, 3),
        debt=debt, deal_spread=round(spread, -2) if spread is not None else None, verdict=verdict,
        frfpa_floor=floor, flags=flags)


def confidence(result: ValueResult, sqft_known: bool, min_comps: int) -> float:
    if not result.arv:
        return 0.0
    n = len(result.comps)
    score = min(n / (min_comps * 4), 1.0) * 35                      # comp volume
    if result.dispersion is not None:
        score += max(0.0, 25 * (1 - min(result.dispersion / 0.8, 1)))  # tight comps
    if result.radius_used is not None:
        score += 15 if result.radius_used <= 0.5 else 8
    score += 15 if result.method.startswith(("listing", "blended")) else 8  # listing comps carry condition info
    score += 10 if sqft_known else 0
    return round(min(score, 100), 1)


def cross_check(listing: ValueResult, public: ValueResult) -> ValueResult:
    """Blend listing and public-record valuations when the listing comp set is thin or noisy,
    or when the two methods disagree by more than 25%."""
    gap = abs(listing.arv - public.arv) / public.arv
    thin = len(listing.comps) < 6 or (listing.dispersion or 0) > 0.6
    listing.notes.append(f"public-record cross-check: ARV ${public.arv:,.0f} / as-is ${public.as_is:,.0f} "
                         f"({gap:.0%} apart)")
    if thin or gap > 0.25:
        w = 0.5 if thin else 0.65
        listing.arv = round(w * listing.arv + (1 - w) * public.arv, -3)
        listing.as_is = round(w * listing.as_is + (1 - w) * public.as_is, -3)
        listing.method = "blended_listing_public"
        listing.notes.append(f"blended {w:.0%} listing / {1 - w:.0%} public-record "
                             f"({'thin/noisy listing comps' if thin else 'methods disagree'})")
        listing.dispersion = max(listing.dispersion or 0, gap)
    return listing


class Underwriter:
    def __init__(self, gis: NJGISClient | None = None, listing_provider: Any | None = None):
        self.gis = gis or NJGISClient()
        self.public = PublicRecordComps(self.gis)
        self.listing_provider = listing_provider
        self.cfg = dict(settings().deal)

    def value(self, prop: Property) -> tuple[ValueResult, int, bool]:
        cfg = self.cfg
        sqft_known = bool(prop.sqft)
        sqft = prop.sqft or estimate_sqft(prop.building_desc, prop.dwellings,
                                          settings().get("rehab", {}).get("default_sqft_per_unit", 1400))
        result: ValueResult | None = None
        if self.listing_provider is not None and prop.lat and prop.lon:
            try:
                result = self.listing_provider.value(prop=prop, sqft=sqft, cfg=cfg)
            except Exception as exc:  # noqa: BLE001 - fall back to public records
                log.warning("listing comps failed for %s: %s", prop.parcel_id, exc)
                result = None
            if result and not result.arv:
                result = None
            if prop.sqft and not sqft_known:   # the subject lookup found the real living area
                sqft, sqft_known = prop.sqft, True
        public: ValueResult | None = None
        if prop.lat and prop.lon and prop.assessed_total and prop.mun_code:
            public = self.public.value(
                lat=prop.lat, lon=prop.lon, assessed_total=prop.assessed_total, mun_code=prop.mun_code,
                municipality=prop.municipality or "", units=prop.dwellings, building_desc=prop.building_desc,
                radius=cfg.get("comp_radius_miles", 0.5), fallback_radius=cfg.get("comp_radius_fallback_miles", 1.0),
                months=int(cfg.get("public_record_comp_months", 24)), appreciation=cfg.get("annual_appreciation", 0.05),
                min_comps=int(cfg.get("min_comps", 3)), renovated_pct=cfg.get("renovated_percentile", 0.70),
                distressed_pct=cfg.get("distressed_percentile", 0.30))
            if not public.arv:
                public = None
        if result is None:
            result = public
        elif public is not None:
            result = cross_check(result, public)
        return result or ValueResult(None, None, "none", notes=["no location/assessment data"]), sqft, sqft_known

    def underwrite(self, session: Session, prop: Property) -> Valuation | None:
        result, sqft, sqft_known = self.value(prop)
        if not result.arv:
            log.info("no valuation for %s: %s", prop.parcel_id, "; ".join(result.notes))
            return None
        signals = {s.signal_type for s in prop.active_signals}
        sheriff = next((s for s in prop.active_signals if s.signal_type == SignalType.SHERIFF_SALE), None)
        debt = sheriff.amount_owed if sheriff and sheriff.amount_owed else None
        tenure = years_owned(prop.last_sale_date)
        rehab = rehab_model.estimate(
            sqft, prop.year_built,
            vacant=SignalType.VACANT in signals,
            estate=bool(prop.is_estate_owned) or SignalType.ESTATE_OWNER in signals,
            foreclosure=SignalType.SHERIFF_SALE in signals or SignalType.LIS_PENDENS in signals,
            years_owned=tenure, absentee_rental=bool(prop.is_absentee),
            as_is_to_arv=(result.as_is / result.arv) if result.as_is and result.arv else None,
            units=prop.dwellings)
        as_is = min(result.as_is or 0, result.arv) or None
        decision = frfpa_decision(prop)
        frfpa = decision.applies
        om = offer_math(arv=result.arv, repairs=rehab.cost, annual_taxes=prop.annual_taxes, debt=debt, cfg=self.cfg,
                        frfpa=frfpa, fmv=as_is)
        track_info: dict[str, Any] = {}
        if frfpa and om.verdict == "FRFPA_BLOCKED" and as_is:
            ct = covered_track(arv=om.arv, fmv=as_is, repairs=om.repairs, debt=debt,
                               aggressive_pct=float(self.cfg.get("mao_pct_aggressive", 0.75)),
                               min_fee=float(self.cfg.get("min_assignment_fee", 10000)))
            om.verdict = ct.track
            om.flags.extend(ct.notes)
            track_info = {"compliant_offer": ct.compliant_offer, "track_buyer_price": ct.buyer_price,
                          "track_fee": ct.fee, "equity_at_risk": ct.equity_at_risk}
            if ct.track == "WORKABLE_82_PCT_FLOOR":
                om.anchor_offer = ct.compliant_offer
                om.assignment_fee = ct.fee
        exempt_scenario = None
        if frfpa:   # what the deal looks like if occupancy verification shows FRFPA does not apply
            ex = offer_math(arv=result.arv, repairs=rehab.cost, annual_taxes=prop.annual_taxes, debt=debt, cfg=self.cfg)
            exempt_scenario = {"verdict": ex.verdict, "mao_conservative": ex.mao_conservative,
                               "anchor_offer": ex.anchor_offer, "fee": ex.assignment_fee}
        if prop.lead:
            prop.lead.frfpa_track = decision.track
        conf = confidence(result, sqft_known, int(self.cfg.get("min_comps", 3)))
        as_is = min(as_is or 0, om.arv) or None
        equity = estimate_equity(as_is, prop.last_sale_price, prop.last_sale_date, known_debt=debt)

        for old in prop.valuations:
            old.is_current = False
        val = Valuation(
            property=prop, arv_estimate=om.arv, as_is_value=as_is, est_repair_cost=om.repairs,
            rehab_tier=rehab.tier, target_assignment_fee=om.assignment_fee, mao_conservative=om.mao_conservative,
            mao_aggressive=om.mao_aggressive, anchor_offer=om.anchor_offer, confidence_score=conf,
            comps_json=[c.to_json() for c in result.comps[:40]], comp_count=len(result.comps),
            comp_source=result.method, sqft_used=sqft, effective_tax_rate=om.effective_tax_rate,
            tax_drag_adjustment=om.tax_drag_adjustment, debt_estimate=debt, equity_estimate=equity,
            deal_spread=om.deal_spread, verdict=om.verdict, is_current=True, calculated_at=utcnow(),
            assumptions_json={
                "sqft_source": "record" if sqft_known else "estimated_from_building_description",
                "rehab_by_tier": rehab.by_tier, "rehab_reasons": rehab.reasons,
                "arv_before_tax_drag": om.arv_before_tax_drag, "buyer_purchase_price": om.buyer_purchase_price,
                "buyer_profit": om.buyer_profit, "buyer_roi": om.buyer_roi, "flags": om.flags,
                "comp_notes": result.notes, "renovated_comps": result.renovated_count,
                "distressed_comps": result.distressed_count, "dispersion": result.dispersion,
                "radius_miles": result.radius_used, "valued_on": date.today().isoformat(),
                "frfpa_applies": frfpa, "frfpa_floor": om.frfpa_floor, "frfpa_track": decision.track,
                "frfpa_confidence": decision.confidence, "frfpa_basis": decision.basis,
                "verify_before_contract": decision.verify_before_contract, "exempt_scenario": exempt_scenario,
                **track_info,
            })
        session.add(val)
        prop.estimated_value = as_is
        prop.estimated_equity = equity
        session.flush()
        return val


def known_debt(prop: Property) -> float | None:
    sheriff = next((s for s in prop.active_signals if s.signal_type == SignalType.SHERIFF_SALE), None)
    return sheriff.amount_owed if sheriff and sheriff.amount_owed else None


def inputs_changed(prop: Property, val: Valuation) -> bool:
    """A fresh valuation is still stale if the facts it priced have changed since (debt, sqft, signals)."""
    if (known_debt(prop) or 0) != (val.debt_estimate or 0):
        return True
    if prop.sqft and val.sqft_used != prop.sqft:
        return True
    lead = prop.lead
    if lead and lead.occupancy_confirmed_at and lead.occupancy_confirmed_at > val.calculated_at:
        return True
    if (val.assumptions_json or {}).get("frfpa_track") != frfpa_decision(prop).track:
        return True
    return any(s.first_seen_at > val.calculated_at for s in prop.active_signals)


class UnderwriterAgent(BaseAgent):
    name = "underwriter"
    description = "Comps, ARV, rehab and MAO for the highest-priority leads."

    def run(self, ctx: AgentContext) -> None:
        top = int(self.options.get("top", 15))
        stale_days = int(self.options.get("stale_days", 7))
        scope = self.options.get("scope", "top")      # top | valued | sheriff
        listing = None if self.options.get("public_only") else (
            self.options.get("listing_provider") or _default_listing_provider())
        underwriter = Underwriter(listing_provider=listing)
        q = select(Lead).where(Lead.status_stage.notin_(["DEAD", "DO_NOT_CONTACT", "CLOSED"]))
        if scope == "valued":
            q = q.join(Valuation, Valuation.property_id == Lead.property_id).where(Valuation.is_current.is_(True))
        elif scope == "sheriff":
            q = q.join(DistressSignal, DistressSignal.property_id == Lead.property_id).where(
                DistressSignal.signal_type == SignalType.SHERIFF_SALE, DistressSignal.active.is_(True))
        q = q.order_by(Lead.priority_rank_score.desc(), Lead.motivation_score.desc())
        if scope == "top":
            q = q.limit(top)
        leads = list(dict.fromkeys(ctx.session.scalars(q).all()))
        self.verdicts: dict[str, int] = {}
        self.tried: set[int] = set()
        for lead in leads:
            self._underwrite(ctx, underwriter, lead, stale_days)

        # Convergence: underwriting re-ranks leads (underwater deals drop, priced deals rise). Re-score
        # and underwrite whatever entered the top `ensure_top` without a valuation, until stable.
        ensure_top = int(self.options.get("ensure_top", 15))
        from engine.scoring import TERMINAL, active_weights, apply_score
        version, weights = active_weights(ctx.session)
        open_leads = ctx.session.scalars(select(Lead).where(Lead.status_stage.notin_(TERMINAL))).all()
        for round_no in range(int(self.options.get("max_rounds", 4))):
            for lead in open_leads:
                apply_score(lead, version, weights)
            ranked = sorted(open_leads, key=lambda l: l.priority_rank_score, reverse=True)[:ensure_top]
            missing = [l for l in ranked if l.property.current_valuation is None and l.id not in self.tried]
            if not missing:
                break
            for lead in missing:
                self._underwrite(ctx, underwriter, lead, stale_days)
            ctx.metrics[f"convergence_round_{round_no + 1}"] = len(missing)
        ctx.checkpoint()
        ctx.metrics["verdicts"] = self.verdicts

    def _underwrite(self, ctx: AgentContext, underwriter: "Underwriter", lead: Lead, stale_days: int) -> None:
        prop = lead.property
        self.tried.add(lead.id)
        cur = prop.current_valuation
        if cur and not self.options.get("force") and (utcnow() - cur.calculated_at).days < stale_days \
                and not inputs_changed(prop, cur):
            return
        try:
            val = underwriter.underwrite(ctx.session, prop)
        except Exception as exc:  # noqa: BLE001
            ctx.error(f"{prop.parcel_id}: {type(exc).__name__}: {exc}")
            return
        ctx.records_processed += 1
        if val:
            self.verdicts[val.verdict] = self.verdicts.get(val.verdict, 0) + 1
            ctx.records_updated += 1
        else:
            ctx.bump("no_valuation")
        ctx.checkpoint()


def _default_listing_provider():
    try:
        from engine.listing_comps import ListingComps
    except ImportError:
        return None
    return ListingComps.from_settings()


def main() -> None:
    parser = argparse.ArgumentParser(description="Underwrite the top leads")
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--force", action="store_true", help="re-underwrite even if a fresh valuation exists")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run = UnderwriterAgent(top=args.top, force=args.force).execute()
    print(f"underwriter: {run.status} processed={run.records_processed} errors={run.error_count} {run.metrics_json.get('verdicts')}")
    for e in (run.errors or [])[:10]:
        print("  error:", e)


if __name__ == "__main__":
    main()
