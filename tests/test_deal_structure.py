"""engine/deal_structure.py: the attorney-review assignment rider, the Correa clause in the assignment, the disclosed
entity option and the assignment-vs-double-close net check."""
from __future__ import annotations

from datetime import date

import pytest

from engine import contracts as ct
from engine import deal_structure as ds
from engine import quant_pricing as qp
from engine.contracts import ContractBlocked, generate
from tests.factories import add_contact, make_lead


def test_rider_generates_three_files_with_every_section(db, tmp_path):
    lead = make_lead(db, sheriff_days=None, upset=None, vacant=True, verdict="STRONG")
    g = generate(db, lead, "rider", out_root=tmp_path, today=date(2026, 10, 20))
    assert g.doc_type == "rider" and len(g.paths) == 3 and all(p.exists() for p in g.paths)
    md = (tmp_path / str(lead.property_id) / "rider.md").read_text()
    for needle in ("Assignment (modifies the No Assignment section)", "Section 37", "Item 14", "single-purpose limited liability",
                   "and/or assigns or an affiliated investment entity", "Property Condition Disclosure", "P.L.2024, c.32",
                   "45:15-16.87", "56:8-19.2", "10 business days", "underground oil tank sweep", "NJDEP", "escrow holdback",
                   "inspectors, contractors, appraisers, lenders, partners and prospective assignees", "Attorney Review",
                   "Study by Attorney", "not a licensed real estate broker", "may take title in a limited liability company",
                   "resell the Property or assign this Agreement for a profit", "Nothing about this Property is marketed"):
        assert needle in md, needle
    assert "Friday, October 23, 2026" in md                 # 3 NJ business days after Tuesday 10/20
    assert g.price == 228800 and any("VERIFY" in w for w in g.warnings)
    for phrase in ("drive by", "stop the foreclosure", "postpone"):
        assert phrase not in md.lower()


def test_rider_adds_tenant_foreclosure_and_estate_clauses(db, tmp_path):
    lead = make_lead(db, sheriff_days=20, upset=150000, vacant=True, verdict="STRONG", dwellings=3, is_estate_owned=True)
    md = ct.to_markdown(ds.build_rider(lead, today=date(2026, 10, 20)))
    assert "Leases and Deposits" in md and "46:8-19" in md and "Foreclosure" in md and "HUD-approved" in md
    assert "Estate" in md and "inheritance" in md and "tenants' rights" in md


def test_rider_is_gated_like_the_loi(db, tmp_path):
    covered = make_lead(db, sheriff_days=20, upset=150000, verdict="FRFPA_BLOCKED",
                        assumptions={"frfpa_applies": True, "frfpa_floor": 328000})
    with pytest.raises(ContractBlocked):
        generate(db, covered, "rider", out_root=tmp_path)
    referral = make_lead(db, sheriff_days=None, upset=None, verdict="EQUITY_RESCUE_REFERRAL")
    with pytest.raises(ContractBlocked):
        generate(db, referral, "rider", out_root=tmp_path)
    assert "rider" in ct.DOC_TYPES


def test_assignment_carries_the_correa_acknowledgment(db, tmp_path):
    lead = make_lead(db, sheriff_days=None, upset=None, vacant=True, verdict="STRONG")
    lead.offer_amount, lead.contract_date = 240000, date(2026, 10, 1)
    lead.status_stage = "UNDER_CONTRACT"
    lead.attorney_review_ends = date(2026, 10, 6)
    db.flush()
    doc = ct.build_assignment(lead, None, 275000, today=date(2026, 10, 10))
    clauses = dict(next(v for k, v in doc.blocks if k == "clauses"))
    assert ds.CORREA_TITLE in clauses
    text = clauses[ds.CORREA_TITLE]
    assert "Weintraub v. Krobatsch, 64 N.J. 445 (1974)" in text and "Correa v. Maggiore, 196 N.J. Super. 273" in text
    assert "every material defect actually known" in text and "never held title" in text
    order = list(clauses)
    assert order.index(ds.CORREA_TITLE) < order.index("Attorney Review")
    assert clauses["Assignment Fee"].startswith("Assignee pays Assignor an assignment fee of **$35,000**")


def test_entity_option_is_disclosed_and_caveated(db):
    lead = make_lead(db, sheriff_days=None, upset=None, verdict="STRONG")
    e = ds.entity_option(lead)
    text = " ".join(e["how"] + e["pros"] + e["conditions"] + [e["when"]])
    assert "tells the seller in writing" in text and "never used to keep the fee" in text
    assert "C-9600" in text and "10 business days" in text and "GIT/REP-3" in text and "54:15C-1" in text
    assert "title insurer and lender must accept" in text and e["costs"]["state_filing_fee"] == 125
    assert e["verify"] and "confirm the current amounts" in e["verify"][0]


@pytest.mark.parametrize("contract, buyer, refuses, expect", [
    (250000, 270000, False, "assignment"),       # $20K spread: assignment
    (250000, 295000, False, "double_close"),     # $45K spread: above the $30K threshold
    (250000, 270000, True, "double_close"),      # seller refuses the rider
    (250000, 250000, True, "assignment"),        # no spread: nothing to structure
])
def test_compare_structures_recommendation(contract, buyer, refuses, expect):
    c = ds.compare_structures(contract_price=contract, buyer_price=buyer, seller_refuses_assignment=refuses)
    assert c.recommended == expect and c.reasons and c.summary.startswith("Assignment: fee")


def test_compare_structures_math():
    c = ds.compare_structures(contract_price=250000, buyer_price=295000)
    dc = qp.costs()["double_close"]
    assert c.spread == 45000 and c.assignment["net"] == 45000 - dc["assignment_recording_fee"]
    tf = max(0.01 * 250000, dc["transactional_min_fee"]) + dc["transactional_flat_fees"]
    assert c.double_close["transactional_funding"] == round(tf)
    assert c.double_close["rtf_leg_b"] == qp.realty_transfer_fee(295000)
    assert c.double_close["net"] == round(45000 - tf - dc["our_closing_costs_leg_a"] - dc["our_closing_costs_leg_b"]
                                          - qp.realty_transfer_fee(295000))
    assert c.double_close["out_of_pocket"] == 0.0 and "escrow" in c.double_close["condition"]
    big = ds.compare_structures(contract_price=1000000, buyer_price=1100000, in_llc=True)
    assert big.double_close["rtf_leg_b"] == qp.realty_transfer_fee(1100000) + 11000.0      # 1% graduated fee on $1.1M
    assert any("graduated fee" in n for n in big.double_close["notes"]) and any("C-9600" in n for n in big.double_close["notes"])
    thin = ds.compare_structures(contract_price=250000, buyer_price=256000, seller_refuses_assignment=True)
    assert thin.double_close["net"] < 0 and any("warning" in r for r in thin.reasons)


def test_structure_for_lead_uses_the_leads_numbers(db):
    lead = make_lead(db, sheriff_days=None, upset=None, vacant=True, verdict="STRONG")
    add_contact(db, lead, name="Quinn Sample", phone="9735551234")
    c = ds.structure_for_lead(lead)
    assert c.contract_price == 228800 and c.buyer_price == 285000 and c.spread == 56200 and c.recommended == "double_close"
    c2 = ds.structure_for_lead(lead, buyer_price=250000)
    assert c2.spread == 21200 and c2.recommended == "assignment"
