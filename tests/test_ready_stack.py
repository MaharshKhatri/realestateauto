"""Listing gap, READY_TO_CALL, stacked-distress scoring and the briefing sections."""
from engine import briefing
from engine.scoring import apply_score, default_weights, ready_to_call, signal_families, stack_badge
from engine.underwriter import LISTING_ARV_CONFLICT, listing_gap
from tests.factories import add_contact, make_lead


def test_listing_gap_ratios_and_conflict():
    g = listing_gap(400_000, 280_000, 310_000, arv=520_000)
    assert g["list_to_mao_70"] == 0.7 and g["list_to_mao_75"] == 0.775 and g["discount_off_ask_70"] == 0.3
    assert g["striking_distance"] and not g["valuation_conflict"]
    far = listing_gap(400_000, 200_000, 240_000, arv=450_000)
    assert not far["striking_distance"]                     # 60% of ask: too far for a first call
    wild = listing_gap(200_000, 600_000, 650_000, arv=200_000 * (LISTING_ARV_CONFLICT + 0.5))
    assert wild["valuation_conflict"] and not wild["striking_distance"]   # comps are wrong, not the seller
    assert listing_gap(None, 1, 2) == {} and listing_gap(0, 1, 2) == {}


def test_ready_to_call_listing_needs_striking_distance_and_agent(db):
    close = make_lead(db, sheriff_days=None, stale_listing=True, list_price=360_000)   # mao75 285K = 79% of ask
    far = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450_000)     # 63% of ask
    for lead in (close, far):
        add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100")
    assert ready_to_call(close) == (True, "listing agent phone")
    assert ready_to_call(far) == (False, None)              # STRONG on paper, but the ask is out of reach

    email_only = make_lead(db, sheriff_days=None, stale_listing=True, list_price=360_000)
    add_contact(db, email_only, contact_type="LISTING_AGENT", name="Ed Mail", phone=None, email="ed@broker.test")
    assert ready_to_call(email_only) == (True, "listing agent e-mail")


def test_ready_to_call_skips_dnc_and_no_offer_verdicts(db):
    sheriff = make_lead(db)
    c = add_contact(db, sheriff, phone="9735550111")
    assert ready_to_call(sheriff)[0]
    c.dnc_status = "dnc"
    assert ready_to_call(sheriff) == (False, None)
    blocked = make_lead(db, verdict="FRFPA_BLOCKED")
    add_contact(db, blocked, phone="9735550112")
    assert ready_to_call(blocked) == (False, None)
    nothing = make_lead(db, verdict=None)
    add_contact(db, nothing, phone="9735550113")
    assert ready_to_call(nothing) == (False, None)


def test_stack_families_badge_and_motivation_bonus(db):
    single = make_lead(db)
    double = make_lead(db, vacant=True)
    triple = make_lead(db, vacant=True, stale_listing=True)
    assert signal_families(single.property) == ["foreclosure"] and stack_badge(single.property) is None
    assert stack_badge(double.property) == "[2x STACKED]"
    assert signal_families(triple.property) == ["foreclosure", "listing", "vacant"]
    w = default_weights()
    s1, s2 = apply_score(single, 1, w), apply_score(double, 1, w)
    s3 = apply_score(triple, 1, w)
    assert s2.factors.get("stack_2x") and s3.factors.get("stack_3x") and "stack_2x" not in s1.factors
    assert s2.motivation > s1.motivation
    assert double.score_breakdown_json["stack"] == ["foreclosure", "vacant"]
    assert any("2x stacked distress" in r for r in double.score_breakdown_json["reasons"])


def test_briefing_sections(db):
    ready = make_lead(db, sheriff_days=None, stale_listing=True, list_price=360_000)
    add_contact(db, ready, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100")
    stacked = make_lead(db, vacant=True)
    huge = make_lead(db, property_class="4A")                  # commercial parcel: kept out of section B
    w = default_weights()
    for lead in (ready, stacked, huge):
        apply_score(lead, 1, w)
    db.flush()
    rows = briefing.ready_rows(db)
    assert [r["lead_id"] for r in rows] == [ready.id]
    assert rows[0]["ask"] == 360_000 and rows[0]["list_to_mao_75"] == round(285_000 / 360_000, 3)
    assert rows[0]["phone"] == "9735550100" and rows[0]["via"] == "listing agent phone"
    spread_ids = [r["lead_id"] for r in briefing.high_spread_rows(db)]
    assert stacked.id in spread_ids and huge.id not in spread_ids
    stacked_rows = briefing.stacked_rows(db)
    assert stacked.id in [r["lead_id"] for r in stacked_rows]
    assert all(len(r["families"]) >= 2 for r in stacked_rows)
    counts = briefing.counts(db)
    assert counts["ready_to_call"] == 1 and counts["stacked_2x"] >= 1


def test_listing_only_parcels_use_the_mls_property_type():
    from engine.listing_comps import CONDO_TYPES, MULTI_TYPES, SINGLE_TYPES, subject_type
    assert subject_type(None, None, "condos") >= CONDO_TYPES          # unmatched condo listing → condo comps
    assert subject_type(None, None, "multi_family") == MULTI_TYPES
    assert subject_type(2, None, "condos") == MULTI_TYPES              # MOD-IV facts win over the listing
    assert subject_type(None, None, None) == SINGLE_TYPES
