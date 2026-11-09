"""S6 tax-appeal data packets: the Chapter 123 screen, the official ratio table, deadlines, the comparability rule,
suppression, the packet's legal wording (data only, no value, no names) and the flat-fee pitch."""
from __future__ import annotations

from datetime import date, datetime
from itertools import count

import pytest

from db.models import FirmTarget, Valuation
from db.repository import upsert_property
from engine.streams import s6_tax_appeals as s6
from engine.streams.base import StreamContext

NOW = datetime(2026, 11, 9, 10, 0)          # ISO week 2026-W46
OP = {"name": "Alex Operator", "first": "Alex", "company": "Desk Research", "phone": "(555) 555-0100",
      "email": "desk@example.com", "postal": "PO Box 1, Newark NJ 07101"}
_n = count(1)


def _comp(street: str, price: float, *, assessed: float = 300_000, town: str = "NEWARK CITY", units: int = 1,
          sold: str = "2026-06-01", dist: float = 0.2) -> dict:
    return {"address": street, "municipality": town, "sold_price": price, "sold_date": sold, "distance_mi": dist,
            "source": "njgin_public_record", "units": units, "assessed_total": assessed}


def _subject(db, *, comps: list[dict] | None = None, assessed: int = 300_000, taxes: float = 13_000,
             cls: str = "2", county: str = "Essex", mun_code: str = "0714", dwellings: int = 1, **kw):
    n = next(_n)
    data = {"parcel_id": f"{county.upper()}-{mun_code}-{100 + n}-{n}", "county": county, "municipality": "NEWARK CITY",
            "mun_code": mun_code, "block": str(100 + n), "lot": str(n), "street_address": f"{n} SUBJECT AVE",
            "city": "Newark", "zip_code": "07106", "property_class": cls, "dwellings": dwellings,
            "assessed_total": assessed, "annual_taxes": taxes, "owner_name": "QUINN Q SAMPLE",
            "owner_mailing_address": f"{n} SUBJECT AVE", "owner_city_state_zip": "NEWARK, NJ 07106"}
    data.update(kw)
    prop, _ = upsert_property(db, data, source="test")
    if comps is None:
        comps = [_comp(f"{10 + i} SALE ST", 500_000 + 10_000 * i, dist=0.1 + 0.05 * i) for i in range(4)]
    db.add(Valuation(property=prop, arv_estimate=550_000, as_is_value=480_000, verdict="PASS", is_current=True,
                     comps_json=comps, comp_count=len(comps), assumptions_json={}))
    db.flush()
    db.refresh(prop)
    return prop


def _ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=NOW, out_dir=tmp_path / "out", operator=OP, send=send, limits=limits)


def _target(db, county: str = "Essex", stage: str = "NEW", email: str = "tax@lawfirm-example.com") -> FirmTarget:
    t = FirmTarget(kind=s6.TARGET_KIND, stream="s6", org="Example Tax Appeal Law", email=email, county=county,
                   stage=stage, score=1.0)
    db.add(t)
    db.flush()
    return t


def _pdf_text(path) -> tuple[int, str]:
    from pypdf import PdfReader
    r = PdfReader(str(path))
    return len(r.pages), " ".join(p.extract_text() for p in r.pages)


# -- pure math ----------------------------------------------------------------------------------------------------
def test_screen_math_and_strong_cases_only():
    s = s6.chapter123_screen(500_000, 15_000, 400_000, 0.80)
    assert s.upper == pytest.approx(0.92)
    assert s.implied == pytest.approx(1.25)
    assert s.revised == pytest.approx(320_000)
    assert s.gap == pytest.approx(180_000) and s.gap_pct == pytest.approx(0.36)
    assert s.tax_diff == pytest.approx(180_000 * 0.03)
    assert s.passed
    # inside the common level range: never kept
    assert not s6.chapter123_screen(300_000, 9_000, 400_000, 0.80).passed
    # just above the upper limit, but the reduction is under 15% of the assessment: not a strong case
    weak = s6.chapter123_screen(372_000, 9_000, 400_000, 0.80)          # implied 0.93 > 0.92
    assert weak.implied > weak.upper and weak.gap_pct < 0.15 and not weak.passed


def test_official_ratio_table_from_the_division_of_taxation():
    table = s6.load_ratio_table()
    assert table["source"]["tax_year"] == 2027
    assert table["source"]["url"].startswith("https://www.nj.gov/")
    newark = s6.official_ratio("0714", table)
    assert newark is not None and newark.official
    assert newark.avg == pytest.approx(0.3583)
    assert newark.upper == pytest.approx(0.3583 * 1.15)
    assert newark.lower == pytest.approx(0.3583 * 0.85)
    # every target county is present and the printed limits are ratio +/- 15%
    counties = {r["county"] for r in table["ratios"].values()}
    assert {"Middlesex", "Union", "Essex", "Hudson", "Passaic", "Bergen", "Camden", "Mercer", "Monmouth"} <= counties
    for row in table["ratios"].values():
        assert row["upper"] == pytest.approx(row["avg"] * 1.15, abs=0.011)
    # ratios over 100% are taken as 100% (the certification's footnote)
    over = s6.official_ratio("9999", {"source": {"tax_year": 2027}, "ratios": {"9999": {"name": "X", "avg": 104.2}}})
    assert over.avg == 1.0 and over.printed == 104.2
    assert s6.official_ratio("0000", table) is None
    assert s6.load_ratio_table("/nonexistent/ratios.yaml") == {"source": {}, "ratios": {}}


def test_deadlines_april_1_january_15_counties_and_rollover():
    assert s6.next_deadline(date(2026, 11, 9)) == date(2027, 4, 1)
    assert s6.next_deadline(date(2027, 4, 1)) == date(2027, 4, 1)
    assert s6.next_deadline(date(2027, 4, 2)) == date(2028, 4, 1)
    assert s6.next_deadline(date(2026, 11, 9), "Monmouth") == date(2027, 1, 15)
    assert s6.next_deadline(date(2027, 1, 16), "Monmouth County") == date(2028, 1, 15)
    line = s6.deadline_line(date(2027, 4, 1), "Essex")
    assert "April 1, 2027" in line and "May 1, 2027" in line and "45 days" in line
    assert "January 15" in line and "Tax Court" in line
    assert "January 15, 2027" in s6.deadline_line(date(2027, 1, 15), "Monmouth")


def test_comparability_rule_units_town_and_size():
    one = s6.Comp("1 A ST", "NEWARK CITY", 500_000, date(2026, 6, 1), 500_000, None, 0.1, 1, None, 300_000, "njgin")
    three = s6.Comp("2 B ST", "NEWARK CITY", 700_000, date(2026, 6, 1), 700_000, None, 0.1, 3, None, 300_000, "njgin")
    mls = s6.Comp("3 C ST", "Newark", 520_000, date(2026, 6, 1), 520_000, 1500, 0.1, None, "single_family", None, "mls")
    assert s6.same_kind(three, 3) and not s6.same_kind(one, 3)       # a 3-family is never compared with a 1-family
    assert s6.same_kind(mls, 1) and not s6.same_kind(mls, 2)         # MLS sales carry no unit count
    assert s6.comparable(one, assessed=300_000, sqft=None, town="NEWARK CITY")
    assert not s6.comparable(one, assessed=300_000, sqft=None, town="EAST ORANGE CITY")   # other assessing base
    assert not s6.comparable(one, assessed=500_000, sqft=None, town="NEWARK CITY")        # a much bigger house
    assert s6.comparable(mls, assessed=300_000, sqft=1400, town="NEWARK CITY")
    assert not s6.comparable(mls, assessed=300_000, sqft=3000, town="NEWARK CITY")
    assert not s6.comparable(mls, assessed=300_000, sqft=None, town="NEWARK CITY")


def test_parse_comps_window_order_and_time_adjustment():
    vd = date(2026, 10, 1)
    comps = s6.parse_comps([
        _comp("FAR ST", 500_000, dist=0.9), _comp("NEAR ST", 500_000, dist=0.1),
        _comp("OLD ST", 500_000, sold="2023-01-15"), _comp("LATE ST", 500_000, sold="2026-10-20"),
        {"address": "NOPRICE ST", "sold_date": "2026-01-01"}, "junk"], vd, months=30)
    assert [c.address for c in comps] == ["NEAR ST", "LATE ST", "FAR ST"]   # nearest first; old / unpriced dropped
    late = next(c for c in comps if c.address == "LATE ST")
    assert late.adj_price == 500_000                                    # a sale after the assessing date: as sold
    near = next(c for c in comps if c.address == "NEAR ST")
    assert near.adj_price > 500_000                                     # older sale adjusted up (stricter screen)
    assert s6.months_before(date(2026, 10, 1), 30) == date(2024, 4, 1)


# -- the stream ---------------------------------------------------------------------------------------------------
def test_packet_is_data_only_one_page_no_names_and_the_pitch_is_flat_fee(db, tmp_path):
    from engine import human_copy as hc
    prop = _subject(db)
    t = _target(db)
    res = s6.STREAM.run(db, _ctx(tmp_path))
    assert res.found == 1 and res.products == 1 and res.metrics["ratio_official"] == 1
    pdf = next((tmp_path / "out").glob("*.pdf"))
    pages, text = _pdf_text(pdf)
    assert pages == 1
    flat = " ".join(text.split())
    assert "not an appraisal, BPO or CMA" in flat and "no opinion of value" in flat
    assert f"Block {prop.block} Lot {prop.lot}" in flat
    assert "35.83%" in flat and "41.20%" in flat                         # Newark tax-year 2027 ratio and upper limit
    assert "If the true value were V" in flat and "April 1, 2027" in flat
    assert "300,000 / $500,000 = 60.00%" in flat                         # per-sale arithmetic
    low = flat.lower()
    for banned in ("quinn", "sample", "over-assessed", "overassessed", "savings", "indicated value",
                   "estimated value", "median value", "best comp", "time-adj"):
        assert banned not in low, banned
    assert len(res.outbound) == 1
    ob = res.outbound[0]
    assert ob.key == f"s6:pitch:{t.id}:2026-W46" and ob.target_id == t.id and ob.attachments == [pdf]
    assert ob.to == t.email and "Essex County" in ob.subject and not ob.subject.lower().startswith("re:")
    assert hc.lint(ob.body) == [] and hc.word_count(ob.body) < 110
    assert ob.body.rstrip().endswith("Alex")
    assert "$100 a packet" in ob.body and "$500 a month" in ob.body and "April 1, 2027" in ob.body
    for banned in ("referral", "client", "savings", "over-assessed", "opportunit", "—"):
        assert banned not in ob.body.lower(), banned


def test_dry_run_proposes_nothing_and_keys_are_stable(db, tmp_path):
    _subject(db)
    t = _target(db)
    dry = s6.STREAM.run(db, _ctx(tmp_path, send=False))
    assert dry.products == 1 and dry.outbound == []
    a = s6.STREAM.run(db, _ctx(tmp_path / "a"))
    b = s6.STREAM.run(db, _ctx(tmp_path / "b"))
    assert [o.key for o in a.outbound] == [o.key for o in b.outbound] == [f"s6:pitch:{t.id}:2026-W46"]


def test_targets_by_kind_stage_and_county_and_the_pitch_cap(db, tmp_path):
    _subject(db)
    _target(db, county="Bergen", email="bergen@law-example.com")           # no packet in their county
    _target(db, stage="LOST", email="lost@law-example.com")
    db.add(FirmTarget(kind="real_estate_cpa", stream="s9", email="cpa@cpa-example.com", county="Essex", stage="NEW"))
    for i in range(4):
        _target(db, county="Essex County", email=f"essex{i}@law-example.com")
    res = s6.STREAM.run(db, _ctx(tmp_path, pitches_per_run=2))
    assert len(res.outbound) == 2
    assert all(o.to.startswith("essex") for o in res.outbound)


def test_suppressed_subject_and_suppressed_sales_are_never_shown(db, tmp_path):
    from engine.compliance import suppress_address
    prop = _subject(db)
    suppress_address(db, f"{prop.street_address}, Newark, NJ 07106", source="daniels_law")
    res = s6.STREAM.run(db, _ctx(tmp_path))
    assert res.products == 0 and res.metrics["suppressed"] == 1

    other = _subject(db, comps=[_comp(f"{20 + i} SALE ST", 500_000, dist=0.1 * (i + 1)) for i in range(4)])
    suppress_address(db, "21 Sale St, Newark, NJ", source="daniels_law")    # written with the postal town name
    res = s6.STREAM.run(db, _ctx(tmp_path / "2"))
    assert res.products == 1
    _, text = _pdf_text(next((tmp_path / "2" / "out").glob("*.pdf")))
    assert f"Lot {other.lot}" in text
    assert "21 Sale St" not in text and "20 Sale St, Newark" in text


def test_class4_commercial_and_cases_inside_the_range_are_skipped(db, tmp_path):
    _subject(db, cls="4A")
    _subject(db, assessed=180_000, comps=[_comp(f"{i} SALE ST", 500_000, assessed=180_000) for i in range(4)])
    # ^ 180K / ~505K = 36%: inside Newark's common level range
    _subject(db, comps=[_comp("1 SALE ST", 500_000), _comp("2 SALE ST", 500_000)])   # too few sales
    _subject(db, comps=[_comp(f"{i} SALE ST", 500_000, units=2) for i in range(4)])  # 2-family sales, 1-family house
    res = s6.STREAM.run(db, _ctx(tmp_path))
    assert res.products == 0 and res.outbound == []
    assert res.metrics["class4_needs_commercial_comps"] == 1
    assert res.metrics["within_range"] == 1 and res.metrics["too_few_comps"] == 2


def test_empty_database_and_low_tax_properties(db, tmp_path):
    res = s6.STREAM.run(db, _ctx(tmp_path))
    assert (res.processed, res.found, res.products, res.outbound) == (0, 0, 0, [])
    _subject(db, taxes=9_000)                                           # under the $12K default
    res = s6.STREAM.run(db, _ctx(tmp_path))
    assert res.processed == 0 and res.products == 0
    assert s6.STREAM.key == "s6" and s6.STREAM.priority == 3 and s6.STREAM.cadence_hours == 24


def test_ten_sales_with_long_addresses_stay_on_one_page(db, tmp_path):
    comps = [_comp(f"{100 + i} VERY LONG NAMED MEMORIAL PARKWAY EXTENSION UNIT {i}", 500_000 + 1_000 * i,
                   dist=0.05 * (i + 1)) for i in range(12)]
    prop = _subject(db, comps=comps, building="2S-F-CL-1G-BSMT-ATTIC", qual="C0101")
    res = s6.STREAM.run(db, _ctx(tmp_path))
    assert res.products == 1
    pages, text = _pdf_text(next((tmp_path / "out").glob("*.pdf")))
    assert pages == 1 and f"Qual {prop.qual}" in text
    assert "109 Very Long" in text and "110 Very Long" not in text and "111 Very Long" not in text   # 10 listed
