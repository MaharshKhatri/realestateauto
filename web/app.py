"""Local CRM cockpit: `python manage.py server` → http://127.0.0.1:8000

FastAPI JSON API + a single dark-mode page (web/static). Everything runs on the operator's laptop against
wholesaling.db; nothing is exposed beyond 127.0.0.1. A Host/Origin guard blocks other websites (DNS
rebinding / cross-site form posts) from reading or writing the lead data through the browser.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
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
from engine.underwriter import frfpa_decision, offer_math

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
DEAL_VERDICTS = ("STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT")
TABS = {
    "all": "All open", "callable": "Callable now", "sheriff": "Sheriff sales", "estate": "Estates",
    "listings": "Stale listings", "deals": "Deals (offer verdict)", "frfpa": "Verify occupancy",
    "followups": "Follow-ups", "pipeline": "In pipeline", "ready": "Ready to call", "stacked": "Stacked distress",
}

app = FastAPI(title="NJ Wholesale Cockpit", docs_url="/api/docs", redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
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
            "valued_on": a.get("valued_on"),
            "comps": sorted(v.comps_json or [], key=lambda c: c.get("distance_mi") or 9)[:8]}


# -- pages ------------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


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
    return {
        "lead": {**lead_row(session, lead), "occupancy": lead.occupancy_status, "occupancy_source": lead.occupancy_source,
                 "occupancy_confirmed_at": iso(lead.occupancy_confirmed_at), "offer_amount": lead.offer_amount,
                 "contract_date": iso(lead.contract_date), "attorney_review_ends": iso(lead.attorney_review_ends),
                 "notes": lead.notes, "brief": lead.ai_strategy_brief,
                 "score": lead.score_breakdown_json or {}},
        "property": {"parcel_id": p.parcel_id, "block": p.block, "lot": p.lot, "qual": p.qual,
                     "municipality": p.municipality, "class": p.property_class, "building": p.building_desc,
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
        "deal_cfg": {k: settings().deal.get(k) for k in ("mao_pct_conservative", "mao_pct_aggressive",
                                                         "anchor_discount_pct", "min_assignment_fee")},
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


@app.post("/api/calc")
def calc(body: CalcIn) -> dict[str, Any]:
    """Slider calculator: the same offer_math the underwriter uses, with operator overrides."""
    cfg = {**dict(settings().deal), "mao_pct_conservative": body.mao_pct_conservative,
           "mao_pct_aggressive": body.mao_pct_aggressive, "anchor_discount_pct": body.anchor_discount_pct}
    om = offer_math(arv=body.arv, repairs=body.repairs, annual_taxes=body.annual_taxes, debt=body.debt, cfg=cfg,
                    frfpa=body.frfpa, fmv=body.fmv)
    out = {k: getattr(om, k) for k in ("arv", "repairs", "assignment_fee", "mao_conservative", "mao_aggressive",
                                       "anchor_offer", "buyer_purchase_price", "buyer_profit", "buyer_roi",
                                       "deal_spread", "verdict", "frfpa_floor", "flags", "tax_drag_adjustment")}
    if body.frfpa and om.verdict == "FRFPA_BLOCKED" and body.fmv:
        ct = covered_track(arv=om.arv, fmv=body.fmv, repairs=om.repairs, debt=body.debt,
                           aggressive_pct=body.mao_pct_aggressive,
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
