"""Stream 5: the hard-money borrower feed (engine/streams/s5_hard_money_feed.py)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from engine.streams import s5_hard_money_feed as s5
from engine.streams.base import StreamContext
from tests.factories import make_lead

OP = {"name": "Sam Operator", "first": "Sam", "phone": "(973) 555-0199", "email": "sam@example.com",
      "postal": "1 Main St, Newark, NJ 07102"}


def _ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=datetime.now().replace(microsecond=0), out_dir=tmp_path / "out", operator=OP, send=send,
                         limits=limits)


def _text(path) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text() for p in PdfReader(str(path)).pages)


def _pages(path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(path)).pages)


def _buy(street: str, days: int, price: int, *, block: str = "100", lot: str = "1", units: int = 1,
         mun: str = "0714", muni: str = "NEWARK CITY") -> dict:
    return {"address": street, "municipality": muni, "county": "Essex", "price": price, "class": "2", "units": units,
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


def test_pitch_body_is_plain_short_and_truthful():
    from engine import human_copy as hc
    body = s5.pitch_body(40, 750, "Sam")
    assert hc.lint(body) == [] and hc.word_count(body) < 110
    assert body.splitlines()[-1] == "Sam" and "$750/month" in body and "no per-loan fee" in body
    low = body.lower()
    assert "referral" not in low and "client" not in low and "—" not in body and "guarantee" not in low


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
    assert ob.attachments == [pdf] and ob.priority == 3 and ob.kind == "pitch" and ob.to == "loans@fastcap.example"
    assert "the 2 most active investor companies" in ob.body


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
    assert _pages(pdf) == 1
    assert res.found == 60 and res.metrics["feed_rows"] == 40           # one line each: all 40 fit
    text = _text(pdf)
    assert "COMMERCE" not in text and "NORTH JERSEY RENOVATION AND HO..." in text   # cut to one line, marked


def test_empty_database(db, tmp_path):
    res = s5.STREAM.run(db, _ctx(tmp_path))
    assert (res.processed, res.found, res.products, res.outbound) == (0, 0, 0, [])
    assert res.metrics["feed_rows"] == 0
