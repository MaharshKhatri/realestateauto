"""Fast-track queues for the two highest-leverage stacks the signal stacker finds.

1. **Listed foreclosures** — a sheriff-sale property that is also on the MLS. The seller already wants to sell,
   there is a hard auction date, and the listing agent (reachable without skip tracing) earns nothing if the
   house goes to the sale. Each one gets an honest *track* from its numbers:

   CASH_OFFER           deal verdict, FRFPA exempt → as-is cash offer (anchor / best, never above ask)
   FRFPA_FLOOR_OFFER    owner-occupied (FRFPA-covered) and a buyer still works at ≥ 82% of FMV → attorney-drafted
   CONFIRM_OCCUPANCY    exemption only presumed → the agent confirms who lives there, then the offer goes out
   SHORT_SALE           upset/payoff above what the house supports → a cash offer the agent submits to the lender
                        for short-sale approval (the negotiation stays with the agent and the seller's attorney)
   EQUITY_MONITOR       big equity, covered owner-occupant → the agent's retail sale is the seller's best path;
                        no lowball, watch for price cuts
   NO_OFFER             blocked / not valued / non-residential

   Scripts never offer to stop, postpone or negotiate the foreclosure (a regulated distressed-property service).

2. **Portfolio owners** — one absentee owner behind several distressed / absentee leads (PORTFOLIO_OWNER
   signals): a call or letter that names every one of their NJ addresses at once.

Run: python manage.py fast-track [--top 30] [--packets] [--portfolios 20] [--portfolio-letters 20]
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from agents.base import AgentContext, BaseAgent
from core.config import settings
from core.dates import long_date
from core.text import format_phone, postal_title
from db.models import Contact, DistressSignal, Interaction, Lead, LeadStage, Property, SignalType
from engine.agent_outreach import AgentPacket, agent_contact
from engine.contracts import DEALS_DIR, ContractBlocked, contract_gate, generate
from engine.scripts import DONT_SAY_FORECLOSURE, lead_facts, money, speaking_name

TRACK_ORDER = ["CASH_OFFER", "FRFPA_FLOOR_OFFER", "CONFIRM_OCCUPANCY", "SHORT_SALE", "EQUITY_MONITOR", "NO_OFFER"]
TRACK_LABEL = {
    "CASH_OFFER": "cash offer now", "FRFPA_FLOOR_OFFER": "FRFPA floor offer (attorney-drafted)",
    "CONFIRM_OCCUPANCY": "confirm occupancy, then offer", "SHORT_SALE": "short-sale offer via the agent",
    "EQUITY_MONITOR": "seller has equity — monitor", "NO_OFFER": "no offer",
}
DEAL_VERDICTS = {"STRONG", "WORKABLE", "TIGHT"}
FAR_BELOW_ASK = 0.60          # a cash offer under 60% of ask isn't a credible first call
LISTING_TYPES = (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)
OPEN_STAGES_EXCLUDED = [LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED]


def _op() -> dict[str, str]:
    op = settings().operator
    digits = "".join(ch for ch in (op.get("phone") or "") if ch.isdigit())
    return {"name": op.get("name") or "", "company": op.get("company_name") or "",
            "phone": format_phone(digits) or "", "email": op.get("email") or "", "postal": op.get("mailing_address") or ""}


# -- listed foreclosures -----------------------------------------------------------------------------
@dataclass
class ListedForeclosure:
    lead_id: int
    address: str
    town: str
    county: str
    sale_date: str | None
    days_to_sale: int | None
    upset: float | None
    list_price: float | None
    dom: int | None
    listing_url: str | None
    agent: dict[str, Any] | None
    verdict: str | None
    frfpa_track: str | None
    track: str
    anchor: float | None
    best: float | None
    floor: float | None
    arv: float | None
    as_is: float | None
    priority: float
    stage: str
    last_outreach: str | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["track_label"] = TRACK_LABEL[self.track]
        return d


def _sheriff(prop: Property) -> DistressSignal | None:
    return next((s for s in prop.active_signals if s.signal_type == SignalType.SHERIFF_SALE), None)


def _listing(prop: Property) -> DistressSignal | None:
    return next((s for s in prop.active_signals if s.signal_type == SignalType.STALE_LISTING), None) or \
        next((s for s in prop.active_signals if s.signal_type == SignalType.ACTIVE_LISTING), None)


def is_listed_foreclosure(prop: Property) -> bool:
    return _sheriff(prop) is not None and _listing(prop) is not None


def deal_track(lead: Lead) -> tuple[str, list[str]]:
    """Which honest offer path this listed foreclosure supports, and why."""
    from engine.scoring import valuation_suspect
    from engine.underwriter import frfpa_decision
    v = lead.property.current_valuation
    if v is None:
        return "NO_OFFER", ["not underwritten yet — run the underwriter"]
    if valuation_suspect(v):
        return "NO_OFFER", ["valuation was outlier-capped or the parcel is non-residential — re-check comps"]
    decision = frfpa_decision(lead.property)
    verdict = v.verdict or ""
    if verdict == "FRFPA_BLOCKED" or verdict == "PASS":
        return "NO_OFFER", [f"verdict {verdict}"]
    if verdict == "EQUITY_RESCUE_REFERRAL":
        return "EQUITY_MONITOR", ["owner-occupied with large equity: a retail sale by the listing agent nets the "
                                  "seller more than any investor offer"]
    if verdict == "WORKABLE_82_PCT_FLOOR":
        return "FRFPA_FLOOR_OFFER", ["FRFPA covers the owner-occupant: offer ≥ 82% of FMV on an attorney-drafted "
                                     "contract with the 10-business-day cancellation right"]
    if verdict == "SHORT_SALE_ONLY":
        return "SHORT_SALE", ["the upset/payoff is above what the house supports — needs lender short-sale approval"]
    if verdict in DEAL_VERDICTS:
        if decision.applies:
            return "FRFPA_FLOOR_OFFER", ["FRFPA presumed to apply (owner-occupied): offer at or above the floor"]
        if decision.confidence == "presumed" and not lead.occupancy_confirmed_at:
            return "CONFIRM_OCCUPANCY", [f"exemption only presumed ({decision.basis}) — ask the agent who lives there"]
        return "CASH_OFFER", [f"verdict {verdict}; FRFPA exempt ({decision.basis})"]
    return "NO_OFFER", [f"verdict {verdict or 'none'}"]


def offer_numbers(lead: Lead) -> tuple[float | None, float | None, float | None]:
    """(anchor, best, FRFPA floor) — never above the asking price, never below the FRFPA floor when it applies."""
    from engine.agent_outreach import offer_prices
    from engine.underwriter import frfpa_decision
    v = lead.property.current_valuation
    pct = float(settings().get("compliance", {}).get("frfpa_min_pct_fmv", 0.82))
    floor = round(v.as_is_value * pct, -2) if v and v.as_is_value and frfpa_decision(lead.property).applies else None
    anchor, best = offer_prices(lead)
    if v is not None and v.verdict == "SHORT_SALE_ONLY":
        anchor, best = v.anchor_offer, v.mao_conservative
        ask = (_listing(lead.property).raw_metadata_json or {}).get("list_price") if _listing(lead.property) else None
        if ask:
            best = min(best, ask) if best else best
            anchor = min(anchor, round(ask * 0.90, -2)) if anchor else anchor
    if floor:
        anchor = max(anchor or 0, floor) or None
        best = max(best or 0, anchor or 0) or None
    return anchor, best, floor


def listed_foreclosure(session: Session, lead: Lead, today: date | None = None) -> ListedForeclosure:
    today = today or date.today()
    p = lead.property
    sheriff, lst = _sheriff(p), _listing(p)
    meta = (lst.raw_metadata_json or {}) if lst else {}
    f = lead_facts(lead)
    v = p.current_valuation
    track, reasons = deal_track(lead)
    anchor, best, floor = offer_numbers(lead) if track in ("CASH_OFFER", "FRFPA_FLOOR_OFFER", "SHORT_SALE") \
        else (None, None, None)
    ask = meta.get("list_price")
    if track == "CASH_OFFER" and ask and best and best < FAR_BELOW_ASK * ask:
        track = "NO_OFFER"
        reasons = [f"our best ({money(best)}) is only {best / ask:.0%} of the {money(ask)} ask — re-check comps "
                   "and the listing before calling with a number"]
        anchor = best = floor = None
    agent = agent_contact(lead)
    sent = session.scalar(select(Interaction).where(
        Interaction.lead_id == lead.id, Interaction.outcome.in_(("OFFER_SENT_TO_AGENT", "AGENT_INQUIRY_SENT")))
        .order_by(Interaction.occurred_at.desc()).limit(1))
    sale = sheriff.scheduled_auction_date if sheriff else None
    return ListedForeclosure(
        lead_id=lead.id, address=f["address"], town=f["town"], county=p.county or "",
        sale_date=sale.isoformat() if sale else None, days_to_sale=(sale - today).days if sale else None,
        upset=sheriff.amount_owed if sheriff else None, list_price=meta.get("list_price"),
        dom=meta.get("days_on_market"), listing_url=meta.get("url") or (lst.source_url if lst else None),
        agent={"contact_id": agent.id, "name": agent.name, "company": agent.company, "phone": agent.phone,
               "phone_display": format_phone(agent.phone) if agent.phone else None, "email": agent.email}
        if agent else None,
        verdict=v.verdict if v else None, frfpa_track=lead.frfpa_track, track=track, anchor=anchor, best=best,
        floor=floor, arv=v.arv_estimate if v else None, as_is=v.as_is_value if v else None,
        priority=lead.priority_rank_score or 0, stage=lead.status_stage,
        last_outreach=sent.occurred_at.isoformat() if sent else None, reasons=reasons)


def listed_foreclosures(session: Session, include_past: bool = False, today: date | None = None
                        ) -> list[ListedForeclosure]:
    """Every open lead with an active sheriff sale AND an MLS listing, best opportunities first."""
    today = today or date.today()
    listed = select(DistressSignal.property_id).where(DistressSignal.active.is_(True),
                                                      DistressSignal.signal_type.in_(LISTING_TYPES))
    stmt = (select(Lead).join(Property, Lead.property_id == Property.id)
            .join(DistressSignal, (DistressSignal.property_id == Property.id)
                  & (DistressSignal.signal_type == SignalType.SHERIFF_SALE) & DistressSignal.active.is_(True))
            .where(Property.id.in_(listed), Lead.status_stage.notin_(OPEN_STAGES_EXCLUDED))
            .options(selectinload(Lead.property).selectinload(Property.signals), selectinload(Lead.contacts)))
    rows = [listed_foreclosure(session, lead, today) for lead in session.scalars(stmt).unique()]
    if not include_past:
        rows = [r for r in rows if r.days_to_sale is None or r.days_to_sale >= 0]

    def rank(r: ListedForeclosure) -> tuple:
        soon = r.days_to_sale if r.days_to_sale is not None and r.days_to_sale > 0 else 999
        return (TRACK_ORDER.index(r.track), r.agent is None, soon, -r.priority)
    return sorted(rows, key=rank)


def _countdown(lf: ListedForeclosure) -> str:
    if lf.days_to_sale is None:
        return "a scheduled sheriff's sale"
    if lf.days_to_sale <= 0:
        return f"a sheriff's sale listed for today ({lf.sale_date}) — it may have been adjourned"
    when = long_date(date.fromisoformat(lf.sale_date), year=False) if lf.sale_date else ""
    return f"a {lf.county} County sheriff's sale scheduled for {when} ({lf.days_to_sale} days)"


def agent_script(lf: ListedForeclosure) -> dict[str, Any]:
    """Phone script for the listing agent: urgent, B2B, truthful, FRFPA-safe."""
    op = _op()
    first = ((lf.agent or {}).get("name") or "").split(" ")[0].title() or "there"
    intro = (f"this is {op['name']} with {op['company']} — I'm a real estate investor, unrepresented. "
             f"I'm calling about your listing at {lf.address}. Public records show {_countdown(lf)}"
             + (f", with an upset of about {money(lf.upset)}" if lf.upset else "") +
             ". If it goes to the sheriff's sale your seller loses the house and the listing ends with it. "
             "I can put an as-is cash contract in front of your seller today and close before the sale date — "
             "no financing or appraisal contingency, no repair requests.")
    opening = f"Hi {first}, {intro}"
    questions = [
        "Is the seller living there, or is it vacant / tenant-occupied? (Record the answer — it decides whether the "
        "Foreclosure Rescue Fraud Prevention Act applies.)",
        "Do you know the current payoff, and has the lender agreed to anything — a short-sale review, a payoff letter?",
        "Has the sale already been adjourned? (Just for timing — the seller's attorney handles the sale itself.)",
        "What would the seller need to net to say yes?",
        "Can you send interior photos or do a quick video walk-through? I'll firm up the number the same day.",
    ]
    offer = {
        "CASH_OFFER": f"Our number: {money(lf.anchor)} cash as-is, up to {money(lf.best)} with a 10-business-day "
                      "inspection period. Proof of funds and a written offer within the hour.",
        "FRFPA_FLOOR_OFFER": f"Because it's the seller's home and in foreclosure, NJ's Foreclosure Rescue Fraud "
                             f"Prevention Act applies: an attorney drafts our contract, the seller gets a 10-business-"
                             f"day cancellation right, and at least 82% of fair market value — our offer starts at "
                             f"{money(lf.anchor)} (floor {money(lf.floor)}).",
        "CONFIRM_OCCUPANCY": "Once you confirm who lives there, I'll send a written as-is cash offer the same day.",
        "SHORT_SALE": f"The payoff looks higher than the house supports as-is, so this probably needs lender approval. "
                      f"We'll put in a clean cash offer at {money(lf.best)} that you can send to the lender with the "
                      "short-sale package — the negotiation stays with you and the seller's attorney.",
        "EQUITY_MONITOR": "Honestly, with the equity your seller has, your retail sale is probably their best path. "
                          "If the price drops or time runs out, I'm a cash backup — keep my number.",
        "NO_OFFER": "I'd like to follow the listing — could you send me the details and any price updates?",
    }[lf.track]
    return {
        "opening": opening, "intro": intro, "questions": questions, "offer": offer,
        "close": "Can I send the written offer to your e-mail now so you can present it today?",
        "never_say": DONT_SAY_FORECLOSURE + [
            "\"We'll stop or postpone the sheriff's sale\" — we can't, and offering to is a regulated service",
            "\"We'll negotiate with the lender\" — the short-sale negotiation belongs to the agent and the seller's "
            "attorney",
            "Any hint you'll pay the agent or share their commission",
        ],
        "disclosure": "We're investors acting as principals, not licensed agents; we may assign the contract to an "
                      "affiliated investor; the seller should have an attorney review everything.",
    }


def build_packet(session: Session, lead: Lead, generate_loi: bool = True, out_root: Path | None = None
                 ) -> AgentPacket:
    """Urgent listing-agent e-mail + SMS (+ LOI when the compliance gate allows a priced offer)."""
    lf = listed_foreclosure(session, lead)
    if lf.agent is None:
        raise ValueError(f"lead {lead.id} has no reachable listing agent")
    agent = session.get(Contact, lf.agent["contact_id"])
    op = _op()
    script = agent_script(lf)
    priced = lf.track in ("CASH_OFFER", "FRFPA_FLOOR_OFFER", "SHORT_SALE") and lf.anchor
    gate = contract_gate(lead, "loi", lf.anchor) if priced else None
    kind = "offer" if gate is not None and gate.allowed else "inquiry"
    subject = (f"Cash offer before the {_countdown(lf).split(' (')[0].replace('a ', '', 1)} — {lf.address}"
               if kind == "offer" else f"Before the sheriff's sale — {lf.address}")
    body = "\n".join(x for x in [
        f"Hi {(agent.name or 'there').split(' ')[0].title()},", "",
        script["intro"][0].upper() + script["intro"][1:].replace("I'm calling about", "I'm writing about"), "",
        (script["offer"] if kind == "offer" else
         "Before I put a number in writing: is the seller living there, and do you have a current payoff? With those "
         "two answers I can send a written cash offer the same day."), "",
        ("A 1-page letter of intent is attached; the agreement of sale includes a 3-business-day attorney review"
         + (", and for the seller's own home the statutory 10-business-day cancellation right." if lf.floor else ".")
         if kind == "offer" else None),
        "" if kind == "offer" else None,
        "I'm not represented by a buyer's agent and I'm not asking for any part of your commission. We're investors "
        "acting as principals, not licensed agents, buying as \"" + op["company"] + " and/or assigns\" — we may assign "
        "the contract to an affiliated investor; the seller's price and terms don't change. We can't stop or postpone "
        "the sale, and we don't negotiate with the lender — that stays with you and the seller's attorney.", "",
        "Can we talk today?", "",
        op["name"], op["company"] + (f" · {op['phone']}" if op["phone"] else "") + (f" · {op['email']}" if op["email"] else ""),
        op["postal"] or None, "",
        "If you'd rather not get offers from me, reply \"no more\" and I'll stop.",
    ] if x is not None)
    sms = (f"Hi {(agent.name or 'there').split(' ')[0].title()}, {op['name']} with {op['company']} (investor, "
           f"unrepresented). Re {lf.address}: saw the sheriff's sale "
           + (f"on {long_date(date.fromisoformat(lf.sale_date), year=False)} " if lf.sale_date else "")
           + "— we can close cash before it. "
           + (f"Offer {money(lf.anchor)} as-is. " if kind == "offer" else "Is the seller living there? ")
           + "OK to e-mail? Reply STOP to opt out.")
    packet = AgentPacket(
        lead_id=lead.id, property_id=lead.property_id, kind=kind, agent_contact_id=agent.id, agent_name=agent.name or "",
        agent_first=(agent.name or "there").split(" ")[0].title(), brokerage=agent.company, agent_email=agent.email,
        agent_phone=format_phone(agent.phone) if agent.phone else None, address=lf.address, list_price=lf.list_price,
        dom=lf.dom, anchor=lf.anchor if kind == "offer" else None, best=lf.best if kind == "offer" else None,
        list_to_mao_75=None, subject=subject, email_body=body, sms_text=sms,
        mailto=(f"mailto:{agent.email}?subject={quote(subject)}&body={quote(body)}" if agent.email else None),
        reasons=lf.reasons, warnings=list(gate.warnings) if gate else [], sale_date=lf.sale_date,
        days_to_sale=lf.days_to_sale, upset=lf.upset, track=lf.track)
    if gate is not None and not gate.allowed:
        packet.warnings.extend(gate.reasons)
    if not op["postal"]:
        packet.warnings.append("Set operator.mailing_address (CAN-SPAM) before e-mailing.")
    if generate_loi and kind == "offer":
        try:
            g = generate(session, lead, "loi", price=lf.anchor, out_root=out_root)
            packet.loi_files = {p.suffix.lstrip("."): str(p) for p in g.paths}
        except ContractBlocked as exc:
            packet.warnings.extend(exc.reasons)
    return packet


# -- portfolio owners --------------------------------------------------------------------------------
@dataclass
class Portfolio:
    key: str
    owner: str | None
    mailing: str | None
    lead_ids: list[int]
    properties: list[dict[str, Any]]
    total_arv: float
    deal_count: int
    best_lead_id: int
    priority: float
    phone: dict[str, Any] | None = None

    @property
    def size(self) -> int:
        return len(self.properties)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["size"] = self.size
        return d


def portfolios(session: Session, top: int = 50, min_size: int = 2) -> list[Portfolio]:
    """Group the stacker's PORTFOLIO_OWNER signals back into owners, richest opportunity first."""
    from engine.crm import best_phone
    sigs = session.scalars(select(DistressSignal).where(DistressSignal.signal_type == SignalType.PORTFOLIO_OWNER,
                                                        DistressSignal.active.is_(True))).all()
    groups: dict[str, list[int]] = {}
    for s in sigs:
        ids = tuple(sorted((s.raw_metadata_json or {}).get("property_ids") or []))
        if len(ids) >= min_size:
            groups.setdefault(",".join(map(str, ids)), list(ids))
    out: list[Portfolio] = []
    for key, prop_ids in groups.items():
        leads = session.scalars(select(Lead).where(Lead.property_id.in_(prop_ids),
                                                   Lead.status_stage.notin_(OPEN_STAGES_EXCLUDED))
                                .options(selectinload(Lead.property), selectinload(Lead.contacts))).all()
        if len(leads) < min_size:
            continue
        props = []
        for lead in sorted(leads, key=lambda x: -(x.priority_rank_score or 0)):
            p, v = lead.property, lead.property.current_valuation
            props.append({"lead_id": lead.id, "address": postal_title(p.street_address), "town": postal_title(
                p.city or p.municipality), "county": p.county,
                "signals": sorted({s.signal_type for s in p.active_signals if s.signal_type != SignalType.PORTFOLIO_OWNER}),
                "verdict": v.verdict if v else None, "arv": v.arv_estimate if v else None,
                "mao": v.mao_conservative if v else None, "stage": lead.status_stage,
                "priority": lead.priority_rank_score or 0})
        phone = None
        for lead in leads:
            c = best_phone(session, lead)
            if c is not None and c.contact_type != "LISTING_AGENT":
                phone = {"contact_id": c.id, "lead_id": lead.id, "name": c.name, "phone": c.phone,
                         "phone_display": format_phone(c.phone)}
                break
        first = leads[0].property
        sample = next((s for s in sigs if s.property_id in prop_ids), None)
        meta = (sample.raw_metadata_json or {}) if sample else {}
        out.append(Portfolio(
            key=key, owner=meta.get("owner") or first.owner_name, mailing=meta.get("mailing"),
            lead_ids=[x["lead_id"] for x in props], properties=props,
            total_arv=sum(x["arv"] or 0 for x in props),
            deal_count=sum(1 for x in props if x["verdict"] in ("STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT")),
            best_lead_id=props[0]["lead_id"], priority=max(x["priority"] for x in props), phone=phone))
    out.sort(key=lambda p: (-p.deal_count, -p.priority, -p.size))
    return out[:top]


def _address_list(p: Portfolio, limit: int = 4) -> str:
    items = [f"{x['address']} in {x['town']}" for x in p.properties[:limit]]
    more = p.size - len(items)
    text = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
    return text + (f" (and {more} more)" if more > 0 else "")


def portfolio_script(p: Portfolio) -> dict[str, Any]:
    op = _op()
    who = speaking_name(p.owner) if p.owner else ""
    return {
        "opening": (f"Hi{(' ' + who) if who else ''}, this is {op['name']} with {op['company']}, a local real estate "
                    f"investor — not an agent. I noticed you own {_address_list(p)}. I buy rentals and older houses "
                    "as-is, and I'm interested in one or more of yours."),
        "questions": [
            "Are any of them a headache right now — vacancy, a tough tenant, repairs, taxes?",
            "Would you consider selling one, several, or the whole group if the price and timing were right?",
            "Are they rented? What are the rents, and are there leases? (We buy with tenants in place.)",
            "Who else is on title — a partner, an LLC member, family?",
            "What would you need to walk away with on the one you'd sell first?",
        ],
        "value": [
            "One closing or several, on your schedule; no repairs, no cleanouts, no commissions.",
            "Tenants can stay — we take over the leases and deposits at closing.",
            "Your attorney reviews everything; there's a 3-business-day attorney review on the contract.",
        ],
        "disclosure": "We're investors acting as principals, not licensed agents, and may assign the contract to "
                      "another investor.",
        "close": "Could I send you a written number on the first one this week?",
    }


def write_portfolio_batch(session: Session, top: int = 20, out_dir: Path | None = None) -> tuple[Path, list[Any]]:
    """One letter per portfolio owner (PDF + Click2Mail CSV + manifest), listing all of their addresses."""
    from engine import direct_mail as dm
    out_dir = out_dir or DEALS_DIR / "mail_batches" / f"portfolio_{datetime.now():%Y-%m-%d_%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    op = _op()
    letters: list[Any] = []
    covered: list[list[int]] = []          # lead ids each letter speaks for (all logged by mark_mailed)
    for p in portfolios(session, top):
        lead = session.get(Lead, p.best_lead_id)
        if lead is None or not dm.eligible(session, lead, all_verdicts=True, include_callable=True)[0]:
            continue
        recipient, lines, notes = dm.mailing_block(lead)
        paragraphs = [
            f"I'm writing because public records show you own {_address_list(p, limit=6)}.",
            "I'm a local real estate investor. I buy rentals and older houses as-is — tenants in place, no repairs, no "
            "cleanout, no commissions — and I'd be glad to make an offer on one of them, several, or all of them, on "
            "whatever timeline works for you.",
            f"If selling any of them is on your mind, call or text me at {op['phone'] or 'the number below'}.",
            f"So you know who we are: {op['company']} is a real estate investment company. We are not licensed real "
            "estate agents and we don't represent you. If we agree on a price, we may buy the property ourselves or "
            "assign our contract to another investor, and you should have your own attorney review any agreement — "
            "there is a 3-business-day attorney review period before anything is binding.",
            "If you'd rather not hear from us, call or text STOP, or write to us at the address above, and we will "
            "remove your address right away.",
        ]
        who = speaking_name(p.owner) if p.owner else ""
        letters.append(dm.Letter(lead_id=lead.id, kind="portfolio", recipient=recipient, address_lines=lines,
                                 salutation=f"Dear {who}," if who else "Dear Property Owner,", paragraphs=paragraphs,
                                 property_address=_address_list(p, limit=6), notes=notes + [f"portfolio {p.lead_ids}"]))
        covered.append(p.lead_ids)
    proof = not dm.operator_ready()
    if letters:
        dm.write_pdf(letters, out_dir / "portfolio.pdf", proof)
    with (out_dir / "mailing_list.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Name", "Address1", "Address2", "City", "State", "Zip", "lead_id", "letter", "property_address"])
        for lt in letters:
            city, state, zipc = dm.split_city_state_zip(lt.address_lines[-1] if len(lt.address_lines) > 1 else "")
            w.writerow([lt.recipient, lt.address_lines[0], " ".join(lt.address_lines[1:-1]), city, state, zipc,
                        lt.lead_id, lt.kind, lt.property_address])
    (out_dir / "manifest.json").write_text(json.dumps(
        {"created": datetime.now().isoformat(timespec="seconds"), "proof": proof,
         "letters": [{"lead_id": lid, "kind": "portfolio"} for ids in covered for lid in ids]}, indent=1))
    return out_dir, letters


# -- swarm agent -------------------------------------------------------------------------------------
class FastTrackAgent(BaseAgent):
    name = "fast_track"
    description = "Listed-foreclosure sniper queue (tracks, agents, countdown) and portfolio-owner groups."

    def run(self, ctx: AgentContext) -> None:
        s = ctx.session
        rows = listed_foreclosures(s)
        ctx.records_processed = len(rows)
        tracks: dict[str, int] = {}
        for r in rows:
            tracks[r.track] = tracks.get(r.track, 0) + 1
        actionable = [r for r in rows if r.agent and r.track not in ("NO_OFFER", "EQUITY_MONITOR")]
        untouched = [r for r in actionable if not r.last_outreach]
        urgent = [r for r in untouched if r.days_to_sale is not None and 0 < r.days_to_sale <= 14]
        ctx.metrics.update({"listed_foreclosures": len(rows), "tracks": tracks, "actionable": len(actionable),
                            "not_contacted": len(untouched), "sale_within_14_days_not_contacted": len(urgent),
                            "portfolio_owners": len(portfolios(s, top=1000))})
        ctx.note(f"Fast track: {len(rows)} listed foreclosures ({', '.join(f'{k} {v}' for k, v in tracks.items())}); "
                 f"{len(untouched)} actionable not contacted yet, {len(urgent)} with a sale within 14 days.")
        for r in urgent[:5]:
            ctx.note(f"Call today: #{r.lead_id} {r.address} — sale in {r.days_to_sale} days, agent "
                     f"{(r.agent or {}).get('name')} ({TRACK_LABEL[r.track]}).")
