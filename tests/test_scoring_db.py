from datetime import date, timedelta

from db.models import SignalType, Valuation
from db.repository import ensure_lead, upsert_property, upsert_signal
from engine.scoring import active_weights, score_property
from engine.underwriter import frfpa_applies


def _prop(db, **kw):
    data = {"parcel_id": "ESSEX-0714-1-1", "county": "Essex", "municipality": "NEWARK CITY", "mun_code": "0714",
            "street_address": "1 TEST ST", "property_class": "2", "dwellings": 1, "year_built": 1925,
            "assessed_total": 200000, "last_sale_date": date(1995, 1, 1), "last_sale_price": 90000,
            "is_absentee": False, "estimated_value": 450000}
    data.update(kw)
    prop, _ = upsert_property(db, data, source="test")
    return prop


def test_upserts_are_idempotent(db):
    p1 = _prop(db)
    p2 = _prop(db, year_built=1930)
    assert p1.id == p2.id and p2.year_built == 1930
    s1, created1 = upsert_signal(db, p1, signal_type=SignalType.SHERIFF_SALE, source="civilview", source_ref="Essex:F-1")
    s2, created2 = upsert_signal(db, p1, signal_type=SignalType.SHERIFF_SALE, source="civilview", source_ref="Essex:F-1")
    assert created1 and not created2 and s1.id == s2.id
    l1, c1 = ensure_lead(db, p1, primary_source="sheriff_sale")
    l2, c2 = ensure_lead(db, p1, primary_source="sheriff_sale")
    assert c1 and not c2 and l1.id == l2.id


def test_sheriff_sale_scores_above_plain_absentee(db):
    _, weights = active_weights(db)
    sheriff = _prop(db)
    upsert_signal(db, sheriff, signal_type=SignalType.SHERIFF_SALE, source="civilview", source_ref="Essex:F-1",
                  scheduled_auction_date=date.today() + timedelta(days=20), amount_owed=150000,
                  raw_metadata_json={"adjournments_total": 1, "defendant_adjournments": 1})
    db.refresh(sheriff)
    absentee = _prop(db, parcel_id="ESSEX-0714-1-2", is_absentee=True, last_sale_date=date(2020, 1, 1))
    s1 = score_property(sheriff, weights, "sheriff_sale")
    s2 = score_property(absentee, weights, "absentee_owner")
    assert s1.priority > s2.priority
    assert any("Sheriff sale" in r for r in s1.reasons)
    assert s1.factors["sheriff_sale_scheduled"] == weights["motivation"]["sheriff_sale_scheduled"]
    assert frfpa_applies(sheriff) and not frfpa_applies(absentee)


def test_valuation_drives_viability(db):
    _, weights = active_weights(db)
    prop = _prop(db)
    weak = score_property(prop, weights).viability
    db.add(Valuation(property=prop, arv_estimate=450000, mao_conservative=220000, verdict="STRONG",
                     confidence_score=80, deal_spread=90000, is_current=True))
    db.flush()
    db.refresh(prop)
    assert score_property(prop, weights).viability > weak
