"""Local CRM cockpit: `python manage.py server` → http://127.0.0.1:8000

FastAPI JSON API + a single dark-mode page (web/static). Everything runs on the operator's laptop against
wholesaling.db; nothing is exposed beyond 127.0.0.1. A Host/Origin guard blocks other websites (DNS
rebinding / cross-site form posts) from reading or writing the lead data through the browser.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from agents.enrichment.skiptrace_pipeline import import_text, lookup_links, skiptrace_target
from core.config import settings
from core.text import format_phone
from db.database import session_scope
from db.models import (CallOutcome, CashBuyer, Contact, ContactType, DistressSignal, GeneratedDocument, Lead,
                       Occupancy, Property, SignalType, Valuation)
from engine import compliance, crm
from engine.contracts import DEALS_DIR, DOC_TYPES, ContractBlocked, contract_gate, default_price, generate
from engine.frfpa_classifier import COVERED_TRACKS, covered_track
from engine.scoring import stack_badge
from engine.scripts import NO_OFFER_VERDICTS, build_battle_card, build_buyer_pitch
from engine import quant_pricing as qp
from engine.underwriter import frfpa_decision, offer_math, quant_overlay

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
DEAL_VERDICTS = ("STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT")
TABS = {
    "all": "All open", "callable": "Callable now", "sheriff": "Sheriff sales", "estate": "Estates",
    "listings": "Stale listings", "deals": "Deals (offer verdict)", "frfpa": "Verify occupancy",
    "followups": "Follow-ups", "pipeline": "In pipeline", "ready": "Ready to call", "stacked": "Stacked distress",
}

app = FastAPI(title="NJ Wholesale Cockpit", docs_url="/api/docs", redoc_url=None)


def public_hosts() -> set[str]:
    """Tunnel / public host names allowed to reach ONLY the inbound site and its form posts (web.public_hosts)."""
    return {str(h).lower() for h in (settings().get("web", {}) or {}).get("public_hosts") or []}


def public_path(request: Request) -> bool:
    path = request.url.path
    return path.startswith("/site/") or (request.method == "POST" and path in ("/api/inbound/seller",
                                                                               "/api/inbound/buyer"))


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host in public_hosts() and public_path(request):
        return await call_next(request)
    if host not in ALLOWED_HOSTS and host != "testserver":
        return JSONResponse({"detail": "cockpit is local-only"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and request.method != "GET":
        o_host = origin.split("://", 1)[-1].split(":")[0].split("/")[0]
        if o_host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "cross-origin write refused"}, status_code=403)
    return await call_next(request)


def db() -> Iterator[Session]:
    with session_scope() as session:
        yield session


def get_lead(session: Session, lead_id: int) -> Lead:
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, f"lead {lead_id} not found")
    return lead


def iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (date, datetime)) else v


# -- serializers ------------------------------------------------------------------------------------
def lead_row(session: Session, lead: Lead) -> dict[str, Any]:
    p = lead.property
    v = p.current_valuation
    sd = crm.sale_date(lead)
    reasons = (lead.score_breakdown_json or {}).get("reasons") or []
    return {
        "id": lead.id, "property_id": p.id, "address": (p.street_address or "").title(),
        "town": (p.city or p.municipality or "").title(), "county": p.county, "zip": p.zip_code,
        "priority": round(lead.priority_rank_score or 0, 1), "motivation": round(lead.motivation_score or 0),
        "viability": round(lead.deal_viability_score or 0), "stage": lead.status_stage,
        "source": lead.primary_source, "verdict": v.verdict if v else None, "frfpa_track": lead.frfpa_track,
        "arv": v.arv_estimate if v else None, "anchor": v.anchor_offer if v else None,
        "mao": v.mao_conservative if v else None, "fee": v.target_assignment_fee if v else None,
        "sale_date": iso(sd), "days_to_sale": (sd - date.today()).days if sd else None,
        "next_followup_at": iso(lead.next_followup_at), "attempts": lead.contact_attempts or 0,
        "contact_status": crm.contact_status(session, lead), "reasons": reasons[:3],
        "owner": (p.owner_name or "").title() or None, "badge": stack_badge(p), "ready": bool(lead.ready_to_call),
    }


def contact_card(session: Session, lead: Lead, c: Contact) -> dict[str, Any]:
    ok, why = compliance.can_call(session, c) if c.phone else (False, "no phone")
    return {
        "id": c.id, "type": c.contact_type, "name": c.name, "phone": c.phone,
        "phone_display": format_phone(c.phone) if c.phone else None, "phone_type": c.phone_type,
        "dnc_status": c.dnc_status, "callable": ok, "call_note": why, "email": c.email, "company": c.company,
        "mailing_address": c.mailing_address, "source": c.source, "confidence": c.confidence_score, "notes": c.notes,
        "links": {k: v for k, v in (c.raw_json or {}).items() if isinstance(v, str) and v.startswith("http")},
    }


def signal_dict(s: DistressSignal) -> dict[str, Any]:
    meta = s.raw_metadata_json or {}
    return {"type": s.signal_type, "auction_date": iso(s.scheduled_auction_date), "amount": s.amount_owed,
            "plaintiff": s.plaintiff_or_lienholder, "defendant": s.defendant, "case": s.case_number,
            "status": s.status, "source": s.source, "url": s.source_url or meta.get("url"),
            "adjournments": meta.get("adjournments_total"), "dom": meta.get("days_on_market"),
            "list_price": meta.get("list_price")}


def valuation_dict(v: Valuation | None) -> dict[str, Any] | None:
    if v is None:
        return None
    a = v.assumptions_json or {}
    return {"id": v.id, "arv": v.arv_estimate, "arv_before_tax_drag": a.get("arv_before_tax_drag") or v.arv_estimate,
            "as_is": v.as_is_value, "repairs": v.est_repair_cost, "rehab_tier": v.rehab_tier,
            "rehab_by_tier": a.get("rehab_by_tier"), "fee": v.target_assignment_fee, "mao": v.mao_conservative,
            "mao_aggressive": v.mao_aggressive, "anchor": v.anchor_offer, "confidence": v.confidence_score,
            "comp_count": v.comp_count, "comp_source": v.comp_source, "debt": v.debt_estimate,
            "equity": v.equity_estimate, "spread": v.deal_spread, "verdict": v.verdict,
            "effective_tax_rate": v.effective_tax_rate, "flags": a.get("flags") or [],
            "comp_notes": a.get("comp_notes") or [], "frfpa_floor": a.get("frfpa_floor"),
            "compliant_offer": a.get("compliant_offer"), "exempt_scenario": a.get("exempt_scenario"),
            "valued_on": a.get("valued_on"), "quant": a.get("quant"), "listing": a.get("listing"),
            "pricing_model": a.get("pricing_model") or ("quant" if a.get("quant") else "fixed_pct"),
            "comps": sorted(v.comps_json or [], key=lambda c: c.get("distance_mi") or 9)[:8]}


# -- pages ------------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
SITE = Path(__file__).resolve().parent.parent / "site"


@app.get("/site/seller", include_in_schema=False)
def site_seller() -> FileResponse:
    return FileResponse(SITE / "index.html")


@app.get("/site/buyers", include_in_schema=False)
def site_buyers() -> FileResponse:
    return FileResponse(SITE / "buyers.html")


@app.get("/site/config.js", include_in_schema=False)
def site_config_js() -> Response:
    from engine.inbound import site_config
    return Response("window.SITE_CONFIG = " + json.dumps(site_config(mode="local")) + ";",
                    media_type="application/javascript")


@app.get("/deals/{property_id}/{filename}", include_in_schema=False)
def deal_file(property_id: int, filename: str) -> FileResponse:
    path = (DEALS_DIR / str(property_id) / filename).resolve()
    if not path.is_relative_to(DEALS_DIR.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


# -- dashboard --------------------------------------------------------------------------------------
@app.get("/api/dashboard")
def dashboard(session: Session = Depends(db)) -> dict[str, Any]:
    queue = crm.call_queue(session)
    countdown = crm.sale_countdown(session)
    open_q = crm.open_leads_stmt()
    open_total = session.scalar(select(func.count()).select_from(open_q.subquery()))
    top100 = session.scalars(open_q.order_by(Lead.priority_rank_score.desc()).limit(100)
                             .options(selectinload(Lead.contacts))).all()
    ready = sum(1 for lead in top100 if crm.best_phone(session, lead))
    verdicts = dict(session.execute(select(Valuation.verdict, func.count()).where(Valuation.is_current.is_(True))
                                    .group_by(Valuation.verdict)).all())
    return {
        "operator": settings().operator.get("name"), "company": settings().operator.get("company_name"),
        "funnel": crm.funnel(session), "open_leads": open_total,
        "kpis": {"contact_ready_top100": ready, "deals": sum(verdicts.get(v, 0) for v in DEAL_VERDICTS),
                 "ready_to_call": session.scalar(select(func.count()).select_from(
                     open_q.where(Lead.ready_to_call.is_(True)).subquery())),
                 "verify_occupancy": sum(1 for _ in session.scalars(open_q.where(Lead.frfpa_track.in_(COVERED_TRACKS)))),
                 "followups_due": len(crm.due_followups(session)), "sales_21d": len(countdown)},
        "verdicts": verdicts,
        "call_queue": [{**lead_row(session, i.lead), "reason": i.reason, "due": iso(i.due),
                        "contact": contact_card(session, i.lead, i.contact) if i.contact else None} for i in queue],
        "countdown": [{**lead_row(session, lead), "sale_date": iso(d), "days_to_sale": n} for lead, d, n in countdown],
        "dnc": {**compliance.ftc_data_status(session), "calling_hours": compliance.within_calling_hours()},
        "tabs": TABS, "outcomes": crm.OUTCOME_LABELS, "occupancy": list(Occupancy.__members__),
    }


@app.get("/api/leads")
def leads(tab: str = "all", q: str | None = None, limit: int = Query(100, le=500), offset: int = 0,
          session: Session = Depends(db)) -> dict[str, Any]:
    stmt = crm.open_leads_stmt().join(Property, Lead.property_id == Property.id)

    def has_signal(*types: str):
        return exists().where(DistressSignal.property_id == Property.id, DistressSignal.active.is_(True),
                              DistressSignal.signal_type.in_(types))

    if tab == "callable":
        stmt = stmt.where(exists().where(Contact.lead_id == Lead.id, Contact.phone.is_not(None),
                                         crm.not_dnc(), Contact.confidence_score > 0))
    elif tab == "sheriff":
        stmt = stmt.where(has_signal(SignalType.SHERIFF_SALE))
    elif tab == "estate":
        stmt = stmt.where(or_(Property.is_estate_owned.is_(True), has_signal(SignalType.ESTATE_OWNER, SignalType.PROBATE),
                              exists().where(Contact.lead_id == Lead.id,
                                             Contact.contact_type == ContactType.HEIR_EXECUTOR)))
    elif tab == "listings":
        stmt = stmt.where(has_signal(SignalType.STALE_LISTING))
    elif tab == "deals":
        stmt = stmt.where(exists().where(Valuation.property_id == Property.id, Valuation.is_current.is_(True),
                                         Valuation.verdict.in_(DEAL_VERDICTS)))
    elif tab == "frfpa":
        stmt = stmt.where(Lead.frfpa_track.in_(COVERED_TRACKS))
    elif tab == "followups":
        stmt = stmt.where(Lead.next_followup_at.is_not(None))
    elif tab == "pipeline":
        stmt = stmt.where(Lead.status_stage.notin_(["NEW", "SKIP_TRACE"]))
    elif tab == "ready":
        stmt = stmt.where(Lead.ready_to_call.is_(True))
    elif tab == "stacked":
        multi = (select(DistressSignal.property_id).where(DistressSignal.active.is_(True))
                 .group_by(DistressSignal.property_id)
                 .having(func.count(func.distinct(DistressSignal.signal_type)) >= 2))
        stmt = stmt.where(Property.id.in_(multi))
    elif tab != "all":
        raise HTTPException(400, f"unknown tab {tab!r}")
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Property.street_address.ilike(like), Property.city.ilike(like),
                              Property.municipality.ilike(like), Property.owner_name.ilike(like),
                              Property.parcel_id.ilike(like)))
    total = session.scalar(select(func.count()).select_from(stmt.subquery()))
    order = Lead.next_followup_at.asc() if tab == "followups" else Lead.priority_rank_score.desc()
    rows = session.scalars(stmt.order_by(order).offset(offset).limit(limit)
                           .options(selectinload(Lead.contacts))).all()
    return {"tab": tab, "total": total, "rows": [lead_row(session, lead) for lead in rows]}


# -- lead battle station ----------------------------------------------------------------------------
@app.get("/api/leads/{lead_id}")
def lead_detail(lead_id: int, contact_id: int | None = None, session: Session = Depends(db)) -> dict[str, Any]:
    lead = get_lead(session, lead_id)
    p = lead.property
    contact = session.get(Contact, contact_id) if contact_id else None
    if contact is not None and contact.lead_id != lead.id:
        raise HTTPException(400, "contact does not belong to this lead")
    card = build_battle_card(lead, contact)
    decision = frfpa_decision(p)
    target = skiptrace_target(lead)
    order = {ContactType.HEIR_EXECUTOR: 0, ContactType.OWNER: 1, ContactType.LISTING_AGENT: 2, ContactType.RELATIVE: 3,
             ContactType.ENTITY_PRINCIPAL: 4, ContactType.REGISTERED_AGENT: 5}
    contacts = sorted(lead.contacts, key=lambda c: (not c.phone, order.get(c.contact_type, 9), -(c.confidence_score or 0)))
    price = default_price(lead)
    gates = {t: {"allowed": g.allowed, "reasons": g.reasons, "warnings": g.warnings}
             for t in DOC_TYPES for g in [contract_gate(lead, t, price)]}
    docs = session.scalars(select(GeneratedDocument).where(GeneratedDocument.lead_id == lead.id)
                           .order_by(GeneratedDocument.created_at.desc()).limit(20)).all()
    from engine import agent_outreach as ao
    from engine import deal_structure as ds
    from engine import jv_partner_vault as vault
    structure = ds.structure_for_lead(lead).to_json() if (p.current_valuation and price) else None
    return {
        "partner": vault.deal_partner_summary(session, lead), "structure": structure,
        "funnel": {"first_touch": "opener" if ao.opener_enabled() else "offer", "last_touch": ao.last_touch(session, lead),
                   "touches": ao.prior_touches(session, lead), "last_counter_amount": lead.last_counter_amount},
        "lead": {**lead_row(session, lead), "occupancy": lead.occupancy_status, "occupancy_source": lead.occupancy_source,
                 "occupancy_confirmed_at": iso(lead.occupancy_confirmed_at), "offer_amount": lead.offer_amount,
                 "contract_date": iso(lead.contract_date), "attorney_review_ends": iso(lead.attorney_review_ends),
                 "notes": lead.notes, "brief": lead.ai_strategy_brief,
                 "score": lead.score_breakdown_json or {}},
        "property": {"parcel_id": p.parcel_id, "block": p.block, "lot": p.lot, "qual": p.qual,
                     "municipality": p.municipality, "mun_code": p.mun_code, "county": p.county,
                     "class": p.property_class, "building": p.building_desc,
                     "units": p.dwellings, "year_built": p.year_built, "sqft": p.sqft, "beds": p.beds, "baths": p.baths,
                     "lot_size": p.lot_size, "assessed": p.assessed_total, "taxes": p.annual_taxes,
                     "last_sale_date": iso(p.last_sale_date), "last_sale_price": p.last_sale_price,
                     "owner": p.owner_name, "owner_mailing": " ".join(x for x in (p.owner_mailing_address,
                                                                                   p.owner_city_state_zip) if x),
                     "absentee": p.is_absentee, "out_of_state": p.is_out_of_state, "lat": p.lat, "lon": p.lon},
        "signals": [signal_dict(s) for s in p.active_signals],
        "valuation": valuation_dict(p.current_valuation),
        "frfpa": {"track": decision.track, "applies": decision.applies, "confidence": decision.confidence,
                  "basis": decision.basis, "verify": decision.verify_before_contract},
        "contacts": [contact_card(session, lead, c) for c in contacts],
        "skiptrace": {"target": target.__dict__ if target else None, "links": lookup_links(lead, target)},
        "card": card.to_dict(), "playbook": card.playbook,
        "buyers": [{"rank": m.rank, "score": m.score, "reasons": m.reasons, "id": m.buyer.id,
                    "name": m.buyer.entity_name, "mailing": m.buyer.mailing_address, "type": m.buyer.buyer_type,
                    "purchases": m.buyer.cash_purchases_count} for m in lead.buyer_matches],
        "interactions": [{"id": i.id, "at": iso(i.occurred_at), "outcome": i.outcome, "channel": i.channel,
                          "notes": i.notes, "next": iso(i.next_followup_at), "coach": (i.debrief_json or {}).get("coach")}
                         for i in lead.interactions[:30]],
        "documents": [{"type": d.doc_type, "price": d.price, "at": iso(d.created_at),
                       "files": _doc_files(lead.property_id, d.doc_type)} for d in docs],
        "gates": gates, "default_price": price,
        "deal_cfg": {**{k: settings().deal.get(k) for k in ("mao_pct_conservative", "mao_pct_aggressive",
                                                            "anchor_discount_pct", "min_assignment_fee")},
                     "pricing_model": qp.pricing_model(), "min_roi": qp.costs()["end_buyer"]["min_roi"],
                     "min_profit": qp.costs()["end_buyer"]["min_profit"]},
        "outcomes": crm.OUTCOME_LABELS, "occupancy_values": list(Occupancy.__members__),
    }


def _doc_files(property_id: int, doc_type: str) -> dict[str, str]:
    return {ext: f"/deals/{property_id}/{doc_type}.{ext}" for ext in ("pdf", "html", "md")
            if (DEALS_DIR / str(property_id) / f"{doc_type}.{ext}").exists()}


class CalcIn(BaseModel):
    arv: float = Field(gt=0)
    repairs: float = Field(ge=0)
    debt: float | None = None
    fmv: float | None = None
    annual_taxes: float | None = None
    frfpa: bool = False
    mao_pct_conservative: float = Field(0.70, gt=0.3, lt=0.95)
    mao_pct_aggressive: float = Field(0.75, gt=0.3, lt=0.95)
    anchor_discount_pct: float = Field(0.12, ge=0, lt=0.5)
    pricing_model: str | None = Field(None, pattern="^(quant|fixed_pct)$")   # default: settings deal.pricing_model
    min_roi: float | None = Field(None, ge=0, le=0.6)          # quant: the end buyer's ROI floor
    min_profit: float | None = Field(None, ge=0, le=500000)    # quant: the end buyer's $ profit floor
    ask: float | None = Field(None, gt=0)
    mun_code: str | None = Field(None, max_length=8)
    county: str | None = Field(None, max_length=40)


@app.post("/api/calc")
def calc(body: CalcIn) -> dict[str, Any]:
    """Slider calculator: the same offer math the underwriter uses, with operator overrides."""
    cfg = {**dict(settings().deal), "mao_pct_conservative": body.mao_pct_conservative,
           "mao_pct_aggressive": body.mao_pct_aggressive, "anchor_discount_pct": body.anchor_discount_pct}
    om = offer_math(arv=body.arv, repairs=body.repairs, annual_taxes=body.annual_taxes, debt=body.debt, cfg=cfg,
                    frfpa=body.frfpa, fmv=body.fmv)
    model = body.pricing_model or qp.pricing_model(cfg)
    extra: dict[str, Any] = {}
    if model == "quant":
        om, extra = quant_overlay(om, arv=body.arv, repairs=body.repairs, annual_taxes=body.annual_taxes, debt=body.debt,
                                  ask=body.ask, mun_code=body.mun_code, county=body.county, cfg=cfg,
                                  min_roi=body.min_roi, min_profit=body.min_profit)
    out = {k: getattr(om, k) for k in ("arv", "repairs", "assignment_fee", "mao_conservative", "mao_aggressive",
                                       "anchor_offer", "buyer_purchase_price", "buyer_profit", "buyer_roi",
                                       "deal_spread", "verdict", "frfpa_floor", "flags", "tax_drag_adjustment")}
    out.update({"pricing_model": model, "quant": extra.get("quant")})
    if body.frfpa and om.verdict == "FRFPA_BLOCKED" and body.fmv:
        ct = covered_track(arv=om.arv, fmv=body.fmv, repairs=om.repairs, debt=body.debt,
                           aggressive_pct=(extra.get("quant") or {}).get("alpha_effective") or body.mao_pct_aggressive,
                           min_fee=float(settings().deal.get("min_assignment_fee", 10000)))
        if ct.track != "FRFPA_BLOCKED":
            out["flags"] = [f for f in out["flags"] if "no compliant wholesale offer" not in f]
        out.update({"verdict": ct.track, "compliant_offer": ct.compliant_offer, "track_fee": ct.fee,
                    "equity_at_risk": ct.equity_at_risk, "track_notes": ct.notes})
    if out["verdict"] in NO_OFFER_VERDICTS:
        out["anchor_offer"] = None     # no investor offer on this deal: the calculator must not suggest one
    return out


class CallIn(BaseModel):
    outcome: str
    notes: str = ""
    contact_id: int | None = None
    occupancy: str | None = None
    offer_amount: float | None = None
    debrief: dict[str, Any] = Field(default_factory=dict)
    followup_at: datetime | None = None
    use_ai: bool = False


@app.post("/api/leads/{lead_id}/calls")
def log_call(lead_id: int, body: CallIn, session: Session = Depends(db)) -> dict[str, Any]:
    lead = get_lead(session, lead_id)
    if body.outcome not in CallOutcome.__members__:
        raise HTTPException(400, f"unknown outcome {body.outcome}")
    contact = session.get(Contact, body.contact_id) if body.contact_id else None
    if contact is not None and contact.lead_id != lead.id:
        raise HTTPException(400, "contact does not belong to this lead")
    try:
        r = crm.log_call(session, lead, body.outcome, notes=body.notes, contact=contact, occupancy=body.occupancy or None,
                         offer_amount=body.offer_amount, debrief=body.debrief, followup_at=body.followup_at,
                         use_ai=body.use_ai)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"interaction_id": r.interaction_id, "stage": r.stage, "next_followup_at": iso(r.next_followup_at),
            "priority": r.priority, "dnc_added": r.dnc_added, "suggested_docs": r.suggested_docs, "coach": r.coach,
            "notes": r.notes}


class OccupancyIn(BaseModel):
    status: str
    source: str = Field(min_length=3, max_length=200)


@app.post("/api/leads/{lead_id}/occupancy")
def occupancy(lead_id: int, body: OccupancyIn, session: Session = Depends(db)) -> dict[str, Any]:
    lead = get_lead(session, lead_id)
    try:
        notes = crm.confirm_occupancy(session, lead, body.status, body.source)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"notes": notes, "frfpa_track": lead.frfpa_track, "valuation": valuation_dict(lead.property.current_valuation)}


class ContractIn(BaseModel):
    price: float = Field(gt=0)
    signed: date | None = None


@app.post("/api/leads/{lead_id}/contract")
def contract(lead_id: int, body: ContractIn, session: Session = Depends(db)) -> dict[str, Any]:
    lead = get_lead(session, lead_id)
    notes = crm.record_contract(session, lead, body.price, body.signed)
    return {"notes": notes, "stage": lead.status_stage, "attorney_review_ends": iso(lead.attorney_review_ends)}


class PasteIn(BaseModel):
    text: str = Field(min_length=5, max_length=20000)
    name: str | None = None
    role: str | None = None


@app.post("/api/leads/{lead_id}/skiptrace")
def skiptrace_paste(lead_id: int, body: PasteIn, session: Session = Depends(db)) -> dict[str, Any]:
    lead = get_lead(session, lead_id)
    parsed = import_text(session, lead, body.text, name=body.name or None, role=body.role or None)
    session.flush()
    session.refresh(lead)
    return {"parsed": parsed, "contacts": [contact_card(session, lead, c) for c in lead.contacts]}


class DocIn(BaseModel):
    price: float | None = None
    buyer_id: int | None = None
    partner: str | None = None
    split_pct: float | None = None


@app.post("/api/leads/{lead_id}/docs/{doc_type}")
def make_doc(lead_id: int, doc_type: str, body: DocIn, session: Session = Depends(db)) -> JSONResponse:
    lead = get_lead(session, lead_id)
    if doc_type not in DOC_TYPES:
        raise HTTPException(400, f"unknown document type {doc_type}")
    buyer = session.get(CashBuyer, body.buyer_id) if body.buyer_id else None
    try:
        g = generate(session, lead, doc_type, price=body.price, buyer=buyer, partner=body.partner,
                     split_pct=body.split_pct)
    except ContractBlocked as exc:
        return JSONResponse({"blocked": True, "reasons": exc.reasons}, status_code=409)
    if doc_type == "loi":
        lead.status_stage = crm.advance_stage(lead.status_stage, "OFFER_MADE")
        lead.offer_amount = g.price
    return JSONResponse({"blocked": False, "price": g.price, "warnings": g.warnings,
                         "files": _doc_files(lead.property_id, doc_type)})


class DncIn(BaseModel):
    phone: str
    source: str = "internal"
    notes: str | None = None


@app.post("/api/dnc")
def add_dnc(body: DncIn, session: Session = Depends(db)) -> dict[str, Any]:
    if body.source not in ("internal", "daniels_law", "litigator"):
        raise HTTPException(400, "source must be internal, daniels_law or litigator")
    row = compliance.add_internal(session, body.phone, source=body.source, notes=body.notes)
    if row is None:
        raise HTTPException(400, "not a valid phone number")
    return {"phone": row.phone, "source": row.source}


@app.get("/api/buyers/{buyer_id}/pitch")
def buyer_pitch(buyer_id: int, lead_id: int | None = None, session: Session = Depends(db)) -> dict[str, Any]:
    buyer = session.get(CashBuyer, buyer_id)
    if buyer is None:
        raise HTTPException(404)
    return build_buyer_pitch(buyer, get_lead(session, lead_id) if lead_id else None).to_dict()


# -- Phase 3: ready-to-call, agent LOI blast, virtual inspector + mail merge, remote disposition ----------
def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        r = {k: iso(v) for k, v in r.items()}
        r["phone_display"] = format_phone(r["phone"]) if r.get("phone") else None
        out.append(r)
    return out


@app.get("/api/briefing")
def briefing_sections(top: int = Query(15, le=100), session: Session = Depends(db)) -> dict[str, Any]:
    from engine import briefing
    return {"counts": briefing.counts(session), "ready": _rows(briefing.ready_rows(session, top)),
            "spread": _rows(briefing.high_spread_rows(session, 10)), "stacked": _rows(briefing.stacked_rows(session, 10))}


@app.get("/api/agent-outreach")
def agent_outreach_queue(top: int = Query(50, le=200), session: Session = Depends(db)) -> dict[str, Any]:
    from engine import agent_outreach as ao
    packets = [ao.build_packet(lead, generate_loi=False, session=session).to_dict()
               for lead in ao.candidates(session, top)]
    ok, why = ao.smtp_ready()
    return {"packets": packets, "smtp": {"ready": ok, "why": why},
            "sent_24h": ao.sent_last_24h(session), "daily_limit": int(ao._cfg().get("daily_email_limit", 25))}


class SendIn(BaseModel):
    channel: str = "email"          # email | sms | mailto | phone (logged after sending by hand) | smtp


@app.post("/api/agent-outreach/{lead_id}/loi")
def agent_loi(lead_id: int, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import agent_outreach as ao
    lead = get_lead(session, lead_id)
    try:
        packet = ao.build_packet(lead, session=session)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"packet": packet.to_dict(), "files": _doc_files(lead.property_id, "loi")}


@app.post("/api/agent-outreach/{lead_id}/send")
def agent_send(lead_id: int, body: SendIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import agent_outreach as ao
    lead = get_lead(session, lead_id)
    if body.channel not in ("email", "sms", "mailto", "phone", "smtp"):
        raise HTTPException(400, "channel must be email, sms, mailto, phone or smtp")
    try:
        packet = ao.build_packet(lead, session=session)
        stage = ao.send_smtp(session, lead, packet) if body.channel == "smtp" else ao.record_sent(session, lead, packet,
                                                                                                   body.channel)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"stage": stage, "kind": packet.kind, "followup_at": iso(lead.next_followup_at),
            "sent_24h": ao.sent_last_24h(session)}


class OptOutIn(BaseModel):
    contact_id: int
    notes: str = "agent asked to stop"


@app.post("/api/agent-outreach/opt-out")
def agent_opt_out(body: OptOutIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import agent_outreach as ao
    contact = session.get(Contact, body.contact_id)
    if contact is None:
        raise HTTPException(404, "contact not found")
    ao.opt_out(session, contact, body.notes)
    return {"contact_id": contact.id, "dnc_status": contact.dnc_status}


@app.get("/api/leads/{lead_id}/inspect")
def inspect_lead(lead_id: int, session: Session = Depends(db)) -> dict[str, Any]:
    from engine.virtual_inspector import build_dossier
    return build_dossier(session, get_lead(session, lead_id)).to_dict()


@app.get("/api/mail-merge")
def mail_preview(top: int = Query(50, le=500), kinds: str | None = None, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import direct_mail as dm
    wanted = set(kinds.split(",")) if kinds else None
    letters = [lt for lt in (dm.build_letter(lead) for lead in dm.select_leads(session, top, wanted)) if lt]
    batches = sorted((p.name for p in dm.MAIL_DIR.glob("*") if (p / "manifest.json").exists()), reverse=True) \
        if dm.MAIL_DIR.exists() else []
    return {"operator_ready": dm.operator_ready(), "letters": [lt.to_dict() for lt in letters], "batches": batches[:12]}


class MailIn(BaseModel):
    top: int = Field(50, ge=1, le=500)
    kinds: list[str] | None = None


def _batch_files(batch: str) -> dict[str, str]:
    from engine import direct_mail as dm
    folder = dm.MAIL_DIR / batch
    return {p.name: f"/mail/{batch}/{p.name}" for p in sorted(folder.iterdir()) if p.is_file()} if folder.exists() else {}


@app.post("/api/mail-merge")
def mail_batch(body: MailIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import direct_mail as dm
    out, letters = dm.write_batch(session, body.top, kinds=set(body.kinds) if body.kinds else None)
    return {"batch": out.name, "letters": len(letters), "proof": not dm.operator_ready(), "files": _batch_files(out.name)}


@app.post("/api/mail-merge/{batch}/mailed")
def mail_mailed(batch: str, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import direct_mail as dm
    folder = (dm.MAIL_DIR / batch).resolve()
    if not folder.is_relative_to(dm.MAIL_DIR.resolve()) or not (folder / "manifest.json").exists():
        raise HTTPException(404, "batch not found")
    try:
        return {"logged": dm.mark_mailed(session, folder)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/mail/{batch}/{filename}", include_in_schema=False)
def mail_file(batch: str, filename: str) -> FileResponse:
    from engine import direct_mail as dm
    path = (dm.MAIL_DIR / batch / filename).resolve()
    if not path.is_relative_to(dm.MAIL_DIR.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


class SuppressIn(BaseModel):
    address: str = Field(min_length=6, max_length=250)
    source: str = "internal"
    notes: str | None = None


@app.post("/api/mail-suppress")
def mail_suppress(body: SuppressIn, session: Session = Depends(db)) -> dict[str, Any]:
    if body.source not in ("internal", "daniels_law"):
        raise HTTPException(400, "source must be internal or daniels_law")
    row = compliance.suppress_address(session, body.address, body.source, body.notes)
    if row is None:
        raise HTTPException(400, "not a usable address")
    return {"address_key": row.address_key, "source": row.source}


@app.get("/api/hotzones")
def hotzones(top: int = Query(10, le=50), commercial: bool = False, session: Session = Depends(db)) -> dict[str, Any]:
    from engine.disposition import buyer_hotzones
    return {"zones": [z.to_dict() for z in buyer_hotzones(session, top, include_commercial=commercial)]}


def _bid_dict(b: Any, contract: float | None) -> dict[str, Any]:
    from engine.disposition import buyer_label
    return {"id": b.id, "buyer_id": b.buyer_id, "buyer": buyer_label(b.buyer), "status": b.status,
            "walkthrough_date": iso(b.walkthrough_date), "amount": b.bid_amount, "emd": b.emd_amount,
            "fee": (b.bid_amount - contract) if b.bid_amount and contract else None, "notes": b.notes}


@app.get("/api/leads/{lead_id}/walkthrough")
def walkthrough(lead_id: int, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import disposition as dp
    lead = get_lead(session, lead_id)
    plan = dp.walkthrough_plan(lead, dp.matched_buyers(session, lead))
    best = dp.best_bid(session, lead)
    return {"plan": {k: ([iso(x) for x in v] if isinstance(v, list) and v and isinstance(v[0], (date, datetime))
                         else iso(v)) for k, v in plan.items()},
            "bids": [_bid_dict(b, lead.offer_amount) for b in dp.bids_for(session, lead)],
            "best_bid_id": best.id if best else None, "contract_price": lead.offer_amount,
            "asking_price": dp.asking_price(lead)}


class BidIn(BaseModel):
    buyer_id: int
    action: str = "bid"             # invite | walked | bid
    amount: float | None = Field(None, gt=0)
    emd: float | None = Field(None, ge=0)
    walkthrough_at: datetime | None = None
    notes: str = ""


@app.post("/api/leads/{lead_id}/bids")
def add_bid(lead_id: int, body: BidIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import disposition as dp
    lead = get_lead(session, lead_id)
    buyer = session.get(CashBuyer, body.buyer_id)
    if buyer is None:
        raise HTTPException(404, "buyer not found")
    warnings: list[str] = []
    try:
        if body.action == "invite":
            bid = dp.invite(session, lead, buyer, body.walkthrough_at, body.notes)
        elif body.action == "walked":
            bid = dp.mark_walked(session, dp.invite(session, lead, buyer), body.walkthrough_at, body.notes)
        elif body.action == "bid":
            if body.amount is None:
                raise HTTPException(400, "amount is required for a bid")
            bid, warnings = dp.record_bid(session, lead, buyer, body.amount, body.emd, body.walkthrough_at, body.notes)
        else:
            raise HTTPException(400, "action must be invite, walked or bid")
    except dp.DispositionBlocked as exc:
        return JSONResponse({"blocked": True, "reasons": exc.reasons}, status_code=409)
    return JSONResponse({"blocked": False, "bid": _bid_dict(bid, lead.offer_amount), "warnings": warnings})


class AcceptIn(BaseModel):
    structure: str = "assignment"
    split_pct: float | None = Field(None, ge=0, le=100)
    assignee_name: str | None = None


@app.post("/api/bids/{bid_id}/accept")
def accept(bid_id: int, body: AcceptIn, session: Session = Depends(db)) -> JSONResponse:
    from db.models import BuyerBid
    from engine import disposition as dp
    bid = session.get(BuyerBid, bid_id)
    if bid is None:
        raise HTTPException(404, "bid not found")
    try:
        doc, notes = dp.accept_bid(session, bid, body.structure, body.split_pct, assignee_name=body.assignee_name)
    except dp.DispositionBlocked as exc:
        return JSONResponse({"blocked": True, "reasons": exc.reasons}, status_code=409)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    lead = get_lead(session, bid.lead_id)
    return JSONResponse({"blocked": False, "notes": notes, "stage": lead.status_stage,
                         "files": _doc_files(lead.property_id, doc.doc_type)})


# -- Phase 4: fast track (listed foreclosures + portfolio owners) -------------------------------------
@app.get("/api/fast-track")
def fast_track_queue(portfolios: int = Query(20, le=100), session: Session = Depends(db)) -> dict[str, Any]:
    from engine import fast_track as ft
    listed = []
    for lf in ft.listed_foreclosures(session):
        listed.append({**lf.to_dict(), "script": ft.agent_script(lf)})
    owners = [{**p.to_dict(), "script": ft.portfolio_script(p)} for p in ft.portfolios(session, portfolios)]
    return {"listed": listed, "portfolios": owners}


@app.post("/api/fast-track/{lead_id}/packet")
def fast_track_packet(lead_id: int, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import fast_track as ft
    lead = get_lead(session, lead_id)
    if not ft.is_listed_foreclosure(lead.property):
        raise HTTPException(400, "not a listed foreclosure")
    try:
        packet = ft.build_packet(session, lead)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"packet": packet.to_dict(), "files": _doc_files(lead.property_id, "loi") if packet.loi_files else {}}


# -- Phase 4: closing coordinator + money -------------------------------------------------------------
def _milestone_dict(m: Any) -> dict[str, Any]:
    from engine.closing_coordinator import LABELS
    return {"key": m.milestone, "label": LABELS[m.milestone], "sequence": m.sequence, "due_date": iso(m.due_date),
            "status": m.status, "completed_at": iso(m.completed_at), "notes": m.notes,
            "overdue": bool(m.status == "pending" and m.due_date and m.due_date < date.today())}


@app.get("/api/closing")
def closing_overview(session: Session = Depends(db)) -> dict[str, Any]:
    from engine import closing_coordinator as cc
    return {"alerts": [{**a.__dict__, "due_date": iso(a.due_date)} for a in cc.alerts(session)],
            "files": cc.pipeline(session)}


@app.get("/api/closing/{lead_id}")
def closing_file(lead_id: int, structure: str = "assignment", session: Session = Depends(db)) -> dict[str, Any]:
    from engine import closing_coordinator as cc
    lead = get_lead(session, lead_id)
    if not lead.contract_date:
        return {"open": False, "milestones": [], "reason": "No signed contract on this lead yet."}
    if not cc.milestones(session, lead):
        cc.open_file(session, lead)
    notes = cc.sync(session, lead)
    e = cc.title_order_email(lead, None, structure if structure in ("assignment", "double_close") else "assignment")
    return {"open": True, "notes": notes, "milestones": [_milestone_dict(m) for m in cc.milestones(session, lead)],
            "title_email": e, "contract_price": lead.offer_amount, "stage": lead.status_stage}


class MilestoneIn(BaseModel):
    notes: str = ""


@app.post("/api/closing/{lead_id}/milestones/{key}")
def closing_done(lead_id: int, key: str, body: MilestoneIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import closing_coordinator as cc
    lead = get_lead(session, lead_id)
    try:
        cc.sync(session, lead)
        m = cc.complete(session, lead, key.upper(), body.notes)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _milestone_dict(m)


@app.post("/api/closing/{lead_id}/disapproved")
def closing_disapproved(lead_id: int, body: MilestoneIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import closing_coordinator as cc
    lead = get_lead(session, lead_id)
    cc.record_disapproval(session, lead, body.notes)
    return {"stage": lead.status_stage}


class CloseIn(BaseModel):
    fee: float = Field(gt=0)
    closed_on: date | None = None


@app.post("/api/closing/{lead_id}/close")
def closing_close(lead_id: int, body: CloseIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import closing_coordinator as cc
    lead = get_lead(session, lead_id)
    try:
        cc.close_deal(session, lead, body.fee, body.closed_on)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"stage": lead.status_stage}


@app.get("/api/finance")
def finance_summary(year: int | None = None, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import finance
    from db.models import LedgerEntry
    sm = finance.summary(session, year)
    rows = session.scalars(select(LedgerEntry).order_by(LedgerEntry.entry_date.desc()).limit(50)).all()
    return {**sm.to_dict(), "entries": [{"id": r.id, "date": iso(r.entry_date), "kind": r.kind, "category": r.category,
                                         "amount": r.amount, "description": r.description, "lead_id": r.lead_id}
                                        for r in rows],
            "categories": {"income": finance.INCOME_CATEGORIES, "expense": finance.EXPENSE_CATEGORIES}}


class LedgerIn(BaseModel):
    kind: str
    category: str
    amount: float = Field(gt=0)
    entry_date: date | None = None
    description: str | None = Field(None, max_length=300)
    lead_id: int | None = None


@app.post("/api/finance")
def finance_add(body: LedgerIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import finance
    try:
        row = finance.add_entry(session, kind=body.kind, category=body.category, amount=body.amount,
                                entry_date=body.entry_date, description=body.description, lead_id=body.lead_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": row.id}



# -- Phase 4: inbound funnel (site/) ----------------------------------------------------------------------
def inbound_gis() -> Any:
    """NJGIN client for parcel matching + underwriting of web leads (None when inbound.offline is set)."""
    if (settings().get("inbound", {}) or {}).get("offline"):
        return None
    from agents.scrapers.njgis_client import NJGISClient
    return NJGISClient()


def underwrite_inbound(lead_id: int) -> None:
    """After the visitor has their answer: value the property and re-score (NJGIN + listing comps take seconds)."""
    from engine.scoring import active_weights, apply_score
    from engine.underwriter import Underwriter, _default_listing_provider
    gis = inbound_gis()
    if gis is None:
        return
    with session_scope() as s:
        lead = s.get(Lead, lead_id)
        if lead is None:
            return
        Underwriter(gis=gis, listing_provider=_default_listing_provider()).underwrite(s, lead.property)
        version, weights = active_weights(s)
        apply_score(lead, version, weights)


@app.post("/api/inbound/seller")
def inbound_seller(body: dict[str, Any], tasks: BackgroundTasks, session: Session = Depends(db)) -> dict[str, Any]:
    from engine.inbound import InboundError, ingest_seller
    try:
        r = ingest_seller(session, body, gis=inbound_gis(), underwrite=False)
    except InboundError as exc:
        raise HTTPException(400, str(exc)) from exc
    tasks.add_task(underwrite_inbound, r.lead_id)
    return {"ok": True, "lead_id": r.lead_id}


@app.post("/api/inbound/buyer")
def inbound_buyer(body: dict[str, Any], session: Session = Depends(db)) -> dict[str, Any]:
    from engine.inbound import InboundError, ingest_buyer
    try:
        b, created = ingest_buyer(session, body)
    except InboundError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "buyer_id": b.id, "created": created}



@app.get("/api/today")
def today_view(session: Session = Depends(db)) -> dict[str, Any]:
    from engine.today import today
    return today(session)


# -- Phase 5: command center (health, KPIs, setup, swarm refresh, guided autopilot) ---------------------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    """The desktop launcher polls this before opening the browser."""
    return {"status": "ok", "app": "nj-wholesale-cockpit", "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/kpis")
def kpi_pills(session: Session = Depends(db)) -> dict[str, Any]:
    from engine.autopilot import kpis
    return kpis(session)


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    from engine import operator_setup
    return operator_setup.status()


@app.post("/api/settings")
def post_settings(body: dict[str, Any]) -> dict[str, Any]:
    from engine import operator_setup
    try:
        return operator_setup.save(body)
    except operator_setup.SetupError as exc:
        raise HTTPException(400, {"errors": exc.errors}) from exc


class SwarmIn(BaseModel):
    mode: str = "quick"


@app.post("/api/swarm/trigger")
def swarm_trigger(body: SwarmIn) -> dict[str, Any]:
    from swarm import runner
    try:
        return runner.trigger(body.mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/swarm/status")
def swarm_status(session: Session = Depends(db)) -> dict[str, Any]:
    from db.models import AgentRun
    from swarm import runner
    last = session.scalars(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(8)).all()
    return {**runner.status(), "recent": [{"agent": r.agent_name, "status": r.status, "started_at": iso(r.started_at),
                                           "records": r.records_processed, "new_leads": r.new_leads_found,
                                           "errors": r.error_count} for r in last]}


@app.get("/api/autopilot")
def autopilot_deck(session: Session = Depends(db)) -> dict[str, Any]:
    from engine.autopilot import deck
    return deck(session)


class AutoSendIn(BaseModel):
    channel: str
    subject: str | None = None
    body: str | None = None


@app.post("/api/autopilot/{lead_id}/send")
def autopilot_send(lead_id: int, body: AutoSendIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine.autopilot import AutopilotError, send
    try:
        return send(session, lead_id, body.channel, subject=body.subject, body=body.body)
    except AutopilotError as exc:
        raise HTTPException(409, str(exc)) from exc


class AutoOutcomeIn(BaseModel):
    outcome: str
    notes: str = ""


@app.post("/api/autopilot/{lead_id}/outcome")
def autopilot_outcome(lead_id: int, body: AutoOutcomeIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine.autopilot import AutopilotError, outcome
    try:
        return outcome(session, lead_id, body.outcome, notes=body.notes)
    except AutopilotError as exc:
        raise HTTPException(409, str(exc)) from exc


# -- Phase 6: fast skip-trace mode, deal structure, first-touch preference ---------------------------------------
@app.get("/api/skiptrace/queue")
def skiptrace_queue(limit: int = 25, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import clipboard_skiptrace as cs
    return {"rows": cs.queue(session, limit=max(1, min(limit, 100)))}


@app.post("/api/leads/{lead_id}/skiptrace-paste")
def skiptrace_fast(lead_id: int, body: PasteIn, session: Session = Depends(db)) -> dict[str, Any]:
    """One-click paste of a people-search page: parse, DNC-scrub, rank, save, flip READY_TO_CALL, hand back the next lead."""
    from engine import clipboard_skiptrace as cs
    try:
        return cs.trace(session, lead_id, body.text)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/leads/{lead_id}/structure")
def lead_structure(lead_id: int, buyer_price: float | None = None, seller_refuses: bool = False,
                   session: Session = Depends(db)) -> dict[str, Any]:
    from engine import deal_structure as ds
    lead = get_lead(session, lead_id)
    c = ds.structure_for_lead(lead, buyer_price=buyer_price, seller_refuses_assignment=seller_refuses)
    g = contract_gate(lead, "loi", default_price(lead))
    return {**c.to_json(), "entity_option": ds.entity_option(lead),
            "rider_gate": {"allowed": g.allowed, "reasons": g.reasons, "warnings": g.warnings}}


class StructureIn(BaseModel):
    contract_price: float = Field(gt=0)
    buyer_price: float = Field(gt=0)
    seller_refuses_assignment: bool = False
    in_llc: bool = False


@app.post("/api/structure/compare")
def structure_compare(body: StructureIn) -> dict[str, Any]:
    from engine import deal_structure as ds
    return ds.compare_structures(contract_price=body.contract_price, buyer_price=body.buyer_price,
                                 seller_refuses_assignment=body.seller_refuses_assignment, in_llc=body.in_llc).to_json()


class FirstTouchIn(BaseModel):
    mode: str = Field(pattern="^(opener|offer)$")


@app.post("/api/autopilot/first-touch")
def set_first_touch(body: FirstTouchIn) -> dict[str, Any]:
    """opener = the two-line availability question first (default); offer = the priced note first."""
    from engine import operator_setup
    operator_setup.save_preference("autopilot", "first_touch", body.mode)
    return {"first_touch": body.mode}


# -- Phase 6: live agent-reply negotiator ----------------------------------------------------------------------
class ReplyIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    save: bool = True


@app.post("/api/leads/{lead_id}/analyze-reply")
def analyze_reply(lead_id: int, body: ReplyIn, session: Session = Depends(db)) -> dict[str, Any]:
    """Paste the agent's reply: parsed intent and counter, the traffic light against the buyer-implied numbers, the
    three-sentence reply, the follow-up it schedules. save=false previews without recording."""
    from engine import reply_negotiator as rn
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "lead not found")
    if body.save:
        analysis, inter = rn.analyze_and_record(session, lead, body.text)
        out = analysis.to_json()
        out["interaction_id"] = inter.id
    else:
        out = rn.analyze(session, lead, body.text).to_json()
    out.update({"lead_id": lead.id, "stage": lead.status_stage, "last_counter_amount": lead.last_counter_amount,
                "next_followup_at": iso(lead.next_followup_at)})
    return out


# -- Phase 6: JV partner & proof-of-funds vault ----------------------------------------------------------------
@app.get("/api/partners")
def partners_list(session: Session = Depends(db)) -> dict[str, Any]:
    from engine import jv_partner_vault as vault
    rows = vault.partners(session)
    return {"partners": [vault.partner_dict(p) for p in rows], "active": vault.active_count(session),
            "needed": vault.partners_needed(), "phase0_open": vault.phase0_open(session),
            "cards": vault.phase0_cards(session, limit=5)}


@app.post("/api/partners/seed")
def partners_seed(top: int = 30, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import jv_partner_vault as vault
    rows = vault.seed(session, top=max(1, min(top, 100)))
    return {"seeded": len(rows), "partners": [vault.partner_dict(p) for p in rows]}


class PartnerIn(BaseModel):
    contact_name: str | None = None
    entity_name: str | None = None
    phone: str | None = None
    email: str | None = None
    target_counties: list[str] | None = None
    target_towns: list[str] | None = None
    min_price: float | None = Field(None, ge=0)
    max_price: float | None = Field(None, ge=0)
    max_rehab: str | None = Field(None, pattern="^(LIGHT|MEDIUM|HEAVY)$")
    pof_verified: bool | None = None
    pof_share_ok: bool | None = None
    provides_emd: bool | None = None
    will_walk_properties: bool | None = None
    default_split_pct: float | None = Field(None, ge=0, le=100)
    status: str | None = None
    notes: str | None = None


def _partner(session: Session, partner_id: int) -> Any:
    from db.models import JVPartner
    p = session.get(JVPartner, partner_id)
    if p is None:
        raise HTTPException(404, "partner not found")
    return p


@app.post("/api/partners")
def partners_create(body: PartnerIn, session: Session = Depends(db)) -> dict[str, Any]:
    from db.models import JVPartner
    from engine import jv_partner_vault as vault
    if not (body.entity_name or body.contact_name):
        raise HTTPException(400, "give the partner a name or an entity name")
    p = JVPartner(entity_name=body.entity_name or body.contact_name, status="TARGET")
    session.add(p)
    session.flush()
    try:
        vault.update(session, p, **body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return vault.partner_dict(p)


@app.patch("/api/partners/{partner_id}")
def partners_update(partner_id: int, body: PartnerIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import jv_partner_vault as vault
    p = _partner(session, partner_id)
    try:
        vault.update(session, p, **body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return vault.partner_dict(p)


class PartnerTouchIn(BaseModel):
    channel: str = Field("email", pattern="^(email|sms|phone)$")
    notes: str = ""


@app.post("/api/partners/{partner_id}/touch")
def partners_touch(partner_id: int, body: PartnerTouchIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import jv_partner_vault as vault
    p = vault.log_touch(session, _partner(session, partner_id), body.channel, body.notes)
    return {**vault.partner_dict(p), "phase0_open": vault.phase0_open(session)}


@app.post("/api/partners/{partner_id}/pof")
async def partners_pof(partner_id: int, file: UploadFile = File(...), share_ok: bool = Form(False),
                       session: Session = Depends(db)) -> dict[str, Any]:
    from engine import jv_partner_vault as vault
    p = _partner(session, partner_id)
    data = await file.read()
    try:
        vault.attach_pof(session, p, file.filename or "pof.pdf", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    vault.update(session, p, pof_share_ok=bool(share_ok))
    return {**vault.partner_dict(p), "active": vault.active_count(session), "needed": vault.partners_needed()}


@app.get("/api/leads/{lead_id}/partner")
def lead_partner(lead_id: int, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import jv_partner_vault as vault
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(404, "lead not found")
    return vault.deal_partner_summary(session, lead)


@app.get("/api/pricing/calibration")
def pricing_calibration() -> dict[str, Any]:
    """The empirical alpha table the quant model prices from (aggregates only)."""
    cal = qp.calibration()
    towns = sorted(((code, t) for code, t in (cal.get("towns") or {}).items()), key=lambda kv: -kv[1].get("n", 0))
    return {"generated_on": cal.get("generated_on"), "statewide": cal.get("statewide"), "counties": cal.get("counties"),
            "towns": [{"code": code, **t, "alpha": qp.local_alpha(code, t.get("county")).alpha} for code, t in towns[:60]],
            "model": qp.pricing_model()}


# -- Phase 5: public records (OPRA tax lists, clerk filings, uploads) ----------------------------------------
def records_gis() -> Any:
    """NJGIN client for parcel lookups from the records page (tests replace it). Someone is waiting on the page, so
    a slow NJGIN gets a short timeout and one retry; the import then falls back to parcels we already track."""
    from agents.scrapers.njgis_client import NJGISClient
    from core.http import PoliteSession
    return NJGISClient(http=PoliteSession(min_interval=0.5, timeout=20, max_retries=1))


@app.get("/api/opra")
def opra_list(n: int = 20, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import operator_setup
    from engine import opra_generator as og
    counties = settings().markets.get("counties", [])
    return {"requests": [d.to_dict() for d in og.drafts(session, n, extra_counties=[c for c in counties
                                                                                    if c in ("Mercer", "Monmouth")])],
            "rules": og.rules(), "operator_ready": operator_setup.status()["live"]}


class OpraSentIn(BaseModel):
    county: str
    town: str
    channel: str = "email"


@app.post("/api/opra/sent")
def opra_sent(body: OpraSentIn, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import opra_generator as og
    if body.channel not in ("email", "portal", "mail", "phone"):
        raise HTTPException(400, "channel must be email, portal, mail or phone")
    row = og.mark_sent(session, body.county, body.town, body.channel)
    return {"county": row.county, "town": row.town, "status": row.status, "due_date": iso(row.due_date)}


@app.post("/api/imports/tax-list")
async def import_tax_list(request: Request, session: Session = Depends(db)) -> dict[str, Any]:
    from engine import opra_generator as og
    from engine.tax_list_import import import_tax_list as run_import
    form = await request.form()
    upload, county, town = form.get("file"), str(form.get("county") or ""), str(form.get("town") or "")
    if upload is None or not hasattr(upload, "read") or not county or not town:
        raise HTTPException(400, "send the spreadsheet with its county and town")
    data = await upload.read()
    try:
        r = run_import(session, data, upload.filename or "list.csv", county=county, town=town, gis=records_gis(),
                       list_year=str(form.get("year") or "") or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    og.mark_received(session, county, town, notes=f"imported {r.parcels} parcels")
    return r.to_dict()


@app.post("/api/imports/lis-pendens")
async def import_lis_pendens(request: Request) -> dict[str, Any]:
    import re as _re

    from agents.scrapers.lis_pendens_agent import IMPORT_DIR, LisPendensAgent
    form = await request.form()
    upload, county = form.get("file"), str(form.get("county") or "").strip().title()
    if upload is None or not hasattr(upload, "read") or not county:
        raise HTTPException(400, "send the saved results page or CSV with its county")
    name = _re.sub(r"[^A-Za-z0-9._-]+", "_", upload.filename or "export.csv")
    if not name.lower().endswith((".csv", ".tsv", ".txt", ".html", ".htm")):
        raise HTTPException(400, "save the clerk's results page as .html, or export .csv")
    folder = IMPORT_DIR / county.lower()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{date.today():%Y-%m-%d}_{name}").write_bytes(await upload.read())
    from starlette.concurrency import run_in_threadpool
    agent = LisPendensAgent(import_only=True, counties=[county], imports=IMPORT_DIR, offline=records_gis() is None)
    run = await run_in_threadpool(agent.execute)
    return {"status": run.status, "filings": run.records_processed, "new_leads": run.new_leads_found,
            "metrics": {k: v for k, v in (run.metrics_json or {}).items() if isinstance(v, int)}}


@app.get("/api/clerk-filings")
def clerk_filings(days: int = 45, session: Session = Depends(db)) -> dict[str, Any]:
    from datetime import timedelta

    from db.models import ClerkFiling
    since = date.today() - timedelta(days=days)
    rows = session.scalars(select(ClerkFiling).where(ClerkFiling.is_discharge.is_(False),
                                                     or_(ClerkFiling.recorded_date >= since,
                                                         ClerkFiling.recorded_date.is_(None)))
                           .order_by(ClerkFiling.recorded_date.desc())).all()
    out = []
    for r in rows:
        lead = session.scalar(select(Lead).where(Lead.property_id == r.property_id)) if r.property_id else None
        out.append({"id": r.id, "county": r.county, "town": r.town, "instrument": r.instrument,
                    "doc_type": r.doc_type, "recorded": iso(r.recorded_date), "defendants": r.defendants[:3],
                    "plaintiffs": r.plaintiffs[:2], "estate": r.estate, "tax_lien": r.tax_lien, "url": r.url,
                    "match": r.match_method, "lead_id": lead.id if lead else None})
    matched = [x for x in out if x["lead_id"]]
    return {"matched": matched, "unmatched": [x for x in out if not x["lead_id"]][:150],
            "counts": {"total": len(out), "matched": len(matched)}}


class LocateIn(BaseModel):
    address: str | None = None
    town: str | None = None
    block: str | None = None
    lot: str | None = None
    qual: str | None = None


@app.post("/api/clerk-filings/{filing_id}/locate")
def clerk_filing_locate(filing_id: int, body: LocateIn, session: Session = Depends(db)) -> dict[str, Any]:
    """The operator read the address (or block/lot) off the public document: tie the filing to its parcel."""
    from agents.scrapers import lis_pendens_agent as lp
    from db.models import ClerkFiling
    row = session.get(ClerkFiling, filing_id)
    if row is None:
        raise HTTPException(404, "filing not found")
    if not (body.address or (body.block and body.lot)):
        raise HTTPException(400, "give the street address or the block and lot")
    for key in ("address", "town", "block", "lot", "qual"):
        if getattr(body, key):
            setattr(row, key, getattr(body, key).strip())
    f = lp.Filing(county=row.county, source=row.source, instrument=row.instrument, doc_type=row.doc_type,
                  recorded=row.recorded_date, defendants=list(row.defendants or []), plaintiffs=list(row.plaintiffs
                                                                                                      or []),
                  town=row.town, block=row.block, lot=row.lot, qual=row.qual, address=row.address)
    prop, how = lp.resolve_property(session, f, lp.OwnerIndex(), records_gis())
    if prop is None or how in ("unmatched", "ambiguous_name"):
        raise HTTPException(409, "no parcel found for that address / block and lot in this town")
    row.property_id, row.match_method = prop.id, f"operator_{how}"
    lp.apply_filing(session, row, prop)
    lead = session.scalar(select(Lead).where(Lead.property_id == prop.id))
    return {"lead_id": lead.id if lead else None, "property_id": prop.id, "match": row.match_method}


app.mount("/site", StaticFiles(directory=SITE), name="site")
