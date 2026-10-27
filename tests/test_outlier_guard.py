"""Valuation outlier guard: comp hygiene (package deeds, price/$-sqft/ratio bands), subject guard, re-guard agent."""
from types import SimpleNamespace

import engine.comps as comps_mod
from db.models import Valuation
from engine.comps import CompHygiene, PublicRecordComps, explicit_sqft, package_deed_keys
from engine.scoring import ready_to_call, valuation_suspect
from engine.underwriter import guarded_confidence, outlier_guard
from engine.valuation_guard import OutlierGuardAgent, clean_stored_comps, reguard, stored_equalized
from tests.factories import add_contact, make_lead


def sale(pin, price, assessed=300_000, date="250601", desc="2S-F", dwell=1):
    return {"attributes": {"PAMS_PIN": pin, "SALE_PRICE": price, "NET_VALUE": assessed, "DEED_DATE": date,
                           "PCL_MUN": "1219", "MUN_NAME": "TEST TWP", "PROP_LOC": f"{pin} TEST ST",
                           "BLDG_DESC": desc, "DWELL": dwell}, "centroid": (-74.40, 40.57)}


def test_hygiene_bands_and_explicit_sqft():
    h = CompHygiene()
    assert not h.price_ok(30_000) and h.price_ok(400_000) and not h.price_ok(3_000_000)
    assert h.price_ok(3_000_000, subject_value=1_500_000)          # high-value subject lifts the cap
    assert h.ppsf_ok(500_000, 1_500) and not h.ppsf_ok(4_725_000, 1_884) and not h.ppsf_ok(50_000, 1_500)
    assert h.ppsf_ok(500_000, None)                                  # unknown living area is not a reason to drop
    assert h.ratio_ok(1.2) and not h.ratio_ok(2.6) and not h.ratio_ok(0.3)
    assert explicit_sqft("1,884 SF.") == 1884 and explicit_sqft("2775 SQ FT 3BR") == 2775
    assert explicit_sqft("2S-F-2G") is None and explicit_sqft("12 SF") is None


def test_package_deed_detection():
    rows = [sale(f"C{i}", 4_725_000, 60_000) for i in range(6)] + [sale("H1", 450_000), sale("H2", 455_000)]
    assert package_deed_keys(rows) == {(4_725_000, "250601")}
    same_parcel_twice = [sale("H1", 450_000), sale("H1", 450_000)]
    assert package_deed_keys(same_parcel_twice) == set()


def test_public_record_comps_drop_package_deed(monkeypatch):
    monkeypatch.setattr(comps_mod, "muni_stats", lambda gis, code, name="": SimpleNamespace(median_ratio=1.0))
    clean = [sale(f"H{i}", 400_000 + i * 10_000) for i in range(8)]
    package = [sale(f"C{i}", 4_725_000, 60_000) for i in range(6)]
    gis = SimpleNamespace(nearby_sales=lambda lat, lon, r, months: clean + package)
    r = PublicRecordComps(gis).value(lat=40.57, lon=-74.40, assessed_total=300_000, mun_code="1219",
                                     municipality="TEST TWP", units=1, building_desc="2S-F", radius=0.5,
                                     fallback_radius=1.0, months=24, appreciation=0.0, min_comps=3,
                                     renovated_pct=0.7, distressed_pct=0.3)
    assert len(r.comps) == 8 and 400_000 <= r.arv <= 500_000          # without the guard: ~$12M
    assert r.subject_equalized == 300_000 and any("6 package deed" in n for n in r.notes)


def test_subject_guard_benchmarks():
    ok = outlier_guard(arv=520_000, as_is=400_000, prop_class="2", ask=450_000, equalized=380_000, sqft=1_600,
                       sqft_known=True)
    assert not ok.capped and not ok.non_residential and ok.arv == 520_000
    ask = outlier_guard(arv=3_923_000, as_is=3_000_000, prop_class="2", ask=699_000, equalized=None, sqft=None,
                        sqft_known=False)
    assert ask.capped and ask.benchmark == "asking price" and ask.arv == round(1.25 * 699_000, -3)
    assert ask.as_is == round(3_000_000 * ask.arv / 3_923_000, -3) and "OUTLIER_CAPPED" in ask.flags[0]
    eq = outlier_guard(arv=12_785_000, as_is=9_000_000, prop_class="2", ask=None, equalized=480_000, sqft=None,
                       sqft_known=False)
    assert eq.capped and eq.benchmark == "equalized assessment" and eq.arv == 600_000
    sqft = outlier_guard(arv=2_400_000, as_is=None, prop_class="2", ask=None, equalized=900_000, sqft=1_884,
                         sqft_known=True)
    assert sqft.capped and sqft.benchmark == "$/sqft ceiling" and sqft.arv == round(850 * 1_884, -3)
    tower = outlier_guard(arv=215_000_000, as_is=None, prop_class="4A", ask=None, equalized=90_000_000, sqft=None,
                          sqft_known=False)
    assert tower.non_residential and tower.arv == 90_000_000 and "NON_RESIDENTIAL" in " ".join(tower.flags)
    assert guarded_confidence(80, ask) == 40                         # capped: halved, at most 40
    assert guarded_confidence(80, tower) == 10                       # non-residential (20) and capped


def test_reguard_cleans_stored_comps_idempotently(db):
    lead = make_lead(db, sheriff_days=None, absentee=True)
    add_contact(db, lead, phone="9735550101")
    v = lead.property.current_valuation
    base = [{"address": f"{i} Comp St", "sold_price": 400_000 + i * 5_000, "sold_date": "2026-05-01",
             "normalized_ratio": 1.0 + i * 0.05} for i in range(8)]
    package = [{"address": f"{i} Tower Rd", "sold_price": 4_725_000, "sold_date": "2026-04-01",
                "normalized_ratio": 30.0} for i in range(3)]
    v.comps_json, v.comp_count = base + package, 11
    v.comp_source = "public_record_equalized_ratio"
    v.arv_estimate = 9_000_000
    v.assumptions_json = {**v.assumptions_json, "arv_before_tax_drag": 9_000_000, "subject_equalized": 400_000}
    db.flush()
    assert stored_equalized(v) == 400_000
    kept, dropped = clean_stored_comps(v.comps_json, CompHygiene())
    assert len(kept) == 8 and dropped["package deed"] == 3
    assert reguard(db, lead.property) == "repriced"
    db.refresh(lead.property)
    new = lead.property.current_valuation
    assert new.id != v.id and new.arv_estimate < 700_000 and new.comp_count == 8
    assert any("3 package deed" in n for n in new.assumptions_json["comp_notes"])
    assert reguard(db, lead.property) == "unchanged"


def test_capped_valuation_is_suspect_and_not_ready(db):
    lead = make_lead(db, sheriff_days=None, stale_listing=True, list_price=360_000)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100")
    assert ready_to_call(lead)[0]
    v = lead.property.current_valuation
    v.assumptions_json = {**v.assumptions_json, "outlier_guard": {"capped": True, "original_arv": 2_000_000}}
    assert valuation_suspect(v) and ready_to_call(lead) == (False, None)
    assert not valuation_suspect(Valuation(assumptions_json={}))


def test_guard_agent_runs_offline(db):
    lead = make_lead(db, property_class="4A")
    db.commit()
    run = OutlierGuardAgent(offline=True).execute()
    assert run.status == "success" and run.records_processed == 1
    db.expire_all()
    cur = lead.property.current_valuation
    assert cur.verdict == "PASS" and cur.assumptions_json["outlier_guard"]["non_residential"]
    assert OutlierGuardAgent(offline=True).execute().metrics_json.get("unchanged") == 1
