from datetime import date

from agents.scrapers.nj_tax_assessment_agent import buyer_reliability, harvest_rank, is_package_sale, mailing_key
from agents.scrapers.stale_listing_distress_agent import distress_score, find_keywords
from swarm.self_improvement import outcome_lifts, smoothed_rate, tuned_weights


def test_mailing_key_merges_variants():
    assert mailing_key("25 ALLIK WAY, #101", "SPRING VALLEY, NY") == mailing_key("25 ALLIK WAY UNIT 101", "SPRING VALLEY NY")
    assert mailing_key("6 ROCKAGE RD", "WARREN, NJ") == mailing_key("6 ROCKAGE ROAD", "WARREN, NJ")


def test_package_sale_and_rank():
    assert is_package_sale(2_000_000, 409_000)
    assert not is_package_sale(300_000, 280_000)
    old = harvest_rank({"year_built": 1926, "dwellings": 2}, 40, True, True, 1980)
    recent = harvest_rank({"year_built": 1926, "dwellings": 1}, 1.5, True, False, 1980)
    assert old > recent


def test_buyer_reliability_rewards_volume_and_recency():
    assert buyer_reliability(20, 9e6, date.today(), 5) > buyer_reliability(2, 5e5, date(2023, 1, 1), 1)


def test_listing_keywords_with_negation():
    assert find_keywords("Sold AS-IS. Cash only. Needs TLC.") == ["as-is", "cash only", "needs tlc"]
    assert find_keywords("This home is not a short sale. No need for TLC here.") == []
    assert find_keywords("24-hour handyman service on site") == []
    assert "fixer upper" in find_keywords("Great block", ["fixer_upper"])
    assert distress_score(400, True, ["as-is", "cash only"]) > distress_score(95, None, [])


def test_learning_math():
    assert smoothed_rate(0, 0) == 0.4
    rows = [({"absentee": 1}, True)] * 8 + [({"absentee": 1}, False)] * 2 + [({"stale_listing": 1}, False)] * 10
    lifts = outcome_lifts(rows)
    assert lifts["absentee"] > 1 > lifts["stale_listing"]
    tuned = tuned_weights({"absentee": 12, "stale_listing": 18}, {"absentee": 12, "stale_listing": 18}, lifts)
    assert tuned["absentee"] > 12 and tuned["stale_listing"] < 18
