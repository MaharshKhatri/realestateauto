"""RealAuction tax-sale parsing (fixtures: real pages, trimmed to 3 rows)."""
import shutil
from datetime import date

from agents.scrapers.tax_sale_agent import (TaxSaleAgent, group_by_parcel, load_sites, page_count, page_kind,
                                            parse_block_lot, parse_rows, parse_sale_date)
from conftest import FIXTURES

RA = FIXTURES / "realauction"


def test_preview_rows_have_owner_amount_status():
    rows = parse_rows((RA / "preview_eastorange.html").read_text(), "preview")
    assert len(rows) == 3
    r = rows[1]
    assert (r.block, r.lot, r.owner, r.charge_type, r.face, r.status) == ("12", "2.06", "METROMAN, ALPHALEY", "T", 12285.87, "Active")


def test_results_rows_have_bid_and_bidder():
    rows = parse_rows((RA / "results_camden.html").read_text(), "results")
    assert rows[0].face == 946.91 and rows[0].winning_bid == "$2,100.00" and rows[0].bidder == "40712"


def test_paging_sale_date_and_page_kinds():
    assert page_count((RA / "preview_eastorange.html").read_text()) == 9
    assert parse_sale_date((RA / "rules_eastorange.html").read_text()) == date(2026, 10, 29)
    assert page_kind((RA / "preview_eastorange.html").read_text()) == "preview"
    assert page_kind((RA / "results_camden.html").read_text()) == "results"
    assert page_kind((RA / "rules_eastorange.html").read_text()) == "rules"


def test_block_lot_and_grouping():
    assert parse_block_lot("8 - 52 - C001") == ("8", "52", "C001")
    assert parse_block_lot("garbage") is None
    rows = parse_rows((RA / "preview_eastorange.html").read_text(), "preview")
    groups = group_by_parcel(rows + rows)
    assert all(len(g.rows) == 2 for g in groups.values())


def test_site_directory_and_saved_page_import(tmp_path):
    assert len(load_sites(["Essex", "Union", "Middlesex", "Hudson", "Passaic", "Bergen", "Camden"])) == 91
    site = load_sites(towns=["EastOrange"])[0]
    folder = tmp_path / "EastOrange"
    folder.mkdir()
    for name in ("preview_eastorange.html", "rules_eastorange.html"):
        shutil.copy(RA / name, folder / name)
    pages = TaxSaleAgent._load_saved(site, tmp_path)
    assert len(pages["preview"]) == 1 and len(pages["rules"]) == 1
