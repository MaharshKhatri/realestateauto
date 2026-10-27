"""Closing coordinator (NJ business-day milestones, review gate, disapproval, title e-mail) and finance ledger."""
from datetime import date

import pytest

from db.models import CashBuyer
from engine import closing_coordinator as cc
from engine import crm, finance
from engine import disposition as dp
from tests.factories import make_lead


def test_schedule_skips_weekends_and_nj_holidays():
    # Signed Thu Nov 19 2026; review ends Tue Nov 24. Thanksgiving (Thu Nov 26) is a NJ legal holiday; the Friday
    # after is not (N.J.S.A. 36:1-1).
    due = cc.schedule(date(2026, 11, 19), date(2026, 11, 24), frfpa=True)
    assert due["CONTRACT_EXECUTED"] == date(2026, 11, 19)
    assert due["TITLE_SEARCH_AND_MUNICIPAL_LIEN_ORDERED"] == date(2026, 11, 25)
    assert due["EMD_DEPOSITED"] == date(2026, 11, 30)           # Wed 25, (Thu 26 holiday), Fri 27, Mon 30
    assert due["ASSIGNMENT_OR_DOUBLE_CLOSE_EXECUTED"] > due["VIRTUAL_WALKTHROUGH_AND_OIL_TANK_SWEEP"]
    assert due["CLEAR_TO_CLOSE"] < due["CLOSED_PAID"] and due["CLOSED_PAID"].weekday() < 5
    assert "FRFPA_CONTRACT_RECORDED" in due
    assert "FRFPA_CONTRACT_RECORDED" not in cc.schedule(date(2026, 11, 19), date(2026, 11, 24), frfpa=False)


def test_record_contract_opens_file_and_review_gate(db):
    lead = make_lead(db, sheriff_days=None, absentee=True)
    notes = crm.record_contract(db, lead, 230_000, signed=date(2026, 10, 20))
    assert any("Closing file opened" in n for n in notes)
    ms = cc.milestones(db, lead)
    assert [m.milestone for m in ms][:2] == ["CONTRACT_EXECUTED", "ATTORNEY_REVIEW_WINDOW"] and len(ms) == 8
    assert ms[0].status == "done" and ms[1].due_date == date(2026, 10, 23)
    with pytest.raises(ValueError, match="attorney review"):
        cc.complete(db, lead, "ASSIGNMENT_OR_DOUBLE_CLOSE_EXECUTED")       # review still pending
    assert cc.sync(db, lead, today=date(2026, 10, 23)) == []               # the deadline day itself: still open
    assert "concluded" in cc.sync(db, lead, today=date(2026, 10, 26))[0]
    cc.complete(db, lead, "EMD_DEPOSITED", "wired $1,000 to escrow")
    alerts = cc.alerts(db, today=date(2026, 10, 27))
    assert any(a.milestone == "TITLE_SEARCH_AND_MUNICIPAL_LIEN_ORDERED" and a.overdue for a in alerts)
    assert all(a.milestone != "EMD_DEPOSITED" for a in alerts)
    assert cc.pipeline(db, today=date(2026, 10, 27))[0]["done"] == 3


def test_disapproval_closes_file(db):
    lead = make_lead(db, sheriff_days=None, absentee=True)
    crm.record_contract(db, lead, 230_000, signed=date(2026, 10, 20))
    cc.record_disapproval(db, lead, "seller attorney disapproved on day 2")
    assert lead.status_stage == "NEGOTIATING" and lead.contract_date is None
    assert all(m.status in ("done", "cancelled") for m in cc.milestones(db, lead))


def test_bids_tick_milestones_and_close_books_fee(db, tmp_path):
    lead = make_lead(db, sheriff_days=None, absentee=True)
    crm.record_contract(db, lead, 230_000, signed=date(2026, 10, 1))
    buyer = CashBuyer(entity_key="RIVER", entity_name="RIVER HOMES LLC", contact_name="Sam Rivera")
    db.add(buyer)
    db.flush()
    bid = dp.invite(db, lead, buyer)
    dp.mark_walked(db, bid)
    bid, _ = dp.record_bid(db, lead, buyer, 262_000, emd=5_000)
    dp.accept_bid(db, bid, out_root=tmp_path)
    status = {m.milestone: m.status for m in cc.milestones(db, lead)}
    assert status["VIRTUAL_WALKTHROUGH_AND_OIL_TANK_SWEEP"] == "done"
    assert status["ASSIGNMENT_OR_DOUBLE_CLOSE_EXECUTED"] == "done" and status["ATTORNEY_REVIEW_WINDOW"] == "done"
    assert finance.expected_fee(db, lead) == 32_000
    cc.close_deal(db, lead, 32_000, closed_on=date(2026, 11, 20))
    cc.close_deal(db, lead, 32_000, closed_on=date(2026, 11, 20))       # idempotent: one fee per closing
    assert lead.status_stage == "CLOSED"
    sm = finance.summary(db, 2026, today=date(2026, 11, 21))
    assert sm.income == 32_000 and sm.tax_reserve == round(32_000 * 0.28, 2)
    assert sm.self_employment_tax_estimate == round(32_000 * 0.9235 * 0.153, 2)
    q4 = next(q for q in sm.quarters if q.due_date == "2027-01-15")
    assert q4.period_net == 32_000 and sm.next_due["date"] == "2027-01-15"


def test_ledger_validation_expenses_and_pipeline(db):
    with pytest.raises(ValueError):
        finance.add_entry(db, kind="income", category="mail", amount=10)
    with pytest.raises(ValueError):
        finance.add_entry(db, kind="expense", category="mail", amount=-5)
    finance.add_entry(db, kind="expense", category="mail", amount=120, entry_date=date(2026, 5, 2), description="letters")
    finance.add_entry(db, kind="income", category="assignment_fee", amount=20_000, entry_date=date(2026, 5, 20))
    lead = make_lead(db, sheriff_days=None, absentee=True)
    crm.record_contract(db, lead, 230_000, signed=date(2026, 10, 20))
    sm = finance.summary(db, 2026, today=date(2026, 6, 1))
    assert sm.net == 19_880 and sm.by_category == {"mail": -120, "assignment_fee": 20_000}
    q2 = next(q for q in sm.quarters if q.due_date == "2026-06-15")
    assert q2.period_net == 19_880 and q2.set_aside == round(19_880 * 0.28, 2)
    assert sm.pipeline[0]["lead_id"] == lead.id and sm.pipeline[0]["probability"] == 0.5
    assert sm.pipeline_projected == round(30_000 * 0.5, -2)


def test_partner_pick_and_title_email(db):
    data = cc.partners()
    assert data["verified_on"] and all(p.get("source") for r in data["regions"].values()
                                       for k in ("title_agencies", "closing_attorneys") for p in r.get(k) or [])
    north = cc.pick_partner("Essex")
    assert "assign" in (north.get("investor_evidence") or "").lower()       # investor-friendly evidence first
    lead = make_lead(db, sheriff_days=20, absentee=True, block="120", lot="7")
    crm.record_contract(db, lead, 230_000, signed=date(2026, 10, 20))
    e = cc.title_order_email(lead)
    assert "Block 120, Lot 7" in e["subject"] and "municipal lien search" in e["body"].lower()
    assert "insure this structure" in e["body"] and "payoff/judgment" in e["body"] and "and/or assigns" in e["body"]
    assert "oil tank sweep" in e["body"]
