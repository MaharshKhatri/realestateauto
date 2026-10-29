from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.factories import add_contact, make_lead


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    import engine.contracts as contracts
    import web.app as webapp
    monkeypatch.setattr(contracts, "DEALS_DIR", tmp_path / "deals")
    monkeypatch.setattr(webapp, "DEALS_DIR", tmp_path / "deals")
    db.commit()
    return TestClient(webapp.app)


def test_dashboard_tabs_and_lead_page(db, client):
    lead = make_lead(db, sheriff_days=6, vacant=True)
    add_contact(db, lead, name="Quinn Sample", phone="9735550001")
    db.commit()
    d = client.get("/api/dashboard").json()
    assert d["funnel"]["NEW"] == 1 and d["kpis"]["sales_21d"] == 1 and d["call_queue"][0]["id"] == lead.id
    for tab in ("all", "callable", "sheriff", "deals", "pipeline", "frfpa", "listings", "estate", "followups"):
        r = client.get(f"/api/leads?tab={tab}")
        assert r.status_code == 200, tab
    assert client.get("/api/leads?tab=callable").json()["total"] == 1
    assert client.get("/api/leads?tab=bogus").status_code == 400
    detail = client.get(f"/api/leads/{lead.id}").json()
    assert detail["card"]["playbook"] == "A" and detail["contacts"][0]["phone_display"] == "(973) 555-0001"
    assert detail["frfpa"]["track"] == "EXEMPT_VACANT" and "loi" in detail["gates"]
    assert client.get("/").status_code == 200 and "Cockpit" in client.get("/").text


def test_calculator_matches_offer_math(client):
    r = client.post("/api/calc", json={"arv": 500000, "repairs": 60000, "debt": 150000,
                                       "pricing_model": "fixed_pct"}).json()
    assert r["pricing_model"] == "fixed_pct" and r["quant"] is None
    assert r["mao_conservative"] == 500000 * 0.70 - 60000 - r["assignment_fee"]
    blocked = client.post("/api/calc", json={"arv": 500000, "repairs": 60000, "debt": 200000, "fmv": 420000,
                                             "frfpa": True, "pricing_model": "fixed_pct"}).json()
    assert blocked["verdict"] == "EQUITY_RESCUE_REFERRAL" and blocked["anchor_offer"] is None


def test_calculator_quant_model_exposes_buyer_math(client):
    r = client.post("/api/calc", json={"arv": 500000, "repairs": 60000, "annual_taxes": 10000, "ask": 400000,
                                       "mun_code": "1205", "county": "Middlesex", "pricing_model": "quant"}).json()
    q = r["quant"]
    assert r["pricing_model"] == "quant" and q["buyer_max"] == r["buyer_purchase_price"]
    assert q["mao_walkaway"] == r["mao_aggressive"] == q["buyer_max"] - 10000
    assert r["mao_conservative"] == q["mao_target"] < q["mao_walkaway"]
    assert r["anchor_offer"] == q["opening_offer"] <= 0.90 * 400000
    assert q["buyer"]["roi"] >= 0.15 - 1e-9 and q["buyer"]["profit"] >= 45000 - 1 or q["binding"] == "local_alpha"
    tighter = client.post("/api/calc", json={"arv": 500000, "repairs": 60000, "annual_taxes": 10000,
                                             "min_roi": 0.25, "pricing_model": "quant"}).json()
    assert tighter["buyer_purchase_price"] < r["buyer_purchase_price"]


def test_call_logging_occupancy_and_documents(db, client):
    lead = make_lead(db, absentee=True)
    c = add_contact(db, lead)
    db.commit()
    blocked = client.post(f"/api/leads/{lead.id}/docs/loi", json={})
    assert blocked.status_code == 409 and "presumed" in " ".join(blocked.json()["reasons"])
    r = client.post(f"/api/leads/{lead.id}/calls", json={"outcome": "SPOKE_NEEDS_OFFER", "contact_id": c.id,
                                                         "occupancy": "TENANT_OCCUPIED", "notes": "tenant pays 1800"})
    body = r.json()
    assert r.status_code == 200 and body["stage"] == "INTERESTED" and "loi" in body["suggested_docs"]
    doc = client.post(f"/api/leads/{lead.id}/docs/loi", json={})
    assert doc.status_code == 200 and doc.json()["files"]["pdf"].endswith("/loi.pdf")
    assert client.get(doc.json()["files"]["pdf"]).content.startswith(b"%PDF")
    assert client.get(f"/api/leads/{lead.id}").json()["lead"]["stage"] == "OFFER_MADE"
    assert client.post(f"/api/leads/{lead.id}/calls", json={"outcome": "MAYBE"}).status_code == 400


def test_skiptrace_paste_and_dnc(db, client):
    lead = make_lead(db)
    db.commit()
    r = client.post(f"/api/leads/{lead.id}/skiptrace", json={"text": "Quinn Sample (201) 555-0177 Wireless"})
    assert r.status_code == 200 and any(c["phone"] == "2015550177" for c in r.json()["contacts"])
    assert client.post("/api/dnc", json={"phone": "2015550177"}).json()["phone"] == "2015550177"
    assert client.post("/api/dnc", json={"phone": "1"}).status_code == 400


def test_local_only_guards(client):
    assert client.get("/api/dashboard", headers={"host": "evil.example"}).status_code == 403
    r = client.post("/api/dnc", json={"phone": "2015550100"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/deals/1/../../etc/passwd").status_code == 404


# -- Phase 3 cockpit tabs ----------------------------------------------------------------------------
def _listed_ready(db):
    from engine.scoring import apply_score, default_weights
    lead = make_lead(db, sheriff_days=None, stale_listing=True, list_price=360_000)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test")
    apply_score(lead, 1, default_weights())
    db.commit()
    return lead


def test_ready_tab_briefing_and_agent_outreach(db, client):
    lead = _listed_ready(db)
    assert client.get("/api/leads?tab=ready").json()["total"] == 1
    assert client.get("/api/leads?tab=stacked").status_code == 200
    assert client.get("/api/dashboard").json()["kpis"]["ready_to_call"] == 1
    b = client.get("/api/briefing").json()
    assert b["counts"]["ready_to_call"] == 1 and b["ready"][0]["phone_display"] == "(973) 555-0100"
    q = client.get("/api/agent-outreach").json()
    assert q["packets"][0]["lead_id"] == lead.id and q["smtp"]["ready"] is False
    loi = client.post(f"/api/agent-outreach/{lead.id}/loi").json()
    assert loi["files"]["pdf"].endswith("/loi.pdf")
    sent = client.post(f"/api/agent-outreach/{lead.id}/send", json={"channel": "email"}).json()
    assert sent["stage"] == "OFFER_SENT_TO_AGENT" and sent["followup_at"]
    smtp = client.post(f"/api/agent-outreach/{lead.id}/send", json={"channel": "smtp"})
    assert smtp.status_code == 409                       # no SMTP credentials / postal address configured
    assert client.post(f"/api/agent-outreach/{lead.id}/send", json={"channel": "fax"}).status_code == 400
    agent_id = q["packets"][0]["agent_contact_id"]
    assert client.post("/api/agent-outreach/opt-out", json={"contact_id": agent_id}).json()["dnc_status"] == "dnc"


def test_inspector_mail_merge_and_suppression(db, client, tmp_path, monkeypatch):
    import engine.direct_mail as dm
    monkeypatch.setattr(dm, "MAIL_DIR", tmp_path / "mail")
    lead = make_lead(db, owner_name="ROE, JANE")
    db.commit()
    d = client.get(f"/api/leads/{lead.id}/inspect").json()
    assert "street_view" in d["links"] and d["checklist"]
    prev = client.get("/api/mail-merge").json()
    assert prev["letters"][0]["kind"] == "pre_auction" and prev["operator_ready"] is False
    made = client.post("/api/mail-merge", json={"top": 5}).json()
    assert made["letters"] == 1 and made["proof"] is True
    assert client.get(made["files"]["pre_auction.pdf"]).content.startswith(b"%PDF")
    assert client.post(f"/api/mail-merge/{made['batch']}/mailed").status_code == 409   # proofs can't be mailed
    assert client.post("/api/mail-merge/../../etc/mailed").status_code == 404
    assert client.get("/mail/../../wholesaling.db").status_code == 404
    r = client.post("/api/mail-suppress", json={"address": f"{lead.property.street_address}, Newark",
                                                "source": "daniels_law"})
    assert r.status_code == 200 and client.get("/api/mail-merge").json()["letters"] == []


def test_hotzones_walkthrough_bids_accept(db, client):
    from db.models import CashBuyer
    from engine import crm
    lead = make_lead(db, sheriff_days=None, absentee=True)
    b = CashBuyer(entity_key="RIVER", entity_name="RIVER HOMES LLC", contact_name="Sam Rivera",
                  recent_purchases_json=[{"address": "1 A ST", "municipality": "NEWARK CITY", "county": "Essex",
                                          "price": 290000, "deed_date": "2026-06-01", "units": 1, "class": "2"}])
    b2 = CashBuyer(entity_key="OAK", entity_name="OAK LLC",
                   recent_purchases_json=[{"address": "2 B ST", "municipality": "NEWARK CITY", "county": "Essex",
                                           "price": 300000, "deed_date": "2026-05-01", "units": 1, "class": "2"}])
    db.add_all([b, b2])
    db.commit()
    zones = client.get("/api/hotzones").json()["zones"]
    assert zones and zones[0]["leads"][0]["lead_id"] == lead.id
    blocked = client.post(f"/api/leads/{lead.id}/bids", json={"buyer_id": b.id, "amount": 270000})
    assert blocked.status_code == 409 and blocked.json()["blocked"]
    crm.record_contract(db, lead, 230000, signed=date.today() - timedelta(days=20))
    db.commit()
    w = client.get(f"/api/leads/{lead.id}/walkthrough").json()
    assert w["plan"]["blocked"] == [] and w["contract_price"] == 230000
    assert client.post(f"/api/leads/{lead.id}/bids", json={"buyer_id": b.id, "action": "invite"}).json()["bid"]["status"] == "invited"
    r = client.post(f"/api/leads/{lead.id}/bids", json={"buyer_id": b.id, "amount": 270000, "emd": 5000}).json()
    assert r["bid"]["fee"] == 40000 and r["warnings"] == ["Assignment fee at this bid: $40,000."]
    acc = client.post(f"/api/bids/{r['bid']['id']}/accept", json={"structure": "assignment"}).json()
    assert acc["stage"] == "ASSIGNED" and acc["files"]["pdf"].endswith("/assignment.pdf")
    assert client.get(acc["files"]["pdf"]).content.startswith(b"%PDF")
    assert client.post(f"/api/leads/{lead.id}/bids", json={"buyer_id": b.id, "action": "nope"}).status_code == 400
