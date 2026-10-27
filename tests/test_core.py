from datetime import date

from core.dates import is_business_day, nj_business_days_after, nj_state_holidays, parse_modiv_date
from core.text import (is_entity, is_estate, mailing_state, muni_base, normalize_street, parse_one_line_address,
                       street_key)


def test_attorney_review_worked_example_skips_veterans_day():
    # docs/recon/legal_rules.md: delivered Fri Nov 6 2026 -> ends Thu Nov 12 (Wed Nov 11 is Veterans Day)
    assert nj_business_days_after(date(2026, 11, 6), 3) == date(2026, 11, 12)


def test_nj_holidays_include_state_specific_days():
    h = nj_state_holidays(2026)
    assert date(2026, 2, 12) in h          # Lincoln's Birthday
    assert date(2026, 4, 3) in h           # Good Friday 2026
    assert date(2026, 11, 3) in h          # Election Day
    assert not is_business_day(date(2026, 11, 3))
    assert is_business_day(date(2026, 11, 4))


def test_modiv_deed_date_century():
    assert parse_modiv_date("240726") == date(2024, 7, 26)
    assert parse_modiv_date("680821") == date(1968, 8, 21)
    assert parse_modiv_date("") is None
    assert parse_modiv_date("991399") is None


def test_address_normalization():
    assert normalize_street("186 Newark Avenue") == "186 NEWARK AVE"
    assert normalize_street("718 722 No 8th Street") == "718 722 N 8TH ST"
    assert street_key("194 CLARKEN DRIVE UNIT 194") == "194 CLARKEN"
    p = parse_one_line_address("186 NEWARK AVE BLOOMFIELD NJ 07003", known_cities=["BLOOMFIELD", "NEWARK"])
    assert (p.street, p.city, p.zip) == ("186 NEWARK AVE", "BLOOMFIELD", "07003")
    p = parse_one_line_address("12 Main St, West Orange, NJ 07052")
    assert (p.street, p.city) == ("12 MAIN ST", "WEST ORANGE")


def test_owner_classification():
    assert is_entity("555 BERKELEY AVE LLC")
    assert is_entity("SMITH FAMILY TRUST")
    assert not is_entity("QUINN SAMPLE")
    assert is_estate("SAMPLE, QUINN EST OF")
    assert is_estate("ESTATE OF MARY JONES")
    assert mailing_state("STATEN ISLAND  NY") == "NY"
    assert mailing_state("EFFORT,  PA") == "PA"
    assert mailing_state("NEWARK, NJ") == "NJ"
    assert muni_base("WOODBRIDGE TWP") == "WOODBRIDGE"
    assert muni_base("Township of Edison") == "EDISON"


def test_lender_and_servicer_detection():
    from core.text import is_lender, is_servicer_mailing
    assert is_lender("JPMORGAN CHASE BANK, NATIONAL ASSO.")
    assert is_lender("BANKERS TRUST CO. OF CA.")
    assert not is_lender("JOHN BANKS")
    assert is_servicer_mailing("111 E. WISCONSIN AVE.", "MILWAUKEE, WI 53201")
    assert is_servicer_mailing("PO BOX 1 ATTN TAX DEPT", "DALLAS TX")
    assert not is_servicer_mailing("4 BANK ST 1ST FL", "ELMWOOD PARK NJ")
    assert not is_servicer_mailing("1354 SLOANE BLVD", "PLAINFIELD, NJ")
