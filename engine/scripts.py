"""Pre-call battle cards: situation-specific scripts, discovery questions, offer strategy and objections.

Deterministic (works offline, numbers come straight from wholesaling.db); engine/ai.py can optionally
rephrase for tone. Playbooks:
  A  Imminent sheriff sale — homeowner / defendant
  B  Estate / deceased-owner foreclosure or inherited property — executor, administrator, heirs
  C  Tired out-of-state / long-tenure absentee landlord
  D  Stale MLS listing — listing agent (B2B)
  E  Cash buyer / flipper disposition pitch

Compliance is part of every card, not an afterthought (CLAUDE.md "NJ Legal & Market Rules"):
  * NJ telemarketing (N.J.S.A. 56:8-119 et seq.): name, company, phone number and purpose within 30 s.
  * Investor/principal disclosure, may assign the contract, attorney review encouraged (Consumer Fraud Act).
  * FRFPA: never offer to stop/postpone a foreclosure or negotiate with the lender (a regulated
    "distressed property service"); covered owner-occupied deals need >= 82% of FMV and an attorney-drafted
    contract with a 10-business-day cancellation right.
  * Call recording: NJ is one-party consent; FL, CA, PA, MD, MA, WA (and others) require everyone's consent.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from core.config import settings
from core.text import format_phone
from db.models import CashBuyer, Contact, ContactType, Lead, SignalType

TWO_PARTY_CONSENT_STATES = {"CA", "CT", "FL", "IL", "MD", "MA", "MI", "MT", "NV", "NH", "PA", "WA", "OR"}
HELP_RESOURCES = [
    "HUD-approved housing counseling (free): 1-800-569-4287 · hud.gov/counseling",
    "Legal Services of New Jersey hotline: 1-888-576-5529 · lsnj.org",
    "NJ Foreclosure Mediation Program (owner-occupied homes) · njcourts.gov",
    "The county sheriff's foreclosure unit (sale status and procedures)",
]
NO_OFFER_VERDICTS = {"FRFPA_BLOCKED", "PASS", "EQUITY_RESCUE_REFERRAL"}
DONT_SAY_FORECLOSURE = [
    "\"I can stop / postpone / delay your foreclosure\" or \"I'll get the sale adjourned for you\"",
    "\"I'll negotiate with your bank\" or \"I can save your credit\"",
    "\"I'm here to help you as your advisor\" — you are a buyer, not their representative",
    "Any number above your walk-away price, or a promise you can't put in writing",
]


def money(v: float | None) -> str:
    return f"${v:,.0f}" if v is not None else "—"


@dataclass
class Objection:
    key: str
    label: str
    response: str


@dataclass
class BattleCard:
    playbook: str
    title: str
    lead_id: int | None
    contact: dict[str, Any]
    facts: dict[str, Any]
    opening: str
    disclosure: str
    discovery: list[str]
    value: list[str]
    offer: dict[str, Any]
    close: str
    voicemail: str
    objections: list[Objection]
    dont_say: list[str] = field(default_factory=list)
    compliance: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# -- facts ------------------------------------------------------------------------------------------
def lead_facts(lead: Lead) -> dict[str, Any]:
    p = lead.property
    v = p.current_valuation
    a = (v.assumptions_json or {}) if v else {}
    sheriff = next((s for s in p.active_signals if s.signal_type == SignalType.SHERIFF_SALE), None)
    stale = next((s for s in p.active_signals if s.signal_type == SignalType.STALE_LISTING), None)
    smeta = (sheriff.raw_metadata_json or {}) if sheriff else {}
    lmeta = (stale.raw_metadata_json or {}) if stale else {}
    days = (sheriff.scheduled_auction_date - date.today()).days if sheriff and sheriff.scheduled_auction_date else None
    tenure = (date.today() - p.last_sale_date).days / 365.25 if p.last_sale_date else None
    debt = sheriff.amount_owed if sheriff else None
    fmv = v.as_is_value if v else p.estimated_value
    return {
        "address": f"{(p.street_address or '').title()}, {(p.city or p.municipality or '').title()}",
        "town": (p.city or p.municipality or "").title(), "county": p.county, "zip": p.zip_code,
        "year_built": p.year_built, "units": p.dwellings, "sqft": p.sqft or (v.sqft_used if v else None),
        "annual_taxes": p.annual_taxes, "tenure_years": round(tenure) if tenure else None,
        "owner_state": p.owner_state, "owner_name": p.owner_name,
        "sale_date": sheriff.scheduled_auction_date.strftime("%B %d") if sheriff and sheriff.scheduled_auction_date else None,
        "days_to_sale": days, "plaintiff": (sheriff.plaintiff_or_lienholder or "").title()[:80] if sheriff else None,
        "upset": debt, "adjournments": smeta.get("adjournments_total"),
        "defendant_adjournments": smeta.get("defendant_adjournments"), "docket": sheriff.case_number if sheriff else None,
        "arv": v.arv_estimate if v else None, "fmv": fmv, "repairs": v.est_repair_cost if v else None,
        "rehab_tier": v.rehab_tier if v else None, "anchor": v.anchor_offer if v else None,
        "walkaway": v.mao_conservative if v else None, "ceiling": v.mao_aggressive if v else None,
        "fee": v.target_assignment_fee if v else None, "verdict": v.verdict if v else None,
        "frfpa_track": a.get("frfpa_track") or lead.frfpa_track, "frfpa_applies": a.get("frfpa_applies"),
        "frfpa_floor": a.get("frfpa_floor"), "compliant_offer": a.get("compliant_offer"),
        "equity_at_risk": a.get("equity_at_risk") or ((fmv - debt) if fmv and debt else None),
        "verify": a.get("verify_before_contract") or [],
        "dom": lmeta.get("days_on_market"), "list_price": lmeta.get("list_price"),
        "price_reduced": lmeta.get("price_reduced"), "keywords": lmeta.get("keywords") or [],
        "listing_url": lmeta.get("url"),
    }


def pick_contact(lead: Lead) -> Contact | None:
    order = {ContactType.HEIR_EXECUTOR: 0, ContactType.OWNER: 1, ContactType.LISTING_AGENT: 2,
             ContactType.ENTITY_PRINCIPAL: 3, ContactType.RELATIVE: 4, ContactType.REGISTERED_AGENT: 5}
    contacts = sorted(lead.contacts, key=lambda c: (c.dnc_status == "dnc", not c.phone, order.get(c.contact_type, 9),
                                                     -(c.confidence_score or 0)))
    return contacts[0] if contacts else None


def choose_playbook(lead: Lead, contact: Contact | None) -> str:
    sig = {s.signal_type for s in lead.property.active_signals}
    if (contact and contact.contact_type == ContactType.LISTING_AGENT) or (
            SignalType.STALE_LISTING in sig and SignalType.SHERIFF_SALE not in sig):
        return "D"
    estate = (contact and contact.contact_type == ContactType.HEIR_EXECUTOR) or bool(lead.property.is_estate_owned) \
        or SignalType.ESTATE_OWNER in sig or SignalType.PROBATE in sig
    if estate:
        return "B"
    if SignalType.SHERIFF_SALE in sig:
        return "A"
    return "C"


# -- objection matrix -------------------------------------------------------------------------------
def objection_matrix(f: dict[str, Any], playbook: str) -> list[Objection]:
    op = settings().operator
    offer_line = (f"my number is based on what similar homes need and sell for — around {money(f.get('arv'))} after "
                  f"about {money(f.get('repairs'))} of work" if f.get("arv") else
                  "my number is based on recent sales nearby and the repairs the house needs")
    if f.get("verdict") in NO_OFFER_VERDICTS:
        too_low = Objection("too_low", "Your offer is too low / what would you pay?",
                            "Honestly, with the equity you have, an investor's cash price probably isn't your best "
                            "option — a market sale through a listing agent will likely net you more. I'd rather tell "
                            "you that than lowball you. Want the free counseling numbers and a few agent names?")
    else:
        too_low = Objection("too_low", "Your offer is too low",
                  f"I hear you. Fair question — {offer_line}. A buyer like me pays cash, takes it as-is, and you skip "
                  "repairs, showings and commissions. If you list it you may net more; I'm happy to walk through both "
                  "so you can compare. Which matters more to you right now — top dollar or speed and certainty?")
    items = [
        too_low,
        Objection("realtor", "Are you a realtor?",
                  f"No — I'm a real estate investor with {op.get('company_name')}, not a licensed agent, and I don't "
                  "represent you. If we agree on terms I'd either buy it myself or assign my contract to another "
                  "investor, and your attorney reviews everything before it's binding."),
        Objection("prove_close", "How do I know you can close?",
                  "Everything closes through a NJ title company or attorney — you don't get paid by me directly. "
                  "The deposit is held in escrow, you'll see proof of funds before closing, and there's a 3-business-day "
                  "attorney review where your lawyer can cancel for any reason."),
        Objection("lawyer", "My lawyer has to review it",
                  "Absolutely — I'd insist on it. Our contract includes a 3-business-day attorney review period. "
                  "Can I send it to you and your attorney today so the clock can start?"),
        Objection("family", "I need to talk to my family / spouse",
                  "Of course — everyone on title needs to be comfortable. Would it help if I joined a quick call with "
                  "them, or sent a one-page summary of the numbers so you're all looking at the same thing?"),
        Objection("list_it", "I'm going to list it with an agent",
                  "That can be a great option if the house shows well and you have time. Rough math: commission ~5–6%, "
                  "the NJ transfer fee, repairs a buyer's inspector will ask for, and months of taxes "
                  f"({money(f.get('annual_taxes'))}/yr here). If my number nets you close to that without the hassle, "
                  "great; if not, list it — no hard feelings. Can I check back in 30 days?"),
        Objection("why_less", "Why would I sell for less than it's worth?",
                  "Only if certainty and speed are worth more to you than the extra dollars. I'm paying for the house "
                  "as-is: no repairs, no cleanout, no showings, no appraisal or mortgage approval to fall through."),
        Objection("how_info", "How did you get my information?",
                  "It's from public records — the county's property tax records" +
                  (" and the sheriff's sale listing" if f.get("sale_date") else "") +
                  ". If you'd rather I not call again, just say so and I'll take you off my list today."),
        Objection("stop_calling", "Take me off your list / don't call",
                  "Understood — I'm removing your number right now. Sorry for the bother, and have a good day. "
                  "[Log the call as DNC immediately — it is added to the internal do-not-call list.]"),
        Objection("other_offers", "I already have other offers",
                  "Good — you should compare. Ask each one: is it all-cash, any inspection or financing contingency, "
                  "who pays closing costs, and when do they close? If mine's cleaner, I'd like a shot at matching."),
        Objection("assign", "Are you going to flip it / sell my contract?",
                  "Possibly — I'm upfront about that. I may buy it myself or assign the contract to another investor I "
                  "work with. Your price and terms don't change either way, and the contract says so in writing."),
        Objection("need_time", "I need more time to think",
                  "Totally fair. What would you need to see to feel good about it? I'll follow up on a day that works "
                  "for you — would Thursday be okay?"),
    ]
    if playbook in ("A", "B") and f.get("sale_date"):
        items.insert(1, Objection(
            "wait_sale", "I'm going to wait until after the sheriff sale",
            f"That's your call. Just so you have the full picture: at the sale on {f.get('sale_date')} the house goes "
            f"to the highest bidder starting from roughly the upset amount ({money(f.get('upset'))}), and anything above "
            "what's owed has to be claimed through the court afterwards. A sale before the auction lets you control the "
            "price and timing. For questions about the sale itself, the sheriff's office or an attorney is the right call "
            "— I can't advise on that."))
        items.insert(2, Objection(
            "modification", "I'm working with my lender on a modification",
            "That's worth pursuing and I don't want to get in the way. If it works out, great. If it doesn't, keep my "
            "number — I'd want to know before the sale date rather than after."))
    if playbook == "D":
        items = [
            Objection("list_firm", "The seller won't take less than list",
                      f"Understood. It's been on {f.get('dom') or 'a while'} days — if the seller's priority shifts to "
                      "certainty, a clean as-is offer with no financing contingency and a fast close is on the table. "
                      "Can I send an LOI so you have it in the file?"),
            Objection("no_assign", "We don't do assignable contracts",
                      "No problem — we can close in our company's name, or I can disclose the end buyer up front. "
                      "What does the seller need to feel comfortable?"),
            Objection("proof", "Send proof of funds first",
                      "Of course — I'll send proof of funds with the LOI (from our funding partner/buyer), plus the "
                      "title company we use."),
            Objection("other_offers_agent", "We have other offers",
                      "Great — is the seller still taking offers? What would make ours stand out: price, close date, "
                      "or no contingencies?"),
        ] + [o for o in items if o.key in ("realtor", "prove_close", "assign")]
    return items


# -- playbooks ---------------------------------------------------------------------------------------
def _intro(contact_name: str, about: str) -> tuple[str, str]:
    op = settings().operator
    phone = format_phone("".join(ch for ch in (op.get("phone") or "") if ch.isdigit())) or "[your callback number]"
    opening = (f"Hi, is this {contact_name or '[name]'}? My name is {op.get('name')} with {op.get('company_name')} — "
               f"my number is {phone}. I'm calling about {about}. Do you have two minutes?")
    disclosure = settings().get("compliance", {}).get(
        "wholesaler_disclosure",
        "I'm a real estate investor, not a licensed agent. If we reach an agreement, I may buy the property myself "
        "or assign my contract to another investor, and your attorney reviews everything before it's binding.")
    return opening, disclosure


def offer_block(f: dict[str, Any]) -> dict[str, Any]:
    verdict = f.get("verdict")
    if verdict in NO_OFFER_VERDICTS:
        return {"type": "no_offer", "say": "Don't name a price. Explain the options honestly; the best outcome for "
                "this owner is likely a market sale through a listing agent before the auction.",
                "equity_at_risk": f.get("equity_at_risk")}
    if verdict == "WORKABLE_82_PCT_FLOOR":
        return {"type": "frfpa_compliant", "offer": f.get("compliant_offer"), "floor": f.get("frfpa_floor"),
                "say": f"Any offer must be at least {money(f.get('frfpa_floor'))} (82% of fair market value) and uses an "
                       "attorney-drafted contract with a 10-business-day cancellation right."}
    if f.get("frfpa_applies"):
        return {"type": "frfpa_floor", "anchor": max(f.get("anchor") or 0, f.get("frfpa_floor") or 0) or None,
                "walkaway": f.get("walkaway"), "floor": f.get("frfpa_floor"),
                "say": f"Owner-occupancy unconfirmed: price at or above {money(f.get('frfpa_floor'))} unless they confirm "
                       "nobody (or only tenants) lives there — then record it and re-run underwriting."}
    return {"type": "standard", "anchor": f.get("anchor"), "walkaway": f.get("walkaway"),
            "ceiling": f.get("ceiling"), "fee": f.get("fee"),
            "say": f"Open at {money(f.get('anchor'))}. Walk away above {money(f.get('walkaway'))}; "
                   f"{money(f.get('ceiling'))} only if the buyer side is confirmed."}


def build_battle_card(lead: Lead, contact: Contact | None = None) -> BattleCard:
    contact = contact or pick_contact(lead)
    f = lead_facts(lead)
    pb = choose_playbook(lead, contact)
    name = (contact.name if contact else None) or f.get("owner_name") or ""
    cdict = {"name": name, "type": contact.contact_type if contact else None,
             "phone": format_phone(contact.phone) if contact and contact.phone else None,
             "phone_type": contact.phone_type if contact else None, "dnc": contact.dnc_status if contact else None,
             "email": contact.email if contact else None, "company": contact.company if contact else None}
    compliance = ["Call 8am–9pm recipient time; never call a number marked DNC — mail or door-knock instead.",
                  "Say your name, company, callback number and why you're calling in the first 30 seconds."]
    if f.get("owner_state") in TWO_PARTY_CONSENT_STATES:
        compliance.append(f"Owner is in {f['owner_state']} (all-party consent state): ask before recording the call.")
    resources: list[str] = []
    dont_say: list[str] = []
    if pb == "A":
        card = _playbook_a(f, name)
        dont_say, resources = DONT_SAY_FORECLOSURE, HELP_RESOURCES
    elif pb == "B":
        card = _playbook_b(f, name, contact)
        if f.get("sale_date"):
            dont_say, resources = DONT_SAY_FORECLOSURE, HELP_RESOURCES
    elif pb == "D":
        card = _playbook_d(f, name)
        compliance = ["Business call to the agent's advertised number about their listing; still honor any "
                      "do-not-call request immediately.",
                      "If the agent represents both sides, NJ requires disclosed dual agency with both parties' "
                      "informed written consent."]
    else:
        card = _playbook_c(f, name)
    opening, disclosure = _intro(name, card.pop("about"))
    offer = offer_block(f)
    if pb == "D":
        disclosure = ("We're investors buying as principals, not licensed agents. Our offers may name "
                      f"\"{settings().operator.get('company_name')} and/or assigns\" — we'll say so in writing.")
        if f.get("list_price") and f.get("anchor"):
            card["value"].insert(1, f"Our opening number is {f['anchor'] / f['list_price']:.0%} of list "
                                    f"({money(f['anchor'])} vs {money(f['list_price'])}) — expect a counter; "
                                    "lead with terms (cash, as-is, close date).")
    if offer["type"] == "no_offer":
        card["close"] = ("Honestly, an investor's cash offer probably isn't your best move — there's real equity here. "
                         "Can I text you the free counseling numbers below? If you'd like, I can also suggest a few "
                         "experienced local listing agents — no charge, and I don't get paid for referrals.")
    if f.get("frfpa_applies"):
        compliance.append("FRFPA applies until occupancy is confirmed otherwise: >= 82% of FMV, attorney-drafted contract, "
                          "10-business-day cancellation, closing at an attorney/title office.")
    for item in f.get("verify") or []:
        compliance.append(f"Verify on this call: {item}.")
    return BattleCard(playbook=pb, title=card.pop("title"), lead_id=lead.id, contact=cdict, facts=f, opening=opening,
                      disclosure=disclosure, offer=offer, objections=objection_matrix(f, pb),
                      dont_say=dont_say, compliance=compliance, resources=resources, **card)


def _playbook_a(f: dict, name: str) -> dict:
    days = f.get("days_to_sale")
    when = f"on {f['sale_date']}" + (f" — {days} days from now" if days is not None and days >= 0 else "")
    used = f.get("defendant_adjournments") or 0
    return {
        "title": "Playbook A — Imminent sheriff sale (homeowner)",
        "about": f"the house at {f['address']}",
        "discovery": [
            "Are you still living in the house, or is it rented or empty right now? (Record the answer — it decides "
            "whether the foreclosure-rescue law applies.)",
            "Who else is on the title or the mortgage?",
            f"I saw it's listed for a sheriff's sale {when}. What's your plan between now and then?",
            "Have you talked with your lender, an attorney or a housing counselor? (If not, share the free resources below.)",
            "If you could wave a magic wand, what would happen with the house?",
            "What condition is it in — roof, heating, kitchen, baths? Anything that needs work?",
            "Is there a number you'd need to walk away with to make a sale work for you?",
        ],
        "value": [
            f"Public records show about {money(f.get('upset'))} owed (the plaintiff's upset amount); similar homes nearby "
            f"suggest it's worth around {money(f.get('fmv'))} as-is. That's roughly {money(f.get('equity_at_risk'))} of "
            "equity on paper.",
            "At a sheriff's sale you don't control the price, and any surplus has to be claimed through the court later.",
            "A sale before the auction lets you choose the buyer, the price and the move-out date. Options: list with an "
            "agent if there's time, or sell as-is to an investor like me with no repairs or showings.",
            f"Sale adjournments so far: {f.get('adjournments') or 0} ({used} by the homeowner). Questions about the sale "
            "itself go to the sheriff's office or an attorney — don't advise on postponing.",
        ],
        "close": "Would it make sense for me to put together a written offer you and your attorney can look at? "
                 "If it doesn't beat your other options, you just say no.",
        "voicemail": (f"Hi {name or 'there'}, this is {settings().operator.get('name')} with "
                      f"{settings().operator.get('company_name')}, a local real estate investor — not an agent. I'm calling "
                      f"about {f['address']}. If you're considering selling before {f.get('sale_date') or 'the sale date'}, "
                      "I'd like to talk. My number is [callback]. Thanks."),
    }


def _playbook_b(f: dict, name: str, contact: Contact | None) -> dict:
    is_executor = bool(contact and contact.contact_type == ContactType.HEIR_EXECUTOR)
    disc = [
        "I'm sorry for your loss. Is this a good time, or would another day be better?",
        "Who's handling the estate — has the will been probated, or have Letters of Administration been issued by the "
        "Surrogate? (Only the executor/administrator can sign a contract for the estate.)",
        "Does anyone live in the house now — family, tenants — or is it empty? (Record it: it decides whether the "
        "foreclosure-rescue law applies.)",
        "Are all the heirs on the same page about selling?",
        "What's in the house — furniture, belongings? Would a buyer who takes it as-is help?",
        "Any timing pressure — taxes, insurance, the mortgage, a sale date?",
    ]
    value = [
        "We buy as-is: no repairs, no cleanout — take what matters to you and leave the rest.",
        "We close through a NJ attorney/title company on the estate's schedule; your estate attorney reviews everything.",
        "NJ inheritance tax waivers (Form L-8/L-9 or 0-1) may be needed to release the lien at closing — the estate "
        "attorney handles that; we work around their timeline.",
    ]
    if f.get("sale_date"):
        value.insert(0, f"The property is scheduled for a sheriff's sale on {f['sale_date']} (about {money(f.get('upset'))} "
                        "owed). A sale before then lets the estate keep the equity instead of claiming surplus through "
                        "the court. Questions about the sale itself go to the sheriff's office or the estate attorney.")
    return {
        "title": "Playbook B — Estate / inherited property" + (" (executor/administrator)" if is_executor else " (heirs)"),
        "about": f"the property at {f['address']}" + (" and the estate" if is_executor else ""),
        "discovery": disc, "value": value,
        "close": "Would it help if I sent a simple written offer to you and the estate's attorney? There's no obligation, "
                 "and nothing is binding until the attorney approves it.",
        "voicemail": (f"Hi {name or 'there'}, this is {settings().operator.get('name')} with "
                      f"{settings().operator.get('company_name')}. I'm a local investor — not an agent — calling about "
                      f"{f['address']}. I'm sorry for your loss. If the estate is considering selling the house as-is, "
                      "I'd be glad to help make it simple. My number is [callback]."),
    }


def _playbook_c(f: dict, name: str) -> dict:
    taxes = f.get("annual_taxes")
    age = f"built in {f['year_built']}" if f.get("year_built") else "an older building"
    return {
        "title": "Playbook C — Tired absentee / out-of-state landlord",
        "about": f"your property at {f['address']}",
        "discovery": [
            "Is it rented right now? How are the tenants — paying on time?",
            "How long since the roof, heating system and electrical were updated?",
            "How are you managing it from where you are — property manager, family, yourself?",
            "Have you thought about selling it in the next year or so? What's held you back?",
            "If you sold, what would you do with the money?",
        ],
        "value": [
            f"Property taxes here run about {money(taxes)} a year, and it's {age}" +
            (f" — you've owned it about {f['tenure_years']} years." if f.get("tenure_years") else "."),
            "We buy as-is with the tenants in place (NJ's Anti-Eviction Act means leases stay with the property), "
            "so you don't have to empty it or fix anything.",
            "No commissions, no repairs, no showings — and we close on your timeline through a NJ attorney/title company.",
        ],
        "close": "Would you be open to a no-obligation written offer so you have a number to compare against?",
        "voicemail": (f"Hi {name or 'there'}, this is {settings().operator.get('name')} with "
                      f"{settings().operator.get('company_name')}, a local investor (not an agent). I'm calling about "
                      f"{f['address']} — if you've ever thought about selling it as-is, tenants and all, I'd love to talk. "
                      "My number is [callback]."),
    }


def _playbook_d(f: dict, name: str) -> dict:
    kw = ", ".join(f.get("keywords") or [])
    return {
        "title": "Playbook D — Stale MLS listing (listing agent)",
        "about": f"your listing at {f['address']}",
        "discovery": [
            "Is it still available? Any offers in, and has anything fallen through?",
            "What's the seller's situation — why are they selling, and is the timeline flexible?",
            "Occupied or vacant? Any known issues the inspection would find?",
            "What's the lowest number the seller has said they'd consider, roughly?",
            "Would the seller consider an assignable contract, or should we close in our company name?",
        ],
        "value": [
            f"It's been on the market {f.get('dom') or '?'} days at {money(f.get('list_price'))}" +
            (f"; the listing mentions: {kw}" if kw else "") + ".",
            "We write clean, as-is cash offers: no mortgage contingency, no appraisal, quick close through a NJ title company.",
            "Happy to have you write it up for us so you're on both sides of the deal — NJ allows that as disclosed dual "
            "agency with both parties' written consent.",
            "We're investors and may assign the contract to an affiliated buyer — we'll say so in writing up front.",
        ],
        "close": f"Can I send a written LOI at {money(f.get('anchor'))} today so you can present it to the seller?",
        "voicemail": (f"Hi {name or 'there'}, {settings().operator.get('name')} with {settings().operator.get('company_name')} "
                      f"— I'm an investor looking at your listing at {f['address']}. We can do a clean as-is cash offer "
                      "with a quick close. Give me a call at [callback]. Thanks!"),
    }


def marketable(lead: Lead | None, today: date | None = None) -> bool:
    """A deal may be shown to buyers only once the seller contract is signed and attorney review has ended."""
    today = today or date.today()
    return bool(lead and lead.contract_date and lead.attorney_review_ends and lead.attorney_review_ends < today)


def buyer_display_name(buyer: CashBuyer) -> str:
    name = buyer.contact_name or buyer.entity_name or ""
    return "" if name.upper().startswith("OWNER @") else name.title() if name.isupper() else name


def build_buyer_pitch(buyer: CashBuyer, lead: Lead | None = None) -> BattleCard:
    """Playbook E — disposition / buy-box call to a cash buyer."""
    recent = next((p for p in (buyer.recent_purchases_json or []) if isinstance(p, dict) and p.get("address")), None)
    hook = (f"I noticed you picked up {recent['address'].title()} in {(recent.get('municipality') or recent.get('county') or '').title()} "
            f"for {money(recent.get('price'))}" if recent else "I see you've been buying in the area")
    f = lead_facts(lead) if lead else {}
    if lead and marketable(lead):
        deal = f" I have one under contract in {f['town']}: about {money(f.get('arv'))} after repairs, needs ~{money(f.get('repairs'))}."
    elif lead:
        deal = f" I'm working on something in {f['town']} — what's your buy box there?"
    else:
        deal = ""
    op = settings().operator
    who = buyer_display_name(buyer)
    opening = (f"Hi{', is this ' + who + '?' if who else ' there.'} This is {op.get('name')} with {op.get('company_name')}. "
               f"{hook}. We put off-market 1–4 families under contract in that area — do you have a minute?")
    return BattleCard(
        playbook="E", title="Playbook E — Cash buyer / flipper (disposition)", lead_id=lead.id if lead else None,
        contact={"name": who or buyer.entity_name, "type": "BUYER", "mailing": buyer.mailing_address},
        facts={**f, "buyer_purchases": buyer.cash_purchases_count, "buyer_towns": buyer.target_municipalities,
               "buyer_range": [buyer.min_arv, buyer.max_arv]},
        opening=opening + deal,
        disclosure="We wholesale: you'd buy by assignment of our purchase contract (assignment fee disclosed in the "
                   "assignment agreement), closing through a NJ title company.",
        discovery=["What's your buy box right now — towns, price range, property type?",
                   "How heavy a rehab will you take on? Do you hold rentals or flip?",
                   "How fast can you close, and do you use cash or hard money?",
                   "Can you send proof of funds so I can send you deals first?",
                   "What's the best way to send deals — email or text?"],
        value=[f"Recorded purchases: {buyer.cash_purchases_count} totalling {money(buyer.total_volume)}"
               f" ({money(buyer.min_arv)}–{money(buyer.max_arv)}), last on {buyer.last_purchase_date}.",
               "You get first look at off-market contracts before they go to the wider list."],
        offer={"type": "buyer", "say": "Never market a property until the seller contract is signed and attorney review "
                                       "has ended; market the contract, not the house."},
        close="If I send you the next one that fits that box, can you get back to me within 24 hours?",
        voicemail=f"Hi, this is {op.get('name')} with {op.get('company_name')} — I have off-market deals in your area. Call me at [callback].",
        objections=[Objection("no_wholesalers", "I don't buy from wholesalers",
                              "Fair — a lot of deals are overpriced. I underwrite at 70% of ARV minus repairs and send the "
                              "comps with every deal. Judge the next one on its numbers."),
                    Objection("send_it", "Just send me what you have",
                              "Will do — what's your email, and what's an instant no for you?")],
        compliance=["Don't market a property you only have an offer on; disclose the assignment fee in the assignment."])


def render_markdown(card: BattleCard) -> str:
    f = card.facts
    out = [f"# {card.title}", "", f"**Lead #{card.lead_id}** · {f.get('address', '')}  ", ""]
    c = card.contact
    out += [f"**Calling:** {c.get('name') or '—'} ({c.get('type') or '—'}) {c.get('phone') or ''} "
            f"{('[' + c['dnc'] + ']') if c.get('dnc') else ''}", ""]
    out += ["## Open (first 30 seconds)", card.opening, "", f"> {card.disclosure}", ""]
    out += ["## Discovery"] + [f"- {q}" for q in card.discovery] + [""]
    out += ["## Value"] + [f"- {v}" for v in card.value] + [""]
    out += ["## Offer strategy", card.offer.get("say", ""), ""]
    out += ["## Close", card.close, ""]
    out += ["## Objections"] + [f"**{o.label}** — {o.response}" for o in card.objections] + [""]
    if card.dont_say:
        out += ["## Never say"] + [f"- {d}" for d in card.dont_say] + [""]
    if card.resources:
        out += ["## Free resources to share"] + [f"- {r}" for r in card.resources] + [""]
    out += ["## Compliance"] + [f"- {x}" for x in card.compliance] + [""]
    out += ["## Voicemail", card.voicemail]
    return "\n".join(out)
