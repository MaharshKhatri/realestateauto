from datetime import date, datetime, timedelta

import pytest

from db.models import CashBuyer, GeneratedDocument
from engine.contracts import ContractBlocked, contract_gate, generate, to_markdown, build_deal_sheet
from engine.scripts import build_battle_card, build_buyer_pitch, choose_playbook, render_markdown
from tests.factories import add_contact, make_lead


def test_playbook_a_sheriff_sale_owner(db):
    lead = make_lead(db, absentee=False)
    add_contact(db, lead, name="John Smith")
    card = build_battle_card(lead)
    assert card.playbook == "A"
    assert len(card.objections) >= 12
    keys = {o.key for o in card.objections}
    assert {"too_low", "realtor", "lawyer", "stop_calling", "assign", "wait_sale"} <= keys
    md = render_markdown(card)
    assert "not a licensed agent" in md and "HUD" in md
    # never offer to postpone; the card says not to
    assert any("postpone" in d.lower() for d in card.dont_say)
    assert not any("adjourn" in v.lower() and "request" in v.lower() for v in card.value)


def test_playbook_b_estate_executor(db):
    lead = make_lead(db, defendant="JANE DOE, EXECUTRIX OF THE ESTATE OF JOHN DOE")
    add_contact(db, lead, contact_type="HEIR_EXECUTOR", name="Jane Doe", notes="deceased: John Doe")
    card = build_battle_card(lead)
    assert card.playbook == "B" and "executor" in card.title.lower()
    assert any("Surrogate" in q for q in card.discovery)


def test_playbook_c_absentee_and_d_listing_agent(db):
    lead = make_lead(db, sheriff_days=None, absentee=True, owner_state="FL", is_out_of_state=True)
    assert build_battle_card(lead).playbook == "C"
    listing = make_lead(db, sheriff_days=None, stale_listing=True)
    agent = add_contact(db, listing, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100")
    card = build_battle_card(listing, agent)
    assert card.playbook == "D" and choose_playbook(listing, agent) == "D"
    assert "principals" in card.disclosure and "dual agency" in " ".join(card.compliance).lower()
    assert any(o.key == "no_assign" for o in card.objections)


def test_no_offer_verdict_never_names_a_price(db):
    lead = make_lead(db, verdict="EQUITY_RESCUE_REFERRAL")
    card = build_battle_card(lead)
    assert card.offer["type"] == "no_offer"
    assert "don't get paid for referrals" in card.close
    assert "isn't your best option" in next(o for o in card.objections if o.key == "too_low").response


def test_buyer_pitch_does_not_market_before_contract(db):
    lead = make_lead(db)
    buyer = CashBuyer(entity_key="OWNER 1 MAIN", entity_name="Owner @ 1 MAIN ST, NEWARK, NJ", cash_purchases_count=4,
                      recent_purchases_json=[{"address": "5 ELM ST", "municipality": "NEWARK CITY", "price": 300000}])
    db.add(buyer)
    db.flush()
    card = build_buyer_pitch(buyer, lead)
    assert card.playbook == "E" and "after repairs" not in card.opening and "is this" not in card.opening
    lead.contract_date, lead.attorney_review_ends = date.today() - timedelta(days=7), date.today() - timedelta(days=2)
    assert "under contract" in build_buyer_pitch(buyer, lead).opening


def test_gate_blocks_psa_for_covered_owner_occupied(db):
    lead = make_lead(db, verdict="WORKABLE_82_PCT_FLOOR",
                     assumptions={"frfpa_applies": True, "frfpa_floor": 328000, "compliant_offer": 328000})
    gate = contract_gate(lead, "psa", 328000)
    assert not gate.allowed and any("attorney" in r for r in gate.reasons)
    assert not contract_gate(lead, "loi", 300000).allowed            # below 82% of FMV
    assert contract_gate(lead, "loi", 328000).allowed


def test_gate_requires_confirmed_occupancy_for_presumed_exemption(db):
    lead = make_lead(db, absentee=True)
    assert not contract_gate(lead, "loi", 228800).allowed
    lead.occupancy_status, lead.occupancy_confirmed_at = "TENANT_OCCUPIED", datetime.now()
    assert contract_gate(lead, "loi", 228800).allowed


def test_gate_blocks_no_offer_and_assignment_before_contract(db):
    rescue = make_lead(db, verdict="EQUITY_RESCUE_REFERRAL")
    assert not contract_gate(rescue, "loi", 300000).allowed
    assert not contract_gate(rescue, "deal_sheet", None).allowed
    lead = make_lead(db, vacant=True)
    assert not contract_gate(lead, "assignment", None).allowed


def test_generate_package_writes_md_html_pdf(db, tmp_path):
    lead = make_lead(db, vacant=True, year_built=1950)
    lead.occupancy_status, lead.occupancy_confirmed_at = "VACANT", datetime.now()
    add_contact(db, lead, name="John Smith")
    g = generate(db, lead, "psa", out_root=tmp_path)
    assert [p.suffix for p in g.paths] == [".md", ".html", ".pdf"]
    text = g.paths[0].read_text()
    assert "and/or assigns" in text and "Study by Attorney" in text and "Lead-Based Paint" in text
    assert "vacant" in text.lower() and "Foreclosure" in text
    assert g.paths[2].read_bytes().startswith(b"%PDF")
    assert db.query(GeneratedDocument).filter_by(lead_id=lead.id, doc_type="psa").count() == 1
    loi = generate(db, lead, "loi", out_root=tmp_path)
    assert loi.price == 228800 and "not a binding contract" in loi.paths[0].read_text()
    with pytest.raises(ContractBlocked):
        generate(db, lead, "assignment", out_root=tmp_path)


def test_deal_sheet_is_internal_until_attorney_review_ends(db):
    lead = make_lead(db, vacant=True)
    assert build_deal_sheet(lead).watermark
    lead.contract_date, lead.attorney_review_ends = date.today() - timedelta(days=10), date.today() - timedelta(days=5)
    sheet = build_deal_sheet(lead)
    assert sheet.watermark is None and "Comparable sales" in to_markdown(sheet)


def test_estate_without_known_heir_asks_for_the_family(db):
    from agents.scrapers.sheriff_sale_agent import first_defendant
    from engine.contracts import seller_parties
    from engine.scripts import speaking_name
    assert first_defendant("THAMES YOL KIM, HIS UNKNOWN HEIRS, DEVISEES AND PERSONAL REPRESENTATIVES") == "THAMES YOL KIM"
    assert first_defendant("RALPH JACKMAN, DECEASEDM HIS HEIRS, DEVISEES") == "RALPH JACKMAN"
    assert speaking_name("COLODNE, YETTA ESTATE OF") == "Yetta Colodne"
    lead = make_lead(db, defendant="ROSE KIM, HER UNKNOWN HEIRS, DEVISEES", owner_name="ROSE KIM", is_estate_owned=True)
    card = build_battle_card(lead)
    assert card.playbook == "B" and card.opening.startswith("Hi, I'm trying to reach a family member of Rose Kim")
    assert "[your callback number]" in card.opening
    assert seller_parties(lead)[0].startswith("The Estate of Rose Kim, by its court-appointed personal representative")
