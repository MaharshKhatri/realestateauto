"""Reverse wholesaling: buy-box hotzones, remote walkthrough gating/slots/scripts, buyer bids and acceptance."""
from datetime import date, datetime

import pytest

from db.models import CashBuyer, Interaction
from engine import crm
from engine import disposition as dp
from tests.factories import make_lead

TODAY = date(2026, 10, 27)
BANNED = ("drive by", "door-knock", "stop by the house", "i'll come see it")


def buyer(db, key, name=None, purchases=(), contact=None, phone=None):
    b = CashBuyer(entity_key=key, entity_name=name or f"OWNER @ {key}", contact_name=contact, phone=phone,
                  recent_purchases_json=list(purchases), cash_purchases_count=len(purchases))
    db.add(b)
    db.flush()
    return b


def buy(price, town="NEWARK CITY", units=2, cls="2", when="2026-06-01", county="Essex", address=None):
    return {"address": address or f"{price} SAMPLE ST", "municipality": town, "county": county, "price": price,
            "deed_date": when, "units": units, "class": cls}


def test_products_and_bands():
    assert dp.product("2", 1) == "1-family" and dp.product("2", 3) == "2-4 family" and dp.product("2", None) == "1-4 family"
    assert dp.product("4C", None) == "5+ apartments" and dp.product("1", None) == "land"
    assert dp.products_compatible("1-4 family", "2-4 family") and not dp.products_compatible("1-family", "2-4 family")
    assert dp.band_of(199_999) == "under $200K" and dp.band_of(400_000) == "$350K–500K"
    assert dp.band_fits("$350K–500K", 510_000) and not dp.band_fits("$350K–500K", 600_000)
    assert dp.recency_weight(date(2026, 1, 1), TODAY) == 1.0 and dp.recency_weight(date(2024, 1, 1), TODAY) == 0.4


def test_hotzones_need_two_buyers_cap_bulk_buys_and_list_our_deals(db):
    bulk = [buy(400_000 + i, address=f"{i} BULK ST") for i in range(10)]     # one portfolio buyer, 10 houses
    a = buyer(db, "A", purchases=bulk)
    b = buyer(db, "B", "SUNRISE HOMES LLC", purchases=[buy(450_000), buy(5, when="2026-06-02")])   # $5 = not arm's length
    buyer(db, "C", purchases=[buy(420_000, town="PATERSON CITY", county="Passaic")])   # lone buyer: no pocket
    deal = make_lead(db, dwellings=2)          # 2-family; buyer price (75% MAO) $285K → the $200K–350K band
    zones = dp.build_hotzones(dp.buyer_purchases(db, TODAY), {x.id: x for x in (a, b)}, TODAY)
    assert len(zones) == 1
    z = zones[0]
    assert (z.town, z.product, z.band) == ("NEWARK", "2-4 family", "$350K–500K")
    assert z.buyers == 2 and z.purchases == 11
    assert z.score == pytest.approx((400_009 + 400_008 + 400_007 + 450_000) / 1000, abs=0.2)   # bulk capped at 3
    dp.attach_leads(zones, [deal])
    assert z.leads == []                                  # $285K deal is not in the $350K–500K pocket
    mid = buyer(db, "D", purchases=[buy(300_000), buy(310_000, when="2026-07-01")])
    e = buyer(db, "E", "PINE LLC", purchases=[buy(290_000)])
    zones = dp.build_hotzones(dp.buyer_purchases(db, TODAY), {x.id: x for x in (a, b, mid, e)}, TODAY)
    pocket = next(z for z in zones if z.band == "$200K–350K")
    dp.attach_leads([pocket], [deal])
    assert pocket.leads[0]["lead_id"] == deal.id and pocket.leads[0]["headroom"] == 300_000 - 285_000
    assert pocket.top_buyers[0]["name"].startswith("unnamed buyer")        # Daniel's Law: no owner name on record


def test_gate_window_slots_and_scripts(db):
    lead = make_lead(db, sheriff_days=None, absentee=True)
    assert "No signed seller contract" in dp.disposition_gate(lead, TODAY)[0]
    crm.record_contract(db, lead, 230_000, signed=date(2026, 10, 26))     # review ends Thu Oct 29
    assert "runs through" in dp.disposition_gate(lead, TODAY)[0]
    w = dp.walkthrough_window(lead, TODAY)
    assert w["status"] == "not_open" and w["opens"] == date(2026, 10, 30)
    w = dp.walkthrough_window(lead, date(2026, 10, 30))
    assert w["status"] == "open" and w["slots"][0] == datetime(2026, 11, 2, 11, 0)       # next business day
    assert [s.date() for s in w["slots"]] == sorted({s.date() for s in w["slots"]})    # one slot per day
    b = buyer(db, "X", "RIVER HOMES LLC", contact="Sam Rivera")
    plan = dp.walkthrough_plan(lead, [b], date(2026, 10, 30))
    req = plan["access_request"].lower()
    assert "buying partner" in req and "the investor i may assign the contract to" in req
    assert "may bring their own contractor" in req and not any(x in req for x in BANNED)
    inv = plan["invites"][0]
    assert inv["sms"].startswith("Hi Sam,") and "STOP" in inv["sms"]
    assert "acting as a principal, not a licensed agent" in inv["email"] and "equitable interest" in inv["email"]
    assert "don't discuss price or terms with the seller" in inv["email"]


def test_bids_best_bid_accept_and_assignment(db, tmp_path):
    lead = make_lead(db, sheriff_days=None, absentee=True)
    b1 = buyer(db, "B1", "RIVER HOMES LLC", contact="Sam Rivera")
    b2 = buyer(db, "B2", "OAK EQUITY LLC")
    with pytest.raises(dp.DispositionBlocked):
        dp.record_bid(db, lead, b1, 270_000)                     # nothing marketed before a contract
    crm.record_contract(db, lead, 230_000, signed=date(2026, 10, 20))
    bid0 = dp.invite(db, lead, b1, datetime(2026, 10, 28, 11))
    assert bid0.status == "scheduled"
    dp.mark_walked(db, bid0)
    assert bid0.status == "walked"
    bid1, w1 = dp.record_bid(db, lead, b1, 270_000, emd=5_000)
    bid2, w2 = dp.record_bid(db, lead, b2, 275_000, emd=1_000)
    assert bid1.id == bid0.id and w1 == ["Assignment fee at this bid: $40,000."]
    assert any("below the $5,000" in w for w in w2)
    _, low = dp.record_bid(db, lead, buyer(db, "B3"), 220_000)
    assert "loses money" in low[0]
    assert dp.best_bid(db, lead).id == bid1.id                  # full deposit beats $5K more with a token deposit
    doc, notes = dp.accept_bid(db, bid1, out_root=tmp_path)
    assert bid1.status == "accepted" and bid2.status == "backup" and lead.status_stage == "ASSIGNED"
    text = next(p for p in doc.paths if p.suffix == ".md").read_text()
    assert "Assignee:** Sam Rivera" in text and "assignment fee of **$40,000**" in text
    assert "assignment fee $40,000" in notes[0]
    assert db.query(Interaction).filter_by(lead_id=lead.id, outcome="BUYER_BID_ACCEPTED").count() == 1


def test_accept_requires_a_named_assignee_and_no_loss(db, tmp_path):
    lead = make_lead(db, sheriff_days=None, absentee=True)
    crm.record_contract(db, lead, 230_000, signed=date(2026, 10, 20))
    unnamed = buyer(db, "U1")                                     # NJGIN mailing cluster: no name on record
    bid, _ = dp.record_bid(db, lead, unnamed, 260_000, emd=5_000)
    with pytest.raises(dp.DispositionBlocked, match="no name on record"):
        dp.accept_bid(db, bid, out_root=tmp_path)
    doc, _ = dp.accept_bid(db, bid, "jv", 40, assignee_name="Garden Test Homes LLC", out_root=tmp_path)
    assert doc.doc_type == "jv" and "Garden Test Homes LLC" in next(p for p in doc.paths if p.suffix == ".md").read_text()
    loser, _ = dp.record_bid(db, lead, buyer(db, "U2", "LOW LLC"), 200_000)
    with pytest.raises(dp.DispositionBlocked, match="below our"):
        dp.accept_bid(db, loser, out_root=tmp_path)


def test_asking_price_and_find_buyer(db):
    lead = make_lead(db, sheriff_days=None, absentee=True)
    assert dp.asking_price(lead) == 285_000
    lead.offer_amount = 270_000                                   # contract + $30K target fee beats the 75% price
    assert dp.asking_price(lead) == 300_000
    b = buyer(db, "F1", "MAPLE CAPITAL LLC")
    assert dp.find_buyer(db, str(b.id)) is b and dp.find_buyer(db, "maple") is b and dp.find_buyer(db, "nobody") is None
