from engine.frfpa_classifier import COVERED_TRACKS, EXEMPT_TRACKS, OccupancyFacts, classify, covered_track


def facts(**kw):
    base = {"in_foreclosure": True, "one_to_four": True, "owner_names": ["JOHN Q SMITH"], "defendant": "JOHN Q SMITH, ET AL"}
    base.update(kw)
    return OccupancyFacts(**base)


def test_not_in_foreclosure_is_exempt():
    d = classify(facts(in_foreclosure=False))
    assert d.track == "NOT_IN_FORECLOSURE" and not d.applies


def test_presumed_owner_occupied_is_covered():
    d = classify(facts())
    assert d.track == "OWNER_OCCUPIED" and d.applies and d.track in COVERED_TRACKS


def test_entity_owner_exempt_but_trust_is_not():
    assert classify(facts(owner_names=["MAPLE HOLDINGS LLC"])).track == "EXEMPT_ENTITY"
    trust = classify(facts(owner_names=["SMITH FAMILY REVOCABLE TRUST"]))
    assert trust.applies, "a trust can hold the beneficiary's residence — not automatically exempt"


def test_vacant_and_absentee_are_presumed_exemptions_with_verification():
    v = classify(facts(vacant_signal=True))
    assert v.track == "EXEMPT_VACANT" and not v.applies and v.confidence == "presumed" and v.verify_before_contract
    a = classify(facts(is_absentee=True))
    assert a.track == "EXEMPT_NON_OWNER_OCCUPIED" and a.confidence == "presumed"


def test_estate_needs_occupancy_unless_absentee():
    d = classify(facts(defendant="JANE DOE, EXECUTRIX OF THE ESTATE OF JOHN DOE"))
    assert d.track == "ESTATE_VERIFY_OCCUPANCY" and d.applies
    assert classify(facts(estate_signal=True, is_absentee=True)).track == "EXEMPT_ESTATE"


def test_operator_confirmation_overrides_presumptions():
    assert classify(facts(is_absentee=True, confirmed_occupancy="OWNER_OCCUPIED")).track == "CONFIRMED_OWNER_OCCUPIED"
    assert classify(facts(confirmed_occupancy="HEIR_OCCUPIED")).applies
    d = classify(facts(confirmed_occupancy="TENANT_OCCUPIED"))
    assert d.track == "EXEMPT_CONFIRMED" and d.track in EXEMPT_TRACKS and d.confidence == "confirmed"


def test_covered_track_workable_at_82_percent():
    # 82% of 400K = 328K floor; buyer at 75% of 600K ARV - 60K repairs = 390K → 62K spread, fee capped at 35K.
    ct = covered_track(arv=600000, fmv=400000, repairs=60000, debt=150000)
    assert ct.track == "WORKABLE_82_PCT_FLOOR"
    assert ct.compliant_offer >= 328000 and ct.fee <= 35000 and ct.buyer_price - ct.compliant_offer >= 10000


def test_covered_track_equity_rescue_when_owner_has_big_equity_but_no_spread():
    ct = covered_track(arv=500000, fmv=450000, repairs=80000, debt=100000)
    assert ct.track == "EQUITY_RESCUE_REFERRAL"
    assert ct.equity_at_risk > 75000
    assert any("45:15-3" in n for n in ct.notes), "unlicensed: no referral fee"


def test_covered_track_blocked_when_thin_equity():
    ct = covered_track(arv=300000, fmv=260000, repairs=60000, debt=200000)
    assert ct.track == "FRFPA_BLOCKED"
