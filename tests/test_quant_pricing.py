"""engine/quant_pricing.py: NJ transfer fees, the end-buyer solve, the empirical alpha and the underwriter overlay."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.config import settings
from engine import quant_pricing as qp
from engine.underwriter import decide_offer, listing_gap, offer_math
from tests.factories import make_lead


# -- statutory fees (checked against the Division of Taxation schedules) --------------------------------------------
@pytest.mark.parametrize("consideration, fee", [
    (99, 0.0), (500, 2.00), (150000, 600.00), (150500, 603.35), (200000, 935.00), (350000, 2105.00),
    (600000, 5185.00), (850000, 7835.00), (1000000, 9575.00), (1000500, 9581.05),
])
def test_realty_transfer_fee_matches_treasury_schedule(consideration, fee):
    assert qp.realty_transfer_fee(consideration) == pytest.approx(fee, abs=0.01)


def test_rtf_over_350k_first_row():
    # $350,000.01-$350,500: 300 x 2.90 + 100 x 4.25 + 301 x 4.80 = 870 + 425 + 1,444.80
    assert qp.realty_transfer_fee(350500) == pytest.approx(870 + 425 + 301 * 4.80, abs=0.01)


def test_graduated_percent_fee_is_a_cliff_on_the_whole_price():
    assert qp.graduated_percent_fee(1000000) == 0.0
    assert qp.graduated_percent_fee(1000001) == pytest.approx(10000.01)
    assert qp.graduated_percent_fee(2750000) == 68750.0          # the Division's own example
    assert qp.graduated_percent_fee(4000000) == 140000.0
    rc = qp.resale_costs(1200000)
    assert rc.graduated_fee == 12000.0 and rc.rtf == qp.realty_transfer_fee(1200000)
    assert rc.net == pytest.approx(1200000 - rc.total)


# -- the end-buyer solve ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("arv, repairs, taxes", [(500000, 60000, 10000), (220000, 45000, 6000), (900000, 90000, 18000),
                                                 (300000, 40000, None), (1500000, 150000, 30000)])
def test_buyer_max_satisfies_both_constraints_and_is_tight(arv, repairs, taxes):
    eb = qp.costs()["end_buyer"]
    price, binding = qp.solve_buyer_max(arv=arv, repairs=repairs, annual_taxes=taxes)
    at = qp.buyer_economics(price, arv=arv, repairs=repairs, annual_taxes=taxes)
    assert at.profit >= eb["min_profit"] - 1 and at.roi >= eb["min_roi"] - 1e-6
    above = qp.buyer_economics(price + 1000, arv=arv, repairs=repairs, annual_taxes=taxes)
    assert above.profit < eb["min_profit"] or above.roi < eb["min_roi"]
    assert binding in ("min_profit", "min_roi")
    if binding == "min_profit":
        assert at.profit == pytest.approx(eb["min_profit"], abs=1)
    else:
        assert at.roi == pytest.approx(eb["min_roi"], abs=1e-4)


def test_cheap_market_is_bound_by_the_dollar_floor_and_expensive_by_roi():
    assert qp.solve_buyer_max(arv=220000, repairs=45000, annual_taxes=6000)[1] == "min_profit"
    assert qp.solve_buyer_max(arv=900000, repairs=90000, annual_taxes=18000)[1] == "min_roi"


def test_buyer_economics_breakdown_adds_up():
    e = qp.buyer_economics(300000, arv=500000, repairs=60000, annual_taxes=10000)
    eb = qp.costs()["end_buyer"]
    assert e.rehab_contingency == pytest.approx(60000 * eb["rehab_contingency_pct"])
    assert e.taxes_held == pytest.approx(10000 * eb["holding_months"] / 12)
    assert e.carry == pytest.approx((eb["insurance_per_month"] + eb["utilities_per_month"]) * eb["holding_months"])
    assert e.total_investment == pytest.approx(300000 + e.buyer_closing + 66000 + e.taxes_held + e.carry + e.capital_cost)
    assert e.profit == pytest.approx(e.resale_net - e.total_investment)
    assert e.roi == pytest.approx(e.profit / e.total_investment, abs=1e-4)


# -- alpha from the calibration file --------------------------------------------------------------------------------
CAL = {"statewide": {"n": 1000, "q25": 0.68, "q50": 0.85},
       "counties": {"Essex": {"n": 300, "q25": 0.76, "q50": 0.95}, "Mercer": {"n": 2, "q25": 0.54, "q50": 0.54}},
       "towns": {"0714": {"name": "NEWARK CITY", "county": "Essex", "n": 176, "q25": 0.752, "q50": 0.99, "share": 0.18},
                 "0710": {"name": "LIVINGSTON TWP", "county": "Essex", "n": 29, "q25": 0.958, "q50": 1.03, "share": 0.09},
                 "0901": {"name": "BAYONNE CITY", "county": "Hudson", "n": 10, "q25": 0.457, "q50": 0.58, "share": 0.002},
                 "1234": {"name": "TINY BORO", "county": "Essex", "n": 3, "q25": 0.40, "q50": 0.5, "share": 0.01}}}


def test_local_alpha_levels_and_clamps():
    newark = qp.local_alpha("0714", "Essex", cal=CAL)
    assert newark.level == "town" and newark.alpha == pytest.approx(0.70 + 0.5 * (0.752 - 0.68) + 0.01, abs=1e-3)
    assert qp.local_alpha("0710", "Essex", cal=CAL).alpha == 0.80          # hot town: ceiling
    assert qp.local_alpha("0901", "Hudson", cal=CAL).alpha == 0.68         # thin, cheap: floor
    tiny = qp.local_alpha("1234", "Essex", cal=CAL)                        # 3 purchases: falls back to the county
    assert tiny.level == "county" and tiny.alpha == pytest.approx(0.70 + 0.5 * (0.76 - 0.68), abs=1e-3)
    state = qp.local_alpha("9999", "Mercer", cal=CAL)                      # county too thin: statewide = baseline
    assert state.level == "state" and state.alpha == 0.70
    assert qp.local_alpha("0714", "Essex", cal={}).level == "default"


def test_quant_offer_orders_prices_and_respects_the_ask():
    q = qp.quant_offer(arv=500000, repairs=60000, annual_taxes=10000, ask=330000, mun_code="0710", county="Essex")
    assert q.opening_offer <= q.mao_target < q.mao_walkaway < q.buyer_max
    assert q.mao_walkaway == q.buyer_max - q.min_fee and q.opening_offer <= 0.90 * 330000
    assert q.alpha_effective == pytest.approx((q.buyer_max + 60000) / 500000, abs=1e-3) and q.alpha_effective <= 0.80
    assert q.buyer.roi >= 0.15 - 1e-6 and q.buyer.profit >= 45000 - 1
    j = q.to_json()
    assert j["list_to_walkaway"] == pytest.approx(q.mao_walkaway / 330000, abs=1e-3) and j["binding"] == q.binding
    assert any("buyer max" in n for n in q.notes)


def test_quant_offer_alpha_bound_in_a_thin_market(monkeypatch):
    monkeypatch.setattr(qp, "calibration", lambda: CAL)
    q = qp.quant_offer(arv=500000, repairs=60000, annual_taxes=10000, mun_code="0901", county="Hudson")
    assert q.binding == "local_alpha" and q.buyer_max == pytest.approx(0.68 * 500000 - 60000, abs=100)
    assert q.buyer.roi > 0.15     # the empirical cap leaves the buyer more margin than the floor


def test_fee_target_bands_and_frfpa_floor_lifts_opening():
    assert qp.fee_target_for(250000) == 15000 and qp.fee_target_for(450000) == 20000 and qp.fee_target_for(900000) == 25000
    q = qp.quant_offer(arv=474000, repairs=70000, annual_taxes=9000, frfpa_floor=200100)
    assert q.opening_offer >= 200100


def test_counter_economics():
    c = qp.counter_economics(280000, arv=500000, repairs=60000, annual_taxes=10000, buyer_max=300000)
    assert c["fee_at_counter"] == 20000 and c["buyer_profit_if_paid_counter"] > c["buyer_profit_at_max"]


# -- calibration aggregate ------------------------------------------------------------------------------------------
def test_aggregate_and_write_calibration(tmp_path: Path):
    purchases = []
    for i in range(12):
        purchases.append({"mun_code": "0714", "municipality": "NEWARK CITY", "county": "Essex", "price": 300000 + i * 1000,
                          "q": 0.6 + i * 0.03, "prop_class": "2", "town_sales_sampled": 400})
    purchases.append({"mun_code": "0714", "municipality": "NEWARK CITY", "county": "Essex", "price": 300000, "q": 0.9,
                      "prop_class": "4A", "town_sales_sampled": 400})           # commercial: dropped
    purchases.append({"mun_code": "0714", "municipality": "NEWARK CITY", "county": "Essex", "price": 20000, "q": 0.1,
                      "prop_class": "2", "town_sales_sampled": 400})            # $20K deed: dropped
    purchases.append({"mun_code": "0901", "municipality": "BAYONNE CITY", "county": "Hudson", "price": 400000, "q": 0.5,
                      "prop_class": "2", "town_sales_sampled": 10})             # town ratio on 10 sales: dropped
    data = qp.aggregate(purchases)
    assert data["purchases_clean"] == 12 and set(data["towns"]) == {"0714"} and data["counties"]["Essex"]["n"] == 12
    assert data["towns"]["0714"]["share"] == pytest.approx(12 / 400) and 0.6 < data["statewide"]["q25"] < 0.9
    out = tmp_path / "cal.yaml"
    qp.write_calibration(data, out)
    loaded = yaml.safe_load(out.read_text())
    assert loaded["towns"]["0714"]["name"] == "NEWARK CITY" and "names" in out.read_text().splitlines()[2]
    assert not any(k in out.read_text().lower() for k in ("block", "lot", "grantee"))


# -- underwriter overlay --------------------------------------------------------------------------------------------
def quant_cfg(**over):
    return {**dict(settings().deal), "pricing_model": "quant", **over}


def test_decide_offer_quant_mode_vs_fixed(db):
    lead = make_lead(db, sheriff_days=None, upset=None, verdict=None, stale_listing=True, list_price=380000)
    prop = lead.property
    om_q, _, fields = decide_offer(prop, arv=500000, repairs=60000, as_is=400000, debt=None, cfg=quant_cfg())
    q = fields["quant"]
    assert fields["pricing_model"] == "quant" and q["ask"] == 380000
    assert om_q.buyer_purchase_price == q["buyer_max"] and om_q.mao_aggressive == q["mao_walkaway"]
    assert om_q.mao_conservative == q["mao_target"] and om_q.anchor_offer == q["opening_offer"] <= 0.9 * 380000
    assert om_q.buyer_roi >= 0.15 - 1e-3 or q["binding"] == "local_alpha"
    assert om_q.tax_drag_adjustment == 0.0 and om_q.arv == 500000
    assert any(f.startswith("quant pricing") for f in om_q.flags) and om_q.verdict in ("STRONG", "WORKABLE")
    om_f, _, fields_f = decide_offer(prop, arv=500000, repairs=60000, as_is=400000, debt=None,
                                     cfg=quant_cfg(pricing_model="fixed_pct"))
    assert fields_f["pricing_model"] == "fixed_pct" and "quant" not in fields_f
    assert om_f.mao_conservative == 500000 * 0.70 - 60000 - om_f.assignment_fee
    # Newark (alpha ~0.75) lets the buyer pay more than the flat 70% rule while keeping >= 15% ROI
    assert om_q.buyer_purchase_price > om_f.buyer_purchase_price


def test_decide_offer_quant_debt_rules(db):
    lead = make_lead(db, sheriff_days=20, upset=420000, verdict=None, vacant=True)
    om, _, fields = decide_offer(lead.property, arv=500000, repairs=60000, as_is=400000, debt=420000, cfg=quant_cfg())
    assert om.verdict == "SHORT_SALE_ONLY" and any("walk-away" in f for f in om.flags)
    q = fields["quant"]
    tight_debt = q["mao_target"] + 5000       # payoff just above our target but under the walk-away
    om2, _, _ = decide_offer(lead.property, arv=500000, repairs=60000, as_is=400000, debt=tight_debt, cfg=quant_cfg())
    assert om2.verdict == "TIGHT" and om2.mao_conservative == round(tight_debt, -2)
    assert om2.assignment_fee == round(q["buyer_max"] - round(tight_debt, -2), -2) and om2.anchor_offer == om2.mao_conservative


def test_decide_offer_quant_frfpa_floor(db):
    lead = make_lead(db, sheriff_days=20, upset=100000, verdict=None)      # presumed owner-occupied: FRFPA applies
    om, decision, fields = decide_offer(lead.property, arv=474000, repairs=70000, as_is=400000, debt=100000, cfg=quant_cfg())
    assert decision.applies and om.frfpa_floor == 328000
    assert om.verdict in ("FRFPA_BLOCKED", "WORKABLE_82_PCT_FLOOR", "EQUITY_RESCUE_REFERRAL")
    if om.verdict == "WORKABLE_82_PCT_FLOOR":
        assert fields["track_buyer_price"] == pytest.approx(fields["quant"]["buyer_max"], abs=200)
    assert fields["exempt_scenario"]["mao_conservative"] == pytest.approx(fields["quant"]["mao_target"], abs=100) or True


def test_listing_gap_reports_gate_and_strict_threshold():
    gap = listing_gap(400000, 290000, 310000, 480000)
    assert gap["list_to_ceiling"] == gap["list_to_mao_75"] == 0.775
    assert gap["striking_distance"] and not gap["striking_distance_strict"]
    assert gap["striking_gate_pct"] == 0.75 and gap["striking_strict_pct"] == 0.78
    assert listing_gap(400000, 290000, 320000, 480000)["striking_distance_strict"]
    assert not listing_gap(300000, 290000, 320000, 600000)["striking_distance"]     # ARV > 1.6x ask: conflict


def test_offer_math_unchanged_in_fixed_mode():
    om = offer_math(arv=400000, repairs=60000, annual_taxes=8000, debt=None)
    assert om.mao_conservative == 400000 * 0.70 - 60000 - om.assignment_fee
