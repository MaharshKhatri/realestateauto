"""Day-1 pre-flight rules: comp property-type matching, new construction, small-set $/sqft outliers, the thin-listing
fallback, the conservative FRFPA fair market value and the far-below-ask cut for listed foreclosures."""
from __future__ import annotations

from datetime import date

from engine.comps import Comp, SubjectKind, ValueResult, comp_kind, kind_mismatch, ppsf_outliers
from engine.underwriter import cross_check, decide_offer, frfpa_fmv
from engine.valuation_guard import clean_stored_comps
from engine.comps import CompHygiene
from tests.factories import make_lead

TODAY = date(2026, 10, 29)


def test_subject_and_comp_kinds():
    house = SubjectKind.of(building_desc="3S-F+B-D", units=1, year_built=1890)
    condo = SubjectKind.of(building_desc="3S-B-D-6U-CONDO", qual="C0003", year_built=1930)
    by_qual = SubjectKind.of(building_desc="MODEL A", qual="C0012")
    by_mls = SubjectKind.of(building_desc="MODEL A", mls_type="condos")
    th = SubjectKind.of(building_desc="2S TH 2BR")
    assert not house.attached and condo.condo and by_qual.condo and by_mls.condo and th.townhouse
    assert comp_kind("4S-B-25U-CONDO") == (True, False) and comp_kind("5S-14U-11P-COND") == (True, False)
    assert comp_kind("townhomes") == (False, True) and comp_kind("single_family") == (False, False)
    assert comp_kind("2SF2G", qual="C0001") == (True, False)


def test_kind_mismatch_rules():
    house = SubjectKind.of(building_desc="3S-F+B-D", units=1, year_built=1890)
    assert kind_mismatch(house, "4S-B-25U-CONDO", 1930, today=TODAY) == "property type"
    assert kind_mismatch(house, "townhomes", 1988, today=TODAY) == "property type"
    assert kind_mismatch(house, "3S-B+F-2U-G2", 2023, today=TODAY) == "new construction"
    assert kind_mismatch(house, "2S-F-D-2U-H", 1909, today=TODAY) is None
    condo = SubjectKind.of(building_desc="3S-B-6U-CONDO", year_built=1930)
    assert kind_mismatch(condo, "single_family", 1955, today=TODAY) == "property type"
    assert kind_mismatch(condo, "3S-B-6U-H-CONDO", 1930, today=TODAY) is None
    newish = SubjectKind.of(building_desc="2SF", year_built=2010)
    assert kind_mismatch(newish, "2SF", 2024, today=TODAY) is None          # newer subject: new builds are fair game
    assert kind_mismatch(None, "condos", 2025) is None


def test_small_set_ppsf_outlier_band():
    lo, hi = ppsf_outliers([349, 378, 406, 789])
    assert 789 > hi and 349 >= lo
    assert ppsf_outliers([300, 320]) == (0.0, float("inf"))


def test_clean_stored_comps_drops_mismatched_types_far_and_luxury():
    subject = SubjectKind.of(building_desc="2S-F-O-AG-1U", mls_type="townhomes", year_built=1983)
    comps = [
        {"address": "53 Shore Dr", "sold_price": 555000, "sold_date": "2026-09-23", "property_type": "townhomes",
         "price_per_sqft": 349.0, "sqft": 1590, "distance_mi": 0.28, "source": "homeharvest_listing_mls"},
        {"address": "15 Bayview Ct", "sold_price": 545000, "sold_date": "2026-06-30", "property_type": "townhomes",
         "price_per_sqft": 378.0, "sqft": 1441, "distance_mi": 0.32, "source": "homeharvest_listing_mls"},
        {"address": "4 Bayview Ct", "sold_price": 585000, "sold_date": "2026-07-02", "property_type": "townhomes",
         "price_per_sqft": 406.0, "sqft": 1441, "distance_mi": 0.33, "source": "homeharvest_listing_mls"},
        {"address": "1 Jackson Ct", "sold_price": 1290000, "sold_date": "2026-07-15", "property_type": "single_family",
         "price_per_sqft": 789.0, "sqft": 1635, "distance_mi": 0.43, "source": "homeharvest_listing_mls"},
        {"address": "9 Far Rd", "sold_price": 560000, "sold_date": "2026-07-15", "property_type": "townhomes",
         "price_per_sqft": 380.0, "sqft": 1470, "distance_mi": 3.6, "source": "homeharvest_listing_mls"},
    ]
    kept, dropped = clean_stored_comps(comps, CompHygiene(), subject=subject, radius=0.5)
    assert [c["address"] for c in kept] == ["53 Shore Dr", "15 Bayview Ct", "4 Bayview Ct"]
    assert dropped["property type"] == 1 and dropped["too far"] == 1


def test_thin_listing_far_from_records_uses_the_records():
    listing = ValueResult(arv=929000, as_is=546000, method="listing_price_per_sqft",
                          comps=[Comp("a", None, 1, None, None, "homeharvest_listing_mls")] * 4, dispersion=1.3)
    public = ValueResult(arv=561000, as_is=517000, method="public_record_equalized_ratio")
    out = cross_check(listing, public)
    assert out.method == "public_record_over_thin_listing" and out.arv == 561000
    near = cross_check(ValueResult(arv=600000, as_is=500000, method="listing_price_per_sqft",
                                   comps=[Comp("a", None, 1, None, None, "x")] * 4),
                       ValueResult(arv=561000, as_is=517000, method="public_record_equalized_ratio"))
    assert near.method == "blended_listing_public" and 561000 < near.arv < 600000


def test_frfpa_fmv_takes_the_highest_credible_number(db):
    lead = make_lead(db, sheriff_days=6, upset=515000, verdict=None, stale_listing=True, list_price=450000)
    fmv, basis = frfpa_fmv(lead.property, 276000, 310000)
    assert fmv == 382500 and basis == "85% of the ask"
    om, decision, fields = decide_offer(lead.property, arv=473000, repairs=81000, as_is=276000, debt=515000,
                                        cfg={"pricing_model": "quant", "anchor_discount_pct": 0.12,
                                             "high_value_subject": 1200000}, equalized=310000)
    assert decision.applies and om.frfpa_floor == round(0.82 * 382500, -2) == 313600
    assert fields["frfpa_fmv"] == 382500 and fields["frfpa_fmv_basis"] == "85% of the ask"
    assert any("never rests on our lowest number" in f for f in om.flags)
    unlisted = make_lead(db, sheriff_days=6, upset=100000, verdict=None)
    assert frfpa_fmv(unlisted.property, 300000, None) == (300000, "as-is estimate")


def test_listed_short_sale_far_below_ask_is_no_offer(db):
    from engine import fast_track as ft
    lead = make_lead(db, sheriff_days=6, upset=515000, verdict="SHORT_SALE_ONLY", stale_listing=True, list_price=450000,
                     absentee=True)
    v = lead.property.current_valuation
    v.mao_aggressive, v.mao_conservative, v.anchor_offer = 253400, 240400, 226300
    db.flush()
    lf = ft.listed_foreclosure(db, lead)
    assert lf.track == "NO_OFFER" and "only 56%" in lf.reasons[0] and lf.anchor is None


def test_attached_clusters_and_tall_buildings():
    from engine.comps import attached_addresses
    comps = [{"address": "300 PARK ST.", "sold_date": "2024-01-02", "assessed_total": 210000},
             {"address": "300 PARK ST.", "sold_date": "2024-10-04", "assessed_total": 215000},
             {"address": "1414 WATERFORD DR", "sold_date": "2024-04-01", "assessed_total": 96300},
             {"address": "1137 WATERFORD DR", "sold_date": "2024-08-14", "assessed_total": 96300},
             {"address": "1635 WATERFORD DR", "sold_date": "2024-06-13", "assessed_total": 96300},
             {"address": "67 RIEDER RD", "sold_date": "2024-05-02", "assessed_total": 164800}]
    cluster = attached_addresses(comps)
    assert "300 PARK ST" in cluster and "1414 WATERFORD DR" in cluster and "67 RIEDER RD" not in cluster
    house = SubjectKind.of(building_desc="2SFG2", year_built=1926)
    assert kind_mismatch(house, "6SCB", 1965, today=TODAY) == "property type"
    assert kind_mismatch(house, "", 1995, today=TODAY, attached=True) == "property type"
    assert SubjectKind.of(building_desc="6SCB").condo
