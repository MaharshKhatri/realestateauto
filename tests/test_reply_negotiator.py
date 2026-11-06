"""engine/reply_negotiator.py: parsing agent replies, the traffic light against the buyer-implied numbers, the human
reply, proof-of-funds and risk handling, and what gets recorded."""
from __future__ import annotations

from datetime import datetime

import pytest

from core.config import settings
from db.models import CashBuyer, Interaction, JVPartner
from engine import human_copy as hc
from engine import reply_negotiator as rn
from tests.factories import add_contact, make_lead

QUANT = {"model": "quant", "buyer_max": 302000, "buyer_max_solve": 302000, "buyer_max_alpha": 338500, "binding": "min_roi",
         "mao_walkaway": 292000, "mao_target": 278800, "opening_offer": 245300, "fee_target": 20000, "min_fee": 10000,
         "alpha_effective": 0.724, "alpha": {"alpha": 0.797, "level": "town", "n": 20, "q25": 0.85, "name": "EDISON TWP"},
         "buyer": {"purchase_price": 302000, "rehab": 60000, "rehab_contingency": 6000, "buyer_closing": 4530,
                   "taxes_held": 5000, "carry": 2100, "capital_cost": 24000, "resale_net": 467000,
                   "total_investment": 403630, "profit": 60555, "roi": 0.15, "holding_months": 6}}


@pytest.fixture()
def operator(monkeypatch):
    for k, v in {"name": "Jordan Operator", "company_name": "Garden State Home Buyers LLC", "phone": "908-555-0100",
                 "email": "jordan@example.test", "mailing_address": "PO Box 10, Edison, NJ 08817"}.items():
        monkeypatch.setitem(settings()["operator"], k, v)


def listed(db, **kw):
    kw.setdefault("assumptions", {"quant": QUANT, "arv_before_tax_drag": 500000, "frfpa_applies": False})
    lead = make_lead(db, sheriff_days=None, upset=None, stale_listing=True, list_price=380000, **kw)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test",
                company="Garden Realty")
    v = lead.property.current_valuation
    v.mao_conservative, v.mao_aggressive, v.anchor_offer, v.target_assignment_fee = 278800, 292000, 245300, 20000
    db.flush()
    return lead


# -- parsing ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, counter, intents", [
    ("Seller would take 355k, not a penny less.", 355000, {"COUNTER"}),
    ("They countered at $355,000. Taxes are $9,800. Built 1925, 1,400 sqft.", 355000, {"COUNTER"}),
    ("Honestly they're looking for something in the mid 300s", 350000, {"COUNTER"}),
    ("its in the low 400s, high 200s won't do it", 425000, {"COUNTER"}),
    ("Your offer of $228,800 is way too low. Seller wants 310", 310000, {"COUNTER", "REJECT"}),
    ("Rent is $2,100 a month, tenants are month to month. Seller needs 30 days. Payoff is $190,000; they'd take 265k.",
     265000, {"COUNTER", "TENANTS"}),
    ("$1.2M is the number, firm.", 1200000, {"COUNTER"}),
    ("Owner wants 275,000 net. The HOA is $300/mo.", 275000, {"COUNTER"}),
    ("Yes it's still available, no offers yet. What's your number?", None, {"AVAILABLE"}),
    ("Still active, bring me an offer.", None, {"AVAILABLE"}),
    ("Not under contract yet - send me your best offer with proof of funds", None, {"AVAILABLE", "POF_REQUEST"}),
    ("Sorry, we went under contract last week.", None, {"UNDER_CONTRACT"}),
    ("It's no longer available, accepted an offer Monday.", None, {"UNDER_CONTRACT"}),
    ("We have multiple offers, highest and best by Friday 5pm. FYI there's an underground oil tank and it's tenant occupied.",
     None, {"HIGHEST_AND_BEST", "OIL_TANK", "TENANTS"}),
    ("Accepted! Send over the contract.", None, {"ACCEPT"}),
    ("That works for the seller, send the paperwork.", None, {"ACCEPT"}),
    ("Please remove me from your list", None, {"OPT_OUT"}),
    ("STOP", None, {"OPT_OUT"}),
    ("Not interested, way too low.", None, {"REJECT"}),
    ("The house needs a new CO and the seller is the estate executor. Who are you with?", None, {"CO", "ESTATE", "QUESTION"}),
    ("Bank has to approve it, it's a short sale.", None, {"SHORT_SALE"}),
    ("Can you send proof of funds and your POF letter?", None, {"POF_REQUEST"}),
    ("Good morning, we just arip'd it. Feel free to show for backup.", None, {"BACKUP"}),   # backup wins
    ("It's ARIP right now, attorney review in progress.", None, {"UNDER_CONTRACT"}),
    ("Hi Maharsh,\nThe home is still available!\nAre you interested in a tour?", None, {"AVAILABLE", "ASKS_SHOWING"}),
    ("Good morning.\nThe asking price is $549k.", None, set()),
    ("They want maximum return as per the condition of the house, asking is 395\nMake an offer", None, {"AVAILABLE"}),
    ("Asking is 395 firm.", 395000, {"COUNTER"}),
])
def test_parse_reply(text, counter, intents):
    p = rn.parse_reply(text)
    assert p.counter_amount == counter
    assert intents <= p.intents, (p.intents, p.notes)


def test_parse_ignores_phones_years_and_units():
    p = rn.parse_reply("Call me at (973) 555-0100 or 201-555-0199. Built in 1925, 1,400 sqft, taxes 9,800. 10 day inspection.")
    assert p.amounts == [] and p.counter_amount is None and p.intents == {"CALL_ME"}   # "call me" is read, numbers aren't
    assert rn.parse_reply("highest and best by Friday 5pm").deadline == "by Friday 5pm"


# -- traffic light -------------------------------------------------------------------------------------
def test_green_when_counter_is_under_the_walkaway(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "Seller would take 285k if you can close fast.", now=datetime(2026, 10, 20, 9, 0))
    assert a.verdict == "GREEN" and a.headline.startswith("ACCEPT & LOCK UP NOW") and a.numbers["fee_at_counter"] == 17000
    assert a.numbers["counter"] == 285000 and a.numbers["buyer_roi_at_max"] >= 0.15 - 1e-6
    assert "$285,000 works." in a.reply_text and "--- Offer Summary ---" in a.reply_text and "PO Box 10" in a.reply_text
    assert "may assign the contract" in a.reply_text and "not asking for any part of it" in a.reply_text
    assert a.style == [] and hc.word_count(hc.split_signature(a.reply_text)[0]) < 90
    assert "STOP" in a.sms_text and a.stage_to == "NEGOTIATING" and a.followup_at is not None
    assert any("written offer" in x for x in a.actions) and any("rider" in x for x in a.actions)


def test_yellow_counters_with_the_cost_math(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "Seller countered at $305,000.")
    # 2026-11-03: one move only, and it is the best and final (buyer max 302K - the $5K minimum fee)
    assert a.verdict == "YELLOW" and a.headline.startswith("BEST AND FINAL AT $297,000")
    assert a.numbers["our_counter"] == 297000 and a.numbers["fee_at_our_counter"] == 5000
    assert "$66,000 of work" in a.reply_text and "$5,000 in taxes" in a.reply_text and "best and final is $297,000" in a.reply_text
    assert a.style == [] and a.followup_at.hour == 10


def test_red_holds_and_sets_the_drip(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "They want 360k, not budging.", now=datetime(2026, 10, 20, 9, 0))
    assert a.verdict == "RED" and a.headline == "HOLD FIRM AT $297,000 / SET 14-DAY DRIP"
    assert "$297,000" in a.reply_text and a.followup_at.date().isoformat() == "2026-11-03" and a.style == []
    # we never named a price and our final is 78% of the $380K ask: a "no" gets no number, just a 30-day check-in
    cold = rn.analyze(db, lead, "Not interested.", now=datetime(2026, 10, 20, 9, 0))
    assert cold.verdict == "RED" and "$" not in cold.reply_text.split("---")[0] and cold.followup_at.date().isoformat() == "2026-11-19"
    lead.offer_amount = 285_000                          # after a written offer, the ceiling is on the record
    reject = rn.analyze(db, lead, "Not interested, too low.", now=datetime(2026, 10, 20, 9, 0))
    assert reject.verdict == "RED" and "$297,000" in reject.reply_text and reject.followup_at.date().isoformat() == "2026-11-03"


def test_far_apart_counter_names_no_number(db, operator):
    """2026-11-03: a ceiling far under their number reads as a lowball. Under 80% of it we say we're too far apart."""
    lead = listed(db)
    a = rn.analyze(db, lead, "Seller wants 400k firm.", now=datetime(2026, 10, 20, 9, 0))      # 297K = 74% of 400K
    body = a.reply_text.split("---")[0]
    assert a.verdict == "RED" and "TOO FAR APART" in a.headline and "$297,000" not in a.reply_text
    assert "too far from $400,000" in body and a.followup_at.date().isoformat() == "2026-11-19"
    from engine import human_copy as hc
    assert hc.lint(a.reply_text) == []


def test_full_price_is_their_number(db, operator):
    """2026-11-05: Edison answered "what would the seller need to net?" with "Full price" on 114 Sheridan Ave, and the desk
    read it as a question. "Full price" is their number: the ask, stated plainly."""
    from engine import agent_outreach as ao
    lead = listed(db)
    ask = float(ao.listing_gap(lead)["list_price"])
    a = rn.analyze(db, lead, "Full price\n\nSent from my iPhone", now=datetime(2026, 10, 20, 9, 0))
    assert a.parsed.counter_amount == ask and a.parsed.counter_explicit and "COUNTER" in a.parsed.intents
    assert a.verdict in ("RED", "YELLOW") and a.headline and "ANSWER THEM" not in a.headline
    assert not rn.full_price_asked("Below asking price is fine with them") and not rn.full_price_asked("What's the asking price?")
    assert rn.full_price_asked("Seller wants full asking") and rn.full_price_asked("They're firm on list price")


def test_their_number_above_an_unsure_final_gets_facts_not_a_ceiling(db, operator, monkeypatch):
    """2026-11-05: "Full price" on 114 Sheridan Ave was drafted as "hold at $499K" while the call sheet said agree to
    nothing (no living area on record). With our own number unsure, nothing goes in writing: ask for the fact."""
    from core.config import settings
    from engine import offer_math
    lead = listed(db)
    monkeypatch.setitem(settings(), "firm", {**dict(settings().get("firm") or {}), "enforce": True})
    monkeypatch.setattr(offer_math, "certainty_issues", lambda lead, session=None, today=None:
                        ["the living area isn't on record"])
    for text in ("Seller wants 340k firm.", "Seller wants 310k."):          # 297K final: hold range, best-and-final range
        a = rn.analyze(db, lead, text, now=datetime(2026, 10, 20, 9, 0))
        body = a.reply_text.split("---")[0]
        assert "FACTS FIRST" in a.headline and "$297,000" not in a.reply_text and "best and final" not in body.lower()
        assert "square footage" in body and a.numbers.get("our_counter") is None
        from engine import human_copy as hc
        assert hc.lint(a.reply_text) == []
    # Far apart stays the plain "too far apart" note; a sure number keeps the hold.
    assert "TOO FAR APART" in rn.analyze(db, lead, "Seller wants 400k firm.", now=datetime(2026, 10, 20, 9, 0)).headline
    monkeypatch.setattr(offer_math, "certainty_issues", lambda lead, session=None, today=None: [])
    assert "HOLD FIRM" in rn.analyze(db, lead, "Seller wants 340k firm.", now=datetime(2026, 10, 20, 9, 0)).headline


def test_risk_buffers_move_the_numbers_and_add_language(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "Seller would take 285k. Heads up: there is an underground oil tank and it's tenant occupied, "
                             "and the town will want a new CO.")
    # tank $3.5K (the offer's terms make the seller remove a found tank), tenants $9K, CO $1.5K
    assert a.numbers["risk_buffer"] == 3500 + 9000 + 1500 and a.numbers["adjusted_walkaway"] == 297000 - 14000
    assert a.verdict == "YELLOW"                     # 285K is above the buffered walk-away (283,000)
    assert "tank sweep" in a.reply_text and "46:8-19" in a.reply_text and "5:70-2.3" in a.reply_text
    assert any("TENANT" in x for x in a.actions) and a.style == []


def test_bidding_war_never_goes_above_the_walkaway(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "We have multiple offers, highest and best by noon tomorrow.")
    assert a.verdict == "YELLOW" and a.headline == "BEST AND FINAL AT $297,000" and "best and final is $297,000" in a.reply_text
    lead.offer_amount = 300000          # we somehow offered more than the walk-away before: hold there, never raise
    a2 = rn.analyze(db, lead, "Highest and best please.")
    assert a2.numbers["our_counter"] == 300000


def test_available_hands_off_to_touch_two(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "Yes still available, what's your offer?")
    assert a.verdict == "GREEN" and a.headline.startswith("SEND THE OFFER NOW") and "--- Offer Summary ---" in a.reply_text
    assert "$292,000" in a.reply_text and a.stage_to == "CONTACTED" and a.style == []      # buyer max 302K - $10K


def test_under_contract_and_question_and_accept(db, operator):
    lead = listed(db)
    uc = rn.analyze(db, lead, "Sorry, we're already in attorney review.", now=datetime(2026, 10, 20, 9, 0))
    assert uc.verdict == "INFO" and "under contract" in uc.headline and uc.followup_at.date().isoformat() == "2026-12-04"
    assert "congratulations" in uc.reply_text and "$" not in hc.split_signature(uc.reply_text)[0] and uc.style == []
    q = rn.analyze(db, lead, "Who are you with?")
    assert q.verdict == "INFO" and "investor buying for myself" in q.reply_text and q.style == []
    lead.offer_amount = 245300
    ok = rn.analyze(db, lead, "Seller accepted your offer, send the contract.")
    assert ok.verdict == "GREEN" and ok.headline.startswith("THEY ACCEPTED $245,300") and ok.numbers["fee_at_counter"] == 56700


def test_pof_request_with_and_without_a_partner(db, operator):
    lead = listed(db)
    b = CashBuyer(entity_key="M1", entity_name="MATCH LLC", contact_name="Casey Match", phone="9085550177")
    db.add(b)
    db.flush()
    from db.models import BuyerMatch
    db.add(BuyerMatch(lead_id=lead.id, buyer_id=b.id, score=90, rank=1))
    db.flush()
    db.refresh(lead)
    a = rn.analyze(db, lead, "Can you send proof of funds?")
    assert a.verdict == "INFO" and a.headline == "SEND PROOF OF FUNDS" and a.partner["partner"] is None
    assert any("Casey Match" in x and "#/partners" in x for x in a.actions)
    assert "getting the proof of funds letter together" in a.reply_text and a.style == []
    p = JVPartner(entity_name="RIVER HOMES LLC", contact_name="Sam Rivera", status="ACTIVE_PARTNER", pof_verified=True,
                  pof_share_ok=True, pof_document_path="/x/pof.pdf", target_counties=["Essex"], min_price=100000,
                  max_price=900000, max_rehab="HEAVY", velocity_score=10)
    db.add(p)
    db.flush()
    a2 = rn.analyze(db, lead, "Send POF please, seller would take 288k")
    assert a2.verdict == "GREEN" and a2.partner["partner"]["name"] == "Sam Rivera"
    assert any("Attach the proof of funds" in x for x in a2.actions)


def test_gates_block_prices(db, operator):
    blocked = listed(db, verdict="EQUITY_RESCUE_REFERRAL")
    a = rn.analyze(db, blocked, "Seller wants 250k")
    assert a.verdict == "INFO" and a.headline.startswith("REFER") and "$" not in hc.split_signature(a.reply_text)[0]
    covered = listed(db, assumptions={"quant": QUANT, "frfpa_applies": True, "frfpa_floor": 328000})
    c = rn.analyze(db, covered, "They'd take 300k")
    assert c.verdict == "INFO" and "FRFPA FLOOR" in c.headline and "$" not in hc.split_signature(c.reply_text)[0]
    bare = make_lead(db, sheriff_days=None, upset=None, verdict=None, stale_listing=True)
    add_contact(db, bare, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test")
    u = rn.analyze(db, bare, "Seller wants 250k")
    assert u.headline == "UNDERWRITE FIRST" and u.verdict == "INFO"


def test_record_writes_interaction_counter_stage_and_opt_out(db, operator):
    lead = listed(db)
    a, inter = rn.analyze_and_record(db, lead, "Seller countered at $305,000.", now=datetime(2026, 10, 20, 9, 0))
    assert inter.outcome == "AGENT_REPLIED" and inter.channel == "email_in" and inter.contact_id
    assert lead.last_counter_amount == 305000 and lead.status_stage == "NEGOTIATING"
    assert lead.next_followup_at == a.followup_at and inter.debrief_json["verdict"] == "YELLOW"
    assert db.query(Interaction).filter_by(lead_id=lead.id, outcome="AGENT_REPLIED").count() == 1
    out, _ = rn.analyze_and_record(db, lead, "Please remove me from your list.")
    agent = next(c for c in lead.contacts if c.contact_type == "LISTING_AGENT")
    assert out.headline == "OPTED OUT" and out.reply_text == "" and agent.dnc_status == "dnc"


def test_every_reply_passes_the_human_copy_lint(db, operator):
    lead = listed(db)
    for text in ("Seller would take 285k", "Seller countered at $305,000.", "They want 400k.", "still available",
                 "multiple offers, highest and best", "accepted, send contract", "we're under contract", "who are you?",
                 "send proof of funds", "not interested", "285k but there's an oil tank and tenants"):
        a = rn.analyze(db, lead, text)
        assert a.style == [], (text, a.style)
        head = hc.split_signature(a.reply_text)[0]
        limit = 110 if a.headline.startswith("SEND THE OFFER NOW") else 90     # Touch 2 reuses the outreach packet
        assert hc.word_count(head) < limit, (text, hc.word_count(head))
        for phrase in ("drive by", "door-knock", "stop by", "cash buyer", "postpone the sale", "stop the foreclosure"):
            assert phrase not in a.reply_text.lower()
        assert hc.lint(a.sms_text, sms=True) == [] or a.sms_text == ""


@pytest.mark.parametrize("text, ack", [
    ("Sounds good! Thank you!\n\nGet Outlook for Android", True), ("Thanks!\nJennifer", True), ("Will do, thanks", True),
    ("Thank you so much", True), ("Sounds good, what's your number?", False), ("Thanks, seller wants 340", False),
    ("Thanks. It is still available.", False), ("Sounds good. Send it over", False)])
def test_a_plain_thank_you_is_recognized(text, ack):
    """2026-11-04: "Sounds good! Thank you!" after our backup note became a 📞 call event and "waiting on you"."""
    assert rn.is_ack(text) is ack


@pytest.mark.parametrize("text, counter", [
    ("Prob 550", 550000), ("550", 550000), ("Around 480", 480000), ("maybe 425 if it closes fast", 425000),
    ("about 30 days", None), ("Probably 3 bedrooms", None)])
def test_a_hedged_short_number_is_thousands_but_not_stated_plainly(text, counter):
    """2026-11-04: Juliet's "Prob 550" (ask $575K) was dropped as $550, and the reply asked her for a number again."""
    p = rn.parse_reply(text)
    assert p.counter_amount == counter
    if counter:
        assert p.counter_explicit is False             # a guess: a priced answer to it waits for the operator


@pytest.mark.parametrize("text, ok", [
    ("Sure, you can absolutely", True), ("Yes please do\nGeri", True), ("Of course!", True), ("That's fine, thanks", True),
    ("Sure, but the seller wants 300", False), ("Yes, is your offer coming?", False), ("Still available", False)])
def test_a_yes_to_our_check_in(text, ok):
    """2026-11-04: Geri's "Sure, you can absolutely" to "mind if I check back in about 30 days?" was read as available."""
    assert rn.is_checkin_ok(text) is ok


def test_the_answer_greets_the_person_who_wrote(db):
    """2026-11-04: Jacqui handed the thread to her colleague Geri; the answer to Geri opened "Hi Jacqueline"."""
    lead = listed(db)
    geri = add_contact(db, lead, contact_type="LISTING_AGENT", name="Geri Colleague", email="geri@broker.test",
                       company="Garden Realty")
    db.flush()
    assert rn.analyze(db, lead, "Seller wants 300k").reply_text.startswith("Hi Pat")
    assert rn.analyze(db, lead, "Seller wants 300k", agent=geri).reply_text.startswith("Hi Geri")


def test_what_is_your_max_gets_the_best_and_final(db):
    """2026-11-04: Juliet asked "What is your max?" and the draft asked for the seller's number again."""
    lead = listed(db)
    a = rn.analyze(db, lead, "What is your max?")
    final = a.numbers["adjusted_walkaway"]
    assert a.headline.startswith("THEY ASKED YOUR MAX") and f"${final:,.0f}" in a.reply_text
    assert "what would the seller" not in a.reply_text.lower()


def test_so_no_is_a_no():
    """2026-11-04: "We had 2 offers over 5 so no." was read as a question to answer."""
    assert "REJECT" in rn.parse_reply("We had 2 offers over 5 so no.").intents
    assert "REJECT" in rn.parse_reply("Seller countered at 500 so no").intents
    assert "REJECT" not in rn.parse_reply("Still available, so no rush on your end").intents


def test_withdrawn_is_off_the_market_not_under_contract():
    """2026-11-05: "The house was withdrawn" got "congratulations on getting it under contract"."""
    p = rn.parse_reply("The house was withdrawn")
    assert "EXPIRED" in p.intents and "UNDER_CONTRACT" not in p.intents


def test_sold_as_is_is_the_condition_not_a_sale():
    """2026-11-06: "However, it is sold as is." read as SOLD, and the desk congratulated the agent on a contract."""
    text = ("The listing price is 525,000. The sellers are willing negotiate. Given the comps, they are willing to "
            "negotiate now lower than 510,000.\nThe roof, deck (trex) and water heater are about 3 years old. The "
            "bathroom upstairs is not operational. However, it is sold as is.")
    p = rn.parse_reply(text)
    assert "UNDER_CONTRACT" not in p.intents and p.counter_amount == 510000 and not p.counter_explicit
    for sold in ("Sorry, it sold last week.", "It's already sold.", "We sold it."):
        assert "UNDER_CONTRACT" in rn.parse_reply(sold).intents, sold
    for as_is in ("Being sold as-is.", "Property sold strictly as is", "sold in as is condition"):
        assert "UNDER_CONTRACT" not in rn.parse_reply(as_is).intents, as_is


def test_just_started_review_is_under_contract():
    # 2026-11-06, 2360 Colonial Dr: "Just started review" answers our "or already in attorney review?"
    assert rn.parse_reply("Just started review\n\nTom Minkina\nKeller Williams Realty").intents == {"UNDER_CONTRACT"}
    assert "UNDER_CONTRACT" in rn.parse_reply("Currently in review").intents
    # ...but our offer being reviewed by the seller is not a contract
    assert "UNDER_CONTRACT" not in rn.parse_reply("Your offer is in review with the seller").intents
    assert "UNDER_CONTRACT" not in rn.parse_reply("I'll review your offer with the seller").intents


def test_nothing_the_seller_would_consider_is_a_no():
    # 2026-11-06, 73 Terrace Ave (typo kept, line break mid-sentence)
    text = ("Called you, but no answer.\n\nI am in no position to speak on the seller's behalf regarding their\n"
            "flexibility on price. You're welcome to make an offer like any other buyer.\nBased on this conversation, "
            "I anticipate it not being anyhting the seller\nwould consider.")
    assert "REJECT" in rn.parse_reply(text).intents
    assert "REJECT" in rn.parse_reply("The seller won't even consider anything under asking").intents
    assert "REJECT" not in rn.parse_reply("Yes, still available. The seller would consider offers").intents
