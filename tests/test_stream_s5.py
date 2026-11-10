"""Stream 5: the hard-money borrower feed (engine/streams/s5_hard_money_feed.py)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from engine.streams import s5_hard_money_feed as s5
from engine.streams.base import StreamContext, sample_path
from tests.factories import make_lead

OP = {"name": "Sam Operator", "first": "Sam", "phone": "(973) 555-0199", "email": "sam@example.com",
      "postal": "1 Main St, Newark, NJ 07102"}


def _ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=datetime.now().replace(microsecond=0), out_dir=tmp_path / "out", operator=OP, send=send,
                         limits=limits)


def _text(path) -> str:
    """The PDF's text. ReportLab writes the mask's bullet as WinAnsi code 0x7F (drawn as a bullet), which pypdf
    extracts as "\\x7f": read it back as the bullet it is."""
    from pypdf import PdfReader
    return "\n".join(p.extract_text() for p in PdfReader(str(path)).pages).replace("\x7f", "•")


def _pages(path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(path)).pages)


def _sentences(body: str) -> list[str]:
    """The sentences above the sign-off: the "Hello," line and the operator's first name don't count."""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if lines and re.fullmatch(r"(?i)(hi|hello)\b[^.?!]*,", lines[0]):
        lines = lines[1:]
    if lines and lines[-1] == OP["first"]:
        lines = lines[:-1]
    return [x for x in re.split(r"(?<=[.?!])\s+", " ".join(lines)) if x]


def _buy(street: str, days: int, price: int, *, block: str = "100", lot: str = "1", units: int = 1,
         mun: str = "0714", muni: str = "NEWARK CITY", county: str = "Essex") -> dict:
    return {"address": street, "municipality": muni, "county": county, "price": price, "class": "2", "units": units,
            "deed_date": (date.today() - timedelta(days=days)).isoformat(), "block": block, "lot": lot, "mun_code": mun}


def _buyer(db, name: str, mailing: str, buys: list[dict]):
    from db.models import CashBuyer
    b = CashBuyer(entity_key=re.sub(r"\W", "", name.upper()), entity_name=name, mailing_address=mailing,
                  recent_purchases_json=buys, cash_purchases_count=len(buys))
    db.add(b)
    db.flush()
    return b


# -- pure math ----------------------------------------------------------------------------------------------------
def test_activity_score_rewards_volume_then_loan_need_then_recency():
    assert s5.activity_score(10, 2, 1, 3.0) == 30 + 8 + 5 + 5.25
    assert s5.activity_score(40, 0, 0, None) == 75                       # volume capped at 25 purchases
    assert s5.activity_score(5, 0, 0, 48.0) == 15                        # recency fades to nothing after 2 years
    assert s5.activity_score(5, 1, 0, 6) > s5.activity_score(5, 0, 0, 6)  # a house in rehab = a loan need now
    assert s5.activity_score(5, 0, 1, 6) > s5.activity_score(5, 1, 0, 6)  # a resale listed = payoff / refinance


def test_small_helpers():
    today = date(2026, 11, 9)
    assert s5.block_lot("0031") == "31" and s5.block_lot("31.00") == "31" and s5.block_lot("322.61") == "322.61"
    buys = [{"deed_date": "2026-10-01", "mun_code": "0714", "block": "0500", "lot": "1"},
            {"deed_date": "2026-03-01", "mun_code": "0714", "block": "9", "lot": "2"},
            {"deed_date": "2025-01-01"}, {"deed_date": "not a date"}]
    assert s5.in_progress_count(buys, today, 9) == 2 and s5.in_progress_count(buys, today, 3) == 1
    listed = {("0714", "500", "1"), ("0714", "77", "7")}
    assert s5.resale_parcels(buys, listed) == {("0714", "500", "1")}
    assert len(s5.resale_parcels(buys, listed, {("0714", "500", "1")})) == 1   # found twice, counted once
    assert s5.band(250_000, 400_000) == "$250K-$400K" and s5.band(None, 1) == "-"
    assert s5.month_label("2026-08-11") == "Aug 2026"
    assert s5.is_business_name("ACME FLIP HOMES LLC")
    assert not s5.is_business_name("QUINN Q SAMPLE") and not s5.is_business_name("Owner @ 9 ASSOC PL, NEWARK, NJ")
    assert not s5.is_business_name("QUINN Q SAMPLE LIVING TRUST") and not s5.is_business_name("SAMPLE FAMILY TRUST")
    assert s5.is_business_name("SAMPLE TRUST HOMES LLC")


# -- the 2-step funnel copy ---------------------------------------------------------------------------------------
def _named(entity="ACME FLIP HOMES LLC", street="12 Main St", town="Newark", bought=date(2026, 10, 2)) -> s5.Named:
    b = s5.Borrower(entity=entity, purchases_36m=4, last_purchase=bought.isoformat(), towns=[town], counties=["Essex"],
                    price_lo=None, price_hi=None, multi_share=0.0, in_progress=1, resales=0)
    return s5.Named(b, street, town, bought)


def _plain_touch(subject: str, body: str) -> None:
    """What every Touch 1 obeys: 2 plain sentences, <= 60 words, a yes/no question, no price / link / hype."""
    from engine import human_copy as hc
    sents = _sentences(body)
    assert len(sents) == 2 and sents[1].endswith("?"), sents
    assert body.splitlines()[0] == "Hello," and body.splitlines()[-1] == "Sam"
    assert hc.lint(body, max_words=60) == [] and hc.word_count(body) <= 60, (hc.lint(body, max_words=60), body)
    low = (subject + " " + body).lower()
    for bad in ("$", "http", "www.", "free", "!", "attached", "subscription", "ad:", "referral", "guarantee",
                "a month", "distressed", "borrower"):             # we know they buy houses; not that they borrow
        assert bad not in low, bad
    assert len(subject) <= 70 and not subject.lower().startswith("re:")


def test_touch1_two_plain_sentences_name_one_company_and_one_house():
    subject, body = s5.touch1(40, _named(), date(2026, 11, 10), "Sam")
    _plain_touch(subject, body)
    assert _sentences(body) == [
        ("From public deed records, we track the 40 most active investor companies buying NJ houses, including Acme "
         "Flip Homes LLC, which bought 12 Main St in Newark in October."), "Want me to send the 1-page list?"]
    assert subject == "Acme Flip Homes LLC + 39 more NJ investor companies"
    _, older = s5.touch1(40, _named(bought=date(2025, 12, 5)), date(2026, 1, 8), "Sam")
    assert "in Newark in December 2025." in older                         # never a month that reads as this year
    long_name = _named(entity="NORTH JERSEY RENOVATION AND HOLDINGS GROUP 12 LLC", street="1025-27 Galloping Hill Rd",
                       town="Kenilworth")
    subject, body = s5.touch1(40, long_name, date(2026, 11, 10), "Sam")
    _plain_touch(subject, body)                                           # still 2 sentences, <= 60 words
    assert subject == "North Jersey Renovation And Holdings Group 12 LLC + 39 more"   # the long line falls back


def test_followup_and_sample_copy():
    from engine import human_copy as hc
    assert _sentences(s5.followup_body("Sam")) == ["Should I send this week's 1-page list over?"]
    sb = s5.sample_body(750, True)
    assert sb == ("Here's this week's feed, attached: the top 2 companies in full, the rest with the name hidden. The "
                  "weekly feed is $750 a month flat, a data retainer with no per-loan fee or commission; it is not a "
                  "consumer or credit report. Reply \"start\" and I'll set it up.")
    assert hc.lint(sb) == [] and 2 <= len(re.split(r"(?<=[.?!])\s+", sb)) <= 4
    plain = s5.sample_body(750, False)
    assert "hidden" not in plain and "$750 a month flat" in plain and "not a consumer or credit report" in plain


# -- the feed on a database ---------------------------------------------------------------------------------------
def _families(db):
    from engine.compliance import suppress_address
    listed = make_lead(db, sheriff_days=None, stale_listing=True, mun_code="0714", block="500", lot="1")
    acme = _buyer(db, "ACME FLIP HOMES LLC", "1 OFFICE PARK, NEWARK, NJ 07102", [
        _buy("1 A ST", 30, 210_000, block="500", lot="1"), _buy("2 A ST", 100, 260_000, units=2, lot="2"),
        _buy("3 A ST", 400, 300_000, lot="3"), _buy("4 A ST", 700, 330_000, lot="4")])
    _buyer(db, "BETA PROPERTIES LLC", "2 BETA PLAZA, NEWARK, NJ 07102", [
        _buy("5 B ST", 500, 400_000, lot="5"), _buy("6 B ST", 600, 410_000, lot="6"), _buy("7 B ST", 650, 420_000, lot="7")])
    _buyer(db, "QUINN Q SAMPLE", "3 HOME LN, NEWARK, NJ 07102", [_buy(f"{i} Q ST", 40 + i, 200_000 + i) for i in range(5)])
    _buyer(db, "Owner @ 9 ELM ST, NEWARK, NJ", "9 ELM ST, NEWARK, NJ 07102",
           [_buy(f"{i} C ST", 40 + i, 300_000 + i) for i in range(5)])
    _buyer(db, "ONE DEAL LLC", "4 SOLO RD, NEWARK, NJ 07102", [_buy("8 D ST", 50, 250_000)])
    _buyer(db, "HIDDEN HOLDINGS LLC", "5 QUIET CT, NEWARK, NJ 07102",
           [_buy("9 H ST", 50, 250_000), _buy("10 H ST", 60, 260_000), _buy("11 H ST", 70, 270_000)])
    suppress_address(db, "5 QUIET CT, NEWARK, NJ 07102", "daniels_law")
    return acme, listed


def test_feed_entities_only_in_progress_resale_and_ranking(db):
    from collections import Counter
    acme, _ = _families(db)
    counts: Counter[str] = Counter()
    feed = s5.build_feed(db, date.today(), counts=counts)
    assert [b.entity for b in feed] == ["ACME FLIP HOMES LLC", "BETA PROPERTIES LLC"]
    a = feed[0]
    assert a.purchases_36m == 4 and a.in_progress == 2 and a.resales == 1 and a.resale_listed
    assert a.towns == ["Newark"] and 0.2 < a.multi_share < 0.3 and a.last_purchase == (
        date.today() - timedelta(days=30)).isoformat()
    assert feed[1].in_progress == 0 and not feed[1].resale_listed
    assert counts["not_entity"] == 2 and counts["too_few"] == 1 and counts["suppressed"] == 1 and counts["qualified"] == 2


def test_stream_pdf_privacy_and_pitches(db, tmp_path):
    from db.models import FirmTarget
    _families(db)
    lender = FirmTarget(kind="hard_money_lender", stream="s5", org="Fast Capital LLC", email="loans@fastcap.example",
                        stage="NEW", score=5)
    broker = FirmTarget(kind="mortgage_broker", stream="s5", org="Broker Co", email="desk@brokerco.example",
                        stage="CONTACTED", score=4)
    db.add_all([lender, broker,
                FirmTarget(kind="hard_money_lender", stream="s5", email="gone@lost.example", stage="LOST", score=9),
                FirmTarget(kind="roofer", stream="s4", email="roof@roof.example", stage="NEW", score=8)])
    db.flush()
    res = s5.STREAM.run(db, _ctx(tmp_path, min_rows=1))
    assert res.products == 1 and res.found == 2 and res.metrics["feed_rows"] == 2
    assert res.metrics["resale_listed"] == 1 and res.metrics["in_progress_total"] == 2
    pdf = next((tmp_path / "out").glob("s5_borrower_feed_*.pdf"))
    assert _pages(pdf) == 1
    text = _text(pdf)
    assert "NJ Active Fix-and-Flip Borrower Feed" in text and "ACME FLIP HOMES LLC" in text and "Newark" in text
    for secret in ("QUINN", "Owner @", "HIDDEN", "OFFICE PARK", "Office Park", "BETA PLAZA", "1 A St", "1 A ST",
                   "ONE DEAL"):
        assert secret not in text, secret
    assert "not a consumer or credit report" in text and "no more" in text
    blob = repr(res.metrics) + repr(res.notes)
    assert "ACME" not in blob and "Newark" not in blob
    y, w, _ = datetime.now().isocalendar()
    assert [(o.target_id, o.key) for o in res.outbound] == [(lender.id, f"s5:pitch:{lender.id}:{y}-W{w}"),
                                                           (broker.id, f"s5:pitch:{broker.id}:{y}-W{w}")]
    ob = res.outbound[0]
    assert ob.priority == 3 and ob.kind == "pitch" and ob.to == "loans@fastcap.example" and ob.in_reply_to is None
    # Touch 1: no attachment; the top company by name and its newest house (a public deed), never a person
    assert ob.attachments == []
    _plain_touch(ob.subject, ob.body)
    month = s5.month_phrase(date.today() - timedelta(days=30), date.today())
    assert _sentences(ob.body)[0] == ("From public deed records, we track the 2 most active investor companies buying "
                                      f"NJ houses, including Acme Flip Homes LLC, which bought 1 A St in Newark {month}.")
    assert ob.subject == "Acme Flip Homes LLC + 1 more NJ investor company"
    # two companies: no masked sample is written, so the held sample is the feed itself and says nothing is hidden
    assert not sample_path(pdf).exists() and ob.sample_attachments == [pdf]
    assert ob.sample_body.startswith("Here's this week's feed, attached. The weekly feed is $750 a month flat")
    for o in res.outbound:
        for secret in ("Quinn", "QUINN", "Owner @", "Hidden", "HIDDEN", "Office Park", "OFFICE PARK", "One Deal"):
            assert secret not in o.subject + o.body, secret


def test_sample_hides_names_after_row_two_and_touch1_never_names_a_person(db, tmp_path):
    from db.models import FirmTarget
    from engine.compliance import suppress_address
    _families(db)
    _buyer(db, "GAMMA HOMES LLC", "6 GAMMA WAY, NEWARK, NJ 07102",
           [_buy("12 G ST", 20, 300_000, lot="12"), _buy("13 G ST", 25, 310_000, lot="13")])
    _buyer(db, "DELTA RENTALS LLC", "8 DELTA DR, PATERSON, NJ 07501",
           [_buy(f"{20 + i} D ST", 15 + 10 * i, 280_000 + i, lot=f"2{i}", mun="1608", muni="PATERSON CITY",
                 county="Passaic") for i in range(3)])
    _buyer(db, "QUINN Q SAMPLE REVOCABLE TRUST", "7 HOME CT, NEWARK, NJ 07102",       # a person's trust: never named
           [_buy(f"{30 + i} T ST", 10 + i, 350_000 + i, lot=f"3{i}") for i in range(6)])
    suppress_address(db, "1 A ST, Newark NJ", "daniels_law")             # ACME's newest house: the next one is named
    anywhere = FirmTarget(kind="hard_money_lender", stream="s5", email="loans@anywhere.example", stage="NEW", score=5)
    passaic = FirmTarget(kind="mortgage_broker", stream="s5", email="desk@passaic.example", county="Passaic",
                         stage="NEW", score=4)
    db.add_all([anywhere, passaic])
    db.flush()
    res = s5.STREAM.run(db, _ctx(tmp_path, min_rows=1))
    assert res.metrics["feed_rows"] == 4 and res.metrics["pitches"] == 2  # ACME, DELTA, GAMMA, BETA; never the trust
    pdf = next((tmp_path / "out").glob("s5_borrower_feed_*.pdf"))
    report, sample = _text(pdf), _text(sample_path(pdf))
    for name in ("ACME FLIP HOMES LLC", "DELTA RENTALS LLC", "GAMMA HOMES LLC", "BETA PROPERTIES LLC"):
        assert name in report, name
    assert "QUINN" not in report and _pages(sample_path(pdf)) == 1
    # the sample: the top 2 companies in full, the rest with the name hidden (the identifying column is "Entity")
    assert "ACME FLIP HOMES LLC" in sample and "DELTA RENTALS LLC" in sample
    assert "GAMMA" not in sample and "BETA" not in sample
    assert "G•••• H•••• LLC" in sample and "B••• P" in sample
    flat = " ".join(sample.split())
    assert "Feed (sample)" in flat and "Sample: the top 2 of 4 companies in full, the rest with the name hidden" in flat
    assert "street hidden" not in flat and "Newark" in sample and "Paterson" in sample   # the towns stay readable
    by = {o.to: o for o in res.outbound}
    one, two = by["loans@anywhere.example"], by["desk@passaic.example"]
    assert one.sample_attachments == [sample_path(pdf)] and one.attachments == []
    assert "the top 2 companies in full, the rest with the name hidden" in one.sample_body
    m2 = s5.month_phrase(date.today() - timedelta(days=100), date.today())
    assert f"including Acme Flip Homes LLC, which bought 2 A St in Newark {m2}." in one.body and "1 A St" not in one.body
    md = s5.month_phrase(date.today() - timedelta(days=15), date.today())
    assert f"including Delta Rentals LLC, which bought 20 D St in Paterson {md}." in two.body   # the lender's county
    for o in res.outbound:
        _plain_touch(o.subject, o.body)
        assert "quinn" not in (o.subject + o.body).lower() and "trust" not in (o.subject + o.body).lower()


def test_touch1_prefers_a_house_bought_this_year_over_an_old_one_in_the_lenders_county(db):
    omega = [_buy("40 O ST", 30, 300_000, lot="40"), _buy("41 O ST", 60, 310_000, lot="41"),
             _buy("42 O ST", 500, 320_000, lot="42", mun="1608", muni="PATERSON CITY", county="Passaic")]
    _buyer(db, "OMEGA HOMES LLC", "9 OMEGA RD, NEWARK, NJ 07102", omega)
    feed = s5.build_feed(db, date.today())
    named = s5.pick_named(db, feed, date.today(), "Passaic")
    assert (named.borrower.entity, named.street, named.town) == ("OMEGA HOMES LLC", "40 O St", "Newark")
    old_only = [s5.Borrower(entity="OMEGA HOMES LLC", purchases_36m=2, last_purchase=None, towns=[], counties=[],
                            price_lo=None, price_hi=None, multi_share=0.0, in_progress=0, resales=0,
                            buys=[_buy("43 O ST", 400, 1, lot="43"), omega[2]])]
    assert s5.pick_named(db, old_only, date.today(), "Passaic").street == "42 O St"   # nothing recent: the county's own
    # a county deed export row carries no town: the recorded deed's parcel id (COUNTY-MUN-BLOCK-LOT) supplies it
    from db.models import DeedTransfer
    make_lead(db, sheriff_days=None, verdict=None, mun_code="0705", municipality="EAST ORANGE CITY", block="7",
              lot="45", parcel_id="ESSEX-0705-7-45", street_address="45 O ST", city="East Orange")
    day = date.today() - timedelta(days=9)
    bare = {"address": "45 O ST", "county": "Essex", "price": 300_000, "deed_date": day.isoformat()}
    db.add(DeedTransfer(parcel_id="ESSEX-0705-7-45", property_location="45 O ST", deed_date=day,
                        grantee="OMEGA HOMES LLC", source="test"))
    db.flush()
    fam = [s5.Borrower(entity="OMEGA HOMES LLC", purchases_36m=2, last_purchase=None, towns=[], counties=[],
                       price_lo=None, price_hi=None, multi_share=0.0, in_progress=0, resales=0, buys=[bare])]
    got = s5.pick_named(db, fam, date.today())
    assert (got.street, got.town) == ("45 O St", "East Orange")
    assert s5.purchase_place(db, {**bare, "address": "46 O ST"}, date.today(), {}) is None   # no town: never named
    person = [s5.Borrower(entity="QUINN Q SAMPLE LIVING TRUST", purchases_36m=2, last_purchase=None, towns=[],
                          counties=[], price_lo=None, price_hi=None, multi_share=0.0, in_progress=0, resales=0,
                          buys=[_buy("44 O ST", 10, 1, lot="44")])]
    assert s5.pick_named(db, person, date.today()) is None                # a person's trust is never named


def test_touch1_holds_the_sample_and_the_stream_never_sends_a_second_touch(db, tmp_path):
    from db.models import FirmOutbox, FirmTarget, utcnow
    from engine import firm_outbox as fo
    _families(db)
    t = FirmTarget(kind="hard_money_lender", stream="s5", email="loans@fastcap.example", stage="NEW", score=5)
    db.add(t)
    db.flush()
    now = datetime.now().replace(microsecond=0)
    [ob] = s5.STREAM.run(db, _ctx(tmp_path, min_rows=1)).outbound
    row = fo.enqueue(db, ob)
    held = db.query(FirmOutbox).filter_by(key=f"{ob.key}:sample").one()
    assert row.status == "queued" and row.attachments_json == [] and held.status == "held"
    assert "$750 a month flat" in held.body and held.attachments_json[0]["name"].startswith("s5_borrower_feed_")
    row.status, row.message_id, row.sent_at = "sent", "<t1@desk.example>", utcnow()
    t.touches, t.stage, t.last_contact_at = 1, "CONTACTED", now
    db.flush()
    week2 = StreamContext(now=now + timedelta(days=7), out_dir=tmp_path / "w2", operator=OP, send=True,
                          limits={"min_rows": 1})
    assert s5.STREAM.run(db, week2).outbound == []    # in a sequence: the outbox nudges in-thread


def test_keys_stable_no_resend_cap_and_dry_run(db, tmp_path):
    from db.models import FirmTarget
    from engine import firm_outbox as fo
    _families(db)
    ts = [FirmTarget(kind="hard_money_lender", stream="s5", email=f"lender{i}@hml.example", stage="NEW", score=10 - i)
          for i in range(4)]
    db.add_all(ts)
    db.flush()
    dry = s5.STREAM.run(db, _ctx(tmp_path, send=False, min_rows=1))
    assert dry.outbound == [] and dry.products == 1
    first = s5.STREAM.run(db, _ctx(tmp_path, min_rows=1, pitches_per_run=2))
    again = s5.STREAM.run(db, _ctx(tmp_path, min_rows=1, pitches_per_run=2))
    assert [o.key for o in first.outbound] == [o.key for o in again.outbound] and len(first.outbound) == 2
    for ob in first.outbound:
        assert fo.enqueue(db, ob) is not None
    nxt = s5.STREAM.run(db, _ctx(tmp_path, min_rows=1, pitches_per_run=2))
    assert [o.target_id for o in nxt.outbound] == [ts[2].id, ts[3].id]      # this week's used keys are skipped


def test_feed_fits_one_page_with_forty_companies(db, tmp_path):
    for i in range(60):
        _buyer(db, f"NORTH JERSEY RENOVATION AND HOLDINGS GROUP {i} LLC", f"{100 + i} COMMERCE DR, NEWARK, NJ 07102",
               [_buy(f"{i} Z{j} ST", 60 + 90 * j, 200_000 + 10_000 * j + i, lot=f"{i}{j}",
                     muni=("NEWARK CITY", "IRVINGTON TWP", "EAST ORANGE CITY")[j], mun=("0714", "0709", "0705")[j])
                for j in range(3)])
    res = s5.STREAM.run(db, _ctx(tmp_path))
    pdf = next((tmp_path / "out").glob("s5_borrower_feed_*.pdf"))
    assert _pages(pdf) == 1 and _pages(sample_path(pdf)) == 1          # the sample's longer subtitle fits too
    assert res.found == 60 and res.metrics["feed_rows"] == 40           # one line each: all 40 fit
    text = _text(pdf)
    assert "COMMERCE" not in text and "NORTH JERSEY RENOVATION AND HO..." in text   # cut to one line, marked
    sample = _text(sample_path(pdf))
    assert sample.count("NORTH JERSEY RENOVATION AND HO...") == 2 and "N•••• J•••••" in sample


def test_empty_database(db, tmp_path):
    res = s5.STREAM.run(db, _ctx(tmp_path))
    assert (res.processed, res.found, res.products, res.outbound) == (0, 0, 0, [])
    assert res.metrics["feed_rows"] == 0
