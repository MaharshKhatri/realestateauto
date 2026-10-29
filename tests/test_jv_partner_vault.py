"""engine/jv_partner_vault.py: seeding from recorded buyers, Phase 0 outreach copy, buy-box matching, proof of funds."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.config import settings
from db.models import CashBuyer, JVPartner
from engine import human_copy as hc
from engine import jv_partner_vault as vault
from tests.factories import make_lead


def buyer(db, key, name, *, contact=None, purchases=(), flips=0, phone=None, email=None, registered=False):
    b = CashBuyer(entity_key=key, entity_name=name, contact_name=contact, phone=phone, email=email,
                  recent_purchases_json=list(purchases), cash_purchases_count=len(purchases), flips_count=flips,
                  registered_at=datetime(2026, 9, 1) if registered else None)
    db.add(b)
    db.flush()
    return b


def buy(price, town="NEWARK CITY", county="Essex", when="2026-06-01", address="12 SAMPLE ST", units=1):
    return {"address": address, "municipality": town, "county": county, "price": price, "deed_date": when,
            "units": units, "class": "2"}


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    import engine.contracts as contracts
    import web.app as webapp
    from fastapi.testclient import TestClient
    monkeypatch.setattr(contracts, "DEALS_DIR", tmp_path / "deals")
    monkeypatch.setattr(webapp, "DEALS_DIR", tmp_path / "deals")
    db.commit()
    return TestClient(webapp.app)


@pytest.fixture()
def operator(monkeypatch):
    for k, v in {"name": "Jordan Operator", "company_name": "Garden State Home Buyers LLC", "phone": "908-555-0100",
                 "email": "jordan@example.test", "mailing_address": "PO Box 10, Edison, NJ 08817"}.items():
        monkeypatch.setitem(settings()["operator"], k, v)


def test_seed_picks_named_repeat_buyers_and_is_idempotent(db):
    a = buyer(db, "A", "RIVER HOMES LLC", contact="Sam Rivera", purchases=[buy(300000), buy(350000), buy(410000)])
    buyer(db, "B", "OWNER @ 9 MAIL ST", purchases=[buy(300000), buy(350000)])        # unnamed cluster: nobody to write to
    buyer(db, "C", "ONE OFF LLC", purchases=[buy(200000)])                            # one purchase, not registered
    d = buyer(db, "D", "WEB BUYER", contact="Dana Web", purchases=[], registered=True)
    e = buyer(db, "E", "FLIP CO LLC", purchases=[buy(250000, town="CAMDEN CITY", county="Camden")] * 2, flips=2)
    rows = vault.seed(db, top=30)
    ids = {p.buyer_id for p in rows}
    assert ids == {a.id, d.id, e.id}
    first = next(p for p in rows if p.buyer_id == a.id)
    assert first.contact_name == "Sam Rivera" and first.status == "TARGET" and first.target_counties == ["Essex"]
    assert first.min_price and first.max_price and first.min_price <= 300000 <= first.max_price
    assert vault.velocity(e) > vault.velocity(a) > 0          # confirmed flips outrank plain purchase counts
    again = vault.seed(db, top=30)
    assert len(again) == 3 and db.query(JVPartner).count() == 3
    first.contact_name = "Samuel Rivera"                       # operator edits survive a re-seed
    vault.seed(db, top=30)
    assert db.get(JVPartner, first.id).contact_name == "Samuel Rivera"


def test_phase0_outreach_copy_is_true_plain_and_compliant(db, operator):
    buyer(db, "A", "RIVER HOMES LLC", contact="Sam Rivera", purchases=[buy(300000), buy(350000, address="81 WESTFIELD AVE", when="2026-08-01")])
    p = vault.seed(db, top=5)[0]
    card = vault.outreach(db, p)
    body = card.body
    assert body.startswith("Hi Sam,") and "county deed records" in body and "81 Westfield Ave in Newark" in body
    assert "two active local rehabbers" in body and "buy box" in body and "joint venture" in body
    assert "not a licensed agent" in body and "attorney review" in body and "no referral fees" in body
    assert "PO Box 10" in body and "STOP" in body and card.style == [] and hc.lint(body) == []
    assert "Rutgers" in body        # the operator's own true intro line from settings
    assert "STOP" in card.sms and len(card.phone_script) == 3 and card.mailto is None
    for phrase in ("drive by", "door-knock", "stop by the house", "cash buyer"):
        assert phrase not in body.lower()
    cards = vault.phase0_cards(db, limit=5)
    assert cards and cards[0]["partner_id"] == p.id and vault.phase0_open(db)


def test_deed_record_without_a_town_names_the_county(db, operator):
    buyer(db, "A", "1763 REALTY LLC", purchases=[{"address": "81 WESTFIELD AVE", "county": "Union", "price": 475000,
                                                     "deed_date": "2026-02-26", "class": "2"}] * 2)
    p = vault.seed(db, top=5)[0]
    card = vault.outreach(db, p)
    assert "81 Westfield Ave in Union County" in card.body and " in ." not in card.body
    assert card.first == "there" and card.body.startswith("Hi there,")


def test_touch_update_and_pof_upload(db, tmp_path):
    buyer(db, "A", "RIVER HOMES LLC", contact="Sam Rivera", purchases=[buy(300000), buy(350000)])
    p = vault.seed(db, top=5)[0]
    vault.log_touch(db, p, "email", "sent the opener", when=datetime.now() - timedelta(days=1))
    assert p.status == "CONTACTED" and " email: sent the opener" in p.notes
    assert vault.phase0_cards(db, limit=5) == []          # contacted this week: not on today's cards
    vault.update(db, p, phone="(908) 555-0101", max_rehab="HEAVY", provides_emd=True, default_split_pct=40)
    assert p.phone == "9085550101" and p.max_rehab == "HEAVY" and p.provides_emd and p.default_split_pct == 40
    with pytest.raises(ValueError):
        vault.update(db, p, status="FRIEND")
    with pytest.raises(ValueError):
        vault.attach_pof(db, p, "pof.exe", b"x", root=tmp_path)
    with pytest.raises(ValueError):
        vault.attach_pof(db, p, "pof.pdf", b"", root=tmp_path)
    dest = vault.attach_pof(db, p, "Proof of Funds.pdf", b"%PDF-1.4 sample", root=tmp_path)
    assert dest.exists() and dest.parent == tmp_path / str(p.id) and p.pof_verified and p.status == "ACTIVE_PARTNER"
    assert vault.active_count(db) == 1 and vault.partner_dict(p)["pof_document"] == dest.name
    assert not vault.phase0_open(db) if vault.partners_needed() <= 1 else vault.phase0_open(db)


def test_best_partner_and_pof_language_follow_the_buy_box(db, tmp_path):
    lead = make_lead(db, sheriff_days=None, upset=None, vacant=True, verdict="STRONG")      # Newark, Essex, MEDIUM
    essex = buyer(db, "A", "RIVER HOMES LLC", contact="Sam Rivera", purchases=[buy(300000), buy(350000)])
    camden = buyer(db, "B", "SOUTH LLC", contact="Lee South", purchases=[buy(200000, "CAMDEN CITY", "Camden")] * 2)
    pa, pb = vault.seed(db, top=5)
    assert {pa.buyer_id, pb.buyer_id} == {essex.id, camden.id}
    pa = pa if pa.buyer_id == essex.id else pb
    assert vault.best_partner(db, lead) is None and vault.pof_language(db, lead) is None    # nobody active yet
    s = vault.deal_partner_summary(db, lead)
    assert s["partner"] is None and s["active_partners"] == 0 and "Vault" in s["note"]
    vault.attach_pof(db, pa, "pof.pdf", b"%PDF-1.4 x", root=tmp_path)
    assert vault.best_partner(db, lead).id == pa.id
    assert vault.pof_language(db, lead) == "Acquiring with our verified local capital partner (proof of funds available on request)."
    vault.update(db, pa, pof_share_ok=True)
    assert "proof of funds attached" in vault.pof_language(db, lead)
    vault.update(db, pa, max_price=150000)                      # the buy box no longer covers the deal
    assert vault.best_partner(db, lead) is None
    vault.update(db, pa, max_price=900000, max_rehab="LIGHT")   # a light-rehab-only partner skips a MEDIUM job
    assert vault.best_partner(db, lead) is None


def test_partner_endpoints_and_deck_phase0(db, client, operator, tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "PARTNERS_DIR", tmp_path)
    buyer(db, "A", "RIVER HOMES LLC", contact="Sam Rivera", purchases=[buy(300000), buy(350000)])
    db.commit()
    r = client.post("/api/partners/seed").json()
    assert r["seeded"] == 1 and r["partners"][0]["name"] == "Sam Rivera"
    pid = r["partners"][0]["id"]
    lst = client.get("/api/partners").json()
    assert lst["active"] == 0 and lst["phase0_open"] and lst["cards"][0]["partner_id"] == pid
    deck = client.get("/api/autopilot").json()
    assert deck["partners"]["open"] and deck["partners"]["cards"][0]["partner_id"] == pid and deck["first_touch"] == "opener"
    assert client.patch(f"/api/partners/{pid}", json={"status": "FRIEND"}).status_code == 400
    upd = client.patch(f"/api/partners/{pid}", json={"target_counties": ["Essex", "Union"], "max_price": 500000,
                                                     "provides_emd": True}).json()
    assert upd["target_counties"] == ["Essex", "Union"] and upd["provides_emd"]
    t = client.post(f"/api/partners/{pid}/touch", json={"channel": "email", "notes": "opener sent"}).json()
    assert t["status"] == "CONTACTED" and t["last_contact_at"]
    bad = client.post(f"/api/partners/{pid}/pof", files={"file": ("pof.txt", b"nope", "text/plain")})
    assert bad.status_code == 400
    ok = client.post(f"/api/partners/{pid}/pof", files={"file": ("pof.pdf", b"%PDF-1.4 sample", "application/pdf")},
                     data={"share_ok": "true"}).json()
    assert ok["pof_verified"] and ok["status"] == "ACTIVE_PARTNER" and ok["pof_share_ok"] and ok["active"] == 1
    assert (tmp_path / str(pid)).exists()
    new = client.post("/api/partners", json={"contact_name": "Pat Partner", "phone": "9085550199",
                                             "target_counties": ["Hudson"]}).json()
    assert new["name"] == "Pat Partner" and new["status"] == "TARGET" and new["phone"] == "9085550199"
    assert client.post("/api/partners", json={}).status_code == 400
    lead = make_lead(db, sheriff_days=None, upset=None, vacant=True, verdict="STRONG")
    db.commit()
    lp = client.get(f"/api/leads/{lead.id}/partner").json()
    assert lp["partner"] and lp["partner"]["id"] == pid and "proof of funds attached" in lp["pof_sentence"]
    cal = client.get("/api/pricing/calibration").json()
    assert cal["model"] in ("quant", "fixed_pct") and "statewide" in cal
