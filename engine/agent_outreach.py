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
import os
import smtplib
import ssl
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import settings
from core.text import format_phone
from db.models import Contact, ContactType, DistressSignal, Interaction, Lead, Property, SignalType, Valuation
from engine.contracts import DEALS_DIR
from engine.scripts import lead_facts, money

NO_OFFER = {"EQUITY_RESCUE_REFERRAL", "FRFPA_BLOCKED", "PASS", "SHORT_SALE_ONLY"}
SENT_OUTCOME = "OFFER_SENT_TO_AGENT"
OUTREACH_DIR = DEALS_DIR / "agent_blast"


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
    # An offer far below ask burns the agent relationship: only listings our 75% ceiling can reach.
    if (gap.get("list_to_mao_75") or 0) < float(_cfg().get("min_list_to_mao_75", 0.75)):
        return False, "too far from ask"
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
    from engine.contracts import default_price
    v = lead.property.current_valuation
    anchor = default_price(lead)
    basis = _cfg().get("max_offer_basis", "mao_70")
    best = (v.mao_aggressive if basis == "mao_75" else v.mao_conservative) if v else None
    a = (v.assumptions_json or {}) if v else {}
    if v and v.verdict == "WORKABLE_82_PCT_FLOOR":
        best = a.get("compliant_offer") or anchor
    ask = listing_gap(lead).get("list_price")
    if ask:
        # Never offer more than the seller is asking; open meaningfully below ask.
        best = min(best, ask) if best else best
        anchor = min(anchor, round(ask * 0.90, -2)) if anchor else anchor
    if anchor and best and best < anchor:
        best = anchor
    return anchor, best


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


def build_packet(lead: Lead, agent: Contact | None = None, generate_loi: bool = True,
                 out_root: Path | None = None, session: Session | None = None) -> AgentPacket:
    """Stale-listing note to the listing agent: plain 3-4 sentences on top, the full terms under the summary line."""
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
    kind = "offer" if gate.allowed and anchor else "inquiry"
    dom = f.get("dom")
    reasons: list[str] = []
    if dom:
        reasons.append(f"{dom} days on market")
    if f.get("price_reduced"):
        reasons.append("a price reduction")
    if f.get("keywords"):
        reasons.append("the listing's own words: " + ", ".join(f'"{k}"' for k in f["keywords"][:3]))
    addr, st, town = f["address"], hc.street(f["address"]), f.get("town") or ""
    fast, std = int(cfg.get("fast_close_days", 21)), int(cfg.get("standard_close_days", 45))
    ask = gap.get("list_price")
    subject = hc.pick([f"question on {st}", f"{st} - still available?"], lead.id)
    if kind == "offer":
        paragraphs = [
            f"Quick question on your listing at {st}" + (f" in {town}" if town else "") + ". "
            f"{_why_now(f)} think the seller would look at a clean, as-is cash offer around "
            f"{price_range(anchor, best)} with no mortgage contingency and a quick close? "
            "I'm an investor buying for myself or a partner investor, and I'm not working with a buyer's agent, "
            "so you can handle both sides if your listing agreement allows it. "
            "Let me know if that range is workable and I'll send over the written offer today."]
        terms = [(f"Option 1: {hc.money(anchor)} cash, as-is, no mortgage or appraisal contingency, close in about "
                  f"{fast} days.")]
        if best and anchor and best > anchor:
            terms.append(f"Option 2: {hc.money(best)} cash, as-is, with a 10-business-day inspection period, close in "
                         f"about {std} days.")
        terms += ["No repairs, cleanout or credits requested; the seller picks the closing date.",
                  "The agreement of sale includes a 3-business-day attorney review."
                  + (" A 1-page written offer is attached." if generate_loi and session is not None else "")]
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
           f"as-is cash offer around {hc.money(anchor)} with a quick close. OK to e-mail you the details? "
           if kind == "offer" else
           f"Hi {first}, it's {me}. Quick question on {st}: is the seller living there, or is it vacant or rented? "
           "I'm an investor looking at a cash offer. ")
    sms += "Reply STOP to opt out."
    phone = [f"Hi {first}, this is {me}; I'm an investor calling about your listing at {st}.",
             (f"{_why_now(f)} think the seller would look at a clean as-is cash offer around "
              f"{price_range(anchor, best)} with a quick close?" if kind == "offer" else
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
    return packet


# -- dispatch ----------------------------------------------------------------------------------------
def _eastern(now: datetime | None) -> datetime:
    """Naive America/New_York time, the convention crm.log_call stores interactions in."""
    from engine.crm import TZ
    return (now or datetime.now()).astimezone(TZ).replace(tzinfo=None)


def sent_last_24h(session: Session, now: datetime | None = None) -> int:
    since = _eastern(now) - timedelta(hours=24)
    return int(session.scalar(select(func.count(Interaction.id)).where(
        Interaction.channel == "email_smtp", Interaction.occurred_at >= since)) or 0)


def smtp_ready() -> tuple[bool, str]:
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


def record_sent(session: Session, lead: Lead, packet: AgentPacket, channel: str, now: datetime | None = None) -> str:
    """Log the outreach, move the lead to OFFER_SENT_TO_AGENT and schedule the 48-hour follow-up call."""
    from engine import crm

    contact = session.get(Contact, packet.agent_contact_id)
    outcome = SENT_OUTCOME if packet.kind == "offer" else "SPOKE_WARM_FOLLOWUP"
    if packet.kind != "offer":
        # An inquiry is not an offer: keep the stage where it is, but schedule the same follow-up.
        inter = Interaction(lead_id=lead.id, contact_id=contact.id if contact else None, channel=channel,
                            outcome="AGENT_INQUIRY_SENT", notes=packet.subject,
                            debrief_json={"packet": {"kind": packet.kind}}, next_followup_at=followup_time(now),
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
                     debrief={"packet": {"kind": packet.kind, "anchor": packet.anchor, "best": packet.best}})
    return r.stage


def deliver(packet: AgentPacket, *, session: Session, now: datetime | None = None, smtp_factory: Any = None) -> None:
    """Send one personalized e-mail over SMTP (Gmail app password), inside the rolling 24-hour limit."""
    ok, why = smtp_ready()
    if not ok:
        raise RuntimeError(why)
    limit = int(_cfg().get("daily_email_limit", 25))
    if sent_last_24h(session, now) >= limit:
        raise RuntimeError(f"daily limit reached ({limit} e-mails in 24 hours)")
    if not packet.agent_email:
        raise RuntimeError("agent has no e-mail address")
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = packet.agent_email
    msg["Subject"] = packet.subject
    msg.set_content(packet.email_body)
    pdf = packet.loi_files.get("pdf")
    if pdf and Path(pdf).exists():
        msg.add_attachment(Path(pdf).read_bytes(), maintype="application", subtype="pdf",
                           filename=f"Offer - {packet.address}.pdf")
    host, port = _cfg().get("smtp_host", "smtp.gmail.com"), int(_cfg().get("smtp_port", 465))
    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30))
    with factory() as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_APP_PASSWORD"])
        smtp.send_message(msg)


def send_smtp(session: Session, lead: Lead, packet: AgentPacket, now: datetime | None = None,
              smtp_factory: Any = None) -> str:
    deliver(packet, session=session, now=now, smtp_factory=smtp_factory)
    return record_sent(session, lead, packet, "email_smtp", now)


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
