"""Inbound funnel: export parsing, seller/buyer ingestion, consent-aware DNC, site serving and the public-host guard."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from db.models import CashBuyer, ContactType, Interaction, Property, SignalType
from engine import compliance
from engine import inbound as ib
from tests.factories import make_lead

SELLER = {"form_type": "seller", "address": "41 Sample Ave", "city": "Newark", "zip": "07106", "county": "Essex",
          "situation": ["inherited", "repairs"], "occupancy": "vacant", "timeline": "Within 1–3 months",
          "name": "Robin Testcase", "phone": "(973) 555-0142", "email": "robin@example.test", "consent": "yes",
          "submitted_at": "2026-10-27T14:00:00Z"}
BUYER = {"form_type": "buyer", "name": "Casey Buyer", "llc": "Garden Flip LLC", "email": "casey@example.test",
         "phone": "9735550177", "counties": ["Essex", "Union"], "asset_classes": ["fix_and_flip", "1-4_family"],
         "min_price": "150k", "max_price": "600k", "max_rehab": "heavy", "proof_of_funds": "cash_ready",
         "consent": "yes"}


def test_parse_exports_json_csv_and_text():
    js = json.dumps({"submissions": [{"data": {"Property Address": "41 Sample Ave", "Your Name": "Robin",
                                               "situation": "inherited, repairs"}}]})
    rows = ib.parse_export(js)
    assert rows[0]["address"] == "41 Sample Ave" and rows[0]["name"] == "Robin" and ib.form_type(rows[0]) == "seller"
    csv_rows = ib.parse_export("name,llc,email,counties\nCasey,Garden Flip LLC,c@x.test,Essex\n", "export.csv")
    assert ib.form_type(csv_rows[0]) == "buyer" and csv_rows[0]["llc"] == "Garden Flip LLC"
    text = ib.parse_export("form_type: seller\naddress: 41 Sample Ave\nphone: 9735550142\n")
    assert text[0]["phone"] == "9735550142" and ib.form_type(text[0]) == "seller"


def test_ingest_seller_creates_scored_lead_with_consent_and_reply(db):
    now = datetime(2026, 10, 27, 10, 0)
    r = ib.ingest_seller(db, SELLER, gis=None, now=now)
    assert r.created and not r.matched_parcel and r.lead_id
    lead = db.get(Property, r.property_id).lead
    p = lead.property
    assert p.parcel_id.startswith("ESSEX-INBOUND-41-SAMPLE-AVE") and p.city == "Newark" and p.zip_code == "07106"
    sig = next(s for s in p.active_signals if s.signal_type == SignalType.INBOUND_LEAD)
    assert sig.raw_metadata_json["situation"] == ["inherited / estate", "needs repairs"]
    owner = next(c for c in lead.contacts if c.contact_type == ContactType.OWNER)
    assert owner.phone == "9735550142" and owner.dnc_status == "consented" and owner.raw_json["consent"]["text"]
    assert lead.occupancy_status == "VACANT" and lead.primary_source == "inbound_web"
    assert lead.score_breakdown_json["factors"]["inbound_inquiry"] == 45
    assert lead.score_breakdown_json["reasons"][0].startswith("Owner contacted us through the website")
    assert lead.next_followup_at == now
    inter = db.query(Interaction).filter_by(lead_id=lead.id, outcome="INBOUND_RECEIVED").one()
    reply = inter.debrief_json["response"]["email"]
    assert "not agents" in reply and "assign our contract" in reply and "attorney review" in reply
    assert "STOP" in inter.debrief_json["response"]["sms"]
    again = ib.ingest_seller(db, SELLER, gis=None, now=now)                 # same submission: idempotent
    assert again.lead_id == r.lead_id and not again.created


def test_consent_lets_registry_number_be_called_but_internal_stop_wins(db):
    r = ib.ingest_seller(db, SELLER, gis=None)
    owner = next(c for c in db.get(Property, r.property_id).lead.contacts if c.contact_type == ContactType.OWNER)
    noon = datetime(2026, 10, 27, 16, 0, tzinfo=ZoneInfo("UTC"))
    db.add(compliance.DncEntry(phone="9735550142", source="ftc"))
    db.flush()
    ok, why = compliance.can_call(db, owner, noon)
    assert ok and "consent" in why
    compliance.add_internal(db, "9735550142", notes="asked to stop")
    assert compliance.can_call(db, owner, noon) == (False, "on a do-not-call list: send a letter instead")


def test_seller_validation_and_county_guess(db):
    with pytest.raises(ib.InboundError, match="address"):
        ib.ingest_seller(db, {**SELLER, "address": ""})
    with pytest.raises(ib.InboundError, match="phone number or an e-mail"):
        ib.ingest_seller(db, {**SELLER, "phone": "", "email": ""})
    with pytest.raises(ib.InboundError):
        ib.ingest_seller(db, {**SELLER, "website": "http://spam.test"})
    with pytest.raises(ib.InboundError, match="county"):
        ib.ingest_seller(db, {**SELLER, "county": "", "zip": "99999", "city": "Nowhere"})
    make_lead(db, zip_code="07208", county="Union", municipality="ELIZABETH CITY")
    r = ib.ingest_seller(db, {**SELLER, "county": "", "zip": "07208", "address": "9 Other St"})
    assert db.get(Property, r.property_id).county == "Union"
    no_consent = ib.ingest_seller(db, {**SELLER, "consent": "", "address": "10 Other St"})
    assert no_consent.response_sms == ""                                     # no consent: reply by e-mail only


def test_ingest_buyer_buy_box_and_consent(db):
    with pytest.raises(ib.InboundError, match="tick the box"):
        ib.ingest_buyer(db, {**BUYER, "consent": ""})
    b, created = ib.ingest_buyer(db, BUYER)
    assert created and b.entity_name == "Garden Flip LLC" and b.contact_name == "Casey Buyer"
    assert b.target_counties == ["Essex", "Union"] and b.asset_classes == ["fix & flip", "1-4 family"]
    assert b.buyer_type == "flipper" and b.max_rehab == "HEAVY" and b.min_arv == 150_000 and b.max_arv == 600_000
    assert b.proof_of_funds == "cash ready" and b.registered_at is not None
    b2, created2 = ib.ingest_buyer(db, {**BUYER, "max_rehab": "light"})
    assert not created2 and b2.id == b.id and b2.max_rehab == "LIGHT"
    assert db.query(CashBuyer).count() == 1


@pytest.fixture()
def client(db, monkeypatch):
    import web.app as webapp
    monkeypatch.setattr(webapp, "inbound_gis", lambda: None)
    db.commit()
    return TestClient(webapp.app)


def test_site_pages_inbound_api_and_public_host_guard(db, client, monkeypatch):
    page = client.get("/site/seller")
    assert page.status_code == 200 and "Sell your New Jersey house as-is" in page.text
    assert "not licensed real estate agents" in page.text and client.get("/site/buyers").status_code == 200
    cfg = client.get("/site/config.js").text
    assert cfg.startswith("window.SITE_CONFIG") and '"mode": "local"' in cfg
    assert client.get("/site/site.css").status_code == 200 and client.get("/site/site.js").status_code == 200
    r = client.post("/api/inbound/seller", json=SELLER)
    assert r.status_code == 200 and r.json()["ok"]
    assert client.post("/api/inbound/seller", json={**SELLER, "address": ""}).status_code == 400
    assert client.post("/api/inbound/buyer", json=BUYER).json()["created"]
    monkeypatch.setitem(settings(), "web", {"public_hosts": ["deals.example.test"]})
    public = {"host": "deals.example.test"}
    assert client.get("/site/seller", headers=public).status_code == 200
    assert client.post("/api/inbound/buyer", json=BUYER, headers=public).status_code == 200
    assert client.get("/api/dashboard", headers=public).status_code == 403          # lead data stays local
    assert client.get("/api/leads", headers=public).status_code == 403
    assert client.get("/site/seller", headers={"host": "evil.test"}).status_code == 403


def test_site_build_writes_static_config(tmp_path, monkeypatch):
    import manage
    monkeypatch.setitem(settings()["operator"], "company_name", "Garden State Home Buyers LLC")
    out = tmp_path / "dist"
    manage.main(["site", "--build", str(out), "--endpoint", "https://formspree.io/f/abc123"])
    cfg = (out / "config.js").read_text()
    assert (out / "index.html").exists() and '"mode": "endpoint"' in cfg and "formspree.io/f/abc123" in cfg
    assert "Garden State Home Buyers LLC" in cfg


def test_address_without_town_keeps_street_intact(db):
    r = ib.ingest_seller(db, {**SELLER, "city": "", "address": "41 Sample Ave"}, gis=None)
    p = db.get(Property, r.property_id)
    assert p.street_address == "41 SAMPLE AVE" and p.county == "Essex"
