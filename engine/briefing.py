"""Daily-briefing sections shared by `manage.py briefing` and the cockpit.

(A) Ready to call now — underwritten deals with a live phone or listing-agent e-mail, with the ask-to-MAO gap;
(B) high-spread sheriff-sale / estate deals;
(C) stacked distress ([2x]/[3x] STACKED) and portfolio owners.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from db.models import DistressSignal, Lead, LeadStage, Property, SignalType, Valuation
from engine.crm import best_phone
from engine.scoring import listing_terms, signal_families, stack_badge

OPEN = [LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED]
DEAL_VERDICTS = ("STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT")
ESTATE_TYPES = (SignalType.ESTATE_OWNER, SignalType.PROBATE)


def _base(lead: Lead) -> dict[str, Any]:
    p = lead.property
    v = p.current_valuation
    return {
        "lead_id": lead.id, "address": p.street_address, "town": p.municipality or p.city, "county": p.county,
        "zip": p.zip_code, "priority": lead.priority_rank_score, "motivation": lead.motivation_score,
        "viability": lead.deal_viability_score, "stage": lead.status_stage, "badge": stack_badge(p),
        "families": signal_families(p), "verdict": v.verdict if v else None, "arv": v.arv_estimate if v else None,
        "mao_70": v.mao_conservative if v else None, "mao_75": v.mao_aggressive if v else None,
        "anchor": v.anchor_offer if v else None, "fee": v.target_assignment_fee if v else None,
        "spread": v.deal_spread if v else None, "frfpa_track": lead.frfpa_track,
    }


def reach(session: Session, lead: Lead) -> dict[str, Any]:
    """Who to contact first: a callable phone (not DNC), else a listing agent's e-mail."""
    c = best_phone(session, lead)
    if c is not None:
        return {"contact_id": c.id, "who": c.name, "role": c.contact_type, "phone": c.phone, "email": c.email}
    agent = next((c for c in lead.contacts if c.contact_type == "LISTING_AGENT" and c.email and c.dnc_status != "dnc"), None)
    if agent is not None:
        return {"contact_id": agent.id, "who": agent.name, "role": agent.contact_type, "phone": None, "email": agent.email}
    return {"contact_id": None, "who": None, "role": None, "phone": None, "email": None}


def ready_rows(session: Session, top: int = 15) -> list[dict[str, Any]]:
    leads = session.scalars(select(Lead).where(Lead.ready_to_call.is_(True), Lead.status_stage.not_in(OPEN))
                            .order_by(Lead.priority_rank_score.desc()).limit(top)
                            .options(selectinload(Lead.contacts), selectinload(Lead.property))).all()
    rows = []
    for lead in leads:
        row = _base(lead) | reach(session, lead)
        gap = listing_terms(lead.property)
        row |= {"via": (lead.score_breakdown_json or {}).get("ready_via"), "ask": gap.get("list_price"),
                "list_to_mao_70": gap.get("list_to_mao_70"), "list_to_mao_75": gap.get("list_to_mao_75"),
                "discount_off_ask_70": gap.get("discount_off_ask_70"), "striking_distance": gap.get("striking_distance")}
        sheriff = next((s for s in lead.property.active_signals if s.signal_type == SignalType.SHERIFF_SALE), None)
        row["sale_date"] = sheriff.scheduled_auction_date if sheriff else None
        row["upset"] = sheriff.amount_owed if sheriff else None
        rows.append(row)
    return rows


MAX_SPREAD_ARV = 2_000_000      # above this a public-record ARV is usually a whole complex / commercial parcel
MAX_ARV_PER_SQFT = 1_000


def plausible_house(prop: Property, val: Valuation) -> bool:
    """1–4 family residential with a believable ARV — keeps whole-complex and commercial parcels out of section B."""
    if (prop.property_class or "2") != "2" or (prop.dwellings or 1) > 4 or not val.arv_estimate:
        return False
    if val.arv_estimate > MAX_SPREAD_ARV:
        return False
    sqft = prop.sqft or val.sqft_used
    return not (sqft and val.arv_estimate / sqft > MAX_ARV_PER_SQFT)


def high_spread_rows(session: Session, top: int = 10) -> list[dict[str, Any]]:
    """Sheriff-sale and estate 1–4 family leads with a deal verdict, widest spread first."""
    distressed = (select(DistressSignal.property_id)
                  .where(DistressSignal.active.is_(True),
                         DistressSignal.signal_type.in_((SignalType.SHERIFF_SALE, *ESTATE_TYPES))))
    stmt = (select(Lead).join(Property, Lead.property_id == Property.id)
            .join(Valuation, (Valuation.property_id == Property.id) & Valuation.is_current.is_(True))
            .where(Lead.status_stage.not_in(OPEN), Valuation.verdict.in_(DEAL_VERDICTS),
                   (Property.id.in_(distressed)) | Property.is_estate_owned.is_(True))
            .order_by(Valuation.deal_spread.desc().nulls_last())
            .options(selectinload(Lead.contacts), selectinload(Lead.property)))
    rows = []
    for lead in session.scalars(stmt).unique():
        if len(rows) >= top:
            break
        if not plausible_house(lead.property, lead.property.current_valuation):
            continue
        row = _base(lead) | reach(session, lead)
        sheriff = next((s for s in lead.property.active_signals if s.signal_type == SignalType.SHERIFF_SALE), None)
        row |= {"kind": "estate" if "estate" in row["families"] else "sheriff sale",
                "sale_date": sheriff.scheduled_auction_date if sheriff else None,
                "upset": sheriff.amount_owed if sheriff else None}
        rows.append(row)
    return rows


def _stack_candidates(session: Session) -> list[int]:
    """Property ids with two or more active signal types (families are resolved in Python)."""
    stmt = (select(DistressSignal.property_id).where(DistressSignal.active.is_(True))
            .group_by(DistressSignal.property_id).having(func.count(func.distinct(DistressSignal.signal_type)) >= 2))
    ids = set(session.scalars(stmt))
    one_signal_estates = select(Property.id).where(Property.is_estate_owned.is_(True))
    return sorted(ids | set(session.scalars(one_signal_estates)))


def stacked_rows(session: Session, top: int = 10, min_families: int = 2) -> list[dict[str, Any]]:
    ids = _stack_candidates(session)
    leads = session.scalars(select(Lead).where(Lead.property_id.in_(ids), Lead.status_stage.not_in(OPEN))
                            .order_by(Lead.priority_rank_score.desc())
                            .options(selectinload(Lead.contacts), selectinload(Lead.property))).all()
    rows = []
    for lead in leads:
        fams = signal_families(lead.property)
        if len(fams) < min_families:
            continue
        row = _base(lead) | reach(session, lead)
        port = next((s for s in lead.property.active_signals if s.signal_type == SignalType.PORTFOLIO_OWNER), None)
        meta = (port.raw_metadata_json or {}) if port else {}
        row |= {"portfolio_size": meta.get("properties"), "portfolio_owner": meta.get("owner")}
        rows.append(row)
        if len(rows) >= top:
            break
    return rows


def counts(session: Session) -> dict[str, int]:
    ready = session.scalar(select(func.count(Lead.id)).where(Lead.ready_to_call.is_(True),
                                                             Lead.status_stage.not_in(OPEN))) or 0
    tally = {"ready_to_call": ready, "stacked_2x": 0, "stacked_3x_plus": 0}
    ids = _stack_candidates(session)
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        for prop in session.scalars(select(Property).where(Property.id.in_(chunk))
                                    .options(selectinload(Property.signals))):
            n = len(signal_families(prop))
            if n == 2:
                tally["stacked_2x"] += 1
            elif n >= 3:
                tally["stacked_3x_plus"] += 1
    tally["portfolio_owners"] = session.scalar(select(func.count(DistressSignal.id)).where(
        DistressSignal.signal_type == SignalType.PORTFOLIO_OWNER, DistressSignal.active.is_(True))) or 0
    return tally


def listed_rows(session: Session, top: int = 10) -> list[dict[str, Any]]:
    """Listed foreclosures (sheriff sale + MLS listing) with an agent to call, best track first."""
    from engine.fast_track import listed_foreclosures
    return [lf.to_dict() for lf in listed_foreclosures(session) if lf.agent][:top]
