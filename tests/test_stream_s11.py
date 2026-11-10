"""Stream 11: NJ sheriff-sale bid sheets for auction bidders (engine/streams/s11_sheriff_bid_sheets.py)."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import pytest

from db.models import CashBuyer, DeedTransfer, FirmOutbox, FirmTarget, Prospect, SignalType, utcnow
from engine.streams import s11_sheriff_bid_sheets as s11
from engine.streams.base import StreamContext, sample_path
from tests.factories import make_lead

OP = {"name": "Sam Operator", "first": "Sam", "phone": "(973) 555-0199", "email": "sam@example.com",
      "postal": "1 Main St, Newark, NJ 07102"}
# a Tuesday (the stream proposes no pitches on weekends), on or before today so the signals' last-seen times are fresh
_today = date.today()
NOW = datetime.combine(_today - timedelta(days=(_today.weekday() - 1) % 7), time(10))
TODAY = NOW.date()


def _ctx(tmp_path, send: bool = True, now: datetime = NOW, **limits) -> StreamContext:
    return StreamContext(now=now, out_dir=tmp_path / "out", operator=OP, send=send, limits=limits)


def _text(path) -> str:
    """The PDF's text (the mask's bullet comes back from pypdf as \\x7f)."""
    from pypdf import PdfReader
    return "\n".join(p.extract_text() for p in PdfReader(str(path)).pages).replace("\x7f", "•")


def _flat(path) -> str:
    return " ".join(_text(path).split())


def _pages(path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(path)).pages)


def _sentences(body: str) -> list[str]:
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if lines and re.fullmatch(r"(?i)(hi|hello)\b[^.?!]*,", lines[0]):
        lines = lines[1:]
    if lines and lines[-1] == OP["first"]:
        lines = lines[:-1]
    return [x for x in re.split(r"(?<=[.?!])\s+", " ".join(lines)) if x]


def _sig(lead):
    return next(s for s in lead.property.signals if s.signal_type == SignalType.SHERIFF_SALE)


def _sale(db, days: int, *, status: str = "Scheduled", meta: dict | None = None, plaintiff: str = "BIG BANK NA",
          last_seen: datetime | None = None, **prop_kw):
    """A sheriff sale `days` after TODAY; `meta` replaces the listing's metadata."""
    lead = make_lead(db, sheriff_days=5, **prop_kw)
    sig = _sig(lead)
    sig.scheduled_auction_date = TODAY + timedelta(days=days)
    sig.status = status
    sig.plaintiff_or_lienholder = plaintiff
    sig.raw_metadata_json = {"county": prop_kw.get("county", "Essex"), "sheriff_number": f"F-26{lead.id:06d}",
                             "upset_amount": 250000.0 + lead.id, "judgment_amount": 240000.0,
                             "status_history": [["Scheduled", "1/1/2026"]], **(meta or {})}
    if last_seen is not None:
        sig.last_seen_at = last_seen
    db.flush()
    return lead


def _street(lead) -> str:
    from core.text import postal_title
    return postal_title(lead.property.street_address)


def _has(text: str, lead) -> bool:
    """The lead's street appears as a whole address ("5 Test St" is not in "15 Test St")."""
    return re.search(rf"(?<![\w-]){re.escape(_street(lead))}(?!\w)", " ".join(text.split())) is not None


UNION = dict(county="Union", municipality="ELIZABETH CITY", mun_code="2004", city="Elizabeth", zip_code="07201")


def _desk(db):
    """Five Essex sales in the window (one with a person as plaintiff and stated liens), two Union sales, and the
    sales the sheet must leave out: closed, in bankruptcy, outside the window, not seen lately, no street."""
    essex = [_sale(db, d) for d in (6, 4, 9, 12, 15)]
    priv = essex[3]
    _sig(priv).plaintiff_or_lienholder = "QUINN Q SAMPLE"
    _sig(priv).raw_metadata_json = {**_sig(priv).raw_metadata_json, "prior_liens": 9239.33}
    union = [_sale(db, d, **UNION) for d in (5, 8)]
    out = {
        "closed": _sale(db, 7, status="Purchased - 3rd Party"),
        "bankrupt": _sale(db, 7, status="Bankruptcy"),
        "late": _sale(db, 30),
        "past": _sale(db, 0),
        "stale": _sale(db, 7, last_seen=utcnow() - timedelta(days=40)),
        "nostreet": _sale(db, 7, street_address="569"),
    }
    db.flush()
    return essex, union, out


# -- pure helpers -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Adjourned - Defendant", ("debtor", 1)),
    ("Defendant Adjournment", ("debtor", 1)),
    ("DEFENDANT 1ST 4 WEEK ADJ", ("debtor", 1)),
    ("DEFENDANT TOTAL 8 WEEK ADJ", ("debtor", 2)),             # both debtor adjournments at once
    ("Adjourned Plaintiff", ("lender", 1)),
    ("Adjourned - Plaintiff - New Sale Date:", ("lender", 1)),
    ("JOINT ADJOURNMENT", ("agreed", 1)),
    ("Adjourned - Mutual (PA)", ("agreed", 1)),
    ("Adjourned - Court", ("court", 1)),
    ("Court Ordered Plaintiff Adjournment", ("court", 1)),     # for cause: outside the five
    ("Adjourned Sheriff", ("sheriff", 1)),
    ("Adjourned", ("other", 1)),
    ("Scheduled", None),
    ("Bankruptcy", None),
    ("Rescheduled", None),
    ("Not adjourned", None),
    ("", None),
])
def test_classify_adjournment(text, expected):
    assert s11.classify_adjournment(text) == expected


def test_adjournments_left_per_party_from_history_or_counts():
    hist = [["Scheduled", "3/31/2026"], ["Adjourned Plaintiff", "4/28/2026"], ["Bankruptcy", "4/28/2026"],
            ["Adjourned Plaintiff", "8/18/2026"], ["Adjourned Plaintiff", "9/1/2026"], ["Adjourned Sheriff", "12/1/2026"]]
    a = s11.adjournments({"status_history": hist})
    assert (a.debtor, a.lender, a.agreed, a.sheriff) == (0, 3, 0, 1)
    assert a.used == 2 and a.left == 3                         # the lender's 2 are gone; debtor 2 + agreed 1 remain
    cell = a.cell()
    assert "lender 3" in cell and "3 of 5 left" in cell and "+1 sheriff" in cell
    b = s11.adjournments({"status_history": [["DEFENDANT TOTAL 8 WEEK ADJ", "x"], ["JOINT ADJOURNMENT", "y"],
                                             ["Adjourned - Court", "z"]]})
    assert (b.debtor, b.agreed, b.court, b.left) == (2, 1, 1, 2)
    c = s11.adjournments({"adjournments_total": 4, "defendant_adjournments": 1, "plaintiff_adjournments": 2})
    assert (c.debtor, c.lender, c.other, c.left) == (1, 2, 1, 2)
    assert s11.adjournments({}).cell().startswith("none<br/><b>5 of 5 left</b>")
    assert s11.adjournments({"status_history": [["Adjourned Plaintiff", "a"]] * 6}).left == 3   # never below a party's 0


def test_holds_streets_products_and_figures():
    assert s11.on_hold("Bankruptcy", {}) and s11.on_hold("Bankuptcy", {}) and s11.on_hold("BANKRUPTCY HOLD", {})
    assert s11.on_hold("Scheduled", {"on_hold": True})
    assert not s11.on_hold("POST BANKRUPTCY SALE DATE", {}) and not s11.on_hold("Adjourned - Defendant", {})
    assert s11.has_street("12 Sample St") and not s11.has_street("569") and not s11.has_street("12-75")
    assert s11.product_of("2", "2SF", None, 1) == "1-family"
    assert s11.product_of("2", "2S-2FAMILY", None, 2) == "2-family"
    assert s11.product_of("2", "3FAM", None, 3) == "3-4 family"
    assert s11.product_of("2", "CONDO", "C0003", 1) == "condo"
    assert s11.product_of("4C", None, None, None) == "5+ units"
    assert s11.product_of("4A", None, None, 1) is None and s11.product_of("2", None, None, None) is None
    assert s11.comp_product({"property_type": "2SF", "units": 2}) == "2-family"
    assert s11.comp_product({"property_type": "5S-14U-11P-COND", "units": 1}) == "condo"
    assert s11.comp_product({"property_type": "2SF"}) is None
    assert s11.class_label("15F") == "exempt" and s11.class_label("4A") == "commercial"
    assert s11.span_words(21) == "three weeks" and s11.span_words(10) == "10 days"


@pytest.mark.parametrize("name, expected", [
    ("PENNYMAC LOAN SERVICES, LLC", "PENNYMAC LOAN SERVICES, LLC"),          # never just "LLC"
    ("CITIZENS BANK, N.A.", "CITIZENS BANK, N.A."),
    ("TOWNSHIP OF IRVINGTON", "TOWNSHIP OF IRVINGTON"),                       # a town's tax foreclosure is named
    ("SAMPLE CONDOMINIUM ASSOCIATION, INC.", "SAMPLE CONDOMINIUM ASSOCIATION, INC."),
    ("SAMPLE SAVINGS AND LOAN ASSOCIATION", "SAMPLE SAVINGS AND LOAN ASSOCIATION"),
    ("QUINN SAMPLE, AND ALEX EXAMPLE AND SAMPLE LIVE LLC", "SAMPLE LIVE LLC"),
    ("QUINN SAMPLE & SAMPLE HOMES LLC", "SAMPLE HOMES LLC"),
    ("QUINN Q SAMPLE", "(private party)"),
    ("QUINN SAMPLE AND ALEX SAMPLE", "(private party)"),
    ("QUINN SAMPLE, AS EXECUTOR OF THE ESTATE OF ALEX SAMPLE", "(private party)"),
    ("", "-"),
])
def test_plaintiff_is_a_business_or_a_town_never_a_person(name, expected):
    assert s11.plaintiff_name(name) == expected


def test_long_trust_caption_is_cut_at_a_clause_or_a_word():
    got = s11.plaintiff_name("U.S. BANK TRUST NATIONAL ASSOCIATION, NOT IN ITS INDIVIDUAL CAPACITY BUT SOLELY AS OWNER "
                             "TRUSTEE FOR RCF 2 ACQUISITION TRUST")
    assert got == "U.S. BANK TRUST NATIONAL ASSOCIATION..."
    assert s11.plaintiff_name("THE BANK OF NEW YORK MELLON F/K/A THE BANK OF NEW YORK, AS TRUSTEE FOR THE HOLDERS") == (
        "THE BANK OF NEW YORK MELLON F/K/A THE BANK...")


def test_notice_figures_say_which_figure():
    from types import SimpleNamespace as NS
    cell, has = s11.notice_figures(NS(raw_metadata_json={"upset_amount": 499287.88, "judgment_amount": 499287.88},
                                      amount_owed=None))
    assert cell == "Upset $499,288" and has
    cell, _ = s11.notice_figures(NS(raw_metadata_json={"upset_amount": 300000, "judgment_amount": 280000.5},
                                    amount_owed=None))
    assert cell == "Upset $300,000<br/>Judgment $280,000"
    cell, has = s11.notice_figures(NS(raw_metadata_json={}, amount_owed=150000))
    assert cell == "Amount stated $150,000" and not has
    assert s11.notice_figures(NS(raw_metadata_json={}, amount_owed=None))[0] == "not stated"
    assert s11.liens_cell(NS(raw_metadata_json={"prior_liens": 9239.33})) == "~$9,239<br/>(taxes / water / sewer)"
    assert s11.liens_cell(NS(raw_metadata_json={})) == "none stated"


def test_recorded_sales_range_is_public_record_only_windowed_and_deduped():
    def comp(addr, price, sold, units=2, src="njgin_public_record", town="NEWARK CITY"):
        return ("Essex", {"address": addr, "municipality": town, "sold_price": price, "sold_date": sold, "units": units,
                          "property_type": "2SF", "source": src})
    today = date(2026, 11, 10)
    comps = [comp("1 A ST", 310000, "2025-03-01"), comp("1 A ST", 310000, "2025-03-01"),       # the same sale twice
             comp("2 B ST", 520000, "2024-06-01"), comp("3 C ST", 400000, "2025-09-01"),
             comp("4 D ST", 30000, "2025-01-01"),                                                # nominal: out
             comp("5 E ST", 900000, "2020-01-01"),                                               # too old: out
             comp("6 F ST", 990000, "2025-01-01", src="homeharvest_listing_mls"),                # not a deed: out
             comp("7 G ST", 700000, "2025-01-01", units=1),                                      # another type
             comp("8 H ST", 450000, "2025-02-01", town="IRVINGTON TWP")]
    got = s11.range_from(comps, today, 36, 3)
    r = got[("Essex", "NEWARK", "2-family")]
    assert (r.lo, r.hi, r.n) == (310000, 520000, 3)
    assert r.cell() == "$310K-$520K<br/>3 sales (2-family), 2024-25"
    assert ("Essex", "NEWARK", "1-family") not in got and ("Essex", "IRVINGTON", "2-family") not in got


# -- the sheet ------------------------------------------------------------------------------------------------------
def test_sheet_rows_order_exclusions_and_privacy(db, tmp_path):
    essex, union, out = _desk(db)
    from engine.compliance import suppress_address
    p = essex[2].property
    suppress_address(db, f"{p.street_address}, {p.city} NJ {p.zip_code}", "daniels_law")
    res = s11.STREAM.run(db, _ctx(tmp_path))
    files = sorted(x.name for x in (tmp_path / "out").glob("*.pdf"))
    assert files == [f"s11_bid_sheet_essex_{TODAY.isoformat()}.pdf", f"s11_bid_sheet_union_{TODAY.isoformat()}.pdf"]
    m = res.metrics
    assert (m["pdfs"], m["counties"], res.products, res.found) == (2, 2, 2, 6)
    assert (m["skipped_suppressed"], m["skipped_closed"], m["skipped_on_hold"], m["skipped_stale"],
            m["skipped_no_address"]) == (1, 1, 1, 1, 1)
    assert m["sales_in_window"] == 11 and m["sales_seen"] == 13
    essex_pdf = tmp_path / "out" / files[0]
    text, flat = _text(essex_pdf), _flat(essex_pdf)
    shown = [essex[1], essex[0], essex[3], essex[4]]                      # by sale date: day 4, 6, 12, 15
    assert all(_has(text, x) for x in shown)
    idx = [re.search(rf"(?<![\w-]){re.escape(_street(x))}(?!\w)", flat).start() for x in shown]
    assert idx == sorted(idx)
    for gone in [essex[2]] + list(out.values()):
        assert not _has(text, gone), gone.property.street_address
    for secret in ("QUINN", "Quinn"):                                     # the defendant, a person plaintiff
        assert secret not in text
    assert "(private party)" in flat and "BIG BANK NA" in flat
    assert "~$9,239" in flat and "none stated" in flat and "Upset $" in flat and "Judgment $240,000" in flat
    assert "5 of 5 left" in flat
    assert "NJ Sheriff-Sale Bid Sheet: Essex County" in flat
    assert flat.index("Public-record data; not an appraisal, BPO or CMA; no opinion of value.") < flat.index(
        _street(shown[0]))                                                # near the top, above the table
    for line in ("SURVIVING-LIEN CHECKLIST", "tax sale certificates survive", "N.J.S.A. 46:8B-21",
                 "26 U.S.C. 7425(d)", "R. 4:65-5", "typically 20% of the bid in certified funds",
                 "Order a title search from a licensed title producer before bidding.",
                 "This sheet is not a title search, title insurance, legal advice or an appraisal.",
                 "Owner-occupied homes in foreclosure are protected by the Foreclosure Rescue Fraud Prevention Act; "
                 "this sheet is for auction bidders only.", "N.J.S.A. 2A:17-36", "Sources:", "Sam Operator", "no more"):
        assert line in flat, line
    low = flat.lower()
    for bad in ("arv", "worth", "below market", "equity of", "under-valued", "undervalued", "savings"):
        assert bad not in low, bad
    assert "value" not in low.replace("no opinion of value", "")         # the only "value" is the disclaimer
    union_text = _text(tmp_path / "out" / files[1])
    assert all(_has(union_text, x) for x in union) and not any(_has(union_text, x) for x in shown)
    assert "NJ Sheriff-Sale Bid Sheet: Union County" in _flat(tmp_path / "out" / files[1])
    blob = repr(res.metrics) + repr(res.notes)                            # the public run log: counts only
    assert "Test St" not in blob and "QUINN" not in blob and "@" not in blob and "$" not in blob


def test_recorded_sales_range_and_rehab_tier_on_the_row(db, tmp_path):
    lead = _sale(db, 6, dwellings=2, building_desc="2SF")
    lead.property.current_valuation.comps_json = [
        {"address": f"{i} COMP ST", "municipality": "NEWARK CITY", "sold_price": 300000 + 50000 * i,
         "sold_date": (TODAY - timedelta(days=100 + i)).isoformat(), "units": 2, "property_type": "2SF",
         "source": "njgin_public_record"} for i in range(4)]
    _sale(db, 7)
    _sale(db, 8)
    db.flush()
    res = s11.STREAM.run(db, _ctx(tmp_path, send=False))
    flat = _flat(next((tmp_path / "out").glob("s11_bid_sheet_essex_*.pdf")))
    assert "$300K-$450K" in flat and "4 sales (2-family)" in flat
    assert "Class 2 (residential)" in flat and "2-family" in flat and "built 1925" in flat
    assert flat.count("Medium") >= 2                                      # the row's tier + the note's legend
    assert res.metrics["rows_with_sales_range"] == 1 and res.outbound == []


def test_empty_database(db, tmp_path):
    res = s11.STREAM.run(db, _ctx(tmp_path))
    assert (res.processed, res.found, res.products, res.outbound) == (0, 0, 0, [])
    assert res.metrics["pdfs"] == 0 and res.metrics["pitches"] == 0 and res.metrics["targets_synced"] == 0


def test_many_sales_full_sheet_has_every_row_and_sample_fits_one_page(db, tmp_path):
    leads = [_sale(db, 3 + (i % 18)) for i in range(40)]
    t = FirmTarget(kind="sheriff_bidder", stream="s11", org="Bid Co LLC", email="office@bidco.example",
                   county="Essex", stage="NEW")
    db.add(t)
    db.flush()
    res = s11.STREAM.run(db, _ctx(tmp_path))
    full = next((tmp_path / "out").glob("s11_bid_sheet_essex_*.pdf"))
    assert _pages(full) > 1                                              # a subscriber gets every sale
    text = _text(full)
    assert all(_has(text, x) for x in leads)
    [ob] = res.outbound
    [sample] = ob.sample_attachments
    assert sample == sample_path(full) and _pages(sample) == 1           # the nudge says "It's one page"
    shown = int(re.search(r"Sample: sales from \w+ \d+, 2 of (\d+) rows in full", _flat(sample)).group(1))
    assert 3 <= shown <= 16 and f"{shown} sales from" in ob.sample_body and "all 40 through" in ob.sample_body


# -- the funnel -----------------------------------------------------------------------------------------------------
def _target(db, email: str, *, kind: str = "sheriff_bidder", stream: str = "s11", county: str | None = "Essex",
            stage: str = "NEW", score: float = 0.0, name: str | None = None) -> FirmTarget:
    t = FirmTarget(kind=kind, stream=stream, org=f"{email.split('@')[0]} LLC", email=email, county=county,
                   stage=stage, score=score, name=name)
    db.add(t)
    db.flush()
    return t


def _plain_touch(subject: str, body: str) -> None:
    from engine import human_copy as hc
    sents = _sentences(body)
    assert len(sents) == 2 and sents[1].endswith("?"), sents
    words = hc.word_count(" ".join(sents))
    assert 25 <= words <= 45, (words, body)
    assert hc.lint(body, max_words=60) == [], hc.lint(body, max_words=60)
    low = body.lower()
    for bad in ("$", "http", "www.", "!", "attached", "ad:", "referral", "guarantee", "a month", "worth", "value"):
        assert bad not in low, bad
    assert "free" not in subject.lower() and len(subject) <= 70 and not subject.lower().startswith("re:")


def test_touch1_names_a_sale_far_enough_out_and_holds_the_masked_sample(db, tmp_path):
    essex, _, _ = _desk(db)
    bidder = _target(db, "office@bidder.example", score=5)
    _target(db, "deals@flipco.example", kind="flipper", county="Union")          # 2 Union sales: no sheet to pitch
    _target(db, "s2flip@flipco.example", kind="flipper", stream="s2")              # another stream's flipper
    _target(db, "lost@bidder.example", stage="LOST", score=9)
    _target(db, "cpa@firm.example", kind="real_estate_cpa", score=9)               # not a kind s11 sells to
    res = s11.STREAM.run(db, _ctx(tmp_path))
    assert [o.to for o in res.outbound] == ["office@bidder.example"]
    assert res.metrics["pitches"] == 1 and res.metrics["targets_matched"] == 1 and res.metrics["targets_no_sheet"] == 1
    ob = res.outbound[0]
    assert ob.target_id == bidder.id and ob.kind == "pitch" and ob.priority == 3 and ob.stream == "s11"
    assert ob.key == f"s11:pitch:{bidder.id}:{s11.week_tag(NOW)}" and ob.attachments == [] and ob.in_reply_to is None
    _plain_touch(ob.subject, ob.body)
    # the first Essex sale at least 3 days out (day 4: essex[1]); the count is the sheet's 5 Essex sales
    featured = essex[1]
    d = TODAY + timedelta(days=4)
    when = f"{d:%b} {d.day}"
    assert _sentences(ob.body) == [
        f"{_street(featured)} in Newark is set for sheriff sale on {when}, one of 5 Essex County sales in the next "
        "three weeks on our sheet with the upset, stated liens and adjournments left.",
        "Want this week's Essex County bid sheet, free?"]
    assert ob.body.splitlines()[0] == "Hello," and ob.body.splitlines()[-1] == "Sam"
    assert ob.subject == f"[Essex] sheriff sale {when}: {_street(featured)}"
    # Touch 2 (held): the one-page sample from the featured sale, rows 3+ with the street and sheriff # hidden
    [sample] = ob.sample_attachments
    assert sample.exists() and sample.name.endswith(".sample.pdf") and _pages(sample) == 1
    text = _text(sample)
    rows = [essex[1], essex[0], essex[2], essex[3], essex[4]]           # by sale date: day 4, 6, 9, 12, 15
    assert _has(text, rows[0]) and _has(text, rows[1])
    assert not any(_has(text, x) for x in rows[2:]) and "•" in text
    assert _sig(rows[1]).raw_metadata_json["sheriff_number"] in text
    assert not any(_sig(x).raw_metadata_json["sheriff_number"] in text for x in rows[2:])
    assert "Sample: sales from" in _flat(sample) and "2 of 5 rows in full" in _flat(sample)
    last = TODAY + timedelta(days=15)
    assert ob.sample_body == (
        f"Attached is a sample of the Essex County bid sheet: 5 sales from {when}, the first 2 in full and the rest "
        f"with the street and sheriff # hidden (the full sheet lists all 5 through {last:%b} {last.day}). It's $199 a "
        "month plus NJ sales tax for every sale in one county, or $39 plus NJ sales tax for a single property, with no "
        "commission or per-deal fee. Reply \"start\" and I'll set it up.")
    from engine import human_copy as hc
    assert hc.lint(ob.sample_body) == [] and 2 <= len(re.split(r"(?<=[.?!])\s+", ob.sample_body)) <= 4


def test_union_target_gets_the_union_sheet_and_small_counties_are_not_pitched(db, tmp_path):
    _desk(db)
    _target(db, "deals@flipco.example", kind="flipper", county="Union")
    res = s11.STREAM.run(db, _ctx(tmp_path))
    assert res.outbound == [] and res.metrics["targets_no_sheet"] == 1     # 2 Union sales: under min_rows_to_pitch
    _sale(db, 11, **UNION)
    [ob] = s11.STREAM.run(db, _ctx(tmp_path)).outbound
    _plain_touch(ob.subject, ob.body)
    d = TODAY + timedelta(days=5)
    assert f"in Elizabeth is set for sheriff sale on {d:%b} {d.day}, one of 3 Union County sales" in ob.body
    assert ob.subject.startswith("[Union] sheriff sale") and "Union County bid sheet" in ob.sample_body


def test_greeting_statewide_cap_and_stable_keys(db, tmp_path):
    from engine import firm_outbox as fo
    _desk(db)
    named = _target(db, "pat@bidder.example", county=None, name="Pat Sample", score=3)
    _target(db, "two@bidder.example", county="statewide", score=2)
    _target(db, "three@bidder.example", county="Essex", score=1)
    res = s11.STREAM.run(db, _ctx(tmp_path, pitches_per_run=2))
    assert [o.to for o in res.outbound] == ["pat@bidder.example", "two@bidder.example"]
    first = res.outbound[0]
    assert first.body.startswith("Hi Pat,\n\n") and "Essex County" in first.body      # statewide: the biggest sheet
    again = s11.STREAM.run(db, _ctx(tmp_path, pitches_per_run=2))
    assert [o.key for o in again.outbound] == [o.key for o in res.outbound]
    assert fo.enqueue(db, first, NOW) is not None
    held = db.query(FirmOutbox).filter_by(key=f"{first.key}:sample").one()
    assert held.status == "held" and held.attachments_json[0]["name"].endswith(".sample.pdf")
    assert held.body.count("plus NJ sales tax") == 2
    third = s11.STREAM.run(db, _ctx(tmp_path, pitches_per_run=2))
    assert named.id not in [o.target_id for o in third.outbound]           # its key is used and a pitch is queued


def test_weekend_and_dry_run_propose_nothing_but_write_sheets(db, tmp_path):
    _desk(db)
    _target(db, "office@bidder.example")
    saturday = NOW + timedelta(days=(5 - NOW.weekday()) % 7)
    res = s11.STREAM.run(db, _ctx(tmp_path, now=saturday, max_stale_days=30))
    assert res.outbound == [] and res.products >= 1 and "weekend: no pitches" in res.notes
    dry = s11.STREAM.run(db, _ctx(tmp_path, send=False))
    assert dry.outbound == [] and dry.products == 2


def test_featured_sale_skips_our_own_offers_and_imminent_sales(db, tmp_path):
    soon = _sale(db, 2)                                                   # inside feature_lead_days (3)
    offered = _sale(db, 5)
    offered.offer_amount = 300000                                         # a house we are negotiating: never named
    later = _sale(db, 6)
    for d in (9, 10, 11):
        _sale(db, d)
    _target(db, "office@bidder.example")
    db.flush()
    [ob] = s11.STREAM.run(db, _ctx(tmp_path)).outbound
    assert _has(ob.body, later) and not _has(ob.body, soon) and not _has(ob.body, offered)


def test_delivery_files_find_the_county_sheet(db, tmp_path):
    from engine.firm_outbox import delivery_files
    _desk(db)
    s11.STREAM.run(db, _ctx(tmp_path, send=False))
    got = delivery_files(tmp_path / "out", "Union")
    assert [p.name for p in got] == [f"s11_bid_sheet_union_{TODAY.isoformat()}.pdf"]


# -- syncing sheriff-sale buyers ------------------------------------------------------------------------------------
def _buyers(db):
    """A buyer family that took title by a sheriff's deed (Essex, but most of its purchases are in Union), one that
    never did, and a lender that took a house back."""
    def buy(addr, county, days):
        return {"address": addr, "county": county, "price": 300000, "class": "2",
                "deed_date": (TODAY - timedelta(days=days)).isoformat()}
    db.add_all([
        CashBuyer(entity_key="SAMPLE BIDS", entity_name="SAMPLE BIDS LLC", mailing_address="9 OFFICE PARK, CRANFORD, NJ 07016",
                  recent_purchases_json=[buy("1 UNION ST", "Union", 40), buy("2 UNION ST", "Union", 80),
                                         buy("3 ESSEX ST", "Essex", 120)]),
        CashBuyer(entity_key="RETAIL FLIPS", entity_name="RETAIL FLIPS LLC", mailing_address="7 OTHER RD, SUMMIT, NJ 07901",
                  recent_purchases_json=[buy("4 MAIN ST", "Union", 30)]),
        DeedTransfer(parcel_id="ESSEX-0714-77-1", county="Essex", property_location="3 ESSEX ST",
                     grantor="ESSEX COUNTY SHERIFF", grantee="SAMPLE BIDS LLC", sale_price=310000,
                     deed_date=TODAY - timedelta(days=120), nu_code="12", source="test"),
        DeedTransfer(parcel_id="ESSEX-0714-78-1", county="Essex", property_location="5 BANK ST",
                     grantor="ESSEX COUNTY SHERIFF", grantee="RETAIL FLIPS BANK NA", sale_price=1000,
                     deed_date=TODAY - timedelta(days=60), nu_code="12", source="test"),
        Prospect(key="SAMPLEBIDSLLC", entity_name="SAMPLE BIDS LLC", email="acq@samplebids.example",
                 source="samplebids.example/contact", purchases_36m=3),
        Prospect(key="RETAILFLIPSLLC", entity_name="RETAIL FLIPS LLC", email="hi@retailflips.example",
                 source="retailflips.example", purchases_36m=1),
    ])
    db.flush()


def test_sync_makes_sheriff_buyers_targets_pitched_where_they_bought_most(db, tmp_path):
    _buyers(db)
    got = s11.sync_targets(db, TODAY)
    assert got["families"] == 1 and got["synced"] == 1
    t = db.query(FirmTarget).filter_by(email="acq@samplebids.example").one()
    assert (t.kind, t.stream, t.county, t.stage) == ("sheriff_bidder", "s11", "Union", "NEW")
    assert t.data_json["sheriff_deeds"] == 1 and t.data_json["counties"][0] == "Union" and t.name is None
    assert db.query(FirmTarget).filter_by(email="hi@retailflips.example").count() == 0     # no sheriff's deed
    again = s11.sync_targets(db, TODAY)
    assert again["synced"] == 0 and again["refreshed"] == 1
    assert db.query(FirmTarget).filter_by(email="acq@samplebids.example").count() == 1


def test_sync_skips_an_email_another_stream_owns_and_a_dry_run_writes_nothing(db, tmp_path):
    _buyers(db)
    dry = s11.sync_targets(db, TODAY, write=False)
    assert dry["synced"] == 1 and db.query(FirmTarget).count() == 0
    other = _target(db, "acq@samplebids.example", kind="flipper", stream="s2", county=None)
    got = s11.sync_targets(db, TODAY)
    assert got["other_stream"] == 1 and got["synced"] == 0
    db.refresh(other)
    assert (other.kind, other.stream) == ("flipper", "s2")


def test_sync_matches_by_mailing_address_and_the_stream_pitches_the_synced_target(db, tmp_path):
    _buyers(db)
    db.query(DeedTransfer).filter_by(grantee="SAMPLE BIDS LLC").update(
        {"grantee": "SAMPLE BIDS II LLC", "grantee_address": "9 Office Park, Cranford, NJ 07016",
         "property_location": "99 ELSEWHERE AVE"})
    db.flush()
    assert s11.sync_targets(db, TODAY)["synced"] == 1
    for d in (5, 6, 9):
        _sale(db, d, **UNION)
    res = s11.STREAM.run(db, _ctx(tmp_path))
    assert res.metrics["targets_synced"] == 0 and res.metrics["targets_refreshed"] == 1
    [ob] = res.outbound
    assert ob.to == "acq@samplebids.example" and "Union County" in ob.body
    _plain_touch(ob.subject, ob.body)


# -- through the orchestrator ---------------------------------------------------------------------------------------
def test_orchestrator_queues_touch1_holds_the_sample_and_delivers_to_a_subscriber(db, tmp_path, monkeypatch):
    from db.models import FirmStreamRun
    from engine import swarm_firm
    monkeypatch.setitem(swarm_firm.STREAMS, "s11", ("engine.streams.s11_sheriff_bid_sheets", s11.LABEL))
    _desk(db)
    _sale(db, 11, **UNION)
    _target(db, "office@bidder.example", score=5)
    _target(db, "client@paying.example", county="Union", stage="PAYING")
    db.commit()
    out = swarm_firm.run_streams(now=NOW, out_dir=tmp_path / "firm", only=["s11"], force=True, send=True)
    assert out["s11"]["queued"] == 1 and out["s11"]["products"] == 2 and out["s11"]["delivered"] == 1
    db.expire_all()
    pitch = db.query(FirmOutbox).filter_by(stream="s11", kind="pitch").one()
    assert pitch.status == "queued" and pitch.attachments_json == [] and "Essex County bid sheet" in pitch.body
    held = db.query(FirmOutbox).filter_by(key=f"{pitch.key}:sample").one()
    assert held.status == "held" and held.attachments_json[0]["name"] == f"s11_bid_sheet_essex_{TODAY.isoformat()}.sample.pdf"
    sent = db.query(FirmOutbox).filter_by(stream="s11", kind="delivery").one()
    assert [a["name"] for a in sent.attachments_json] == [f"s11_bid_sheet_union_{TODAY.isoformat()}.pdf"]
    assert "Sheriff-sale bid sheets" in sent.body and sent.to_email == "client@paying.example"
    run = db.query(FirmStreamRun).filter_by(stream="s11").one()
    assert run.metrics_json["pdfs"] == 2 and run.metrics_json["pitches"] == 1
    assert all(isinstance(v, (int, float)) for v in run.metrics_json.values())
