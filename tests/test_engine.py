from datetime import date

import pytest

from engine.comps import Comp, iqr_filter, time_adjust, value_from_metric
from engine.equity import estimate_equity, estimate_mortgage_balance, likely_free_and_clear, remaining_balance
from engine.rehab import choose_tier, tier_cost
from engine.underwriter import assignment_fee_for, offer_math, tax_drag

CFG = {"mao_pct_conservative": 0.70, "mao_pct_aggressive": 0.75, "anchor_discount_pct": 0.12,
       "target_assignment_fee_min": 15000, "target_assignment_fee_max": 35000, "min_assignment_fee": 10000,
       "assignment_fee_pct_of_spread": 0.40, "high_tax_drag_threshold": 0.025, "tax_drag_arv_haircut_per_pct": 0.02}


def test_mao_formulas_and_anchor():
    om = offer_math(arv=400000, repairs=60000, annual_taxes=8000, debt=None, cfg=CFG)
    fee = 24000  # 6% of ARV, inside the $15K-$35K band
    assert om.assignment_fee == fee
    assert om.mao_conservative == 400000 * 0.70 - 60000 - fee
    assert om.mao_aggressive == 400000 * 0.75 - 60000 - fee
    assert om.anchor_offer == round(om.mao_conservative * 0.88, -2)
    assert om.buyer_purchase_price == 400000 * 0.70 - 60000


def test_fee_is_clamped_and_cut_on_thin_spread():
    assert assignment_fee_for(100000, 50000, None, CFG)[0] == 15000
    assert assignment_fee_for(900000, 500000, None, CFG)[0] == 35000
    fee, flags = assignment_fee_for(400000, 220000, 205000, CFG)
    assert fee == 10000 and flags


def test_short_sale_verdict_when_debt_exceeds_mao():
    om = offer_math(arv=400000, repairs=60000, annual_taxes=None, debt=350000, cfg=CFG)
    assert om.verdict == "SHORT_SALE_ONLY" and om.deal_spread < 0


def test_nj_tax_drag_haircut():
    arv, eff, haircut = tax_drag(300000, 12000, CFG)        # 4% effective -> 1.5 pts over -> 3% haircut
    assert eff == 0.04 and haircut == pytest.approx(0.03) and arv == 291000
    assert tax_drag(300000, 6000, CFG)[2] == 0.0


def test_frfpa_floor_raises_anchor_or_blocks():
    ok = offer_math(arv=474000, repairs=70000, annual_taxes=None, debt=None, cfg=CFG, frfpa=True, fmv=244000)
    assert ok.frfpa_floor == 200100 and ok.anchor_offer >= ok.frfpa_floor and ok.verdict != "FRFPA_BLOCKED"
    blocked = offer_math(arv=474000, repairs=70000, annual_taxes=None, debt=None, cfg=CFG, frfpa=True, fmv=400000)
    assert blocked.verdict == "FRFPA_BLOCKED"


def test_rehab_tiers():
    assert tier_cost("LIGHT", 1000, 2000, {"light_per_sqft": 25, "medium_per_sqft": 50, "heavy_per_sqft": 85,
                                           "heavy_roof_hvac_buffer": 15000, "age_bumps": []}) == 25000
    cfg = {"light_per_sqft": 25, "medium_per_sqft": 50, "heavy_per_sqft": 85, "heavy_roof_hvac_buffer": 15000,
           "age_bumps": [{"before": 1940, "add_per_sqft": 10}]}
    assert tier_cost("HEAVY", 1000, 1920, cfg) == (85 + 10) * 1000 + 15000
    assert choose_tier(year_built=1920, vacant=True)[0] == "HEAVY"
    assert choose_tier(year_built=1995)[0] == "LIGHT"


def test_comp_percentile_split():
    comps = [Comp(address=str(i), municipality="X", sold_price=1, sold_date=None, distance_mi=0.1,
                  source="t", price_per_sqft=v) for i, v in enumerate([150, 200, 250, 300, 350, 400, 450])]
    arv, as_is, n_r, n_d, disp = value_from_metric(comps, "price_per_sqft", 1000, 0.70, 0.30)
    assert arv > as_is
    assert {c.bucket for c in comps} == {"renovated", "middle", "distressed"}
    lo, hi = iqr_filter([1, 2, 3, 4, 100])
    assert hi < 100
    assert time_adjust(100000, date(2024, 10, 26), 0.05, date(2026, 10, 26)) == pytest.approx(110250, rel=1e-3)


def test_equity_model():
    assert remaining_balance(100000, 6, 30, 360) == 0.0
    assert estimate_mortgage_balance(23900, date(1968, 8, 21)) == 0.0
    assert likely_free_and_clear(date(1968, 8, 21))
    bal = estimate_mortgage_balance(500000, date(2024, 1, 1), today=date(2026, 1, 1))
    assert 380000 < bal < 400000
    assert estimate_equity(600000, 500000, date(2024, 1, 1), known_debt=450000) == 150000
