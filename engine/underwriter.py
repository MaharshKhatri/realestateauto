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


# -- outlier guard ---------------------------------------------------------------------------------
@dataclass
class GuardResult:
    arv: float
    as_is: float | None
    capped: bool = False
    non_residential: bool = False
    benchmark: str | None = None
    original_arv: float | None = None
    flags: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"capped": self.capped, "non_residential": self.non_residential, "benchmark": self.benchmark,
                "original_arv": self.original_arv}


def guard_cfg() -> dict[str, Any]:
    g = {"residential_classes": ["2"], "max_arv_to_ask": 1.6, "max_arv_to_equalized": 3.5,
         "benchmark_multiple": 1.25, "max_arv_per_sqft": 850, "confidence_cap": 40,
         "non_residential_confidence_cap": 20}
    g.update(dict(settings().deal.get("outlier_guard") or {}))
    return g


def outlier_guard(*, arv: float, as_is: float | None, prop_class: str | None, ask: float | None,
                  equalized: float | None, sqft: float | None, sqft_known: bool,
                  cfg: dict[str, Any] | None = None) -> GuardResult:
    """Last line of defence against absurd residential ARVs (package-deed comps, a condo master parcel matched
    to one unit, commercial parcels valued off house sales).

    Benchmarks, in order of trust: the live asking price (ARV > 1.6x ask -> 1.25x ask), else the subject's
    equalized assessment (ARV > 3.5x -> 1.25x), and always the $/sqft ceiling when the living area is known.
    """
    g = cfg or guard_cfg()
    out = GuardResult(arv=arv, as_is=as_is, original_arv=arv)
    klass = (prop_class or "2").strip().upper()
    out.non_residential = klass not in {str(c).upper() for c in g["residential_classes"]}
    caps: list[tuple[str, float, str]] = []
    mult = float(g["benchmark_multiple"])
    if ask:
        if arv > float(g["max_arv_to_ask"]) * ask:
            caps.append(("asking price", mult * ask, f"comp ARV ${arv:,.0f} is {arv / ask:.1f}x the ${ask:,.0f} ask"))
    elif equalized and arv > float(g["max_arv_to_equalized"]) * equalized:
        caps.append(("equalized assessment", mult * equalized,
                     f"comp ARV ${arv:,.0f} is {arv / equalized:.1f}x the ${equalized:,.0f} equalized assessment"))
    if out.non_residential and not ask and equalized and arv > equalized:
        caps.append(("equalized assessment (non-residential)", equalized,
                     f"house comps can't value a class {klass} parcel"))
    if sqft_known and sqft and arv / sqft > float(g["max_arv_per_sqft"]):
        caps.append(("$/sqft ceiling", float(g["max_arv_per_sqft"]) * sqft,
                     f"comp ARV is ${arv / sqft:,.0f}/sqft on {sqft:,.0f} sqft"))
    if caps:
        name, value, why = min(caps, key=lambda c: c[1])
        value = round(value, -3)
        out.capped, out.benchmark, out.arv = True, name, value
        out.as_is = round(as_is * value / arv, -3) if as_is else None
        out.flags.append(f"OUTLIER_CAPPED: {why}; ARV capped to ${value:,.0f} from the {name} — re-check comps, "
                         "get photos and a sold-comp opinion before offering")
    if out.non_residential:
        out.flags.append(f"NON_RESIDENTIAL: property class {klass} is not a 1–4 family home"
                         + (" — the listing is probably one unit of a larger parcel; value it from unit sales"
                            if ask else " — not a wholesale target; refer to a commercial broker"))
    return out


def guarded_confidence(conf: float, guard: GuardResult, cfg: dict[str, Any] | None = None) -> float:
    g = cfg or guard_cfg()
    if guard.non_residential:
        conf = min(conf, float(g["non_residential_confidence_cap"]))
    if guard.capped:
        conf = min(conf * 0.5, float(g["confidence_cap"]))
    return round(conf, 1)


class Underwriter:
    def __init__(self, gis: NJGISClient | None = None, listing_provider: Any | None = None):
        self.gis = gis or NJGISClient()
        self.public = PublicRecordComps(self.gis)
        self.listing_provider = listing_provider
        self.cfg = dict(settings().deal)

    def value(self, prop: Property) -> tuple[ValueResult, int, bool]:
        cfg = self.cfg
        self._equalized: float | None = None
        sqft_known = bool(prop.sqft)
        sqft = prop.sqft or estimate_sqft(prop.building_desc, prop.dwellings,
                                          settings().get("rehab", {}).get("default_sqft_per_unit", 1400))
        result: ValueResult | None = None
        ask = asking_price(prop)
        if self.listing_provider is not None and prop.lat and prop.lon:
            try:
                result = self.listing_provider.value(prop=prop, sqft=sqft, cfg=cfg, value_hint=ask)
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
                distressed_pct=cfg.get("distressed_percentile", 0.30), value_hint=ask)
            self._equalized = public.subject_equalized
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
        guard = outlier_guard(arv=result.arv, as_is=result.as_is, prop_class=prop.property_class,
                              ask=asking_price(prop), equalized=self._equalized or self.equalized_value(prop),
                              sqft=sqft, sqft_known=sqft_known)
        result.arv, result.as_is = guard.arv, guard.as_is
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
        om, decision, frfpa_fields = decide_offer(prop, arv=result.arv, repairs=rehab.cost, as_is=as_is, debt=debt,
                                                  cfg=self.cfg, guard=guard)
        conf = guarded_confidence(confidence(result, sqft_known, int(self.cfg.get("min_comps", 3))), guard)
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
                "radius_miles": result.radius_used, "valued_on": date.today().isoformat(), **frfpa_fields,
                "outlier_guard": guard.to_json(), "subject_equalized": self._equalized,
                "listing": listing_gap(asking_price(prop), om.mao_conservative, om.mao_aggressive, om.arv),
            })
        session.add(val)
        prop.estimated_value = as_is
        prop.estimated_equity = equity
        session.flush()
        return val

    def equalized_value(self, prop: Property) -> float | None:
        """Assessed value x the town's sale/assessment ratio (cached per town)."""
        if not (prop.assessed_total and prop.mun_code):
            return None
        from engine.market import muni_stats
        try:
            ratio = muni_stats(self.gis, prop.mun_code, prop.municipality or "").median_ratio
        except Exception as exc:  # noqa: BLE001 - the guard still has the ask and $/sqft benchmarks
            log.info("town ratio unavailable for %s: %s", prop.mun_code, exc)
            return None
        return round(prop.assessed_total * ratio, -2) if ratio else None


def decide_offer(prop: Property, *, arv: float, repairs: float, as_is: float | None, debt: float | None,
                 cfg: dict[str, Any], guard: GuardResult | None = None
                 ) -> tuple[OfferMath, FrfpaDecision, dict[str, Any]]:
    """Offer math + FRFPA track for already-valued inputs (shared by underwrite() and reprice())."""
    decision = frfpa_decision(prop)
    frfpa = decision.applies
    om = offer_math(arv=arv, repairs=repairs, annual_taxes=prop.annual_taxes, debt=debt, cfg=cfg, frfpa=frfpa, fmv=as_is)
    if guard is not None:
        om.flags = guard.flags + om.flags
        if guard.non_residential and not asking_price(prop):
            om.verdict = "PASS"          # commercial / mixed-use parcel: no wholesale offer from house comps
            om.anchor_offer = None
    track_info: dict[str, Any] = {}
    if frfpa and om.verdict == "FRFPA_BLOCKED" and as_is:
        ct = covered_track(arv=om.arv, fmv=as_is, repairs=om.repairs, debt=debt,
                           aggressive_pct=float(cfg.get("mao_pct_aggressive", 0.75)),
                           min_fee=float(cfg.get("min_assignment_fee", 10000)))
        om.verdict = ct.track
        if ct.track != "FRFPA_BLOCKED":   # the 70%-MAO "no compliant offer" note no longer describes the deal
            om.flags = [f for f in om.flags if "no compliant wholesale offer" not in f]
        om.flags.extend(ct.notes)
        track_info = {"compliant_offer": ct.compliant_offer, "track_buyer_price": ct.buyer_price,
                      "track_fee": ct.fee, "equity_at_risk": ct.equity_at_risk}
        if ct.track == "WORKABLE_82_PCT_FLOOR":
            om.anchor_offer = ct.compliant_offer
            om.assignment_fee = ct.fee
    exempt_scenario = None
    if frfpa:   # what the deal looks like if occupancy verification shows FRFPA does not apply
        ex = offer_math(arv=arv, repairs=repairs, annual_taxes=prop.annual_taxes, debt=debt, cfg=cfg)
        exempt_scenario = {"verdict": ex.verdict, "mao_conservative": ex.mao_conservative,
                           "anchor_offer": ex.anchor_offer, "fee": ex.assignment_fee}
    if prop.lead:
        prop.lead.frfpa_track = decision.track
    fields = {"frfpa_applies": frfpa, "frfpa_floor": om.frfpa_floor, "frfpa_track": decision.track,
              "frfpa_confidence": decision.confidence, "frfpa_basis": decision.basis,
              "verify_before_contract": decision.verify_before_contract, "exempt_scenario": exempt_scenario,
              **track_info}
    return om, decision, fields


def asking_price(prop: Property) -> float | None:
    """Current list price of an active (stale/distressed) MLS listing on the property, if any."""
    sig = next((s for s in prop.active_signals if s.signal_type == SignalType.STALE_LISTING), None) or next(
        (s for s in prop.active_signals if s.signal_type == SignalType.ACTIVE_LISTING), None)
    price = (sig.raw_metadata_json or {}).get("list_price") if sig else None
    return float(price) if price else None


LISTING_ARV_CONFLICT = 1.6   # ARV above 1.6x a stale listing's ask: the comps, not the seller, are probably wrong


def listing_gap(list_price: float | None, mao_conservative: float | None, mao_aggressive: float | None,
                arv: float | None = None) -> dict[str, Any]:
    """How far our wholesale numbers sit from the seller's asking price (1.0 = we can pay full ask).

    A listing that has sat 90+ days will often take 80-92% of ask in cash; `list_to_mao_75 >= 0.75` means
    our ceiling is within realistic striking distance of the asking price.
    """
    if not list_price or list_price <= 0:
        return {}
    out: dict[str, Any] = {"list_price": round(list_price)}
    if mao_conservative is not None:
        out["list_to_mao_70"] = round(mao_conservative / list_price, 3)
        out["discount_off_ask_70"] = round(1 - mao_conservative / list_price, 3)
    if mao_aggressive is not None:
        out["list_to_mao_75"] = round(mao_aggressive / list_price, 3)
    if arv:
        out["arv_to_list"] = round(arv / list_price, 2)
    out["valuation_conflict"] = bool(arv and arv > LISTING_ARV_CONFLICT * list_price)
    out["striking_distance"] = bool(mao_aggressive is not None and mao_aggressive >= 0.75 * list_price
                                    and not out["valuation_conflict"])
    return out


FRFPA_FIELDS = ("frfpa_applies", "frfpa_floor", "frfpa_track", "frfpa_confidence", "frfpa_basis",
                "verify_before_contract", "exempt_scenario", "compliant_offer", "track_buyer_price", "track_fee",
                "equity_at_risk")


def reprice(session: Session, prop: Property) -> Valuation | None:
    """Re-run the offer math on the current valuation's ARV/repairs/as-is without re-pulling comps.

    Used when a fact that only affects the offer changes — e.g. the operator confirms occupancy on a call,
    which can move a lead from the 82% FRFPA floor to a standard 70% MAO (or the reverse).
    """
    cur = prop.current_valuation
    if cur is None or not cur.arv_estimate:
        return None
    a = dict(cur.assumptions_json or {})
    arv = a.get("arv_before_tax_drag") or cur.arv_estimate
    cfg = dict(settings().deal)
    debt = known_debt(prop)
    g = a.get("outlier_guard") or {}
    guard = GuardResult(arv=arv, as_is=cur.as_is_value, capped=bool(g.get("capped")),
                        non_residential=bool(g.get("non_residential")), benchmark=g.get("benchmark"),
                        original_arv=g.get("original_arv"),
                        flags=[f for f in (a.get("flags") or []) if f.startswith(("OUTLIER_CAPPED", "NON_RESIDENTIAL"))])
    om, _, fields = decide_offer(prop, arv=arv, repairs=cur.est_repair_cost or 0, as_is=cur.as_is_value, debt=debt,
                                 cfg=cfg, guard=guard)
    for k in FRFPA_FIELDS:
        a.pop(k, None)
    a.update(fields)
    a.update({"buyer_purchase_price": om.buyer_purchase_price, "buyer_profit": om.buyer_profit,
              "buyer_roi": om.buyer_roi, "flags": om.flags, "repriced_from": cur.id,
              "listing": listing_gap(asking_price(prop), om.mao_conservative, om.mao_aggressive, om.arv)})
    cur.is_current = False
    val = Valuation(
        property=prop, arv_estimate=om.arv, as_is_value=cur.as_is_value, est_repair_cost=om.repairs,
        rehab_tier=cur.rehab_tier, target_assignment_fee=om.assignment_fee, mao_conservative=om.mao_conservative,
        mao_aggressive=om.mao_aggressive, anchor_offer=om.anchor_offer, confidence_score=cur.confidence_score,
        comps_json=cur.comps_json, comp_count=cur.comp_count, comp_source=cur.comp_source, sqft_used=cur.sqft_used,
        effective_tax_rate=om.effective_tax_rate, tax_drag_adjustment=om.tax_drag_adjustment, debt_estimate=debt,
        equity_estimate=cur.equity_estimate, deal_spread=om.deal_spread, verdict=om.verdict, is_current=True,
        calculated_at=utcnow(), assumptions_json=a)
    session.add(val)
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
        scope = self.options.get("scope", "top")      # top | valued | sheriff | stale_listing
        listing = None if self.options.get("public_only") else (
            self.options.get("listing_provider") or _default_listing_provider())
        underwriter = Underwriter(listing_provider=listing)
        q = select(Lead).where(Lead.status_stage.notin_(["DEAD", "DO_NOT_CONTACT", "CLOSED"]))
        if scope == "valued":
            q = q.join(Valuation, Valuation.property_id == Lead.property_id).where(Valuation.is_current.is_(True))
        elif scope == "sheriff":
            q = q.join(DistressSignal, DistressSignal.property_id == Lead.property_id).where(
                DistressSignal.signal_type == SignalType.SHERIFF_SALE, DistressSignal.active.is_(True))
        elif scope == "stale_listing":
            q = q.join(DistressSignal, DistressSignal.property_id == Lead.property_id).where(
                DistressSignal.signal_type == SignalType.STALE_LISTING, DistressSignal.active.is_(True))
        lead_ids = [int(i) for i in (self.options.get("lead_ids") or [])]
        if lead_ids:
            q = q.where(Lead.id.in_(lead_ids))
        q = q.order_by(Lead.priority_rank_score.desc(), Lead.motivation_score.desc())
        if scope == "top" and not lead_ids:
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
