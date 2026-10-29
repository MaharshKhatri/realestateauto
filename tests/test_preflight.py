"""engine/preflight.py: GO / VERIFY / HOLD on a deal card, and the deck leaves HOLD cards out."""
from __future__ import annotations

from engine import autopilot as ap
from engine import preflight
from tests.factories import add_contact, make_lead

QUANT = {"buyer_max": 300000, "mao_target": 270000, "mao_walkaway": 290000, "opening_offer": 237600,
         "fee_target": 20000, "alpha_effective": 0.72,
         "buyer": {"rehab": 60000, "rehab_contingency": 6000, "taxes_held": 4500, "carry": 2100, "capital_cost": 22000,
                   "holding_months": 6}}


def listed(db, list_price=380000, **kw):
    lead = make_lead(db, sheriff_days=None, upset=None, stale_listing=True, list_price=list_price,
                     assumptions={"quant": QUANT, "sqft_source": "record", **kw.pop("a", {})}, **kw)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test")
    return lead


def test_go_verify_and_talking_point(db):
    lead = listed(db)
    pf = preflight.check(lead)
    assert pf.status in ("GO", "VERIFY") and "4 comps" in pf.checks[0]
    assert pf.comps[0]["address"].startswith("12 Comp St") and pf.comps[0]["price"] == 510000   # top nearby sale first
    assert "$270,000" in pf.talking_point and "$60,000 of work plus a 10% cushion" in pf.talking_point
    est = listed(db, a={"sqft_source": "estimated_from_building_description"})
    assert any("living area" in x for x in preflight.check(est).issues)


def test_hold_cases(db):
    few = listed(db)
    few.property.current_valuation.comps_json = few.property.current_valuation.comps_json[:2]
    assert preflight.check(few).status == "HOLD"
    refresh = listed(db, a={"comps_need_refresh": 3})
    assert preflight.check(refresh).status == "HOLD"
    inflated = listed(db, list_price=300000)                          # ARV 500K vs a 300K ask = 1.67x
    pf = preflight.check(inflated)
    assert pf.status == "HOLD" and any("1.67x" in x for x in pf.issues)
    mixed = listed(db)
    v = mixed.property.current_valuation
    v.comps_json = v.comps_json + [{"address": "1 Tower Condo", "sold_price": 200000, "sold_date": "2026-01-01",
                                    "property_type": "5S-14U-11P-COND", "distance_mi": 0.1}]
    assert preflight.check(mixed).status == "HOLD"


def test_deck_skips_held_cards(db):
    good = listed(db)
    bad = listed(db, a={"comps_need_refresh": 5})
    d = ap.deck(db)
    ids = [c["lead_id"] for c in d["cards"]]
    assert good.id in ids and bad.id not in ids
    assert any(h["lead_id"] == bad.id for h in d["held"])
    card = next(c for c in d["cards"] if c["lead_id"] == good.id)
    assert card["preflight"]["status"] in ("GO", "VERIFY")
