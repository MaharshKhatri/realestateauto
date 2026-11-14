"""S6 tax-appeal data packets: the Chapter 123 screen, the official ratio table, deadlines, the comparability rule,
suppression, the packet's legal wording (data only, no value, no names) and the 2-step pitch (a bare two-sentence
Touch 1, the packet held as the sample with the flat price, a one-sentence follow-up in the same thread)."""
from __future__ import annotations

import re
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


def street_of(prop) -> str:
    from core.text import postal_title
    return postal_title(prop.street_address)


def sentences(body: str) -> list[str]:
    """The sentences above the sign-off: the greeting line and the operator's first name are not sentences; a lone
    initial ("N.") does not end one."""
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    lines = [ln for ln in lines if ln not in ("Hello,", OP["first"]) and not re.fullmatch(r"Hi [\w.'-]+,", ln)]
    out, cur = [], []
    for tok in " ".join(lines).split():
        cur.append(tok)
        if re.search(r"[.?!][\")]?$", tok) and not re.fullmatch(r"[A-Z]\.", tok):
            out.append(" ".join(cur))
            cur = []
    return out + ([" ".join(cur)] if cur else [])


def touch_words(body: str) -> int:
    """Words above the sign-off (the operator's first name on the last line is the sign-off)."""
    from engine import human_copy as hc
    lines = body.rstrip().splitlines()
    return hc.word_count("\n".join(lines[:-1] if lines and lines[-1].strip() == OP["first"] else lines))


def _sent_pitch(db, t, body: str, subject: str = "Chapter 123 ratios", days_ago: int = 8):
    """An earlier cold touch the outbox sent (the target then counts one touch)."""
    from datetime import timedelta

    from db.models import FirmOutbox
    row = FirmOutbox(key=f"s6:pitch:{t.id}:2026-W45", stream="s6", priority=3, kind="pitch", target_id=t.id,
                     to_email=t.email, subject=subject, body=body, status="sent", message_id=f"<p{t.id}@desk.example>",
                     sent_at=NOW - timedelta(days=days_ago), attachments_json=[])
    db.add(row)
    t.touches, t.stage, t.last_contact_at = 1, "CONTACTED", NOW - timedelta(days=days_ago)
    db.flush()
    return row


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
    street = street_of(prop)
    # Touch 1 (2026-11-10): no attachment; the packet waits as the held sample with the flat price
    assert ob.key == f"s6:pitch:{t.id}:2026-W46" and ob.target_id == t.id and ob.kind == "pitch"
    assert ob.attachments == [] and ob.sample_attachments == [pdf] and ob.in_reply_to is None
    assert ob.to == t.email and ob.subject == f"Chapter 123 ratios: {street}, Newark"
    assert hc.lint(ob.body) == [] and ob.body.rstrip().endswith("Alex")
    assert len(sentences(ob.body)) == 2 and touch_words(ob.body) <= 45
    assert ob.body.startswith("Hello,\n\n") and street in ob.body and "Newark" in ob.body
    assert "$" not in ob.body and "similar recorded sales nearby, and" in ob.body
    assert "Chapter 123 ratio is" in ob.body and "common level range" not in ob.body   # facts only, no screen result
    assert sentences(ob.body)[-1] == "Want the comparable-sales packet for it before the April 1 deadline?"
    assert "$100 a packet" in ob.sample_body and "$500 a month" in ob.sample_body
    assert "April 1, 2027 filing deadline" in ob.sample_body and street in ob.sample_body
    assert ob.sample_body.endswith('Reply "start" and I\'ll set it up.') and hc.lint(ob.sample_body) == []
    assert "no contingency" in ob.sample_body and "no opinion of value" in ob.sample_body
    assert "Director's Chapter 123 ratio" in ob.sample_body
    for text in (ob.body, ob.sample_body, ob.subject):
        for banned in ("referral", "client", "savings", "over-assessed", "overassessed", "opportunit", "—",
                       "lower", "assessed too", "should appeal", "quinn", "free", "!"):
            assert banned not in text.lower(), (banned, text)


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


# -- the 2-step funnel (2026-11-10) -------------------------------------------------------------------------------
def test_touch1_counts_the_town_names_its_top_property_and_holds_that_packet(db, tmp_path):
    low = _subject(db, taxes=13_000)
    top = _subject(db, taxes=20_000)                                    # the largest Chapter 123 tax difference
    mid = _subject(db, taxes=15_000)
    _subject(db, county="Bergen", mun_code="0223", municipality="HACKENSACK CITY", city="Hackensack",
             assessed=600_000, comps=[_comp(f"{i} BERGEN ST", 500_000, assessed=600_000, town="HACKENSACK CITY")
                                      for i in range(4)])
    t = _target(db)
    res = s6.STREAM.run(db, _ctx(tmp_path))
    assert res.found == 4 and res.products == 4 and len(res.outbound) == 1
    ob = res.outbound[0]
    street = street_of(top)
    assert ob.subject == f"Chapter 123 ratios: {street} + 2 more in Newark"
    first, second = sentences(ob.body)
    assert first.startswith(f"{street} in Newark, one of 3 homes there taxed over 12,000 dollars a year, has ")
    assert "similar recorded sales nearby, and" in first and "Newark's official Chapter 123 ratio is" in first
    assert second == "Want the comparable-sales packet for it before the April 1 deadline?"
    assert street_of(low) not in ob.body and street_of(mid) not in ob.body
    # the held sample is exactly the named property's one-page packet (a one-property packet is its own sample)
    (pdf,) = ob.sample_attachments
    assert pdf.exists() and pdf.name == f"s6_Essex_0714_B{top.block}_L{top.lot}.pdf"
    _, text = _pdf_text(pdf)
    assert f"Block {top.block} Lot {top.lot}" in text
    assert not (pdf.parent / "samples").exists()                        # nothing masked for a one-property packet
    assert ob.target_id == t.id and "$" not in ob.body


def test_the_tax_screen_in_words_and_singular_copy():
    assert s6.taxes_floor(12_000) == "12,000" and s6.taxes_floor(12_750) == "12,000" and s6.taxes_floor(800) == "800"
    one = s6.pitch_body("Newark", "12 Main St", 1, 12_000, "Alex", comps=4)
    assert one.startswith("Hello,\n\n12 Main St in Newark has 4 similar recorded sales nearby, and")
    assert len(sentences(one)) == 2 and touch_words(one) <= 45
    many = s6.pitch_body("West Orange", "4 N. Main St", 9, 15_500, "Alex", comps=5)
    assert "one of 9 homes there taxed over 15,000 dollars a year" in many
    assert len(sentences(many)) == 2 and touch_words(many) <= 45
    import re
    for bad in (r"over-assessed", r"savings", r"above the", r"\brange\b", r"\bvalue\b"):
        assert not re.search(bad, (one + many).lower()), bad
    assert s6.pitch_subject("Newark", "12 Main St", 1) == "Chapter 123 ratios: 12 Main St, Newark"
    assert s6.pitch_subject("Newark", "12 Main St", 10) == "Chapter 123 ratios: 12 Main St + 9 more in Newark"
    est = s6.sample_body("12 Main St", date(2027, 4, 1), 100, 500, official=False)
    assert "estimated Chapter 123 ratio" in est and "Director's Chapter 123 ratio" not in est
    assert s6.names_street("found it at 11 Main St.", "11 Main St")
    assert not s6.names_street("found it at 11 Main St.", "1 Main St")


def test_monmouth_sample_states_the_january_15_deadline(db, tmp_path):
    prop = _subject(db, county="Monmouth", mun_code="1316", municipality="FREEHOLD BORO", city="Freehold",
                    zip_code="07728", assessed=700_000,
                    comps=[_comp(f"{i} MAIN ST", 500_000 + 5_000 * i, assessed=600_000, town="FREEHOLD BORO")
                           for i in range(4)])
    _target(db, county="Monmouth", email="monmouth@law-example.com")
    res = s6.STREAM.run(db, _ctx(tmp_path))
    (ob,) = res.outbound
    assert "Freehold" in ob.body and street_of(prop) in ob.body and "$" not in ob.body
    assert "January 15, 2027 filing deadline" in ob.sample_body and "April 1" not in ob.sample_body


def test_a_target_in_a_sequence_gets_no_second_touch_from_the_stream(db, tmp_path):
    """2026-11-10 (the B2B playbook): Touch 1 only; the outbox's queue_nudges sends the nudge in-thread."""
    _subject(db, taxes=20_000)
    b = _subject(db, taxes=14_000)
    t = _target(db)
    _sent_pitch(db, t, f"Hello,\n\nWe ran ... including {street_of(b)}. Want the packet for it?\n\nAlex",
                subject=f"Chapter 123 ratios: {street_of(b)} + 1 more in Newark")
    res = s6.STREAM.run(db, _ctx(tmp_path))
    assert [o for o in res.outbound if o.target_id == t.id] == []


def test_targets_the_outbox_would_skip_never_take_a_pitch_slot(db, tmp_path):
    from datetime import timedelta

    from db.models import FirmOutbox
    _subject(db)
    used = _target(db, email="used@law-example.com")
    used.touches = 2                                                    # cold touches used up
    recent = _target(db, email="recent@law-example.com")
    recent.touches, recent.last_contact_at = 1, NOW - timedelta(days=2)   # inside the 6-day gap
    queued = _target(db, email="queued@law-example.com")
    db.add(FirmOutbox(key=f"s6:pitch:{queued.id}:2026-W45", stream="s6", kind="pitch", target_id=queued.id,
                      to_email=queued.email, subject="x", body="x", status="queued"))
    week = _target(db, email="week@law-example.com")
    db.add(FirmOutbox(key=f"s6:pitch:{week.id}:2026-W46", stream="s6", kind="pitch", target_id=week.id,
                      to_email=week.email, subject="x", body="x", status="sent"))   # this week's key already used
    fresh = [_target(db, email=f"fresh{i}@law-example.com") for i in range(2)]
    db.flush()
    res = s6.STREAM.run(db, _ctx(tmp_path, pitches_per_run=2))
    assert [o.target_id for o in res.outbound] == [f.id for f in fresh]


def test_the_outbox_sends_touch1_bare_and_holds_the_packet_for_a_yes(db, tmp_path):
    from db.models import FirmOutbox
    from engine import firm_outbox as fo
    _subject(db)
    t = _target(db)
    (ob,) = s6.STREAM.run(db, _ctx(tmp_path)).outbound
    row = fo.enqueue(db, ob, NOW)
    assert row.status == "queued" and row.attachments_json == [] and row.body == ob.body
    held = db.query(FirmOutbox).filter_by(key=f"{ob.key}:sample").one()
    assert held.status == "held" and held.kind == "sample" and held.target_id == t.id
    assert held.body == ob.sample_body and [a["name"] for a in held.attachments_json] == [ob.sample_attachments[0].name]
