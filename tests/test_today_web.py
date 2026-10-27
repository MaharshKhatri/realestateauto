"""Daily checklist (today) and the Phase 4 cockpit endpoints + swarm agents."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from db.models import CashBuyer
from engine import crm
from engine import today as td
from engine.closing_coordinator import ClosingAlertsAgent
from engine.fast_track import FastTrackAgent
from engine.scoring import apply_score, default_weights
from tests.factories import add_contact, make_lead


def seed(db):
    """A listed foreclosure with an agent, a ready-to-call stale listing, a vacant sheriff sale with no phone, and a
    named Newark cash buyer."""
    lf = make_lead(db, stale_listing=True, list_price=360_000, absentee=True, sheriff_days=9)
    add_contact(db, lf, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test")
    crm.confirm_occupancy(db, lf, "VACANT", "agent on call")
    ready = make_lead(db, sheriff_days=None, stale_listing=True, list_price=360_000)
    add_contact(db, ready, contact_type="LISTING_AGENT", name="Lee Agent", phone="9735550111")
    vacant = make_lead(db, vacant=True, owner_name="SAMPLE, QUINN ET ALS", sheriff_days=20)
    for lead in (lf, ready, vacant):
        apply_score(lead, 1, default_weights())
    db.add(CashBuyer(entity_key="RIVER", entity_name="RIVER HOMES LLC", contact_name="Sam Rivera", flips_count=2,
                     buyer_type="flipper", cash_purchases_count=3,
                     recent_purchases_json=[{"address": "10 SAMPLE ST", "municipality": "NEWARK CITY", "county": "Essex",
                                             "price": 310000, "deed_date": "2026-06-01", "units": 2, "class": "2"}]))
    db.add(CashBuyer(entity_key="NONAME", entity_name="OWNER @ 1 OCEAN DR", cash_purchases_count=4,
                     recent_purchases_json=[{"address": "11 SAMPLE ST", "municipality": "NEWARK CITY",
                                             "county": "Essex", "price": 300000, "deed_date": "2026-05-01"}]))
    db.flush()
    return lf, ready, vacant


def test_today_blocks_from_live_records(db):
    lf, ready, vacant = seed(db)
    t = td.today(db)
    b1, b2, b3, b4 = t["blocks"]
    assert [r["lead_id"] for r in b1["rows"]] == [lf.id] and b1["rows"][0]["days_to_sale"] == 9
    assert b1["rows"][0]["agent"]["phone_display"] == "(973) 555-0100" and "fast-track --script" in b1["rows"][0]["cli"]
    assert ready.id in [r["lead_id"] for r in b2["rows"]] and lf.id not in [r["lead_id"] for r in b2["rows"]]
    assert b2[ "rows"][0]["gap_to_ask"] == 360_000 - 260_000
    row = next(r for r in b3["rows"] if r["lead_id"] == vacant.id)
    assert row["kind"] == "vacant" and row["target"] and "truepeoplesearch" in row["links"]
    assert "als" not in row["links"]["truepeoplesearch"].lower().split("name=")[1].split("&")[0]   # 'ET ALS' dropped
    assert [r["name"] for r in b4["rows"]] == ["Sam Rivera"] and b4["rows"][0]["flipper"]   # unnamed buyers skipped
    assert b4["rows"][0]["recent"]["town"] == "Newark"
    assert "never offer to stop or postpone" in t["reminder"]


def test_today_skips_recently_contacted_listed_foreclosure(db):
    from engine import agent_outreach as ao
    lf, _, _ = seed(db)
    ao.record_sent(db, lf, ao.build_packet(lf, generate_loi=False, session=db), "email")
    t = td.today(db)
    assert t["blocks"][0]["rows"] == [] and t["blocks"][0]["empty"]


def test_agents_fast_track_and_closing_alerts(db):
    lf, _, _ = seed(db)
    crm.record_contract(db, lf, 230_000, signed=date.today() - timedelta(days=20))
    db.commit()
    run = FastTrackAgent().execute()
    assert run.status == "success" and run.metrics_json["listed_foreclosures"] == 1
    run = ClosingAlertsAgent().execute()
    assert run.status == "success" and run.metrics_json["open_files"] == 1 and run.metrics_json["overdue"] >= 1
    db.expire_all()
    assert lf.next_followup_at is not None                                  # deadline surfaced in the queue


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    import engine.contracts as contracts
    import web.app as webapp
    monkeypatch.setattr(contracts, "DEALS_DIR", tmp_path / "deals")
    monkeypatch.setattr(webapp, "DEALS_DIR", tmp_path / "deals")
    return TestClient(webapp.app)


def test_phase4_endpoints(db, client):
    lf, _, _ = seed(db)
    db.commit()
    t = client.get("/api/today").json()
    assert t["blocks"][0]["rows"][0]["lead_id"] == lf.id
    ft = client.get("/api/fast-track").json()
    assert ft["listed"][0]["track"] == "CASH_OFFER" and ft["listed"][0]["script"]["opening"]
    pk = client.post(f"/api/fast-track/{lf.id}/packet").json()
    assert pk["packet"]["kind"] == "offer" and pk["files"]["pdf"].endswith("/loi.pdf")
    assert client.get("/api/closing").json() == {"alerts": [], "files": []}
    assert client.get(f"/api/closing/{lf.id}").json()["open"] is False
    client.post(f"/api/leads/{lf.id}/contract", json={"price": 230000, "signed": (date.today() - timedelta(days=20))
                                                      .isoformat()})
    f = client.get(f"/api/closing/{lf.id}").json()
    assert f["open"] and len(f["milestones"]) == 8 and "Block" in f["title_email"]["subject"]
    assert client.post(f"/api/closing/{lf.id}/milestones/EMD_DEPOSITED", json={"notes": "wired"}).json()["status"] == "done"
    assert client.post(f"/api/closing/{lf.id}/milestones/BOGUS", json={}).status_code == 409
    assert client.post("/api/finance", json={"kind": "expense", "category": "mail", "amount": 120}).status_code == 200
    assert client.post("/api/finance", json={"kind": "income", "category": "mail", "amount": 5}).status_code == 400
    assert client.post(f"/api/closing/{lf.id}/close", json={"fee": 25000}).json()["stage"] == "CLOSED"
    fin = client.get("/api/finance").json()
    assert fin["income"] == 25000 and fin["expenses"] == 120 and fin["tax_reserve"] == round(24880 * 0.28, 2)
