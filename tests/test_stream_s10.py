"""Stream 10: the pre-MLS distress queue (internal only: no e-mail ever goes to an owner in foreclosure)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from pypdf import PdfReader

from db.models import SignalType, SwarmState, utcnow
from db.repository import upsert_signal
from engine.streams import s10_pre_mls_distress as s10
from engine.streams.base import StreamContext
from tests.factories import make_lead

TODAY = date.today()
OP = {"name": "Sam Operator", "first": "Sam", "phone": "(908) 555-0100", "email": "sam@example.com",
      "postal": "1 Main St, Iselin, NJ 08830"}


def _ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=datetime.combine(TODAY, time(10)), out_dir=tmp_path, operator=OP, send=send,
                         limits=limits)


def _pdf(tmp_path):
    [path] = list(tmp_path.glob("s10_pre_mls_queue_*.pdf"))
    pages = PdfReader(str(path)).pages
    assert len(pages) == 1
    return pages[0].extract_text().replace("\n", " ")


def _lis_pendens(db, lead, filed: date | None = None, n: int = 0):
    upsert_signal(db, lead.property, signal_type=SignalType.LIS_PENDENS, source="clerk_essex",
                  source_ref=f"Essex:LP-{lead.id}-{n}", filing_date=filed or TODAY - timedelta(days=60),
                  defendant="QUINN Q SAMPLE", plaintiff_or_lienholder="BIG BANK NA", case_number=f"F-9{lead.id}-26",
                  status="foreclosure complaint filed", active=True)
    db.flush()
    db.refresh(lead.property)


def _sheriff(lead):
    return next(s for s in lead.property.signals if s.signal_type == SignalType.SHERIFF_SALE)


def _run(db, tmp_path, **kw):
    db.commit()
    db.expire_all()
    return s10.STREAM.run(db, _ctx(tmp_path, **kw))


# -- pure helpers -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,expected", [
    ("Adjourned - Defendant", True), ("Defendant Adjournment", True), ("DEFENDANT TOTAL 8 WEEK ADJ", True),
    ("JOINT ADJOURNMENT", True), ("Scheduled", False), ("Not adjourned", False), ("Bankruptcy", False), (None, False),
])
def test_adjourned_status_classifier(status, expected):
    assert s10.is_adjourned(status) is expected


def test_adjournments_left_freshness_and_frfpa_labels():
    assert [s10.adjournments_left(x) for x in (0, 1, 3, 5, 7, None, "x")] == [5, 4, 2, 0, 0, 5, 5]
    assert s10.fresh_adjournment(1, 2, "Adjourned - Plaintiff", baseline=True)
    assert not s10.fresh_adjournment(1, 2, "Scheduled", baseline=True)          # count rose, status didn't say so
    assert not s10.fresh_adjournment(2, 2, "Adjourned - Plaintiff", baseline=True)
    assert not s10.fresh_adjournment(None, 3, "Adjourned - Plaintiff", baseline=True)   # a sale new to us
    assert not s10.fresh_adjournment(1, 2, "Adjourned - Plaintiff", baseline=False)     # the first run records only
    assert s10.frfpa_label("EXEMPT_ENTITY", "UNKNOWN") == "exempt: entity owner"
    assert s10.frfpa_label(None, "UNKNOWN") == "presumed covered"
    assert s10.frfpa_label(None, "OWNER_OCCUPIED") == "presumed covered"
    assert s10.frfpa_label(None, "TENANT_OCCUPIED") == "exempt: confirmed tenant-occupied"
    assert s10.signed_k(-198_000) == "-$198K" and s10.signed_k(None) == "-"
    # under water sinks a row below a single clean filing
    assert s10.rank_score(3, False, 10, 0, -200_000, False) < s10.rank_score(1, False, None, None, 150_000, False)


# -- the queue ----------------------------------------------------------------------------------------------------
def test_first_run_is_a_baseline_then_a_fresh_adjournment_enters_the_queue(db, tmp_path):
    lead = make_lead(db, sheriff_days=20, upset=150_000)            # a sheriff sale with 1 adjournment, no other signal
    res = _run(db, tmp_path)
    assert res.metrics["baseline"] == 1 and res.metrics["fresh_adjournments"] == 0 and res.found == 0
    snap = db.get(SwarmState, s10.SNAPSHOT_KEY).value_json
    assert snap == {str(_sheriff(lead).id): 1}
    sig = _sheriff(lead)
    sig.status = "Adjourned - Defendant"
    sig.raw_metadata_json = {**sig.raw_metadata_json, "adjournments_total": 2, "defendant_adjournments": 1}
    res = _run(db, tmp_path)
    assert res.metrics["baseline"] == 0 and res.metrics["fresh_adjournments"] == 1 and res.found == 1
    rows, _, _ = s10.build_queue(db, _ctx(tmp_path), {**s10.DEFAULTS})
    [row] = rows
    assert row.lead_id == lead.id and row.fresh and row.adj_left == 3 and row.days_to_sale == 20
    assert "just adjourned" in _pdf(tmp_path)
    # the next run the same week still shows it as fresh, without counting it again
    res = _run(db, tmp_path)
    assert res.metrics["fresh_adjournments"] == 0 and res.metrics["fresh_rows"] == 1


def test_only_pre_mls_properties_with_distress_inputs_are_queued(db, tmp_path):
    lp = make_lead(db, sheriff_days=None, verdict="STRONG")
    _lis_pendens(db, lp)
    listed = make_lead(db, sheriff_days=None, stale_listing=True)
    _lis_pendens(db, listed)                                        # on the MLS: the listing-agent desk has it
    estate = make_lead(db, sheriff_days=None)
    upsert_signal(db, estate.property, signal_type=SignalType.ESTATE_OWNER, source="test",
                  source_ref=f"estate:{estate.id}", active=True)
    tax = make_lead(db, sheriff_days=None)
    upsert_signal(db, tax.property, signal_type=SignalType.TAX_SALE_CERTIFICATE, source="realauction",
                  source_ref=f"cert:{tax.id}", amount_owed=12_000, active=True)
    plain = make_lead(db, sheriff_days=None, absentee=True)       # no distress input: not queued
    dead = make_lead(db, sheriff_days=None)
    _lis_pendens(db, dead)
    dead.status_stage = "DO_NOT_CONTACT"
    res = _run(db, tmp_path)
    rows, _, _ = s10.build_queue(db, _ctx(tmp_path), {**s10.DEFAULTS})
    ids = {r.lead_id for r in rows}
    assert ids == {lp.id, estate.id, tax.id}
    assert plain.id not in ids and listed.id not in ids and dead.id not in ids
    assert res.metrics["listed_skipped"] == 1 and res.metrics["closed_skipped"] == 1
    t = next(r for r in rows if r.lead_id == tax.id)
    assert "tax sale certificate $12K" in t.stack and t.adj_left is None and t.days_to_sale is None


def test_the_queue_is_internal_one_page_names_nobody_and_honors_suppression(db, tmp_path):
    from engine.compliance import suppress_address
    leads = []
    for i in range(22):
        lead = make_lead(db, sheriff_days=None)
        _lis_pendens(db, lead, n=i)
        leads.append(lead)
    hide = leads[0].property
    suppress_address(db, f"{hide.street_address}, {hide.city} NJ {hide.zip_code}", source="daniels_law")
    res = _run(db, tmp_path)
    assert res.outbound == [] and res.products == 1
    # the queue goes to the operator's own inbox (2026-11-09: the PDF was written but never handed to the mailer)
    assert len(res.internal) == 1 and res.internal[0].exists() and res.internal[0].suffix == ".pdf"
    assert res.found == 21 and res.metrics["suppressed"] == 1
    assert 1 <= res.metrics["rows_shown"] <= s10.DEFAULTS["max_rows"]
    text = _pdf(tmp_path)
    for must in ("Pre-MLS distress queue", "Internal only", "46:10B-53", "letters you approve", "not an appraisal",
                 "presumed covered", "1 Main St, Iselin"):
        assert must in text, must
    for never in ("QUINN", "BIG BANK"):                            # no defendant, no plaintiff
        assert never not in text
    from core.text import postal_title
    assert f"#{leads[0].id} {postal_title(hide.street_address)}" not in text
    assert all(isinstance(v, (int, float)) for v in res.metrics.values())


def test_no_outbound_ever_and_a_dry_run_writes_no_state(db, tmp_path):
    lead = make_lead(db, sheriff_days=None)
    _lis_pendens(db, lead)
    res = _run(db, tmp_path, send=False)
    assert res.outbound == [] and res.found == 1
    assert db.get(SwarmState, s10.SNAPSHOT_KEY) is None and db.get(SwarmState, s10.FRESH_KEY) is None
    res = _run(db, tmp_path, send=True)
    assert res.outbound == [] and db.get(SwarmState, s10.SNAPSHOT_KEY) is not None


def test_new_filings_and_the_s2_digest_flag(db, tmp_path):
    fresh = make_lead(db, sheriff_days=None, assumptions={"quant": {"buyer_max": 300_000}})
    _lis_pendens(db, fresh, filed=TODAY - timedelta(days=2))
    old = make_lead(db, sheriff_days=None, verdict=None)            # no valuation: the digest can't show it
    _lis_pendens(db, old, filed=TODAY - timedelta(days=400))
    old_sig = next(s for s in old.property.signals if s.signal_type == SignalType.LIS_PENDENS)
    old_sig.first_seen_at = utcnow() - timedelta(days=300)
    res = _run(db, tmp_path)
    assert res.metrics["new_filings"] == 1 and res.metrics["s2_rows"] == 1
    rows, _, _ = s10.build_queue(db, _ctx(tmp_path), {**s10.DEFAULTS})
    by = {r.lead_id: r for r in rows}
    assert by[fresh.id].s2 and not by[old.id].s2
    assert by[old.id].equity is None and by[old.id].equity_note == "no value on record"
    assert by[fresh.id].rank > by[old.id].rank                       # new filing and equity rank first


def test_an_empty_database_runs_clean(db, tmp_path):
    res = s10.STREAM.run(db, _ctx(tmp_path))
    assert (res.processed, res.found, res.products, res.outbound) == (0, 0, 0, [])
    assert not list(tmp_path.glob("*.pdf"))
    assert s10.STREAM.key == "s10" and s10.STREAM.name == "Pre-MLS distress" and s10.STREAM.cadence_hours == 24
