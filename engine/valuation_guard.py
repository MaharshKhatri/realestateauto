"""Underwriter Outlier Guard: re-checks every current valuation against comp hygiene and the subject guard.

The underwriter applies both on every new valuation. This agent brings *existing* valuations up to the same
standard without re-pulling comps:

  1. the stored comps are cleaned (package deeds = one price + date on several parcels, the residential price
     band, the $/sqft band, the sale-to-assessment band) and the ARV / as-is are recomputed from what survives;
  2. the subject guard runs (ARV vs live ask, vs equalized assessment, $/sqft ceiling, non-residential class);
  3. if anything changed, a new current valuation is written (offer math, FRFPA track and flags redone);
  4. when too few clean comps survive, the valuation keeps its comps but is flagged COMPS_NEED_REFRESH and up to
     `max_live` of those leads are re-underwritten live (NJGIN + listing comps) per run.

Run: python -m swarm.orchestrator run outlier_guard
"""
from __future__ import annotations

import re
from collections import defaultdict
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from agents.base import AgentContext, BaseAgent
from core.config import settings
from db.models import Lead, LeadStage, Property, Valuation, utcnow
from engine.comps import CompHygiene, value_from_metric
from engine.underwriter import (GuardResult, Underwriter, asking_price, decide_offer, guarded_confidence, known_debt,
                                listing_gap, outlier_guard)

PUBLIC = "public_record_equalized_ratio"
LISTING = "listing_price_per_sqft"
EQ_NOTE = re.compile(r"subject equalized value \$([\d,]+)")


def stored_equalized(val: Valuation) -> float | None:
    a = val.assumptions_json or {}
    if a.get("subject_equalized"):
        return float(a["subject_equalized"])
    for note in a.get("comp_notes") or []:
        m = EQ_NOTE.search(note)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def clean_stored_comps(comps: list[dict[str, Any]], hygiene: CompHygiene,
                       value_hint: float | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Apply comp hygiene to comps saved on a valuation (comps_json)."""
    addresses: dict[tuple[Any, Any], set[str]] = defaultdict(set)
    for c in comps:
        addresses[(c.get("sold_price"), c.get("sold_date"))].add(str(c.get("address")))
    packages = {k for k, addrs in addresses.items() if k[0] and k[1] and len(addrs) >= 2}
    dropped = {"package deed": 0, "price band": 0, "$/sqft band": 0, "sale/assessment band": 0}
    kept: list[dict[str, Any]] = []
    for c in comps:
        price = float(c.get("sold_price") or 0)
        if (c.get("sold_price"), c.get("sold_date")) in packages:
            dropped["package deed"] += 1
        elif not hygiene.price_ok(price, value_hint):
            dropped["price band"] += 1
        elif not hygiene.ppsf_ok(price, c.get("sqft")):
            dropped["$/sqft band"] += 1
        elif c.get("normalized_ratio") is not None and not hygiene.ratio_ok(float(c["normalized_ratio"])):
            dropped["sale/assessment band"] += 1
        else:
            kept.append(c)
    return kept, dropped


def recompute(comps: list[dict[str, Any]], method: str, base: float) -> tuple[float, float] | None:
    """ARV / as-is from cleaned stored comps with the same percentile math the providers use."""
    metric = "normalized_ratio" if method == PUBLIC else "price_per_sqft"
    rows = [SimpleNamespace(**{metric: float(c[metric])}) for c in comps if c.get(metric)]
    if not rows or not base:
        return None
    cfg = settings().deal
    arv_m, as_is_m, *_ = value_from_metric(rows, metric, 1.0, cfg.get("renovated_percentile", 0.70),
                                           cfg.get("distressed_percentile", 0.30))
    return round(arv_m * base, -3), round(as_is_m * base, -3)


def rebuild(session: Session, prop: Property, cur: Valuation, *, arv: float, as_is: float | None,
            comps: list[dict[str, Any]], guard: GuardResult, confidence: float, notes: list[str]) -> Valuation:
    """A new current valuation from corrected ARV / as-is (same repairs; offer math and FRFPA redone)."""
    a = dict(cur.assumptions_json or {})
    debt = known_debt(prop)
    om, _, fields = decide_offer(prop, arv=arv, repairs=cur.est_repair_cost or 0, as_is=as_is, debt=debt,
                                 cfg=dict(settings().deal), guard=guard)
    a.update(fields)
    a.update({"arv_before_tax_drag": om.arv_before_tax_drag, "buyer_purchase_price": om.buyer_purchase_price,
              "buyer_profit": om.buyer_profit, "buyer_roi": om.buyer_roi, "flags": om.flags,
              "outlier_guard": guard.to_json(), "guarded_from": cur.id,
              "comp_notes": list(a.get("comp_notes") or []) + notes,
              "listing": listing_gap(asking_price(prop), om.mao_conservative, om.mao_aggressive, om.arv)})
    as_is = min(as_is or 0, om.arv) or None
    cur.is_current = False
    val = Valuation(
        property=prop, arv_estimate=om.arv, as_is_value=as_is, est_repair_cost=om.repairs, rehab_tier=cur.rehab_tier,
        target_assignment_fee=om.assignment_fee, mao_conservative=om.mao_conservative,
        mao_aggressive=om.mao_aggressive, anchor_offer=om.anchor_offer, confidence_score=confidence,
        comps_json=comps, comp_count=len(comps), comp_source=cur.comp_source, sqft_used=cur.sqft_used,
        effective_tax_rate=om.effective_tax_rate, tax_drag_adjustment=om.tax_drag_adjustment, debt_estimate=debt,
        equity_estimate=cur.equity_estimate, deal_spread=om.deal_spread, verdict=om.verdict, is_current=True,
        calculated_at=utcnow(), assumptions_json=a)
    session.add(val)
    prop.estimated_value = as_is
    session.flush()
    return val


def reguard(session: Session, prop: Property, underwriter: Underwriter | None = None,
            hygiene: CompHygiene | None = None) -> str:
    """'unchanged' | 'repriced' | 'needs_underwrite' (too few clean comps; flagged and guarded meanwhile)."""
    cur = prop.current_valuation
    if cur is None or not cur.arv_estimate:
        return "unchanged"
    hygiene = hygiene or CompHygiene.from_settings()
    a = cur.assumptions_json or {}
    prior = a.get("outlier_guard")
    ask = asking_price(prop)
    equalized = stored_equalized(cur) or (underwriter.equalized_value(prop) if underwriter else None)
    method = cur.comp_source or ""
    comps = list(cur.comps_json or [])
    cleaned, dropped = clean_stored_comps(comps, hygiene, max(ask or 0, equalized or 0) or None)
    arv = float((prior or {}).get("original_arv") or a.get("arv_before_tax_drag") or cur.arv_estimate) \
        if (prior or {}).get("capped") else float(a.get("arv_before_tax_drag") or cur.arv_estimate)
    as_is = cur.as_is_value
    if (prior or {}).get("capped") and as_is and (prior or {}).get("original_arv"):
        as_is = as_is * float(prior["original_arv"]) / float(a.get("arv_before_tax_drag") or cur.arv_estimate)
    notes: list[str] = []
    status = "repriced"
    removed = sum(dropped.values())
    conf = float(cur.confidence_score or 0)
    min_comps = int(settings().deal.get("min_comps", 3))
    if removed and method in (PUBLIC, LISTING):
        base = equalized if method == PUBLIC else cur.sqft_used
        redone = recompute(cleaned, method, base or 0) if len(cleaned) >= min_comps else None
        notes.append("outlier guard: comp hygiene dropped " + ", ".join(f"{n} {k}" for k, n in dropped.items() if n))
        if redone:
            arv, as_is = redone
            comps = cleaned
            conf *= (len(cleaned) / max(len(cur.comps_json or []), 1)) ** 0.5
        else:
            status = "needs_underwrite"
            conf *= 0.7
    elif removed:
        notes.append(f"outlier guard: {removed} stored comp(s) fail hygiene — blended valuation, refresh comps")
        status = "needs_underwrite"
    guard = outlier_guard(arv=arv, as_is=as_is, prop_class=prop.property_class, ask=ask, equalized=equalized,
                          sqft=cur.sqft_used, sqft_known=a.get("sqft_source") == "record")
    stable = (prior is not None and abs(guard.arv - float(a.get("arv_before_tax_drag") or cur.arv_estimate)) < 1000
              and guard.capped == bool(prior.get("capped"))
              and guard.non_residential == bool(prior.get("non_residential")))
    if status == "needs_underwrite":
        if stable and a.get("comps_need_refresh"):
            return "needs_underwrite"        # already flagged and guarded; waiting for a live re-underwrite
        guard.flags.append(f"COMPS_NEED_REFRESH: {removed} of {len(cur.comps_json or [])} comps failed hygiene")
    elif stable and not removed:
        return "unchanged"
    if prior is None and not removed and not guard.capped and not guard.non_residential:
        # Never guarded before but nothing to fix: just record that it passed.
        cur.assumptions_json = {**a, "outlier_guard": guard.to_json()}
        session.flush()
        return "unchanged"
    val = rebuild(session, prop, cur, arv=guard.arv, as_is=guard.as_is, comps=comps, guard=guard,
                  confidence=guarded_confidence(conf, guard), notes=notes)
    va = dict(val.assumptions_json)
    va.pop("comps_need_refresh", None)
    if status == "needs_underwrite":
        va["comps_need_refresh"] = removed
    val.assumptions_json = va
    session.flush()
    return status


class OutlierGuardAgent(BaseAgent):
    name = "outlier_guard"
    description = "Re-checks every valuation: comp hygiene, ARV vs ask/assessment/$-per-sqft, non-residential."

    def run(self, ctx: AgentContext) -> None:
        s = ctx.session
        hygiene = CompHygiene.from_settings()
        underwriter = Underwriter() if not self.options.get("offline") else None
        stmt = (select(Lead).join(Valuation, (Valuation.property_id == Lead.property_id) & Valuation.is_current.is_(True))
                .where(Lead.status_stage.notin_([LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED]))
                .order_by(Lead.priority_rank_score.desc())
                .options(selectinload(Lead.property).selectinload(Property.valuations)))
        refresh: list[Lead] = []
        for lead in s.scalars(stmt).unique():
            try:
                status = reguard(s, lead.property, underwriter, hygiene)
            except Exception as exc:  # noqa: BLE001
                ctx.error(f"{lead.property.parcel_id}: {type(exc).__name__}: {exc}")
                continue
            ctx.records_processed += 1
            ctx.bump(status)
            if status != "unchanged":
                ctx.records_updated += 1
            if status == "needs_underwrite":
                refresh.append(lead)
        ctx.checkpoint()
        live = int(self.options.get("max_live", 25))
        if refresh and live and underwriter is not None:
            from engine.underwriter import _default_listing_provider
            uw = Underwriter(listing_provider=_default_listing_provider())
            for lead in refresh[:live]:
                try:
                    if uw.underwrite(s, lead.property):
                        ctx.bump("re_underwritten")
                except Exception as exc:  # noqa: BLE001
                    ctx.error(f"{lead.property.parcel_id}: {type(exc).__name__}: {exc}")
                ctx.checkpoint()
        remaining = s.scalar(select(Valuation.id).where(Valuation.is_current.is_(True),
                                                        Valuation.arv_estimate > 1_800_000).limit(1))
        ctx.metrics["refresh_queue"] = max(len(refresh) - live, 0)
        ctx.note(f"Outlier guard: {ctx.metrics.get('repriced', 0)} repriced, {ctx.metrics.get('needs_underwrite', 0)} "
                 f"need fresh comps ({ctx.metrics.get('re_underwritten', 0)} re-underwritten live)"
                 + ("; some valuations still above $1.8M — affluent-town houses or non-residential (PASS)"
                    if remaining else ""))
