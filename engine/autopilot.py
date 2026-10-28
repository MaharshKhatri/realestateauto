"""Guided autopilot: one deal card at a time, in the order that makes money, with the exact next action.

The deck (up to 20 cards a day) is built from live records:

  1. listed foreclosures with a reachable listing agent (hard auction date; engine/fast_track.py), not contacted in
     the last 48 hours;
  2. follow-ups that are due now (offers / inquiries sent two days ago, voicemails, "send me the offer" calls);
  3. ready-to-call stale MLS fixers within striking distance of the ask (engine/agent_outreach.py).

Each card says what the property is, why the numbers work (ARV, repairs, our walk-away and opening offer, the fee),
and the exact action: the plain-text e-mail (engine/human_copy.py), a short text, a 3-sentence phone script, and
outcome buttons. Sending or logging an outcome writes the same interactions / stages / 48-hour follow-ups as the
rest of the CRM, so the card drops off the deck and the follow-up comes back on its due date.

Progress: "Today's goal" counts distinct leads contacted today (e-mail, text or call logged) against
`autopilot.daily_goal` (15).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import settings
from core.text import format_phone
from db.models import CashBuyer, Contact, ContactType, Interaction, Lead, LeadStage
from engine import agent_outreach as ao
from engine import human_copy as hc
from engine.scripts import lead_facts

CONTACT_OUTCOMES = ("OFFER_SENT_TO_AGENT", "AGENT_INQUIRY_SENT", "AGENT_FOLLOWUP_SENT", "LEFT_VM", "NO_ANSWER",
                    "SPOKE_NEEDS_OFFER", "SPOKE_WARM_FOLLOWUP", "SPOKE_NOT_INTERESTED", "AGENT_WILL_PRESENT_LOI",
                    "OFFER_ACCEPTED", "DEAD", "WRONG_NUMBER")
OUTCOMES = {          # dashboard button -> CRM call outcome
    "interested": ("SPOKE_NEEDS_OFFER", "They want the written offer: it's ready below, send it today."),
    "voicemail": ("LEFT_VM", "Voicemail logged; the follow-up is scheduled."),
    "not_interested": ("SPOKE_NOT_INTERESTED", "Logged; we'll check back in about 45 days."),
    "sold": ("DEAD", "Marked sold / off market and removed from the deck."),
}
CLOSED = (LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED, LeadStage.UNDER_CONTRACT, LeadStage.ASSIGNED)


def _cfg() -> dict[str, Any]:
    return settings().get("autopilot", {}) or {}


def daily_goal() -> int:
    return int(_cfg().get("daily_goal", 15))


def _now(now: datetime | None) -> datetime:
    return ao._eastern(now)


def contacted_today(session: Session, now: datetime | None = None) -> int:
    start = _now(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(session.scalar(select(func.count(func.distinct(Interaction.lead_id))).where(
        Interaction.occurred_at >= start, Interaction.outcome.in_(CONTACT_OUTCOMES))) or 0)


# -- card content ------------------------------------------------------------------------------------
def what_it_is(f: dict[str, Any], lf: Any = None) -> str:
    units = f"{f['units']}-family" if f.get("units") and f["units"] > 1 else "single-family"
    bits = [f"A {units} house in {f.get('town') or '?'} ({f.get('county') or '?'} County)"
            + (f", built {f['year_built']}" if f.get("year_built") else "")
            + (f", about {f['sqft']:,} sqft" if f.get("sqft") else "") + "."]
    if f.get("list_price"):
        bits.append(f"Listed at {hc.money(f['list_price'])}" + (f" for {f['dom']} days" if f.get("dom") else "")
                    + (", with a price cut" if f.get("price_reduced") else "") + ".")
    if lf is not None and lf.sale_date:
        bits.append(f"On the sheriff sale list for {hc.short_date(datetime.fromisoformat(lf.sale_date))}"
                    + (f" ({lf.days_to_sale} days)" if lf.days_to_sale is not None and lf.days_to_sale >= 0 else "")
                    + (f" with about {hc.money(lf.upset)} owed" if lf.upset else "") + ".")
    if f.get("keywords"):
        bits.append("Listing says: " + ", ".join(f'"{k}"' for k in f["keywords"][:3]) + ".")
    return " ".join(bits)


def why_it_works(lead: Lead, f: dict[str, Any], anchor: float | None, best: float | None) -> list[str]:
    v = lead.property.current_valuation
    if v is None:
        return ["Not underwritten yet: run the data refresh before making an offer."]
    out = [f"Fixed-up value (ARV) about {hc.money(v.arv_estimate)}"
           + (f" from {v.comp_count} nearby sales" if v.comp_count else "") + f"; repairs about "
           f"{hc.money(v.est_repair_cost)} ({(v.rehab_tier or '').lower() or 'estimated'})."]
    if v.mao_conservative:
        out.append(f"Investor math: 70% of ARV minus repairs and our fee = {hc.money(v.mao_conservative)} walk-away"
                   + (f"; we open at {hc.money(anchor)}" if anchor else "") + ".")
    gap = ao.listing_gap(lead)
    if gap.get("list_price") and gap.get("list_to_mao_75"):
        out.append(f"The ask is reachable: our 75% ceiling ({hc.money(v.mao_aggressive)}) is "
                   f"{gap['list_to_mao_75']:.0%} of the {hc.money(gap['list_price'])} ask.")
    if f.get("upset") and v.as_is_value:
        equity = v.as_is_value - f["upset"]
        if equity > 0:
            out.append(f"About {hc.money(equity)} of equity is at risk at the auction, so a quick sale protects it.")
    if v.target_assignment_fee:
        out.append(f"Target assignment fee: {hc.money(v.target_assignment_fee)} (valuation confidence "
                   f"{round(v.confidence_score or 0)}/100).")
    return out


def _agent_dict(c: Contact | None) -> dict[str, Any] | None:
    if c is None:
        return None
    return {"contact_id": c.id, "name": c.name, "company": c.company, "email": c.email,
            "phone": c.phone, "phone_display": format_phone(c.phone) if c.phone else None}


def _action(kind: str, packet: ao.AgentPacket, agent: dict[str, Any] | None) -> str:
    who = (agent or {}).get("name") or "the listing agent"
    firm = f" ({agent['company']})" if agent and agent.get("company") else ""
    phone = (agent or {}).get("phone_display")
    if kind == "followup":
        return (f"Follow up with {who}{firm}: call {phone} first" if phone else f"Follow up with {who}{firm}") + \
            ", then send the short follow-up e-mail if you get voicemail."
    first = (f"E-mail {who}{firm} the note below" if (agent or {}).get("email") else
             f"Call {who}{firm} at {phone} using the 3-sentence script" if phone else f"Contact {who}{firm}")
    then = f", then text the short message to {phone}" if phone and (agent or {}).get("email") else ""
    tail = (" Ask who lives there before any price." if packet.kind == "inquiry" else
            " The 48-hour follow-up call is scheduled automatically when you send.")
    return first + then + "." + tail


def _card(session: Session, lead: Lead, packet: ao.AgentPacket, kind: str, lf: Any = None) -> dict[str, Any]:
    f = lead_facts(lead)
    agent = _agent_dict(session.get(Contact, packet.agent_contact_id))
    ok, why = ao.smtp_ready()
    return {
        "lead_id": lead.id, "kind": kind, "track": packet.track, "packet_kind": packet.kind,
        "label": {"listed_foreclosure": "Urgent: listed foreclosure", "followup": "Follow-up due",
                  "mls_fixer": "Stale MLS fixer"}[kind],
        "address": f["address"], "town": f.get("town"), "county": f.get("county"),
        "what": what_it_is(f, lf), "why": why_it_works(lead, f, packet.anchor, packet.best),
        "action": _action(kind, packet, agent), "agent": agent,
        "subject": packet.subject, "body": packet.email_body, "sms": packet.sms_text,
        "phone_script": packet.phone_script, "mailto": packet.mailto, "anchor": packet.anchor, "best": packet.best,
        "list_price": packet.list_price, "sale_date": packet.sale_date, "days_to_sale": packet.days_to_sale,
        "warnings": packet.warnings, "style": hc.lint(packet.email_body),
        "can_smtp": ok and bool(agent and agent.get("email")), "smtp_note": why if not ok else "",
        "lead_url": f"#/lead/{lead.id}", "inspect_url": f"#/inspect/{lead.id}",
    }


def followup_packet(lead: Lead, packet: ao.AgentPacket) -> ao.AgentPacket:
    """Same summary block, new 2-3 sentence note on top."""
    op = hc.operator()
    st = hc.street(packet.address)
    first = packet.agent_first or "there"
    priced = packet.kind == "offer" and packet.anchor
    note = (f"Following up on my note about {st}. Is the seller still open to a clean, as-is cash offer"
            + (f" around {ao.price_range(packet.anchor, packet.best)}" if priced else "")
            + "? If it helps, I can send the written offer over today.")
    _, tail = hc.split_signature(packet.email_body)
    body = f"Hi {first},\n\n{note}\n\n{tail}"
    me = " ".join(x for x in (op["first"], "with " + op["company"] if op["company"] else "") if x) or "an investor"
    sms = f"Hi {first}, it's {me} following up on {st}. Is the seller open to a cash offer? Reply STOP to opt out."
    script = [f"Hi {first}, it's {me}, following up on what I sent over for {st}.",
              "Did the seller get a chance to look at it, and is there a number that would work for them?",
              "If it helps I can adjust the closing date or the inspection period; can I send the written offer again "
              "today?"]
    subject = f"following up on {st}"
    mailto = f"mailto:{packet.agent_email}?subject={quote(subject)}&body={quote(body)}" if packet.agent_email else None
    return replace(packet, subject=subject, email_body=body, sms_text=sms, phone_script=script, mailto=mailto)


def _recently_contacted(lead: Lead, now: datetime, days: int = 2) -> bool:
    return bool(lead.last_contact_at and now - lead.last_contact_at < timedelta(days=days))


def build_card(session: Session, lead: Lead, now: datetime | None = None) -> dict[str, Any] | None:
    """The card this lead gets right now (None when it has no agent / no offer path)."""
    from engine import fast_track as ft
    due = lead.next_followup_at is not None and lead.next_followup_at <= _now(now)
    try:
        if ft.is_listed_foreclosure(lead.property):
            lf = ft.listed_foreclosure(session, lead)
            if lf.agent is None:
                return None
            packet = ft.build_packet(session, lead, generate_loi=False)
            if due and lead.last_contact_at:
                return _card(session, lead, followup_packet(lead, packet), "followup", lf)
            return _card(session, lead, packet, "listed_foreclosure", lf)
        if ao.agent_contact(lead) is None:
            return None
        packet = ao.build_packet(lead, generate_loi=False, session=session)
    except ValueError:
        return None
    if due and lead.last_contact_at:
        return _card(session, lead, followup_packet(lead, packet), "followup")
    return _card(session, lead, packet, "mls_fixer")


def deck(session: Session, limit: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    from engine import crm
    from engine import fast_track as ft
    now_e = _now(now)
    limit = limit or int(_cfg().get("deck_size", 20))
    cards: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(lead: Lead | None) -> None:
        if lead is None or lead.id in seen or len(cards) >= limit or lead.status_stage in CLOSED:
            return
        card = build_card(session, lead, now)
        if card is not None:
            seen.add(lead.id)
            cards.append(card)

    for lf in ft.listed_foreclosures(session):
        if lf.agent and lf.track not in ("NO_OFFER", "EQUITY_MONITOR") and len(cards) < min(limit, 6):
            lead = session.get(Lead, lf.lead_id)
            if lead is not None and not _recently_contacted(lead, now_e):
                add(lead)
    for lead in crm.due_followups(session, now)[:8]:
        if lead.last_contact_at and any(c.contact_type == ContactType.LISTING_AGENT for c in lead.contacts):
            add(lead)
    for lead in ao.candidates(session, limit * 3):
        if not _recently_contacted(lead, now_e, days=7) and lead.status_stage != "OFFER_SENT_TO_AGENT":
            add(lead)
    from engine import operator_setup
    setup = operator_setup.status()
    return {"goal": daily_goal(), "done": contacted_today(session, now), "cards": cards,
            "setup_complete": setup["live"], "auto_email": setup["auto_email"],
            "sent_24h": ao.sent_last_24h(session, now),
            "daily_email_limit": int(settings().get("outreach", {}).get("daily_email_limit", 25))}


# -- actions -----------------------------------------------------------------------------------------
class AutopilotError(ValueError):
    pass


def _packet_for(session: Session, lead: Lead, kind: str, generate_loi: bool) -> ao.AgentPacket:
    from engine import fast_track as ft
    if ft.is_listed_foreclosure(lead.property):
        packet = ft.build_packet(session, lead, generate_loi=generate_loi)
    else:
        packet = ao.build_packet(lead, generate_loi=generate_loi, session=session)
    return followup_packet(lead, packet) if kind == "followup" else packet


def send(session: Session, lead_id: int, channel: str, *, subject: str | None = None, body: str | None = None,
         now: datetime | None = None, smtp_factory: Any = None) -> dict[str, Any]:
    """Send (SMTP) or log (mailto / sms / call) the card's message, with the operator's edits."""
    if channel not in ("smtp", "mailto", "sms"):
        raise AutopilotError("channel must be smtp, mailto or sms (log a phone call with an outcome button)")
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise AutopilotError(f"lead {lead_id} not found")
    card = build_card(session, lead, now)
    if card is None:
        raise AutopilotError("this lead has no listing-agent contact or offer path any more")
    packet = _packet_for(session, lead, card["kind"], generate_loi=(channel == "smtp" and card["kind"] != "followup"))
    op = hc.operator()
    if body is not None and body.strip():
        if channel in ("smtp", "mailto"):
            if op["postal"] and op["postal"] not in body:
                raise AutopilotError("Keep the summary block under your signature: it carries your mailing address "
                                     "(CAN-SPAM).")
            if not any(x in body for x in ('"no more"', "STOP")):
                raise AutopilotError("Keep the opt-out line (reply \"no more\") in the e-mail.")
        packet = replace(packet, email_body=body.strip())
    if subject is not None and subject.strip():
        packet = replace(packet, subject=subject.strip())
    if channel == "smtp":
        try:
            stage = (ao.send_smtp(session, lead, packet, now=now, smtp_factory=smtp_factory) if card["kind"] != "followup"
                     else _smtp_followup(session, lead, packet, now, smtp_factory))
        except RuntimeError as exc:
            raise AutopilotError(str(exc)) from exc
    elif card["kind"] == "followup":
        stage = _log_followup(session, lead, packet, {"mailto": "email", "sms": "sms"}[channel], now)
    else:
        stage = ao.record_sent(session, lead, packet, {"mailto": "email", "sms": "sms"}[channel], now)
    session.flush()
    return {"lead_id": lead.id, "stage": stage, "channel": channel, "followup_at": lead.next_followup_at.isoformat()
            if lead.next_followup_at else None, "style": hc.lint(packet.email_body),
            "done": contacted_today(session, now), "goal": daily_goal()}


def _log_followup(session: Session, lead: Lead, packet: ao.AgentPacket, channel: str, now: datetime | None) -> str:
    when = ao.followup_time(now)
    session.add(Interaction(lead_id=lead.id, contact_id=packet.agent_contact_id, channel=channel,
                            outcome="AGENT_FOLLOWUP_SENT", notes=packet.subject, next_followup_at=when,
                            debrief_json={"packet": {"kind": "followup"}}, occurred_at=ao._eastern(now)))
    lead.next_followup_at = when
    lead.last_contact_at = ao._eastern(now)
    return lead.status_stage


def _smtp_followup(session: Session, lead: Lead, packet: ao.AgentPacket, now: datetime | None,
                   smtp_factory: Any) -> str:
    """SMTP for a follow-up: same limits and postal-address rule, logged as a follow-up (stage unchanged)."""
    ao.deliver(packet, session=session, now=now, smtp_factory=smtp_factory)
    return _log_followup(session, lead, packet, "email_smtp", now)


def outcome(session: Session, lead_id: int, button: str, *, notes: str = "", now: datetime | None = None,
            out_root: Any = None) -> dict[str, Any]:
    """Outcome buttons: Interested (send offer) / Left voicemail / Not interested / Sold."""
    from engine import crm
    from engine.contracts import ContractBlocked, generate
    if button not in OUTCOMES:
        raise AutopilotError(f"outcome must be one of {', '.join(OUTCOMES)}")
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise AutopilotError(f"lead {lead_id} not found")
    code, message = OUTCOMES[button]
    contact = ao.agent_contact(lead) or crm.best_phone(session, lead)
    r = crm.log_call(session, lead, code, notes=notes or message, contact=contact, channel="phone", now=now)
    files: dict[str, str] = {}
    warnings: list[str] = []
    if button == "interested":
        try:
            packet = _packet_for(session, lead, "offer", generate_loi=False)
            if packet.kind == "offer" and packet.anchor:
                g = generate(session, lead, "loi", price=packet.anchor, out_root=out_root)
                files = {p.suffix.lstrip("."): f"/deals/{lead.property_id}/{p.name}" for p in g.paths}
            else:
                message = ("Logged: they're open to an offer. Confirm who lives there first (occupancy decides the "
                           "FRFPA rules), then the written offer unlocks.")
        except (ContractBlocked, ValueError) as exc:
            warnings.extend(getattr(exc, "reasons", None) or [str(exc)])
    session.flush()
    return {"lead_id": lead.id, "outcome": code, "stage": r.stage, "message": message, "files": files,
            "warnings": warnings, "followup_at": lead.next_followup_at.isoformat() if lead.next_followup_at else None,
            "done": contacted_today(session, now), "goal": daily_goal()}


# -- KPI pills ---------------------------------------------------------------------------------------
def kpis(session: Session, now: datetime | None = None) -> dict[str, Any]:
    from engine import fast_track as ft
    from engine.finance import summary
    now_e = _now(now)
    open_leads = int(session.scalar(select(func.count(Lead.id)).where(
        Lead.status_stage.notin_((LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED)))) or 0)
    ready = int(session.scalar(select(func.count(Lead.id)).where(
        Lead.ready_to_call.is_(True), Lead.status_stage.notin_(CLOSED))) or 0)
    listed = ft.listed_foreclosures(session)
    actionable = [r for r in listed if r.agent and r.track not in ("NO_OFFER", "EQUITY_MONITOR")]
    offers = int(session.scalar(select(func.count(func.distinct(Interaction.lead_id))).where(
        Interaction.outcome == "OFFER_SENT_TO_AGENT", Interaction.occurred_at >= now_e - timedelta(days=30))) or 0)
    cutoff = (now_e - timedelta(days=365)).date().isoformat()
    buyers = 0
    for b in session.scalars(select(CashBuyer)):
        recent = any(isinstance(x, dict) and str(x.get("deed_date") or "") >= cutoff
                     for x in (b.recent_purchases_json or []))
        buyers += 1 if (recent or b.registered_at) else 0
    fin = summary(session)
    return {
        "open_leads": open_leads, "listed_foreclosures": len(listed), "listed_actionable": len(actionable),
        "ready_to_call": ready, "offers_sent_30d": offers, "active_cash_buyers": buyers,
        "pipeline_value": round(fin.pipeline_projected or 0, 2), "contacted_today": contacted_today(session, now),
        "goal": daily_goal(),
    }
