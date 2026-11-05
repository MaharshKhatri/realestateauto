"""B2B listing-agent deal origination: personalized offer packets for stale / distressed MLS listings.

For every listed property we can underwrite, the listing agent's name, brokerage, cell and e-mail are
already in `contacts` (agents/enrichment/listing_agent_enricher.py) — no skip-tracing, no driving. This
module turns the top ones into a ready-to-send packet:

  * a plain 3-4 sentence e-mail (engine/human_copy.py) that cites days on market / price cuts / fixer language and
    floats a price range (our anchor to our walk-away); the two structures, the unrepresented-buyer wording, the
    investor / may-assign disclosure, postal address and opt-out sit under the "--- Offer Summary ---" line;
  * a short SMS for the agent's cell (sent by hand from the operator's phone — never automated);
  * a 1-page LOI PDF (engine/contracts.py, addressed c/o the listing agent);
  * a `mailto:` link, copy-ready text, and an optional SMTP send (Gmail app password in .env), limited to
    `outreach.daily_email_limit` personalized e-mails per rolling 24 hours.

Compliance: we write as unrepresented investor-principals who may assign ("and/or assigns"), invite the
agent to handle the transaction only as the seller's listing agreement and NJ disclosed-dual-agency /
transaction-broker rules allow, and never offer or imply a payment to the agent. Every e-mail carries the
operator's postal address and an opt-out line; an opt-out puts the agent's number and address on the
internal suppression list. Listings that are also in foreclosure go through the same FRFPA gate as any
LOI: when the gate blocks a priced offer, the packet becomes an inquiry (occupancy, seller situation)
with no number in it.
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import settings
from core.mail import GmailSendError, clean_app_password, explain, is_fatal, is_transient, ssl_context
from core.text import format_phone
from db.models import Contact, ContactType, DistressSignal, Interaction, Lead, Property, SignalType, Valuation
from engine.contracts import DEALS_DIR
from engine.scripts import lead_facts, money

NO_OFFER = {"EQUITY_RESCUE_REFERRAL", "FRFPA_BLOCKED", "PASS", "SHORT_SALE_ONLY"}
SENT_OUTCOME = "OFFER_SENT_TO_AGENT"
OPENER_OUTCOME = "AGENT_OPENER_SENT"            # Touch 1: the two-line availability check
TOUCH_OUTCOMES = (SENT_OUTCOME, "AGENT_INQUIRY_SENT", OPENER_OUTCOME, "AGENT_FOLLOWUP_SENT")
REPLY_OUTCOME = "AGENT_REPLIED"                 # engine/reply_negotiator.py logs the agent's answer
PHONE_YES_OUTCOME = "SPOKE_NEEDS_OFFER"         # the agent said "send the offer" on a call (✅ Interested)
# Touch 1 bodies: a question a person would type, nothing else. Structure varies per lead so no two agents at
# the same brokerage see the same template; the priced note (Touch 2) goes out when they answer.
OPENER_BODIES = (
    "Quick question on your listing at {place}. Is it still available, or are you already in attorney review?",
    "Is {place} still available, or is it already in attorney review? Wanted to check before I send anything over.",
    "Checking on {place}: is it still available, or did it already go into attorney review?",
    "Is your listing at {place} still available? I wasn't sure if it's already in attorney review.",
)
OPENER_SUBJECTS = ("{st}", "question on {st}", "{st} - still available?", "{place}")
OPENER_OPT_OUT = 'If you\'d rather not hear from me, reply "no more".'
LOI_LATER_LINE = "Let me know if that works and I'll send over the written offer today."
LOI_ATTACHED_LINE = "The 1-page written offer is attached; let me know if that works for the seller."
OUTREACH_DIR = DEALS_DIR / "agent_blast"
log = logging.getLogger(__name__)


def _cfg() -> dict[str, Any]:
    return settings().get("outreach", {})


@dataclass
class AgentPacket:
    lead_id: int
    property_id: int
    kind: str                       # offer | inquiry
    agent_contact_id: int
    agent_name: str
    agent_first: str
    brokerage: str | None
    agent_email: str | None
    agent_phone: str | None
    address: str
    list_price: float | None
    dom: int | None
    anchor: float | None
    best: float | None
    list_to_mao_75: float | None
    subject: str
    email_body: str
    sms_text: str
    mailto: str | None
    reasons: list[str] = field(default_factory=list)
    loi_files: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    sale_date: str | None = None            # listed foreclosures (engine/fast_track.py)
    days_to_sale: int | None = None
    upset: float | None = None
    track: str | None = None
    phone_script: list[str] = field(default_factory=list)   # 3 sentences for the call (autopilot player)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -- selection ---------------------------------------------------------------------------------------
def agent_contact(lead: Lead) -> Contact | None:
    agents = [c for c in lead.contacts if c.contact_type == ContactType.LISTING_AGENT and (c.email or c.phone)
              and c.dnc_status != "dnc" and (c.confidence_score or 0) > 0]
    # Prefer the agent's own record (name + e-mail + mobile) over a brokerage office line.
    agents.sort(key=lambda c: (not c.email, c.phone_type == "office", not c.phone, -(c.confidence_score or 0)))
    return agents[0] if agents else None


def listing_gap(lead: Lead) -> dict[str, Any]:
    from engine.scoring import listing_terms
    return listing_terms(lead.property)


def eligible(lead: Lead) -> tuple[bool, str]:
    v = lead.property.current_valuation
    if v is None:
        return False, "not underwritten"
    if lead.status_stage in ("DEAD", "DO_NOT_CONTACT", "CLOSED", "UNDER_CONTRACT", "ASSIGNED"):
        return False, f"stage {lead.status_stage}"
    if v.verdict in NO_OFFER:
        return False, f"verdict {v.verdict}"
    gap = listing_gap(lead)
    if not gap.get("list_price"):
        return False, "no asking price"
    if gap.get("valuation_conflict"):
        return False, f"valuation conflict: ARV {gap.get('arv_to_list')}x the asking price — re-check comps"
    from engine.scoring import valuation_suspect
    if valuation_suspect(v):
        return False, "outlier-capped or non-residential valuation — re-check comps before offering"
    # An offer far below ask burns the agent relationship (engine/offer_math.py): a firm number only when our final
    # reaches 80% of the ask with every certainty check passing; 65-80% gets a no-price "what does the seller need?".
    from sqlalchemy.orm import object_session

    from engine.offer_math import firm_offer
    f = firm_offer(lead, object_session(lead))
    if f.tier == "skip":
        return False, ("too far from ask" if any("of the ask" in r for r in f.reasons)
                       else "; ".join(f.reasons) or "no offer")
    # Only deals that can sign get a cold e-mail (the same test the call sheet uses; replies are always answered).
    from engine.offer_math import convertible
    ok, why = convertible(object_session(lead), lead, f)
    if not ok:
        return False, why
    if agent_contact(lead) is None:
        return False, "no listing-agent contact"
    return True, "ok"


def candidates(session: Session, top: int = 50) -> list[Lead]:
    """Top stale listings and listed distress (foreclosure/absentee/tax leads found on the MLS) with an offer path
    and a reachable listing agent, best gap first."""
    stmt = (select(Lead).join(Property, Lead.property_id == Property.id)
            .join(DistressSignal, (DistressSignal.property_id == Property.id)
                  & DistressSignal.signal_type.in_((SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING))
                  & DistressSignal.active.is_(True))
            .join(Valuation, (Valuation.property_id == Property.id) & Valuation.is_current.is_(True))
            .where(Lead.status_stage.notin_(["DEAD", "DO_NOT_CONTACT", "CLOSED"])))
    leads = [lead for lead in dict.fromkeys(session.scalars(stmt)) if eligible(lead)[0]]

    def rank(lead: Lead) -> tuple:
        gap = listing_gap(lead)
        # Striking distance first, then the lead's overall priority (motivation + viability, incl. valuation
        # confidence) — a bigger paper gap alone mostly means a shakier valuation.
        return (-(1 if gap.get("striking_distance") else 0), -(lead.priority_rank_score or 0),
                -(gap.get("list_to_mao_75") or 0))
    return sorted(leads, key=rank)[:top]


# -- packet ------------------------------------------------------------------------------------------
def _operator() -> dict[str, str]:
    from engine.human_copy import operator
    return operator()


def offer_prices(lead: Lead) -> tuple[float | None, float | None]:
    """(offer, offer): one firm number (engine/offer_math.py), never a range. (None, None) when no price goes out."""
    from sqlalchemy.orm import object_session

    from engine.offer_math import firm_offer, locked
    f = locked(lead, firm_offer(lead, object_session(lead)))
    return (f.offer, f.offer) if f.priced else (None, None)


def price_math(lead: Lead) -> str | None:
    """One line under the offer that shows how we got there, so the agent can take it to the seller: renovated sales
    nearby, the repair estimate, what the price has to leave room for. Public sale facts only."""
    from engine.human_copy import money, street
    v = lead.property.current_valuation
    if v is None or not v.arv_estimate:
        return None
    comps = [c for c in (v.comps_json or []) if c.get("sold_price") and c.get("address")]
    # the sales that support the value: renovated ones first, closest to the ARV (2026-11-03 audit: the line quoted two
    # sales far below the value it claimed)
    comps.sort(key=lambda c: (c.get("bucket") != "renovated", abs(float(c["sold_price"]) - float(v.arv_estimate))))
    near = [f"{street(c['address'].split(',')[0])} sold for {money(c['sold_price'])}" for c in comps[:2]]
    line = f"How I got there: fixed-up, it should be worth about {money(v.arv_estimate)}"
    line += f" ({'; '.join(near)})" if near else ""
    if v.est_repair_cost:
        line += f", and I estimate about {money(v.est_repair_cost)} of work"
    return line + ". The price leaves room for the work, holding and closing costs, and the resale."


def price_range(anchor: float | None, best: float | None) -> str:
    from engine.human_copy import money as plain_money
    return f"{plain_money(anchor)} to {plain_money(best)}" if best and anchor and best > anchor else plain_money(anchor)


def agent_terms_line() -> str:
    """Unrepresented-buyer wording: the agent may handle both sides only as the listing agreement and NJ rules allow."""
    return ("Agent: I'm not represented by a buyer's agent. You can act as a disclosed dual agent (with both parties' "
            "informed written consent) or a transaction broker only as your listing agreement and NJ rules allow. "
            "Your compensation stays whatever the listing agreement says; I'm not asking for any part of it.")


def _why_now(f: dict[str, Any]) -> str:
    if f.get("dom"):
        return f"Since it's been on the market about {f['dom']} days, do you"
    if f.get("price_reduced"):
        return "Since the price has already come down, do you"
    if f.get("keywords"):
        return f"Since the listing says \"{f['keywords'][0]}\", do you"
    return "Do you"


def reply_flags_for(session: Session, lead: Lead) -> set[str]:
    """What the agent's latest answer asked for (ASKS_ROLE, ASKS_SHOWING, CALL_ME, BACKUP).

    Their own words are read again with today's parser (a reply logged by an older version keeps its old intents),
    falling back to the intents logged with the reply."""
    from db.models import InboundReply
    from engine import reply_negotiator as rn
    row = session.scalar(select(InboundReply).where(InboundReply.lead_id == lead.id,
                                                    InboundReply.status.not_in(("bounce", "auto_reply")),
                                                    InboundReply.body_text.is_not(None))
                         .order_by(InboundReply.received_at.desc(), InboundReply.id.desc()).limit(1))
    if row is not None:
        from engine.gmail_autopilot import strip_quoted
        return set(rn.parse_reply(strip_quoted(row.body_text)).intents)
    inter = session.scalar(select(Interaction).where(Interaction.lead_id == lead.id, Interaction.outcome == REPLY_OUTCOME)
                           .order_by(Interaction.occurred_at.desc(), Interaction.id.desc()).limit(1))
    if inter is None:
        return set()
    text = (inter.notes or "").split(": ", 1)[-1]
    return set(rn.parse_reply(text).intents) if text else set(((inter.debrief_json or {}).get("intents") or []))


def build_packet(lead: Lead, agent: Contact | None = None, generate_loi: bool = True,
                 out_root: Path | None = None, session: Session | None = None,
                 reply_flags: set[str] | None = None) -> AgentPacket:
    """Stale-listing note to the listing agent: plain 3-4 sentences on top, the full terms under the summary line.

    reply_flags: the agent's answer (from engine.reply_negotiator). The first line answers what they asked: who we are
    ("buyer or realtor?"), a showing, a call request, a backup offer while another contract is pending."""
    from engine import human_copy as hc
    from engine.contracts import ContractBlocked, contract_gate, generate
    from engine.fast_track import is_listed_foreclosure

    if session is not None and agent is None and is_listed_foreclosure(lead.property):
        from engine.fast_track import (
            build_packet as urgent_packet,  # sheriff sale + listing: urgent version
        )
        return urgent_packet(session, lead, generate_loi=generate_loi, out_root=out_root)
    agent = agent or agent_contact(lead)
    if agent is None:
        raise ValueError(f"lead {lead.id} has no reachable listing agent")
    f = lead_facts(lead)
    gap = listing_gap(lead)
    op = hc.operator()
    cfg = _cfg()
    first = hc.first_name(agent.name)
    anchor, best = offer_prices(lead)
    gate = contract_gate(lead, "loi", anchor)
    from engine.offer_math import FOLLOW_TERMS, firm_offer
    firm = firm_offer(lead, session)
    occupancy_first = any("FRFPA" in r for r in firm.reasons) or (anchor and not gate.allowed)
    # one firm number; a no-price "what does the seller need?" when the numbers aren't certain or the gap is wide
    # (engine/offer_math.py); the occupancy question when FRFPA could apply
    kind = ("offer" if gate.allowed and anchor else
            "ask_first" if firm.tier == "ask_first" and not occupancy_first else "inquiry")
    dom = f.get("dom")
    reasons: list[str] = []
    if dom:
        reasons.append(f"{dom} days on market")
    if f.get("price_reduced"):
        reasons.append("a price reduction")
    if f.get("keywords"):
        reasons.append("the listing's own words: " + ", ".join(f'"{k}"' for k in f["keywords"][:3]))
    addr, st, town = f["address"], hc.street(f["address"]), f.get("town") or ""
    ask = gap.get("list_price")
    subject = hc.pick([f"question on {st}", f"{st} - still available?"], lead.id)
    if reply_flags is None and session is not None:
        reply_flags = reply_flags_for(session, lead) if last_touch(session, lead) == "reply" else set()
    flags = set(reply_flags or ())
    answered = "ANSWERED" in flags or (session is not None and last_touch(session, lead) == "reply")   # Touch 2
    attach = generate_loi and session is not None
    if kind == "offer":
        who = ("I'm an investor buying for myself or a partner investor, and I'm not working with a buyer's agent, "
               "so you can handle both sides if your listing agreement allows it. ")
        if "ASKS_ROLE" in flags:
            first_line = ("I'm the buyer: an investor buying for myself or a partner investor, not a realtor, and I'm not "
                          "working with a buyer's agent, so you can handle both sides if your listing agreement allows it. ")
            who = ""
        elif "BACKUP" in flags:
            first_line = ("Thanks, and yes, I'd like to put in a clean backup offer in case the current contract doesn't go "
                          "through. ")
        elif "CALL_ME" in flags:
            first_line = "Thanks, I'll give you a call; so you have the numbers in front of you, here they are in writing. "
        elif answered:
            first_line = f"Thanks for getting back to me on {st.rstrip('.')}. "
        else:
            first_line = f"Quick question on your listing at {st}" + (f" in {town}" if town else "") + ". "
        ask_line = (f"{_why_now(f)} think the seller would look at a clean, as-is cash offer of {price_range(anchor, best)} "
               "with no mortgage contingency and a quick close? ")
        if "BACKUP" in flags:
            ask_line = (f"I can do {price_range(anchor, best)} cash, as-is, no mortgage or appraisal contingency, so the seller "
                   "can move straight to me if the current buyer falls out. ")
        showing = ""
        if "ASKS_SHOWING" in flags:
            # the operator works without a car: photos / video first, a walkthrough during the inspection period
            showing = ("As for seeing it, a few interior photos or a short video would help first, and it can be walked "
                       "during the inspection period once the seller is in that range. ")
            who = "I'm an investor buying for myself or a partner investor, with no buyer's agent. " if who else ""
        paragraphs = [first_line + ask_line + who + showing + (LOI_ATTACHED_LINE if attach else LOI_LATER_LINE)]
        # One firm number and one set of terms, the same as the attached LOI (2026-11-03 audit: "Option 1" had no
        # inspection and a 21-day close while the LOI said a 10-day inspection and 30 days).
        terms = [f"Offer: {hc.money(anchor)} cash, as-is." + (" A 1-page written offer is attached." if attach else "")]
        math = price_math(lead)
        if math:
            terms.append(math)
        terms += list(FOLLOW_TERMS)
    elif kind == "ask_first":
        if "ASKS_ROLE" in flags:
            first_line = ("I'm the buyer: an investor buying for myself or a partner investor, not a realtor, and I'm not "
                          "working with a buyer's agent. ")
        elif answered:
            first_line = f"Thanks for getting back to me on {st.rstrip('.')}. "
        else:
            first_line = f"Quick question on your listing at {st}" + (f" in {town}" if town else "") + ". "
        paragraphs = [first_line + "What would the seller need to net for a clean, as-is cash sale with a quick close? "
                      + ("" if "ASKS_ROLE" in flags else
                         "I'm an investor buying for myself or a partner investor, with no buyer's agent. ")
                      + "If it's a number I can do, I'll send the written offer the same day."]
        terms = ["No price from me yet: I'd rather hear the seller's number than send one that misses.",
                 "Cash, as-is, no mortgage or appraisal contingency; attorney review and an inspection period."]
    else:
        paragraphs = [
            f"Quick question on your listing at {st}" + (f" in {town}" if town else "") + ". "
            "Is the seller living there, or is it vacant or rented? "
            "I'm an investor looking at a clean, as-is cash offer, and I'm not working with a buyer's agent. "
            "Once I know who's in the house I can send you numbers the same day."]
        terms = ["No price yet: the offer depends on who lives in the home (NJ foreclosure-rescue rules for "
                 "owner-occupants)." if not gate.allowed else "Numbers follow once I know the occupancy."]
    summary = [f"Property: {addr}" + (f" (asking {hc.money(ask)})" if ask else ""), *terms, agent_terms_line(),
               hc.investor_disclosure(op["company"]),
               *hc.footer_lines(op, 'Reply "no more" and I won\'t send you offers on other listings.')]
    email = hc.compose(subject, f"Hi {first},", paragraphs, summary, op=op,
                       summary_title=hc.DIVIDER if kind == "offer" else hc.DETAILS)
    me = " ".join(x for x in (op["first"], "with " + op["company"] if op["company"] else "") if x) or "an investor"
    sms = (f"Hi {first}, it's {me}. Is {st} still available? I'm an investor (no buyer's agent) and could do a clean "
           f"as-is cash offer of {hc.money(anchor)} with a quick close. OK to e-mail you the details? "
           if kind == "offer" else
           f"Hi {first}, it's {me}. Quick question on {st}: what would the seller need to net for a cash, as-is sale "
           "with a quick close? I'm an investor (no buyer's agent). "
           if kind == "ask_first" else
           f"Hi {first}, it's {me}. Quick question on {st}: is the seller living there, or is it vacant or rented? "
           "I'm an investor looking at a cash offer. ")
    sms += "Reply STOP to opt out."
    phone = [f"Hi {first}, this is {me}; I'm an investor calling about your listing at {st}.",
             (f"{_why_now(f)} think the seller would look at a clean as-is cash offer of "
              f"{price_range(anchor, best)} with a quick close?" if kind == "offer" else
              "What would the seller need to net for a clean, as-is cash sale with a quick close?"
              if kind == "ask_first" else
              "Is the seller living there, or is it vacant or rented? That decides how I can structure an offer."),
             "I'm not working with a buyer's agent, so you can handle both sides if your listing agreement allows it; "
             "can I e-mail you the written offer today?" if kind == "offer" else
             "I'm not working with a buyer's agent; once I know who's in the house can I e-mail you numbers today?"]
    packet = AgentPacket(
        lead_id=lead.id, property_id=lead.property_id, kind=kind, agent_contact_id=agent.id,
        agent_name=agent.name or "", agent_first=first, brokerage=agent.company, agent_email=agent.email,
        agent_phone=format_phone(agent.phone) if agent.phone else None, address=addr,
        list_price=ask, dom=dom, anchor=anchor if kind == "offer" else None,
        best=best if kind == "offer" else None, list_to_mao_75=gap.get("list_to_mao_75"), subject=email.subject,
        email_body=email.text, sms_text=sms, mailto=email.mailto(agent.email), reasons=reasons,
        warnings=list(gate.warnings), phone_script=phone)
    if not op["postal"]:
        packet.warnings.append("Set your mailing address in My Setup (or operator.mailing_address in "
                               "config/settings.local.yaml): commercial e-mail must include your postal address "
                               "(CAN-SPAM). SMTP sending is disabled until then.")
    if kind == "inquiry":
        packet.warnings.extend(gate.reasons)
    if generate_loi and kind == "offer" and session is not None:
        try:
            g = generate(session, lead, "loi", price=anchor, out_root=out_root)
            packet.loi_files = {p.suffix.lstrip("."): str(p) for p in g.paths}
        except ContractBlocked as exc:
            packet.warnings.extend(exc.reasons)
        except Exception as exc:           # noqa: BLE001 - a PDF problem must never break the send
            log.exception("LOI for lead %s could not be built", lead.id)
            packet.warnings.append(f"The 1-page written offer couldn't be built ({type(exc).__name__}); "
                                   "the e-mail goes without it.")
        if not packet.loi_files.get("pdf"):        # no PDF after all: the text must not promise one
            packet.email_body = packet.email_body.replace(" A 1-page written offer is attached.", "") \
                .replace(LOI_ATTACHED_LINE, LOI_LATER_LINE)
    return packet


def opener_enabled() -> bool:
    """autopilot.first_touch: opener (default) sends the availability check first; offer sends the priced note first."""
    return str((settings().get("autopilot", {}) or {}).get("first_touch", "opener")).lower() == "opener"


def prior_touches(session: Session, lead: Lead) -> int:
    return int(session.scalar(select(func.count(Interaction.id)).where(
        Interaction.lead_id == lead.id, Interaction.outcome.in_(TOUCH_OUTCOMES))) or 0)


def last_touch(session: Session, lead: Lead) -> str | None:
    """Kind of the most recent exchange with the listing agent: opener / offer / inquiry / followup / reply / None."""
    for inter in session.scalars(select(Interaction).where(
            Interaction.lead_id == lead.id, Interaction.outcome.in_(TOUCH_OUTCOMES + (REPLY_OUTCOME, PHONE_YES_OUTCOME)))
            .order_by(Interaction.occurred_at.desc(), Interaction.id.desc()).limit(5)):
        if inter.outcome == PHONE_YES_OUTCOME:
            # "Interested" on a call counts as the agent's answer only when it was the listing agent on the phone.
            c = session.get(Contact, inter.contact_id) if inter.contact_id else None
            if c is not None and c.contact_type == ContactType.LISTING_AGENT:
                return "reply"
            continue
        break
    else:
        return None
    if inter.outcome == REPLY_OUTCOME:
        return "reply"
    return ((inter.debrief_json or {}).get("packet") or {}).get("kind") or {
        SENT_OUTCOME: "offer", "AGENT_INQUIRY_SENT": "inquiry", OPENER_OUTCOME: "opener",
        "AGENT_FOLLOWUP_SENT": "followup"}.get(inter.outcome)


def build_opener(lead: Lead, agent: Contact | None = None) -> AgentPacket:
    """Touch 1: the two-line availability question. No price, no links, no attachment, no formatting.

    The only additions to the question are what the law and the house rules require of any commercial e-mail: the
    operator's name and phone, one line with the company and postal address (CAN-SPAM) and a one-clause opt-out.
    The investor / may-assign disclosure and the terms arrive with the priced note once the agent answers.
    """
    from engine import human_copy as hc
    agent = agent or agent_contact(lead)
    if agent is None:
        raise ValueError(f"lead {lead.id} has no reachable listing agent")
    f = lead_facts(lead)
    gap = listing_gap(lead)
    op = hc.operator()
    first = hc.first_name(agent.name)
    st, town = hc.street(f["address"]), f.get("town") or ""
    place = f"{st} in {town}" if town else st
    body = hc.pick(list(OPENER_BODIES), lead.id).format(place=place)
    subject = hc.pick(list(OPENER_SUBJECTS), lead.id + 1).format(st=st, place=place)
    signature = [x for x in ("Thanks,", op["first"] or op["name"], op["phone"]) if x]
    who = op["company"] or op["name"]
    footer = [x for x in ((f"{who}, {op['postal']}" if op["postal"] else who), OPENER_OPT_OUT) if x]
    text = f"Hi {first},\n\n{body}\n\n" + "\n".join(signature) + "\n\n" + "\n".join(footer)
    me = " ".join(x for x in (op["first"], "with " + op["company"] if op["company"] else "") if x) or "an investor"
    sms = f"Hi {first}, it's {me}. Is {place} still available, or already in attorney review? Reply STOP to opt out."
    phone = [f"Hi {first}, this is {me}, calling about your listing at {st}.",
             "Is it still available, or are you already in attorney review?",
             "Good to know. I'm an investor and I can e-mail you a written offer today; is this the best address for it?"]
    mailto = f"mailto:{agent.email}?subject={quote(subject)}&body={quote(text)}" if agent.email else None
    packet = AgentPacket(
        lead_id=lead.id, property_id=lead.property_id, kind="opener", agent_contact_id=agent.id,
        agent_name=agent.name or "", agent_first=first, brokerage=agent.company, agent_email=agent.email,
        agent_phone=format_phone(agent.phone) if agent.phone else None, address=f["address"],
        list_price=gap.get("list_price"), dom=f.get("dom"), anchor=None, best=None,
        list_to_mao_75=gap.get("list_to_mao_75"), subject=subject, email_body=text, sms_text=sms, mailto=mailto,
        reasons=["Touch 1: availability check. The priced note (Touch 2) goes out when the agent answers."],
        phone_script=phone)
    if not op["postal"]:
        packet.warnings.append("Set your mailing address in My Setup (or operator.mailing_address in "
                               "config/settings.local.yaml): commercial e-mail must include your postal address "
                               "(CAN-SPAM). SMTP sending is disabled until then.")
    return packet


def opener_applies(session: Session, lead: Lead, days_to_sale: int | None = None) -> bool:
    """Touch 1 goes to a listing we have never written to, unless the sheriff sale is inside the urgent window."""
    if not opener_enabled() or prior_touches(session, lead) > 0 or last_touch(session, lead) == "reply":
        return False                       # an agent who already answered (e-mail or phone) gets the offer
    urgent_days = int((settings().get("autopilot", {}) or {}).get("urgent_days_to_sale", 14))
    return not (days_to_sale is not None and 0 <= days_to_sale <= urgent_days)


def first_touch(lead: Lead, session: Session, agent: Contact | None = None, generate_loi: bool = True,
                out_root: Path | None = None) -> AgentPacket:
    """The packet this listing gets now: Touch 1 for a cold agent, else the priced note (urgent foreclosures: always)."""
    days = None
    from engine.fast_track import is_listed_foreclosure, listed_foreclosure
    if is_listed_foreclosure(lead.property):
        days = listed_foreclosure(session, lead).days_to_sale
    if opener_applies(session, lead, days):
        return build_opener(lead, agent)
    return build_packet(lead, agent=agent, generate_loi=generate_loi, out_root=out_root, session=session)


# -- dispatch ----------------------------------------------------------------------------------------
def _eastern(now: datetime | None) -> datetime:
    """Naive America/New_York time, the convention crm.log_call stores interactions in."""
    from engine.crm import TZ
    return (now or datetime.now()).astimezone(TZ).replace(tzinfo=None)


def sent_last_24h(session: Session, now: datetime | None = None, *, cold_only: bool = False) -> int:
    """SMTP e-mails in the last 24 hours. cold_only: just the ones to agents who haven't written back (the daily cap
    is for cold outreach; answering an agent who replied is never held back by it)."""
    since = _eastern(now) - timedelta(hours=24)
    rows = session.execute(select(Interaction.id, Interaction.debrief_json).where(
        Interaction.channel.in_(("email_smtp", "email_gmail")), Interaction.occurred_at >= since)).all()
    return sum(1 for _, d in rows if not (cold_only and (d or {}).get("engaged")))


def smtp_ready() -> tuple[bool, str]:
    from engine import operator_setup
    operator_setup.sync_from_disk()        # My Setup / .env edits made since the server started count right away
    if not (os.environ.get("SMTP_USER") and os.environ.get("SMTP_APP_PASSWORD")):
        return False, "SMTP_USER / SMTP_APP_PASSWORD are not set in .env"
    if not _operator()["postal"]:
        return False, "operator.mailing_address is not set (CAN-SPAM postal address)"
    return True, "ok"


def followup_time(now: datetime | None = None) -> datetime:
    """48 hours later at 10:00, moved off Sunday — the follow-up call to the agent's cell."""
    day = (_eastern(now) + timedelta(hours=48)).date()
    if day.weekday() == 6:
        day += timedelta(days=1)
    return datetime.combine(day, time(10))


def record_sent(session: Session, lead: Lead, packet: AgentPacket, channel: str, now: datetime | None = None,
                message_id: str | None = None, engaged: bool = False) -> str:
    """Log the outreach, move the lead to OFFER_SENT_TO_AGENT and schedule the 48-hour follow-up call.

    message_id: the sent e-mail's Message-ID, kept on the interaction so the agent's reply finds its lead."""
    from engine import crm

    contact = session.get(Contact, packet.agent_contact_id)
    outcome = SENT_OUTCOME if packet.kind == "offer" else "SPOKE_WARM_FOLLOWUP"
    if packet.kind != "offer":
        # An opener or inquiry is not an offer: keep the stage where it is, but schedule the same follow-up.
        inter = Interaction(lead_id=lead.id, contact_id=contact.id if contact else None, channel=channel,
                            outcome=OPENER_OUTCOME if packet.kind == "opener" else "AGENT_INQUIRY_SENT",
                            notes=packet.subject, email_message_id=message_id,
                            debrief_json={"packet": {"kind": packet.kind}, "engaged": engaged},
                            next_followup_at=followup_time(now),
                            occurred_at=_eastern(now))
        session.add(inter)
        lead.next_followup_at = followup_time(now)
        lead.last_contact_at = _eastern(now)
        session.flush()
        return lead.status_stage
    if packet.anchor:
        lead.offer_amount = packet.anchor
    r = crm.log_call(session, lead, outcome, notes=f"{packet.subject} — {money(packet.anchor)} / {money(packet.best)}",
                     contact=contact, channel=channel, followup_at=followup_time(now), now=now,
                     debrief={"packet": {"kind": packet.kind, "anchor": packet.anchor, "best": packet.best},
                              "engaged": engaged})
    if message_id:
        inter = session.get(Interaction, r.interaction_id)
        if inter is not None:
            inter.email_message_id = message_id
            session.flush()
    return r.stage


def new_message_id() -> str:
    """A Message-ID on the sender's own domain (Gmail keeps the one the client sets)."""
    user = os.environ.get("SMTP_USER") or ""
    return make_msgid(domain=user.rsplit("@", 1)[-1] if "@" in user else None)


BOUNCED_KEY = "bounced_emails"


def bounced_emails(session: Session) -> set[str]:
    """Every address a delivery failure came back for (`gmail_autopilot._drop_email` records them)."""
    from db.models import SwarmState
    row = session.get(SwarmState, BOUNCED_KEY)
    return {str(x).lower() for x in ((row.value_json or {}).get("emails") or [])} if row else set()


def record_bounce(session: Session, email: str) -> None:
    from db.models import SwarmState
    addr = (email or "").strip().lower()
    if not addr:
        return
    row = session.get(SwarmState, BOUNCED_KEY)
    if row is None:
        row = SwarmState(key=BOUNCED_KEY, value_json={"emails": []})
        session.add(row)
    emails = list((row.value_json or {}).get("emails") or [])
    if addr not in emails:
        row.value_json = {"emails": emails + [addr]}
        session.flush()


def deliver(packet: AgentPacket, *, session: Session, now: datetime | None = None, smtp_factory: Any = None,
            in_reply_to: str | None = None, references: list[str] | None = None, engaged: bool = False) -> str:
    """Send one personalized e-mail over SMTP (Gmail app password), inside the rolling 24-hour limit.

    Returns the Message-ID. in_reply_to / references answer an agent's e-mail inside its own Gmail thread."""
    ok, why = smtp_ready()
    if not ok:
        raise RuntimeError(why)
    if engaged or in_reply_to:
        # Answering an agent who wrote back is never cold outreach: only Gmail's own ceiling (reply_daily_limit).
        cap = int(_cfg().get("reply_daily_limit", 150))
        if sent_last_24h(session, now) >= cap:
            raise RuntimeError(f"Gmail safety ceiling reached ({cap} e-mails in 24 hours); replies resume tomorrow")
    else:
        limit = int(_cfg().get("daily_email_limit", 25))
        if sent_last_24h(session, now, cold_only=True) >= limit:
            raise RuntimeError(f"daily limit reached ({limit} cold e-mails in 24 hours); replies to agents who wrote "
                               "back still go out")
    if not packet.agent_email:
        raise RuntimeError("agent has no e-mail address")
    if packet.agent_email.strip().lower() in bounced_emails(session):
        # 2026-11-04: a 48-hour nudge went to an address that had bounced ("inbox full") that morning; every repeat
        # bounce counts against the sender's reputation (bounce rate 3.8% in the first week).
        raise RuntimeError(f"{packet.agent_email} bounced before: not sent (call the agent or find a new e-mail)")
    message_id = new_message_id()
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = packet.agent_email
    msg["Subject"] = packet.subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(dict.fromkeys([*(references or []), in_reply_to]))
    msg.set_content(packet.email_body)
    pdf = packet.loi_files.get("pdf")
    try:
        if pdf and Path(pdf).exists():
            msg.add_attachment(Path(pdf).read_bytes(), maintype="application", subtype="pdf",
                               filename=f"Offer - {packet.address}.pdf")
    except OSError as exc:                 # an unreadable PDF must not stop the e-mail (the text stands on its own)
        log.warning("offer PDF not attached for lead %s: %s", packet.lead_id, exc)
    user = os.environ["SMTP_USER"].strip()
    password = clean_app_password(os.environ["SMTP_APP_PASSWORD"])
    waits = [float(w) for w in _cfg().get("smtp_retry_waits", [5, 20])]
    for attempt in range(len(waits) + 1):
        phase = "connect"
        try:
            with (smtp_factory or smtp_connection)() as smtp:
                phase = "login"
                smtp.login(user, password)
                phase = "send"
                smtp.send_message(msg)
            return message_id
        except Exception as exc:           # noqa: BLE001 - every Gmail / network failure becomes a plain sentence
            retry = attempt < len(waits) and is_transient(exc)
            log.warning("SMTP send to %s failed: %s: %s%s", packet.agent_email, type(exc).__name__, exc,
                        f" (trying again in {waits[attempt]:.0f} s)" if retry else "")
            if retry:
                _sleep(waits[attempt])
                if phase == "send":
                    # The line dropped while the message itself was going out: Gmail may have taken it anyway.
                    # Its Sent folder says; send again only when it isn't there (never two offers to one agent).
                    there = sent_folder_has(user, password, message_id)
                    if there:
                        log.info("SMTP: the message to %s is in Gmail's Sent folder after all", packet.agent_email)
                        return message_id
                    if there is None:
                        raise GmailSendError(
                            "Gmail closed the connection while the message was going out and the Sent folder "
                            "couldn't be checked. Look in Gmail's Sent folder: if it isn't there, run the same "
                            "command again.", fatal=True) from exc
                continue
            raise GmailSendError(explain(exc, user, str(_cfg().get("smtp_host", "smtp.gmail.com"))),
                                 fatal=is_fatal(exc)) from exc
    raise AssertionError("unreachable")  # pragma: no cover


_sleep = __import__("time").sleep          # tests swap it out (no real waits)


def sent_folder_has(user: str, password: str, message_id: str) -> bool | None:
    """True / False: the Message-ID is / isn't in Gmail's Sent folder (read-only IMAP). None: couldn't look."""
    from engine import gmail_autopilot as gm
    try:
        with gm.Mailbox(user, password, mailbox="\\Sent") as mb:
            typ, data = mb.imap.uid("SEARCH", None, "HEADER", "Message-ID", f'"{message_id}"')
        if typ != "OK":
            return None
        return bool((data or [b""])[0].split())
    except Exception as exc:               # noqa: BLE001 - no answer is "couldn't look", never a crash
        log.warning("SMTP: couldn't check the Sent folder: %s", exc)
        return None


def smtp_connection() -> smtplib.SMTP:
    """An open, encrypted connection to Gmail (465 = SSL, 587 = STARTTLS), certificates from certifi."""
    host, port = str(_cfg().get("smtp_host", "smtp.gmail.com")), int(_cfg().get("smtp_port", 465))
    if port == 587:
        smtp = smtplib.SMTP(host, port, timeout=30)
        smtp.starttls(context=ssl_context())
        return smtp
    return smtplib.SMTP_SSL(host, port, context=ssl_context(), timeout=30)


def send_smtp(session: Session, lead: Lead, packet: AgentPacket, now: datetime | None = None,
              smtp_factory: Any = None, in_reply_to: str | None = None, references: list[str] | None = None,
              engaged: bool = False) -> str:
    engaged = engaged or bool(in_reply_to)
    message_id = deliver(packet, session=session, now=now, smtp_factory=smtp_factory, in_reply_to=in_reply_to,
                         references=references, engaged=engaged)
    return record_sent(session, lead, packet, "email_smtp", now, message_id=message_id, engaged=engaged)


def opt_out(session: Session, contact: Contact, notes: str = "agent asked to stop") -> None:
    """Agent replied "no more"/STOP: suppress the number now and stop all outreach to them."""
    from engine import compliance
    if contact.phone:
        compliance.add_internal(session, contact.phone, notes=notes)
    contact.dnc_status = "dnc"
    contact.notes = ((contact.notes or "") + " | opted out of offers").strip(" |")
    session.flush()


# -- batch ------------------------------------------------------------------------------------------
def write_batch(session: Session, top: int = 50, out_dir: Path | None = None) -> tuple[Path, list[AgentPacket]]:
    """Packets for the top candidates + an HTML review page (deals/agent_blast/<date>/index.html)."""
    out_dir = out_dir or OUTREACH_DIR / datetime.now().strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    packets = [build_packet(lead, session=session) for lead in candidates(session, top)]
    rows = []
    for p in packets:
        link = f'<a href="{html.escape(p.mailto)}">open e-mail</a>' if p.mailto else "—"
        rows.append(
            f"<tr><td>{html.escape(p.address)}<br><small>{p.dom or '?'} DOM · ask {money(p.list_price)}</small></td>"
            f"<td>{html.escape(p.agent_name)}<br><small>{html.escape(p.brokerage or '')}</small></td>"
            f"<td>{money(p.anchor)} / {money(p.best)}<br><small>{(p.list_to_mao_75 or 0):.0%} of ask</small></td>"
            f"<td>{link}"
            f"<br><small>{html.escape(p.agent_phone or '')}</small></td>"
            f"<td><details><summary>text</summary><pre>{html.escape(p.email_body)}</pre>"
            f"<p><b>SMS:</b> {html.escape(p.sms_text)}</p></details></td></tr>")
    page = ("<!doctype html><meta charset='utf-8'><title>Agent LOI blast</title>"
            "<style>body{font-family:sans-serif;margin:24px}td{border-bottom:1px solid #ddd;padding:6px;vertical-align:top}"
            "pre{white-space:pre-wrap;max-width:640px}</style>"
            f"<h1>Listing-agent offers — {datetime.now():%B %d, %Y}</h1>"
            "<p>Review each one before sending. Log what you send in the cockpit (Agent LOI tab) or with "
            "<code>python manage.py agent-blast --mark-sent LEAD_ID</code>.</p>"
            "<table><tr><th>Listing</th><th>Agent</th><th>Anchor / best</th><th>Send</th><th>Message</th></tr>"
            + "".join(rows) + "</table>")
    index = out_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    return index, packets
