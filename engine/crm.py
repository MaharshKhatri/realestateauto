"""CRM services: call logging, pipeline stages, follow-up scheduling, occupancy confirmation, contracts.

One function per operator action so the web cockpit and the CLI behave identically:

  log_call()          record an outcome → stage → next follow-up → DNC → rescore → post-call coach
  confirm_occupancy() record who lives there → re-price the offer (FRFPA 82% floor ⇄ 70% MAO) → rescore
  record_contract()   signed seller contract → UNDER_CONTRACT, attorney-review / FRFPA cancellation end date
  call_queue()        today's dial list: due follow-ups first, then the best uncontacted leads with a phone
  funnel(), sale_countdown(), due_followups()
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.config import settings
from core.dates import nj_business_days_after
from db.models import (CallOutcome, Contact, DistressSignal, Interaction, Lead, LeadStage, Occupancy, Property,
                       SignalType, utcnow)
from engine import compliance

TZ = ZoneInfo("America/New_York")
STAGE_ORDER = [LeadStage.NEW, LeadStage.SKIP_TRACE, LeadStage.CONTACT_ATTEMPTED, LeadStage.CONTACTED,
               LeadStage.INTERESTED, LeadStage.APPOINTMENT, LeadStage.OFFER_SENT_TO_AGENT, LeadStage.OFFER_MADE,
               LeadStage.NEGOTIATING,
               LeadStage.UNDER_CONTRACT, LeadStage.ASSIGNED, LeadStage.CLOSED]
TERMINAL = {LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED}
OUTCOME_STAGE = {
    CallOutcome.NO_ANSWER: LeadStage.CONTACT_ATTEMPTED,
    CallOutcome.LEFT_VM: LeadStage.CONTACT_ATTEMPTED,
    CallOutcome.WRONG_NUMBER: LeadStage.CONTACT_ATTEMPTED,
    CallOutcome.SPOKE_WARM_FOLLOWUP: LeadStage.INTERESTED,
    CallOutcome.SPOKE_NEEDS_OFFER: LeadStage.INTERESTED,
    CallOutcome.SPOKE_NOT_INTERESTED: LeadStage.CONTACTED,
    CallOutcome.AGENT_WILL_PRESENT_LOI: LeadStage.OFFER_MADE,
    CallOutcome.OFFER_SENT_TO_AGENT: LeadStage.OFFER_SENT_TO_AGENT,
    CallOutcome.OFFER_ACCEPTED: LeadStage.NEGOTIATING,
    CallOutcome.DEAD: LeadStage.DEAD,
    CallOutcome.DNC: LeadStage.DO_NOT_CONTACT,
}
OUTCOME_LABELS = {
    CallOutcome.NO_ANSWER: "No answer", CallOutcome.LEFT_VM: "Left voicemail", CallOutcome.WRONG_NUMBER: "Wrong number",
    CallOutcome.SPOKE_WARM_FOLLOWUP: "Spoke — warm, follow up", CallOutcome.SPOKE_NEEDS_OFFER: "Spoke — needs an offer",
    CallOutcome.SPOKE_NOT_INTERESTED: "Spoke — not interested", CallOutcome.AGENT_WILL_PRESENT_LOI:
    "Agent will present LOI", CallOutcome.OFFER_SENT_TO_AGENT: "LOI sent to agent",
    CallOutcome.OFFER_ACCEPTED: "Offer accepted (verbal)", CallOutcome.DEAD: "Dead",
    CallOutcome.DNC: "Do not call",
}
NO_CONTACT = {CallOutcome.NO_ANSWER, CallOutcome.LEFT_VM, CallOutcome.WRONG_NUMBER}
DOCS_FOR_OUTCOME = {CallOutcome.SPOKE_NEEDS_OFFER: ["loi", "deal_sheet"],
                    CallOutcome.AGENT_WILL_PRESENT_LOI: ["loi"],
                    CallOutcome.OFFER_ACCEPTED: ["psa", "deal_sheet"]}


def _crm() -> dict[str, Any]:
    return settings().get("crm", {})


def advance_stage(current: str, target: str) -> str:
    """Stages only move forward, except the terminal DEAD / DO_NOT_CONTACT which always win."""
    if target in (LeadStage.DEAD, LeadStage.DO_NOT_CONTACT):
        return target
    if current in TERMINAL:
        return current
    try:
        return target if STAGE_ORDER.index(target) > STAGE_ORDER.index(current) else current
    except ValueError:
        return target


def sale_date(lead: Lead) -> date | None:
    s = next((s for s in lead.property.active_signals if s.signal_type == SignalType.SHERIFF_SALE), None)
    return s.scheduled_auction_date if s else None


def next_followup(outcome: str, attempts: int, now: datetime | None = None, auction: date | None = None) -> datetime | None:
    """Next touch at 10:00 Eastern; no-answer cadence stretches with attempts; a near auction compresses it."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    days = (_crm().get("followup_days") or {}).get(outcome)
    if outcome in (CallOutcome.DEAD, CallOutcome.DNC) or days is None:
        return None
    if outcome in NO_CONTACT:
        if attempts >= int(_crm().get("max_attempts_no_contact", 8)):
            return None
        days = days + attempts // 3
    if auction:
        until = (auction - now.date()).days
        if 0 <= until <= 14:
            days = min(days, 1)
    day = now.date() + timedelta(days=max(days, 0))
    if days == 0:
        nxt = now + timedelta(hours=2)
        return nxt.replace(tzinfo=None) if nxt.hour < compliance.CALL_END else \
            datetime.combine(day + timedelta(days=1), time(10))
    if day.weekday() == 6:      # Sunday → Monday
        day += timedelta(days=1)
    return datetime.combine(day, time(10))


@dataclass
class CallResult:
    interaction_id: int
    stage: str
    next_followup_at: datetime | None
    priority: float
    dnc_added: bool = False
    suggested_docs: list[str] = field(default_factory=list)
    coach: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def rescore(session: Session, lead: Lead) -> float:
    from engine.scoring import active_weights, apply_score
    version, weights = active_weights(session)
    return apply_score(lead, version, weights).priority


def log_call(session: Session, lead: Lead, outcome: str, *, notes: str = "", contact: Contact | None = None,
             channel: str = "call", occupancy: str | None = None, offer_amount: float | None = None,
             debrief: dict[str, Any] | None = None, followup_at: datetime | None = None,
             now: datetime | None = None, use_ai: bool = False) -> CallResult:
    if outcome not in OUTCOME_STAGE:
        raise ValueError(f"unknown outcome {outcome!r}; choose from {', '.join(OUTCOME_STAGE)}")
    now_local = (now or datetime.now(TZ)).astimezone(TZ)
    extra: list[str] = []
    attempts = lead.contact_attempts + 1
    lead.contact_attempts = attempts
    lead.last_contact_at = now_local.replace(tzinfo=None)
    lead.status_stage = advance_stage(lead.status_stage, OUTCOME_STAGE[outcome])
    if offer_amount:
        lead.offer_amount = offer_amount
    dnc_added = False
    if outcome == CallOutcome.DNC:
        for c in ([contact] if contact else [c for c in lead.contacts if c.contact_type != "LISTING_AGENT"]):
            if c and c.phone:
                compliance.add_internal(session, c.phone, notes=f"stop request on call, lead #{lead.id}")
                dnc_added = True
        extra.append("Added to the internal do-not-call list (honored immediately; kept 5 years).")
    if outcome == CallOutcome.WRONG_NUMBER and contact:
        contact.confidence_score = 0.0
        contact.notes = ((contact.notes or "") + f" | wrong number {now_local:%Y-%m-%d}").strip(" |")
        extra.append("Number marked wrong; skip-trace the next-best phone.")
    if outcome in NO_CONTACT and attempts >= int(_crm().get("max_attempts_no_contact", 8)):
        extra.append(f"{attempts} attempts with no conversation: switch to a letter (manage.py mail-merge) or another contact (agent, relative).")
    nxt = followup_at or next_followup(outcome, attempts, now_local, sale_date(lead))
    lead.next_followup_at = nxt
    if outcome == CallOutcome.DEAD:
        lead.outcome = (notes or "dead")[:48]
    if occupancy:
        extra += confirm_occupancy(session, lead, occupancy, source=f"{channel} {now_local:%Y-%m-%d}", rescore_lead=False)
    from engine.debrief import coach_call
    coach = coach_call(lead, outcome, notes, debrief or {}, use_ai=use_ai)
    inter = Interaction(lead_id=lead.id, contact_id=contact.id if contact else None, channel=channel, outcome=outcome,
                        notes=notes or None, debrief_json={**(debrief or {}), "coach": coach}, next_followup_at=nxt,
                        occurred_at=now_local.replace(tzinfo=None))
    session.add(inter)
    session.flush()
    priority = rescore(session, lead)
    return CallResult(interaction_id=inter.id, stage=lead.status_stage, next_followup_at=nxt, priority=priority,
                      dnc_added=dnc_added, suggested_docs=DOCS_FOR_OUTCOME.get(outcome, []), coach=coach, notes=extra)


def confirm_occupancy(session: Session, lead: Lead, status: str, source: str, *, rescore_lead: bool = True) -> list[str]:
    """Record occupancy the operator confirmed (call, agent, seller photos/video, Street View) and re-price the offer."""
    from engine.underwriter import reprice

    if status not in Occupancy.__members__:
        raise ValueError(f"unknown occupancy {status!r}; choose from {', '.join(Occupancy.__members__)}")
    before = lead.property.current_valuation
    old_verdict, old_anchor = (before.verdict, before.anchor_offer) if before else (None, None)
    lead.occupancy_status = status
    lead.occupancy_source = source[:200]
    lead.occupancy_confirmed_at = utcnow()
    val = reprice(session, lead.property)
    out = [f"Occupancy recorded: {status.replace('_', ' ').lower()} ({source})."]
    if val is not None:
        session.refresh(lead.property)
        out.append(f"Re-priced: {old_verdict} → {val.verdict}; anchor "
                   f"{'$' + format(old_anchor, ',.0f') if old_anchor else '—'} → "
                   f"{'$' + format(val.anchor_offer, ',.0f') if val.anchor_offer else '—'} "
                   f"(FRFPA track {lead.frfpa_track}).")
    if rescore_lead:
        rescore(session, lead)
    return out


def record_contract(session: Session, lead: Lead, price: float, signed: date | None = None) -> list[str]:
    """Fully signed seller contract delivered to both parties on `signed` (day 0 of attorney review)."""
    from engine.underwriter import frfpa_decision

    signed = signed or date.today()
    lead.contract_date = signed
    lead.offer_amount = price
    review_end = nj_business_days_after(signed, int(settings().get("compliance", {}).get(
        "attorney_review_business_days", 3)))
    notes = [f"Attorney review ends {review_end:%a %b %d} (3 NJ business days after delivery)."]
    if frfpa_decision(lead.property).applies:
        cancel_end = nj_business_days_after(signed, int(settings().get("compliance", {}).get(
            "frfpa_cancellation_business_days", 10)))
        review_end = max(review_end, cancel_end)
        notes.append(f"FRFPA: the owner may cancel through {cancel_end:%a %b %d} (10 business days); no deed is signed "
                     "and nothing is marketed before then.")
    lead.attorney_review_ends = review_end
    lead.status_stage = advance_stage(lead.status_stage, LeadStage.UNDER_CONTRACT)
    session.flush()
    rescore(session, lead)
    return notes


# -- queues -----------------------------------------------------------------------------------------
def not_dnc():
    """SQL: a contact phone that isn't suppressed (NULL status = not yet classified, still dialable after a scrub)."""
    return or_(Contact.dnc_status.is_(None), Contact.dnc_status != "dnc")


def open_leads_stmt():
    return select(Lead).where(Lead.status_stage.notin_([s.value for s in TERMINAL]))


def due_followups(session: Session, now: datetime | None = None) -> list[Lead]:
    now = (now or datetime.now(TZ)).astimezone(TZ).replace(tzinfo=None)
    return list(session.scalars(open_leads_stmt().where(Lead.next_followup_at.is_not(None),
                                                        Lead.next_followup_at <= now)
                                .order_by(Lead.next_followup_at)))


def best_phone(session: Session, lead: Lead) -> Contact | None:
    from engine.scripts import pick_contact
    c = pick_contact(lead)
    if c and c.phone and (c.confidence_score or 0) > 0 and c.dnc_status != "dnc":
        return c
    for c in sorted(lead.contacts, key=lambda c: -(c.confidence_score or 0)):
        if c.phone and (c.confidence_score or 0) > 0 and c.dnc_status != "dnc":
            return c
    return None


@dataclass
class QueueItem:
    lead: Lead
    contact: Contact | None
    reason: str
    due: datetime | None = None


def call_queue(session: Session, limit: int | None = None, now: datetime | None = None) -> list[QueueItem]:
    """Due follow-ups first, then the highest-priority leads that have a callable phone and no recent touch."""
    limit = limit or int(_crm().get("call_queue_size", 40))
    items: list[QueueItem] = []
    seen: set[int] = set()
    for lead in due_followups(session, now):
        items.append(QueueItem(lead, best_phone(session, lead), "follow-up due", lead.next_followup_at))
        seen.add(lead.id)
    phone_leads = select(Contact.lead_id).where(Contact.phone.is_not(None), not_dnc()).distinct()
    stmt = (open_leads_stmt().where(Lead.id.in_(phone_leads), Lead.next_followup_at.is_(None))
            .order_by(Lead.priority_rank_score.desc()).limit(limit * 2))
    for lead in session.scalars(stmt):
        if lead.id in seen or len(items) >= limit:
            continue
        c = best_phone(session, lead)
        if c is None:
            continue
        days = None
        sd = sale_date(lead)
        if sd:
            days = (sd - date.today()).days
        items.append(QueueItem(lead, c, f"sale in {days} days" if days is not None and 0 <= days <= 30 else
                               "new — not yet called"))
        seen.add(lead.id)
    return items[:limit]


def funnel(session: Session) -> dict[str, int]:
    rows = dict(session.execute(select(Lead.status_stage, func.count(Lead.id)).group_by(Lead.status_stage)).all())
    return {s.value: int(rows.get(s.value, 0)) for s in [*STAGE_ORDER, LeadStage.DEAD, LeadStage.DO_NOT_CONTACT]}


def sale_countdown(session: Session, days: int | None = None, today: date | None = None) -> list[tuple[Lead, date, int]]:
    days = days or int(_crm().get("sale_countdown_days", 21))
    today = today or date.today()
    stmt = (select(Lead, DistressSignal.scheduled_auction_date)
            .join(Property, Lead.property_id == Property.id)
            .join(DistressSignal, DistressSignal.property_id == Property.id)
            .where(DistressSignal.signal_type == SignalType.SHERIFF_SALE, DistressSignal.active.is_(True),
                   DistressSignal.scheduled_auction_date >= today,
                   DistressSignal.scheduled_auction_date <= today + timedelta(days=days),
                   Lead.status_stage.notin_([s.value for s in TERMINAL]))
            .order_by(DistressSignal.scheduled_auction_date, Lead.priority_rank_score.desc()))
    return [(lead, d, (d - today).days) for lead, d in session.execute(stmt).all()]


def contact_status(session: Session, lead: Lead) -> str:
    """One-word readiness for the briefing: callable / agent / no-phone (+ DNC / unscrubbed flags)."""
    c = best_phone(session, lead)
    if c is None:
        if any(x.dnc_status == "dnc" for x in lead.contacts if x.phone):
            return "DNC only — mail"
        return "skip-trace" if lead.contacts else "no contact"
    kind = "agent" if c.contact_type == "LISTING_AGENT" else "phone"
    flag = {"not_checked": " (unscrubbed)", "dnc": " DNC"}.get(c.dnc_status or "", "")
    return f"{kind}{flag}"
