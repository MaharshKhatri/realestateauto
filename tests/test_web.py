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
    r = client.post("/api/calc", json={"arv": 500000, "repairs": 60000, "debt": 150000}).json()
    assert r["mao_conservative"] == 500000 * 0.70 - 60000 - r["assignment_fee"]
    blocked = client.post("/api/calc", json={"arv": 500000, "repairs": 60000, "debt": 200000, "fmv": 420000,
                                             "frfpa": True}).json()
    assert blocked["verdict"] == "EQUITY_RESCUE_REFERRAL" and blocked["anchor_offer"] is None


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
