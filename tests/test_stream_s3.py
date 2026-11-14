"""Stream 3: NJ sheriff-sale surplus research for foreclosure-surplus attorneys (flat-fee research, never a finder)."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import pytest
from pypdf import PdfReader

from db.models import DeedTransfer, FirmOutbox, FirmTarget, SignalType, utcnow
from engine.streams import s3_surplus_overages as s3
from engine.streams.base import StreamContext
from tests.factories import make_lead

TODAY = date.today()
OP = {"name": "Sam Operator", "first": "Sam", "phone": "(908) 555-0100", "email": "sam@example.com",
      "postal": "1 Main St, Iselin, NJ 08830", "company": "Sample Research"}


def _ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=datetime.combine(TODAY, time(10)), out_dir=tmp_path, operator=OP, send=send,
                         limits=limits)


def _sheriff(lead):
    return next(s for s in lead.property.signals if s.signal_type == SignalType.SHERIFF_SALE)


def _text(path) -> str:
    pages = PdfReader(str(path)).pages
    assert len(pages) == 1
    return pages[0].extract_text().replace("\n", " ")


def _target(db, email: str, county: str | None, *, stage: str = "NEW", score: float = 0, touches: int = 0):
    t = FirmTarget(kind=s3.TARGET_KIND, stream="s3", org=f"{email.split('@')[0]} law", email=email, county=county,
                   stage=stage, score=score, touches=touches)
    db.add(t)
    db.flush()
    return t


# -- pure classifiers and math ------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,expected", [
    ("Purchased - 3rd Party", True),
    ("Sold to Third Party Bidder", True),
    ("3rd Party Purchase", True),
    ("Struck off to a third party", True),
    ("Purchased", True),                                # a bare "Purchased" (not the plaintiff) counts
    ("Purchased - Plaintiff", False),
    ("Sold to Plaintiff", False),
    ("3rd Party Bidder Registration closes at 2 PM", False),
    ("No 3rd party bidders, sold to plaintiff", False),
    ("Purchase by 3rd party vacated", False),
    ("3rd Party sale set aside", False),
    ("Adjourned - Defendant", False),
    ("Scheduled", False),
    ("", False),
])
def test_third_party_sale_classifier(status, expected):
    assert s3.third_party_sale(status) is expected


def test_status_history_is_read_only_when_the_status_is_blank_or_a_bare_sold():
    hist = [["Scheduled", "1/8/2026"], ["Purchased - 3rd Party", "2/5/2026"]]
    assert s3.sold_to_third_party(None, hist)
    assert s3.sold_to_third_party("Sold", hist)
    assert not s3.sold_to_third_party("Scheduled", hist)               # an explicit newer status wins
    assert not s3.sold_to_third_party("Purchased - Plaintiff", hist)


def test_surplus_math_amounts_and_names():
    assert s3.surplus_estimate(480_000, 150_000, 10_000) == 320_000
    assert s3.surplus_estimate(140_000, 150_000, 0) == -10_000
    assert s3.surplus_estimate(None, 150_000) is None
    assert s3.parse_amount("$1,234,567.89") == 1_234_567.89 and s3.parse_amount("n/a") is None
    assert s3.sale_price_of({"winning_bid": "$455,000.00"}) == 455_000
    assert s3.equity_estimate(400_000, 150_000) == 250_000 and s3.equity_estimate(400_000, None) is None
    # a private plaintiff is never named; a party line keeps only its business parts
    assert s3.plaintiff_label("QUINN SAMPLE") == "(private party)"
    assert s3.plaintiff_label("QUINN SAMPLE, AND ALEX EXAMPLE AND SAMPLE LIVE LLC") == "SAMPLE LIVE LLC"
    assert s3.plaintiff_label("WELLS FARGO BANK, N.A.") == "WELLS FARGO BANK, N.A."
    assert s3.plaintiff_label("PENNYMAC LOAN SERVICES, LLC") == "PENNYMAC LOAN SERVICES, LLC"     # was "LLC"
    assert s3.plaintiff_label("TOWNSHIP OF IRVINGTON") == "TOWNSHIP OF IRVINGTON"                 # was "(private party)"
    # association / municipal plaintiffs are junior; a "National Association" lender is not
    assert s3.junior_plaintiff("SAMPLE HILL TOWNHOUSE ASSOCIATION, INC.")
    assert s3.junior_plaintiff("TOWNSHIP OF SAMPLE")
    assert not s3.junior_plaintiff("WELLS FARGO BANK, NATIONAL ASSOCIATION")
    assert not s3.junior_plaintiff("FEDERAL NATIONAL MORTGAGE ASSOCIATION")
    # a sheriff's deed to the plaintiff, its servicer or Fannie Mae is not a third-party sale
    assert s3.third_party_grantee("QUIET RIVER LLC", "BIG BANK NA")
    assert not s3.third_party_grantee("BIG BANK NA", "BIG BANK NA")
    assert not s3.third_party_grantee("FEDERAL NATIONAL MORTGAGE ASSOCIATION", "SAMPLE LOAN SERVICING LLC")
    assert not s3.third_party_grantee("SAMPLETON ACQUISITION TRUST", "US BANK AS TRUSTEE FOR SAMPLETON ACQUISITION TRUST")


# -- the watchlist ------------------------------------------------------------------------------------------------
def test_watchlist_ranks_by_equity_and_leaves_out_what_would_mislead(db, tmp_path):
    rich = make_lead(db, sheriff_days=20, upset=150_000)               # $400K as-is - $150K = $250K
    mid = make_lead(db, sheriff_days=30, upset=300_000)                # $100K
    thin = make_lead(db, sheriff_days=25, upset=380_000)               # $20K: below $75K
    far = make_lead(db, sheriff_days=90, upset=150_000)                # outside the 60-day window
    partial = make_lead(db, sheriff_days=10, upset=20_000)             # a partial / junior figure, not a payoff
    hoa = make_lead(db, sheriff_days=12, upset=150_000)
    _sheriff(hoa).plaintiff_or_lienholder = "SAMPLE HILL TOWNHOUSE ASSOCIATION, INC."
    liens = make_lead(db, sheriff_days=15, upset=250_000)
    _sheriff(liens).raw_metadata_json = {"adjournments_total": 2, "prior_liens": 90_000}   # $400K - $340K = $60K
    gone = make_lead(db, sheriff_days=14, upset=150_000)
    _sheriff(gone).status = "Cancelled"
    db.commit()
    db.expire_all()
    rows, stats = s3.collect(db, TODAY, {**s3.DEFAULTS})
    ids = [r.lead_id for r in rows]
    assert ids == [rich.id, mid.id]
    assert [r.amount for r in rows] == [250_000, 100_000]
    for lead in (thin, far, partial, hoa, liens, gone):
        assert lead.id not in ids
    assert stats["junior_lien_skipped"] == 1 and stats["partial_debt_skipped"] == 1
    assert stats["below_equity"] == 2
    r = rows[0]
    assert r.kind == "watch" and r.county == "Essex" and r.plaintiff == "BIG BANK NA" and r.value.basis == "as-is formula"


def test_a_capped_valuation_falls_back_to_the_equalized_assessment(db):
    lead = make_lead(db, sheriff_days=20, upset=150_000,
                     assumptions={"flags": ["OUTLIER_CAPPED: comp ARV far above the assessment"],
                                  "subject_equalized": 260_000})
    db.commit()
    db.expire_all()
    rows, _ = s3.collect(db, TODAY, {**s3.DEFAULTS})
    [r] = [x for x in rows if x.lead_id == lead.id]
    assert r.value.basis == "equalized assessment" and r.amount == 110_000


# -- sales already made to a third party --------------------------------------------------------------------------
def test_third_party_sales_list_without_a_price_and_compute_surplus_only_with_one(db, tmp_path):
    unpriced = make_lead(db, sheriff_days=-30, upset=150_000)
    priced = make_lead(db, sheriff_days=-40, upset=150_000)
    short = make_lead(db, sheriff_days=-50, upset=150_000)
    plaintiff = make_lead(db, sheriff_days=-20, upset=150_000)
    for lead, status, meta in ((unpriced, "Purchased - 3rd Party", {}),
                               (priced, "Purchased - 3rd Party", {"sale_price": "$480,000.00", "prior_liens": 10_000}),
                               (short, "Purchased - 3rd Party", {"sale_price": 149_000}),
                               (plaintiff, "Purchased - Plaintiff", {"sale_price": 100})):
        sig = _sheriff(lead)
        sig.status, sig.active = status, False
        sig.raw_metadata_json = {"adjournments_total": 1, **meta}
    db.commit()
    db.expire_all()
    res = s3.STREAM.run(db, _ctx(tmp_path))
    assert res.metrics["third_party_sold"] == 3 and res.metrics["sold_without_price"] == 1
    assert res.metrics["sold_no_surplus"] == 1 and res.metrics["surplus_priced"] == 1
    assert res.metrics["watchlist"] == 0 and res.products == 1
    rows, _ = s3.collect(db, TODAY, {**s3.DEFAULTS})
    first, second = rows
    assert first.lead_id == priced.id and first.amount == 320_000 and first.price_source == "sale notice"
    assert second.lead_id == unpriced.id and second.sale_price is None
    text = _text(tmp_path / f"s3_surplus_essex_{s3.week_tag(_ctx(tmp_path).now)}.pdf")
    assert "~$320K surplus" in text and "Surplus if the bid beat the payoff" in text
    assert "sheriff's deed or OPRA" in text


def test_a_recorded_sheriffs_deed_to_a_third_party_marks_the_sale_and_gives_the_bid(db):
    sold = make_lead(db, sheriff_days=-30, upset=150_000)
    back = make_lead(db, sheriff_days=-30, upset=150_000)
    for lead, grantee, price in ((sold, "QUIET RIVER LLC", 410_000), (back, "BIG BANK NA", 100)):
        sig = _sheriff(lead)
        sig.active = False
        db.add(DeedTransfer(parcel_id=lead.property.parcel_id, county="Essex", grantor="SHERIFF OF ESSEX COUNTY",
                            grantee=grantee, sale_price=price, deed_date=TODAY - timedelta(days=10), nu_code="12",
                            source="test"))
    db.commit()
    db.expire_all()
    rows, stats = s3.collect(db, TODAY, {**s3.DEFAULTS})
    [r] = rows
    assert r.lead_id == sold.id and r.kind == "sold" and r.price_source == "sheriff's deed"
    assert r.sale_price == 410_000 and r.amount == 260_000 and stats["sold_by_deed"] == 1


# -- the product --------------------------------------------------------------------------------------------------
def test_the_county_dossier_is_one_page_names_no_defendant_and_carries_the_law_and_footer(db, tmp_path):
    long_plaintiff = ("THE BANK OF NEW YORK MELLON FKA THE BANK OF NEW YORK AS TRUSTEE FOR THE CERTIFICATEHOLDERS "
                      "CWABS, INC., ASSET BACKED CERTIFICATES, SERIES 2005-BC1")
    for i in range(16):
        lead = make_lead(db, sheriff_days=5 + i, upset=100_000 + 5_000 * i)
        sig = _sheriff(lead)
        sig.plaintiff_or_lienholder = long_plaintiff
        sig.status = "Adjourned - Defendant - New Sale Date: 12/01/2026"
    db.commit()
    db.expire_all()
    res = s3.STREAM.run(db, _ctx(tmp_path))
    assert res.found == 16 and res.products == 1
    assert res.metrics["rows_shown"] <= s3.DEFAULTS["max_rows_per_pdf"]
    text = _text(tmp_path / f"s3_surplus_essex_{s3.week_tag(_ctx(tmp_path).now)}.pdf")
    assert "QUINN" not in text                                          # the defendant, never
    for must in ("NJ Sheriff-Sale Surplus Research: Essex County", "For attorneys only", "not an appraisal",
                 "N.J.S.A. 2A:50-37", "R. 4:64-3", "46:30B-41", "46:30B-106", "We are not finders", "$400/month",
                 "$150 for a single county report", "eCourts", "1 Main St, Iselin", "no more"):
        assert must in text, must
    # counts only in the public run log
    assert all(isinstance(v, (int, float)) for v in res.metrics.values())
    assert not any("Test St" in n or "QUINN" in n for n in res.notes)


def test_a_suppressed_address_never_reaches_the_dossier(db, tmp_path):
    from engine.compliance import suppress_address
    keep = make_lead(db, sheriff_days=20, upset=150_000)
    hide = make_lead(db, sheriff_days=21, upset=150_000)
    p = hide.property
    suppress_address(db, f"{p.street_address}, {p.city} NJ {p.zip_code}", source="daniels_law")
    db.commit()
    db.expire_all()
    res = s3.STREAM.run(db, _ctx(tmp_path))
    assert res.metrics["suppressed"] == 1 and res.found == 1
    text = _text(tmp_path / f"s3_surplus_essex_{s3.week_tag(_ctx(tmp_path).now)}.pdf")
    from core.text import postal_title
    assert postal_title(keep.property.street_address) in text
    assert postal_title(p.street_address) not in text


# -- pitches: the 2-step funnel (Touch 1 plain, the masked sample on a yes) ----------------------------------------
def _head(body: str) -> str:
    """The text above the sign-off, without the greeting line and the operator's first name."""
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    return " ".join(ln for ln in lines if not re.fullmatch(r"(Hello|Hi \S+),", ln) and ln != OP["first"])


def _sentences(body: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.?!])\s+", _head(body)) if x]


def test_touch1_is_two_plain_sentences_with_one_case_and_no_attachment(db, tmp_path):
    from engine import human_copy as hc
    lead = make_lead(db, sheriff_days=20, upset=150_000)
    essex = _target(db, "intake@essexlaw.example", "Essex County", score=5)
    _target(db, "office@unionlaw.example", "Union")                     # no Union report this week
    statewide = _target(db, "desk@statewide.example", None, score=1)
    _target(db, "used@law.example", "Essex", stage="CONTACTED", touches=2)
    _target(db, "gone@law.example", "Essex", stage="LOST", score=9)
    db.commit()
    db.expire_all()
    res = s3.STREAM.run(db, _ctx(tmp_path, pitches_per_run=5))
    tag = s3.week_tag(_ctx(tmp_path).now)
    assert [o.key for o in res.outbound] == [f"s3:pitch:{essex.id}:{tag}", f"s3:pitch:{statewide.id}:{tag}"]
    assert res.metrics["targets_matched"] == 3 and res.metrics["pitches"] == 2 and res.metrics["followups"] == 0
    from core.text import postal_title
    street = postal_title(lead.property.street_address)
    sheriff = _sheriff(lead).source_ref.split(":", 1)[1]
    report = tmp_path / f"s3_surplus_essex_{tag}.pdf"
    for o in res.outbound:
        assert o.stream == "s3" and o.kind == "pitch" and o.commercial and o.priority == 3
        assert o.attachments == [] and o.in_reply_to is None             # Touch 1: never an attachment
        assert o.subject == f"{street}, Essex County" and not o.subject.lower().startswith("re:")
        assert o.body.startswith("Hello,\n\n") and o.body.rstrip().endswith("\nSam")
        assert len(_sentences(o.body)) == 2, _sentences(o.body)
        assert hc.word_count(_head(o.body)) <= 45
        assert f"including {street} (sheriff # {sheriff})" in o.body
        assert "1 upcoming Essex County sheriff sale," in o.body       # the true count, and only what the rows are
        assert o.body.rstrip().splitlines()[-3].endswith("e-mailed it over?")
        assert hc.lint(o.body) == [], hc.lint(o.body)
        low = o.body.lower()
        for bad in ("$", "quinn", "referral", "client", "opportunity", "guarantee", "excited", "free", "http",
                    "attached", "a month"):
            assert bad not in low, bad
        assert "!" not in o.subject and "free" not in o.subject.lower()
        # Touch 2 waits: a 1-row report has nothing to mask, so the report itself is the sample
        assert o.sample_attachments == [report] and report.exists()
        for must in ("$400 a month flat", "$150 for one county report",
                     "never a percentage of any recovery or tied to a case", 'Reply "start" and I\'ll set it up.',
                     "eCourts"):
            assert must in o.sample_body, must
        assert hc.lint(o.sample_body) == []
    # the same week proposes the same keys; a key already in the outbox is not proposed again
    again = s3.STREAM.run(db, _ctx(tmp_path, pitches_per_run=5))
    assert [o.key for o in again.outbound] == [o.key for o in res.outbound]
    db.add(FirmOutbox(key=res.outbound[0].key, stream="s3", to_email=essex.email, subject="x", body="x"))
    db.commit()
    third = s3.STREAM.run(db, _ctx(tmp_path, pitches_per_run=5))
    assert [o.key for o in third.outbound] == [res.outbound[1].key]
    # at most pitches_per_run, and nothing at all on a dry run
    assert len(s3.STREAM.run(db, _ctx(tmp_path, pitches_per_run=1)).outbound) == 1
    assert s3.STREAM.run(db, _ctx(tmp_path, send=False)).outbound == []


def test_the_sample_shows_two_cases_in_full_and_hides_street_and_docket_on_the_rest(db, tmp_path):
    from core.text import postal_title
    from engine import firm_outbox as fo
    from engine.streams.base import sample_path
    leads = [make_lead(db, sheriff_days=10 + i, upset=150_000 + 10_000 * i) for i in range(5)]
    t = _target(db, "intake@essexlaw.example", "Essex")
    db.commit()
    db.expire_all()
    res = s3.STREAM.run(db, _ctx(tmp_path))
    [o] = res.outbound
    report = tmp_path / f"s3_surplus_essex_{s3.week_tag(_ctx(tmp_path).now)}.pdf"
    assert o.sample_attachments == [sample_path(report)] and sample_path(report).exists()
    assert res.metrics["samples"] == 1
    assert "5 upcoming Essex County sheriff sales" in o.body
    assert "the first 2 cases in full, the rest with the street and docket hidden" in o.sample_body
    rows, _ = s3.collect(db, TODAY, {**s3.DEFAULTS})
    assert f"including {rows[0].street} (sheriff # {rows[0].sheriff_no})" in o.body   # the first row: shown in full
    assert o.subject == f"{rows[0].street} + 4 more in Essex County"
    full, sample = _text(report), _text(sample_path(report))
    for r in rows[:2]:
        assert r.street in sample and f"Sh. #{r.sheriff_no}" in sample
    for r in rows[2:]:
        assert r.street in full and r.street not in sample                       # rows 3+: street hidden
        assert r.docket in full and r.docket not in sample                       # ... and the docket
        assert f"Sh. #{r.sheriff_no}" in full and f"Sh. #{r.sheriff_no}" not in sample
    assert "Sample: 2 of 5 rows in full" in sample and "QUINN" not in sample
    assert {postal_title(x.property.street_address) for x in leads} == {r.street for r in rows}
    # through the outbox: the cold row carries no file; the masked sample waits for a yes
    row = fo.enqueue(db, o)
    held = db.query(FirmOutbox).filter_by(key=f"{o.key}:sample").one()
    assert row.status == "queued" and row.attachments_json == []
    assert held.status == "held" and [a["name"] for a in held.attachments_json] == [sample_path(report).name]
    assert held.body == o.sample_body and held.target_id == t.id


def test_a_target_in_a_sequence_gets_no_second_touch_from_the_stream(db, tmp_path):
    """2026-11-10 (the B2B playbook): the stream proposes Touch 1 only; the outbox's queue_nudges sends the nudge and
    the close-the-loop note in the first touch's thread."""
    make_lead(db, sheriff_days=20, upset=150_000)
    now = _ctx(tmp_path).now
    t = _target(db, "intake@essexlaw.example", "Essex", stage="CONTACTED", touches=1)
    t.last_contact_at = now - timedelta(days=8)
    fresh = _target(db, "new@essexlaw.example", "Essex")
    db.commit()
    db.expire_all()
    res = s3.STREAM.run(db, _ctx(tmp_path))
    assert [(o.target_id, o.kind) for o in res.outbound] == [(fresh.id, "pitch")]


def test_a_statewide_attorney_hears_about_the_two_largest_counties_in_plain_grammar(db, tmp_path):
    from engine import human_copy as hc
    from engine.streams.base import sample_path
    for i in range(3):
        make_lead(db, sheriff_days=10 + i, upset=150_000)
    for i in range(4):
        make_lead(db, sheriff_days=12 + i, upset=150_000, county="Union", municipality="ELIZABETH CITY",
                  mun_code="2004", city="Elizabeth", zip_code="07201")
    make_lead(db, sheriff_days=14, upset=150_000, county="Hudson", municipality="JERSEY CITY", mun_code="0906",
              city="Jersey City", zip_code="07304")
    _target(db, "desk@statewide.example", "statewide")
    db.commit()
    db.expire_all()
    res = s3.STREAM.run(db, _ctx(tmp_path))
    [o] = res.outbound
    tag = s3.week_tag(_ctx(tmp_path).now)
    assert "for 7 upcoming sheriff sales in Union and Essex counties, including " in o.body
    assert o.subject.endswith(" + 6 more in Union and Essex counties") and len(_sentences(o.body)) == 2
    assert hc.word_count(_head(o.body)) <= 45 and hc.lint(o.body) == [] and o.attachments == []
    assert o.sample_attachments == [sample_path(tmp_path / f"s3_surplus_union_{tag}.pdf"),
                                    sample_path(tmp_path / f"s3_surplus_essex_{tag}.pdf")]
    assert o.sample_body.startswith("Here are this week's sheets for Union and Essex counties, attached: the first 2 "
                                    "cases of each in full")
    assert s3.followup_body([s3.CountyReport("Union", tmp_path / "u.pdf", []),
                             s3.CountyReport("Essex", tmp_path / "e.pdf", [])], "Sam") == (
        "Hello,\n\nShould I send this week's sheets for Union and Essex counties over?\n\nSam")


def test_an_empty_database_runs_clean(db, tmp_path):
    _target(db, "intake@essexlaw.example", "Essex")
    res = s3.STREAM.run(db, _ctx(tmp_path))
    assert (res.processed, res.found, res.products, res.outbound) == (0, 0, 0, [])
    assert not list(tmp_path.glob("*.pdf"))
    assert s3.STREAM.key == "s3" and s3.STREAM.name == "Surplus research" and s3.STREAM.cadence_hours == 24
