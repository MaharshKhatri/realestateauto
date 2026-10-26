"""Parsers tested against real CivilView pages saved 2026-10-26 (trimmed)."""
from datetime import date

import pytest

from agents.scrapers.sheriff_sale_agent import (extract_block_lot, first_defendant, is_estate_defendant,
                                                parse_detail_page, parse_list_page, parse_money, severity_for)
from conftest import FIXTURES

CV = FIXTURES / "civilview"


@pytest.mark.parametrize("county", ["middlesex", "union", "essex", "hudson", "passaic", "bergen", "camden"])
def test_list_pages_map_columns_by_header(county):
    rows = parse_list_page((CV / f"list_{county}.html").read_text(), county.title())
    assert len(rows) == 5
    for r in rows:
        assert r.sheriff_number and r.property_id and r.address and r.defendant and r.plaintiff
        assert isinstance(r.sale_date, date)
    if county == "middlesex":
        assert rows[0].status == "Purchased - 3rd Party"
    if county == "hudson":   # Hudson puts Address before Plaintiff
        assert rows[0].address.startswith("75 HARMON STREET")
        assert rows[0].plaintiff == "OCEANFIRST BANK"


def test_hudson_detail():
    d = parse_detail_page((CV / "detail_hudson.html").read_text())
    assert d["court_case"] == "F-001153-26"
    assert d["block_lot"] == ("3406", "36")
    assert d["upset_amount"] == 416000.0 and d["judgment_amount"] == 386289.13
    assert d["occupancy"] == "UNKNOWN"
    assert d["defendant_adjournments"] == 1


def test_bergen_detail_fka_block_lot_and_municipality():
    d = parse_detail_page((CV / "detail_bergen.html").read_text())
    assert d["block_lot"] == ("152.01", "95.08")
    assert d["municipality_hint"] == "MAHWAH"
    assert d["upset_amount"] == pytest.approx(1873395.84)
    assert d["occupancy"] == "OCCUPIED"


def test_camden_and_passaic_detail_formats():
    camden = parse_detail_page((CV / "detail_camden.html").read_text())
    assert camden["block_lot"] == ("229", "1")
    assert camden["upset_amount"] == pytest.approx(222518.70)
    passaic = parse_detail_page((CV / "detail_passaic.html").read_text())
    assert passaic["block_lot"] == ("10204", "5.06")
    assert passaic["municipality_hint"] == "WEST MILFORD"
    assert passaic["defendant_adjournments"] == 2


def test_block_lot_patterns():
    assert extract_block_lot("LOT: 78 BLOCK: 335") == ("335", "78")
    assert extract_block_lot("Tax Lot(s): 1 in Block: 229") == ("229", "1")
    assert extract_block_lot("BLOCK 12, LOT 4.01") == ("12", "4.01")


def test_money_with_space_thousands_separator():
    assert parse_money("$585 004.93") == 585004.93
    assert parse_money("$ 361,290.01") == 361290.01


def test_defendant_cleanup_and_estates():
    assert first_defendant("JULMALI HABIBUL; ET. AL.") == "JULMALI HABIBUL"
    assert first_defendant("CLARK REINHARD A/K/A CLARK REIHARD; NANCY") == "CLARK REINHARD"
    assert first_defendant("DOROTHEA BRADLEY, HER HEIRS, BENEFICIARIES, SUCCESSORS") == "DOROTHEA BRADLEY"
    assert first_defendant("UNKNOWN HEIRS OF JOHN SMITH; ET AL") == "JOHN SMITH"
    assert is_estate_defendant("ESTATE OF MARY JONES")
    assert not is_estate_defendant("JULMALI HABIBUL; ET. AL.")


def test_severity_peaks_near_sale():
    today = date(2026, 10, 26)
    assert severity_for(date(2026, 11, 5), today) == 1.0
    assert severity_for(date(2027, 3, 1), today) == 0.6


def test_comma_separated_defendants():
    assert first_defendant("ROGER NELSON, CAVALRY SPV I LLC ASSIGNEE CITIBANK, NA, LINEAR MORTGAGE LLC") == "ROGER NELSON"
    assert first_defendant("BRUMFIELD, PHILLIP J.; MARGARET BRUMFIELD") == "BRUMFIELD, PHILLIP J"
    assert first_defendant("HARRY ORTIZ, EDNA ORTIZ, TD BANK, N.A") == "HARRY ORTIZ"
