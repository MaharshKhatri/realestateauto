from datetime import date, datetime
from zoneinfo import ZoneInfo

from db.models import CashBuyer, DncEntry, Interaction
from engine import crm
from engine.ai import redact
from engine.buyer_matcher import run_matching
from engine.debrief import _num, rule_coach
from tests.factories import add_contact, make_lead

ET = ZoneInfo("America/New_York")
NOON = datetime(2026, 10, 27, 12, tzinfo=ET)


def test_stage_only_moves_forward_except_terminal():
    assert crm.advance_stage("INTERESTED", "CONTACT_ATTEMPTED") == "INTERESTED"
    assert crm.advance_stage("NEW", "OFFER_MADE") == "OFFER_MADE"
    assert crm.advance_stage("UNDER_CONTRACT", "DEAD") == "DEAD"
    assert crm.advance_stage("DEAD", "INTERESTED") == "DEAD"


def test_followup_cadence():
    nxt = crm.next_followup("LEFT_VM", 1, NOON)
    assert nxt == datetime(2026, 10, 30, 10, 0)
    assert crm.next_followup("LEFT_VM", 7, NOON) > nxt                       # cadence stretches
    assert crm.next_followup("NO_ANSWER", 8, NOON) is None                    # parked after max attempts
    assert crm.next_followup("DNC", 1, NOON) is None
    near = crm.next_followup("SPOKE_NOT_INTERESTED", 1, NOON, auction=date(2026, 11, 3))
    assert near == datetime(2026, 10, 28, 10, 0)                              # sale in 7 days compresses it


def test_log_call_updates_stage_followup_and_history(db):
    lead = make_lead(db, absentee=True)
    c = add_contact(db, lead)
    r = crm.log_call(db, lead, "LEFT_VM", contact=c, now=NOON)
    assert r.stage == "CONTACT_ATTEMPTED" and lead.contact_attempts == 1 and r.next_followup_at
    r = crm.log_call(db, lead, "SPOKE_NEEDS_OFFER", contact=c, notes="Wants 300k. Roof leaks.",
                     debrief={"asking_price": "300k"}, now=NOON)
    assert r.stage == "INTERESTED" and r.suggested_docs == ["loi", "deal_sheet"]
    assert r.coach["offer"]["gap_to_ask"] == 300000 - 260000
    assert db.query(Interaction).filter_by(lead_id=lead.id).count() == 2


def test_dnc_outcome_suppresses_number(db):
    lead = make_lead(db)
    c = add_contact(db, lead, phone="9735550123")
    r = crm.log_call(db, lead, "DNC", contact=c, now=NOON)
    assert r.dnc_added and lead.status_stage == "DO_NOT_CONTACT" and lead.priority_rank_score == 0
    assert db.get(DncEntry, ("9735550123", "internal")) is not None
    db.refresh(c)
    assert c.dnc_status == "dnc"


def test_confirming_vacancy_reprices_covered_lead(db):
    lead = make_lead(db, verdict="WORKABLE_82_PCT_FLOOR",
                     assumptions={"frfpa_applies": True, "frfpa_floor": 328000, "compliant_offer": 328000})
    assert lead.property.current_valuation.verdict == "WORKABLE_82_PCT_FLOOR"
    notes = crm.confirm_occupancy(db, lead, "VACANT", "seller on call")
    v = lead.property.current_valuation
    assert v.verdict in ("STRONG", "WORKABLE", "TIGHT") and lead.frfpa_track == "EXEMPT_CONFIRMED"
    assert v.anchor_offer < 328000 and any("Re-priced" in n for n in notes)


def test_record_contract_uses_frfpa_cancellation_window(db):
    covered = make_lead(db)                      # presumed owner-occupied
    notes = crm.record_contract(db, covered, 330000, date(2026, 10, 26))
    assert covered.status_stage == "UNDER_CONTRACT"
    assert covered.attorney_review_ends == date(2026, 11, 10)    # 10 NJ business days (Nov 3 is Election Day)
    assert any("FRFPA" in n for n in notes)
    exempt = make_lead(db, vacant=True)
    crm.record_contract(db, exempt, 200000, date(2026, 10, 26))
    assert exempt.attorney_review_ends == date(2026, 10, 29)


def test_queue_funnel_and_countdown(db):
    lead = make_lead(db, sheriff_days=5)
    add_contact(db, lead, phone="9735550001")
    make_lead(db, sheriff_days=40)
    q = crm.call_queue(db, 10)
    assert q and q[0].lead.id == lead.id and "sale in 5 days" in q[0].reason
    assert crm.funnel(db)["NEW"] == 2
    assert [x[0].id for x in crm.sale_countdown(db, 21)] == [lead.id]


def test_coach_flags_compliance_keywords(db):
    lead = make_lead(db)
    coach = rule_coach(lead, "SPOKE_WARM_FOLLOWUP", "He filed chapter 13 and asked if I can postpone the sale", {})
    joined = " ".join(coach["compliance_flags"])
    assert "automatic stay" in joined and "postponing" in joined
    assert "Who lives there (owner / heir / tenant / vacant)" in coach["missing_info"]
    assert _num("$350,000") == 350000 and _num("275k") == 275000 and _num("") is None


def test_ai_payload_redaction():
    text = redact("Call Mary at (973) 555-1234 or mary@example.com, she lives at 12 Main St")
    assert "555-1234" not in text and "example.com" not in text and "12 Main" not in text


def test_buyer_matching_prefers_same_town_and_price(db):
    lead = make_lead(db, vacant=True)
    near = CashBuyer(entity_key="A", entity_name="ALPHA LLC", target_counties=["Essex"],
                     target_municipalities=["NEWARK CITY"], min_arv=200000, max_arv=400000, avg_purchase_price=300000,
                     cash_purchases_count=8, max_rehab="HEAVY", last_purchase_date=date.today(),
                     recent_purchases_json=[{"address": "1 A ST", "municipality": "NEWARK CITY", "county": "Essex", "units": 1}])
    far = CashBuyer(entity_key="B", entity_name="BETA LLC", target_counties=["Camden"],
                    target_municipalities=["CAMDEN CITY"], min_arv=100000, max_arv=150000, cash_purchases_count=20)
    county = CashBuyer(entity_key="C", entity_name="GAMMA LLC", target_counties=["Essex"],
                       target_municipalities=["LIVINGSTON TWP"], min_arv=900000, max_arv=1500000, cash_purchases_count=2)
    db.add_all([near, far, county])
    db.flush()
    stats = run_matching(db, top=10)
    assert stats["deals"] == 1
    db.refresh(lead)
    names = [m.buyer.entity_name for m in lead.buyer_matches]
    assert names[0] == "ALPHA LLC" and "BETA LLC" not in names
    assert any("bought in Newark" in r for r in lead.buyer_matches[0].reasons)
