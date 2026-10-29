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
])
def test_parse_reply(text, counter, intents):
    p = rn.parse_reply(text)
    assert p.counter_amount == counter
    assert intents <= p.intents, (p.intents, p.notes)


def test_parse_ignores_phones_years_and_units():
    p = rn.parse_reply("Call me at (973) 555-0100 or 201-555-0199. Built in 1925, 1,400 sqft, taxes 9,800. 10 day inspection.")
    assert p.amounts == [] and p.counter_amount is None and p.intents == set()
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
    assert a.verdict == "YELLOW" and a.headline.startswith("COUNTER AT $290,000")
    assert a.numbers["our_counter"] == 290000 and a.numbers["fee_at_our_counter"] == 12000
    assert "$66,000 of work" in a.reply_text and "$5,000 in taxes" in a.reply_text and "I can do $290,000" in a.reply_text
    assert a.style == [] and a.followup_at.hour == 10


def test_red_holds_and_sets_the_drip(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "They want 360k, not budging.", now=datetime(2026, 10, 20, 9, 0))
    assert a.verdict == "RED" and a.headline == "HOLD FIRM AT $292,000 / SET 14-DAY DRIP"
    assert "$292,000" in a.reply_text and a.followup_at.date().isoformat() == "2026-11-03" and a.style == []
    reject = rn.analyze(db, lead, "Not interested, too low.", now=datetime(2026, 10, 20, 9, 0))
    assert reject.verdict == "RED" and "$292,000" in reject.reply_text and reject.followup_at.date().isoformat() == "2026-11-03"


def test_risk_buffers_move_the_numbers_and_add_language(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "Seller would take 285k. Heads up: there is an underground oil tank and it's tenant occupied, "
                             "and the town will want a new CO.")
    assert a.numbers["risk_buffer"] == 12000 + 9000 + 2500 and a.numbers["adjusted_walkaway"] == 292000 - 23500
    assert a.verdict == "YELLOW"                     # 285K is above the buffered walk-away (268,500)
    assert "tank sweep" in a.reply_text and "46:8-19" in a.reply_text and "5:70-2.3" in a.reply_text
    assert any("TENANT" in x for x in a.actions) and a.style == []


def test_bidding_war_never_goes_above_the_walkaway(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "We have multiple offers, highest and best by noon tomorrow.")
    assert a.verdict == "YELLOW" and a.headline == "BEST AND FINAL AT $292,000" and "best and final is $292,000" in a.reply_text
    lead.offer_amount = 300000          # we somehow offered more than the walk-away before: hold there, never raise
    a2 = rn.analyze(db, lead, "Highest and best please.")
    assert a2.numbers["our_counter"] == 300000


def test_available_hands_off_to_touch_two(db, operator):
    lead = listed(db)
    a = rn.analyze(db, lead, "Yes still available, what's your offer?")
    assert a.verdict == "GREEN" and a.headline.startswith("SEND THE OFFER NOW") and "--- Offer Summary ---" in a.reply_text
    assert "$245,300" in a.reply_text and a.stage_to == "CONTACTED" and a.style == []


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
