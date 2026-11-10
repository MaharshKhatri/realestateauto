"""Stream 10: the pre-MLS distress queue (internal only: no e-mail ever goes to an owner in foreclosure) and the
docket sheet pitched to listing agents (2-step funnel: plain Touch 1, the masked sample only on a yes)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from pypdf import PdfReader

from db.models import FirmOutbox, FirmTarget, SignalType, SwarmState, utcnow
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
    [path] = list((tmp_path / "internal").glob("s10_pre_mls_queue_*.pdf"))
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


def test_no_owner_email_and_a_dry_run_writes_no_state(db, tmp_path):
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


# -- the listing-agent docket sheet (operator 2026-11-10: "weaponize s10") ------------------------------------------
AGENT = "pat@brokerage.example"
DEBT = 234_567                                                       # a lis pendens debt figure that must never show
SHIFT = {5: 2, 6: 1}.get(TODAY.weekday(), 0)                         # agent pitches go out on weekdays only
WEEKDAY = datetime.combine(TODAY + timedelta(days=SHIFT), time(10))


def _actx(tmp_path, now: datetime = WEEKDAY, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=now, out_dir=tmp_path / "out", operator=OP, send=send, limits=limits)


def _harvested(db, *zips: str, at: datetime | None = None):
    """The listing harvest covered these ZIPs (swarm_state stale_listing_harvests), so "not on the MLS" is provable."""
    row = db.get(SwarmState, "stale_listing_harvests")
    stamp = (at or utcnow()).isoformat()
    value = {"zips": {**(((row.value_json or {}).get("zips") or {}) if row else {}), **{z: [stamp] for z in zips}}}
    if row is None:
        db.add(SwarmState(key="stale_listing_harvests", value_json=value))
    else:
        row.value_json = value
    db.flush()


def _agent(db, email: str = AGENT, name: str = "Pat Agent", **kw):
    """A listing agent with a live listing in Newark (the factory town)."""
    from tests.factories import add_contact
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    return add_contact(db, lead, contact_type="LISTING_AGENT", name=name, phone=None, email=email,
                       company="Sample Realty", **kw)


def _town(db, filings: int = 4, sale: bool = True):
    """Newark: `filings` new foreclosure lis pendens (with a debt figure and party names on record) and one sheriff
    sale in 12 days with a published upset, none listed; the ZIP was harvested today."""
    leads = []
    for i in range(filings):
        lead = make_lead(db, sheriff_days=None, owner_name="QUINN Q SAMPLE")
        upsert_signal(db, lead.property, signal_type=SignalType.LIS_PENDENS, source="clerk_essex",
                      source_ref=f"Essex:LP-{lead.id}", filing_date=TODAY - timedelta(days=3 + i), amount_owed=DEBT,
                      defendant="QUINN Q SAMPLE", plaintiff_or_lienholder="BIG BANK NA",
                      case_number=f"F-00{lead.id}-26", status="foreclosure complaint filed", active=True)
        leads.append(lead)
    if sale:
        lead = make_lead(db, sheriff_days=12, upset=150_000, owner_name="QUINN Q SAMPLE")
        sig = _sheriff(lead)
        sig.raw_metadata_json = {**sig.raw_metadata_json, "upset_amount": 187_500, "sheriff_number": f"26-0{lead.id}"}
        leads.insert(0, lead)                                    # the soonest sale is the sheet's first row
    _harvested(db, "07106")
    db.flush()
    for lead in leads:
        db.refresh(lead.property)
    return leads


def _sentences(body: str) -> list[str]:
    import re
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    head = " ".join(ln for ln in lines if not re.fullmatch(r"(Hello|Hi \S+),", ln) and ln != OP["first"])
    return [x for x in re.split(r"(?<=[.?!])\s+", head) if x]


def _has(text: str, street: str) -> bool:
    """The street as a whole ("7 Test St" is not inside "17 Test St")."""
    import re
    return re.search(rf"(?<![\d\u2022]){re.escape(street)}", text) is not None


def _text(path) -> str:
    pages = PdfReader(str(path)).pages
    assert len(pages) == 1
    return pages[0].extract_text().replace("\n", " ")


def _week(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def test_an_agent_gets_a_plain_touch1_and_the_masked_town_sheet_waits_as_the_sample(db, tmp_path):
    from core.text import postal_title
    from engine import firm_outbox as fo
    from engine import human_copy as hc
    from engine.streams.base import sample_path
    leads = _town(db)
    _agent(db)
    db.commit()
    db.expire_all()
    res = s10.STREAM.run(db, _actx(tmp_path))
    [o] = res.outbound                                            # the agent, never an owner
    assert o.to == AGENT and o.stream == "s10" and o.kind == "pitch" and o.priority == 3 and o.commercial
    assert o.key == f"s10:agent:{AGENT}:{_week(WEEKDAY)}"
    assert o.attachments == [] and o.in_reply_to is None
    first = postal_title(leads[0].property.street_address)
    assert o.subject == f"Newark foreclosures not on the MLS: {first} + 4 more"
    assert o.body.startswith("Hi Pat,\n\n") and o.body.rstrip().endswith("\nSam")
    assert _sentences(o.body) == [
        f"We pulled 5 new foreclosure filings and sheriff sales in Newark this month that aren't on the MLS, including "
        f"{first}.",
        "Want the docket sheet, either to talk with the owners about listing or to work with us on the investor side?"]
    assert hc.word_count(" ".join(_sentences(o.body))) <= 60 and hc.lint(o.body) == []
    for never in ("$", "234,567", "QUINN", "BIG BANK", "upset", "owe", "free", "http", "attached"):
        assert never not in o.body, never
    # the target: one row per e-mail, created for s10
    t = db.get(FirmTarget, o.target_id)
    assert (t.email, t.kind, t.stream, t.stage, t.town, t.county) == (AGENT, "investor_agent", "s10", "NEW", "Newark",
                                                                      "Essex")
    assert t.data_json["s10_mun"] == "0714"
    # Touch 2: the masked sample of the town sheet and the flat price
    [sheet] = list((tmp_path / "out").glob("s10_agent_sheet_*.pdf"))
    assert sheet.name == f"s10_agent_sheet_essex_newark_{_week(WEEKDAY)}.pdf"
    assert o.sample_attachments == [sample_path(sheet)] and sample_path(sheet).exists()
    assert ("$250 a month per town plus NJ sales tax, flat, no referral fee and nothing tied to a listing or sale (N.J.S.A. 45:15-3)"
            in o.sample_body)
    assert 'Reply "start" and I\'ll set it up.' in o.sample_body and "no rescue promises" in o.sample_body
    assert "the first 2 properties in full, the rest with the street and docket hidden" in o.sample_body
    assert hc.lint(o.sample_body) == []
    full, sample = _text(sheet), _text(sample_path(sheet))
    streets = [postal_title(x.property.street_address) for x in leads]
    for must in ("Assessed value (public record)", "Sheriff's published upset: $187,500", "$200,000", "46:10B-53",
                 "contact homeowners only as NJ law and your brokerage allow", "no rescue promises",
                 "not a market value, an appraisal or an opinion of value", "1 Main St, Iselin", "no more",
                 "$250 a month per town", *streets):
        assert must in full, must
    for never in ("QUINN", "BIG BANK", "234,567", "$235K", "equity", "ARV", "as-is", "payoff"):
        assert never not in full and never not in sample, never
    assert full.count("Sheriff's published upset:") == 1                         # the sheriff sale row only
    for s in streets[:2]:
        assert _has(sample, s)
    for s, lead in zip(streets[2:], leads[2:]):
        assert not _has(sample, s) and f"F-00{lead.id}-26" not in sample        # rows 3+: street and docket hidden
    assert "Sample: 2 of 5 rows in full" in sample
    assert res.metrics["agent_targets"] == 1 and res.metrics["agent_pitches"] == 1
    assert res.metrics["agent_sheets"] == 1 and res.metrics["agent_towns"] == 1 and res.products == 2
    assert all(isinstance(v, (int, float)) for v in res.metrics.values())
    assert not any("@" in n or "Test St" in n for n in res.notes)
    # the internal queue still goes only to the operator, from its own folder
    [queue] = res.internal
    assert queue.parent.name == "internal" and queue.exists()
    # through the outbox: Touch 1 carries no file; the sample waits for their yes
    row = fo.enqueue(db, o)
    held = db.query(FirmOutbox).filter_by(key=f"{o.key}:sample").one()
    assert row.status == "queued" and row.attachments_json == []
    assert held.status == "held" and [a["name"] for a in held.attachments_json] == [sample_path(sheet).name]


def test_agents_in_deal_talks_on_a_stop_list_bounced_or_behind_an_office_inbox_are_never_pitched(db, tmp_path):
    from db.models import InboundReply
    from engine import agent_outreach as ao
    _town(db)
    _agent(db, AGENT)                                              # writing to us about a house: no pitch mid-deal
    db.add(InboundReply(message_id="<deal@b.test>", from_email=AGENT, status="needs_approval"))
    _agent(db, "info@sampleoffice.example", name="Front Desk")     # a shared office inbox
    _agent(db, "gone@brokerage.example", name="Gail Gone")
    ao.record_bounce(db, "gone@brokerage.example")
    _agent(db, "stop@brokerage.example", name="Sid Stop", dnc_status="dnc")
    _agent(db, "lost@brokerage.example", name="Lou Lost")
    db.add(FirmTarget(kind="investor_agent", stream="s2", email="lost@brokerage.example", stage="LOST"))
    _agent(db, "lee@brokerage.example", name="Lee Lister")
    db.commit()
    db.expire_all()
    res = s10.STREAM.run(db, _actx(tmp_path))
    assert [o.to for o in res.outbound] == ["lee@brokerage.example"]
    assert res.metrics["agent_targets"] == 1
    assert res.outbound[0].body.startswith("Hi Lee,")


def test_an_existing_target_is_reused_as_is_and_the_cap_and_ranking_hold(db, tmp_path):
    _town(db)
    _agent(db, "amy@brokerage.example", name="Amy Able")
    _agent(db, "bo@brokerage.example", name="Coldwell Banker Office")   # a brokerage in the name field: "Hello,"
    _agent(db, "bo@brokerage.example", name="Coldwell Banker Office")   # two live listings in Newark: ranks first
    s2_row = FirmTarget(kind="investor_agent", stream="s2", email="amy@brokerage.example", stage="NEW",
                        touches=0, score=7.5, name="Amy Able", data_json={"rank": 3})
    db.add(s2_row)
    db.commit()
    db.expire_all()
    res = s10.STREAM.run(db, _actx(tmp_path, agent_pitches_per_run=1))
    [o] = res.outbound
    assert o.to == "bo@brokerage.example" and o.body.startswith("Hello,\n\n")
    res = s10.STREAM.run(db, _actx(tmp_path, agent_pitches_per_run=5))
    by = {x.to: x for x in res.outbound}
    amy = db.get(FirmTarget, by["amy@brokerage.example"].target_id)
    assert amy.id == s2_row.id and amy.stream == "s2" and amy.score == 7.5 and amy.data_json == {"rank": 3}
    assert by["amy@brokerage.example"].kind == "pitch"           # never touched: a first touch from s10
    assert db.query(FirmTarget).count() == 2
    # once s2's sequence has started, s10 leaves her alone (one sequence per person; the outbox nudges in-thread)
    amy.touches, amy.stage, amy.last_contact_at = 1, "CONTACTED", WEEKDAY - timedelta(days=9)
    db.commit()
    res = s10.STREAM.run(db, _actx(tmp_path, agent_pitches_per_run=5))
    assert "amy@brokerage.example" not in {x.to for x in res.outbound}


def test_one_s10_touch_then_the_outboxs_nudge_in_the_same_thread(db, tmp_path):
    from engine import firm_outbox as fo
    from engine.crm import TZ
    _town(db, filings=4)
    _agent(db)
    db.commit()
    db.expire_all()
    [o] = s10.STREAM.run(db, _actx(tmp_path)).outbound
    row = fo.enqueue(db, o)
    row.status, row.message_id, row.sent_at = "sent", "<s10-touch1@example.test>", utcnow()
    t = db.get(FirmTarget, o.target_id)
    t.touches, t.stage, t.last_contact_at = 1, "CONTACTED", WEEKDAY
    db.commit()
    # the stream never proposes a second touch: a week or three weeks later, nothing from s10
    for days in (7, 21):
        later = WEEKDAY + timedelta(days=days)
        _harvested(db, "07106", at=utcnow() + timedelta(days=days))
        db.commit()
        assert s10.STREAM.run(db, _actx(tmp_path, now=later)).outbound == []
    # the outbox's nudge goes in the first touch's thread and names the docket sheet
    nudge_day = WEEKDAY + timedelta(days=3, hours=1)
    while nudge_day.weekday() >= 5:                                  # the outbox nudges on weekdays only
        nudge_day += timedelta(days=1)
    assert fo.queue_nudges(db, nudge_day.replace(tzinfo=TZ)) == 1
    db.commit()
    nudge = db.query(FirmOutbox).filter_by(target_id=t.id, kind="followup").one()
    assert nudge.in_reply_to == "<s10-touch1@example.test>" and nudge.subject == f"Re: {o.subject}"
    assert "docket sheet" in nudge.body and nudge.attachments_json == []


def test_listed_unharvested_suppressed_and_thin_towns_get_no_sheet(db, tmp_path):
    from db.models import Listing
    from engine.compliance import suppress_address
    leads = _town(db, filings=2, sale=True)                       # 3 rows: just enough for a sheet
    _agent(db)
    on_mls = leads[1].property                                    # a live listing row, no listing signal yet
    db.add(Listing(source="homeharvest", source_ref="L-live", status="for_sale", address=on_mls.street_address.title(),
                   zip_code="07106", list_price=300_000))
    db.commit()
    db.expire_all()
    res = s10.STREAM.run(db, _actx(tmp_path))
    assert res.outbound == [] and res.metrics["agent_rows_listed"] == 1 and res.metrics["agent_towns"] == 0
    # a filing in a ZIP the harvest didn't cover can't be called "not on the MLS"
    other = make_lead(db, sheriff_days=None, zip_code="07017")
    upsert_signal(db, other.property, signal_type=SignalType.LIS_PENDENS, source="clerk_essex",
                  source_ref=f"Essex:LP-{other.id}", filing_date=TODAY - timedelta(days=2),
                  status="foreclosure complaint filed", active=True)
    # a suppressed address never appears
    hide = leads[2].property
    suppress_address(db, f"{hide.street_address}, {hide.city} NJ {hide.zip_code}", source="daniels_law")
    db.commit()
    db.expire_all()
    res = s10.STREAM.run(db, _actx(tmp_path))
    assert res.metrics["agent_rows_unverified"] == 1 and res.metrics["agent_rows_suppressed"] == 1
    assert res.outbound == [] and not list((tmp_path / "out").glob("s10_agent_sheet_*.pdf"))


def test_a_dry_run_and_a_weekend_pitch_nobody_and_write_no_target(db, tmp_path):
    _town(db)
    _agent(db)
    db.commit()
    db.expire_all()
    res = s10.STREAM.run(db, _actx(tmp_path, send=False))
    assert res.outbound == [] and res.metrics["agent_targets"] == 1 and res.metrics["agent_sheets"] == 0
    assert db.query(FirmTarget).count() == 0 and not list((tmp_path / "out").glob("s10_agent_sheet_*.pdf"))
    saturday = WEEKDAY + timedelta(days=(5 - WEEKDAY.weekday()) % 7)
    _harvested(db, "07106", at=utcnow() + (saturday - WEEKDAY))
    db.commit()
    res = s10.STREAM.run(db, _actx(tmp_path, now=saturday))
    assert res.outbound == [] and "weekend: no agent pitches" in res.notes
    assert db.query(FirmTarget).count() == 0


def test_a_crash_on_the_agent_side_never_costs_the_operator_the_internal_queue(db, tmp_path, monkeypatch):
    _town(db)
    _agent(db)
    db.commit()
    db.expire_all()

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(s10, "agent_candidates", boom)
    res = s10.STREAM.run(db, _actx(tmp_path))
    assert res.outbound == [] and res.metrics["agent_error"] == 1 and res.products == 1
    assert len(res.internal) == 1 and res.internal[0].exists()
    assert "agent pitches skipped: RuntimeError" in res.notes
    assert db.get(SwarmState, s10.SNAPSHOT_KEY) is not None            # the queue's own state still saved
    db.commit()                                                         # the session is still usable
