"""Stream 12: monthly town market reports for listing agents (aggregates of recorded sales only; 2-step funnel: a plain
Touch 1, the report itself only on a yes; subscribers get their own town, co-branded, once a month)."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import pytest
from pypdf import PdfReader

from db.models import ClerkFiling, FirmOutbox, FirmTarget
from db.repository import upsert_deed
from engine.streams import s12_town_market_reports as s12
from engine.streams.base import StreamContext
from tests.factories import add_contact, make_lead

TODAY = date.today()
SHIFT = {5: 2, 6: 1}.get(TODAY.weekday(), 0)                         # pitches go out on weekdays only
WEEKDAY = datetime.combine(TODAY + timedelta(days=SHIFT), time(10))
OP = {"name": "Sam Operator", "first": "Sam", "company": "Sample Research LLC", "phone": "(908) 555-0100",
      "email": "sam@example.com", "postal": "1 Main St, Iselin, NJ 08830"}
AGENT = "pat@brokerage.example"


def _ctx(tmp_path, now: datetime = WEEKDAY, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=now, out_dir=tmp_path / "out", operator=OP, send=send, limits=limits)


def _deeds(db, *, mun: str = "0714", county: str = "Essex", months: int = 16, per_month: int = 12, condos: int = 2,
           today: date | None = None, as_of_lag: int = 7, start_day: int = 1):
    """Recorded arm's-length deeds in one town: `per_month` a month for `months` months up to today (every third buyer
    a company), `condos` condo units in the newest complete months, plus deeds that must never count (a $1 family
    transfer, a sheriff's deed, a class 4A sale). Streets are invented: none may ever appear in a report."""
    today = today or WEEKDAY.date()
    m = s12.ym(today)
    for _ in range(months):
        m = s12.prev_month(m)
    k = 0
    while m <= s12.ym(today):
        for i in range(per_month):
            d = date(m[0], m[1], min(start_day + 2 * i, 28))
            if d >= today:
                break
            k += 1
            grantee = f"SAMPLE HOLDINGS {k} LLC" if k % 3 == 0 else f"QUINN Q SAMPLE {k}"
            upsert_deed(db, parcel_id=f"{county.upper()}-{mun}-9{k:04d}-2", deed_date=d, grantee=grantee,
                        source="county_tax_list", county=county, property_location=f"{k} SAMPLE ST",
                        sale_price=300_000 + 1_000 * (k % 50), property_class="2", nu_code=None,
                        raw_json={"recorded_date": min(d + timedelta(days=as_of_lag), today).isoformat(),
                                  "sqft": 1500})
        m = s12.next_month(m)
    end = s12.prev_month(s12.prev_month(s12.prev_month(s12.ym(today))))
    for j in range(condos):
        upsert_deed(db, parcel_id=f"{county.upper()}-{mun}-77-1-C00{j + 1}", deed_date=date(end[0], end[1], 10 + j),
                    grantee="QUINN Q SAMPLE CONDO", source="county_tax_list", county=county,
                    property_location="5 SAMPLE CT UNIT 2", sale_price=210_000, property_class="2", nu_code=None,
                    raw_json={"recorded_date": date(end[0], end[1], 20).isoformat(), "sqft": 900})
    bad = date(end[0], end[1], 15)
    for n, (price, nu, cls) in enumerate(((1, "1", "2"), (250_000, "12", "2"), (900_000, None, "4A"))):
        upsert_deed(db, parcel_id=f"{county.upper()}-{mun}-88-{n}", deed_date=bad, grantee="SAMPLE BANK NA",
                    source="county_tax_list", county=county, property_location=f"{n} NEVER ST", sale_price=price,
                    property_class=cls, nu_code=nu, raw_json={"recorded_date": bad.isoformat()})
    db.flush()


def _agent(db, email: str = AGENT, name: str = "Pat Agent", listings: int = 3, **kw):
    """A listing agent with `listings` live listings in Newark (the factory town)."""
    c = None
    for _ in range(listings):
        lead = make_lead(db, sheriff_days=None, stale_listing=True)
        c = add_contact(db, lead, contact_type="LISTING_AGENT", name=name, phone=None, email=email,
                        company="Sample Realty", **kw)
    return c


def _run(db, tmp_path, **kw):
    db.commit()
    db.expire_all()
    return s12.STREAM.run(db, _ctx(tmp_path, **kw))


def _sentences(body: str) -> list[str]:
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    head = " ".join(ln for ln in lines if not re.fullmatch(r"(Hello|Hi \S+),", ln) and ln != OP["first"])
    return [x for x in re.split(r"(?<=[.?!])\s+", head) if x]


def _text(path) -> str:
    pages = PdfReader(str(path)).pages
    assert len(pages) == 1
    return re.sub(r"\s+", " ", pages[0].extract_text())


def _report(tmp_path):
    [path] = list((tmp_path / "out" / "towns").glob("s12_market_*.pdf"))
    return path


# -- pure helpers -------------------------------------------------------------------------------------------------
def test_covered_months_keep_only_whole_settled_months_with_a_normal_deed_volume():
    counts = {}
    m = (2025, 1)
    while m <= (2026, 9):
        counts[m] = 12
        m = s12.next_month(m)
    full = s12.covered_months(counts, date(2025, 1, 3), date(2026, 9, 24), 60)
    assert full[0] == (2025, 1) and full[-1] == (2026, 6) and len(full) == 18   # July ended < 60 days before Sep 24
    assert s12.covered_months(counts, date(2025, 1, 15), date(2026, 9, 24), 60)[0] == (2025, 2)  # mid-month start
    assert s12.covered_months(counts, date(2025, 1, 3), date(2026, 10, 1), 60)[-1] == (2026, 7)
    gap = {**counts, (2026, 3): 0}
    assert s12.covered_months(gap, date(2025, 1, 3), date(2026, 9, 24), 60) == [(2026, 4), (2026, 5), (2026, 6)]
    thin = {**counts, (2026, 2): 5}                                       # under 60% of a 12-deed month: a gap
    assert s12.covered_months(thin, date(2025, 1, 3), date(2026, 9, 24), 60)[0] == (2026, 3)
    small = {k: 2 for k in counts}                                        # a small town: any deed counts the month
    assert len(s12.covered_months(small, date(2025, 1, 3), date(2026, 9, 24), 60)) == 18
    assert s12.covered_months(counts, date(2026, 9, 1), date(2026, 9, 24), 60) == []
    assert s12.span_label([(2026, 4), (2026, 5), (2026, 6)]) == "Apr-Jun 2026"
    assert s12.span_label([(2025, 7), (2026, 6)]) == "Jul 2025-Jun 2026"


def test_stats_suppress_small_cells_and_keep_package_deeds_out_of_the_medians():
    d = date(2026, 5, 1)
    four = [s12.Sale(f"P{i}", d, 300_000 + i, sqft=1500, company=False) for i in range(4)]
    st = s12.stats(four)
    assert st.n == 4 and st.median_price is None and st.median_ppsf is None and st.company_pct is None
    cells, hidden = s12.stat_cells(st)
    assert cells == [s12.SUPPRESSED] * 4 and hidden == 4 and s12.SUPPRESSED == "fewer than 5 recorded sales"
    sales = [s12.Sale(f"P{i}", d + timedelta(days=i), p, sqft=1000, company=i % 2 == 0)
             for i, p in enumerate((200_000, 250_000, 301_000, 352_000, 400_000))]
    sales += [s12.Sale("PK1", d, 2_000_000, sqft=1000, company=True), s12.Sale("PK2", d, 2_000_000, sqft=1000,
                                                                                company=True)]
    s12.mark_packages(sales)
    st = s12.stats(sales)
    assert st.n == 7 and st.median_price == 301_000 and st.median_ppsf == 301      # the $2M package stays out
    assert st.company_pct == pytest.approx(100 * 5 / 7)
    assert s12.company_buyer("SAMPLE HOLDINGS LLC") is True
    for name in ("QUINN Q SAMPLE", "QUINN Q SAMPLE REVOCABLE TRUST", "CITY OF NEWARK", "WELLS FARGO BANK NA"):
        assert s12.company_buyer(name) is False, name
    assert s12.company_buyer("") is None
    assert s12.product_of("ESSEX-0714-77-1-C0001", None) == "condo"
    assert s12.product_of("ESSEX-0714-77-1", None) == "house"


# -- the report ---------------------------------------------------------------------------------------------------
def test_the_report_is_one_page_of_aggregates_with_no_address_name_or_single_sale(db, tmp_path):
    from engine import firm_outbox as fo
    from engine.compliance import suppress_address
    _deeds(db)
    suppress_address(db, "7 Sample St, Newark NJ 07106", source="daniels_law")
    for _ in range(5):
        make_lead(db, sheriff_days=None)                                  # Newark parcels with tax bills on file
    res = _run(db, tmp_path)
    path = _report(tmp_path)
    assert path.name == f"s12_market_essex_newark_{WEEKDAY:%Y-%m}.pdf"
    assert not list((tmp_path / "out").glob("*.pdf")) and fo.delivery_files(tmp_path / "out", "Essex") == []
    text = _text(path)
    [rep] = s12.collect_towns(db, WEEKDAY.date(), dict(s12.DEFAULTS), s12.town_names(db, _table()), {"Essex"},
                              __import__("collections").Counter()).values()
    assert len(rep.long_months) == 12 and rep.long.n == 12 * 12 + 2          # + the 2 condo units
    for must in ("Newark Housing Market Report", "Essex County, NJ", s12.DISCLAIMER, "Recorded home sales",
                 f"Last 12 months on record ({s12.span_label(rep.long_months)})",
                 f"Last 3 months on record ({s12.span_label(rep.short_months)})", f"{rep.long.n:,}",
                 s12.money(rep.long.median_price), f"${rep.long.median_ppsf:,.0f}", "Bulletin 13-05", "45:14F", "never from MLS statistics",
                 "Condo units (MOD-IV qualifier C)", "fewer than 5 recorded sales", "not tracked for Essex County",
                 "Chapter 123 average ratio", "35.83%", "$4.500 per $100 of assessed value", "Ready-to-send e-mail text",
                 "Source: NJ public records via Sample Research LLC.", "1 Main St, Iselin", "no more"):
        assert must in text, must
    for never in ("QUINN", "SAMPLE HOLDINGS", "SAMPLE BANK", "Sample St", "SAMPLE ST", "Never St", "Sample Ct",
                  "$900,000", "$250,000", "ARV", "worth", "under-valued", "below market"):
        assert never not in text, never
    assert res.metrics["towns_with_data"] == 1 and res.metrics["reports"] == 1 and res.found == 1
    assert res.metrics["cells_suppressed"] == 4                           # the condo row (2 sales): all 4 cells
    assert all(isinstance(v, (int, float)) for v in res.metrics.values())
    assert not any("@" in n or "Sample" in n for n in res.notes)


def _table():
    from engine.streams.s6_tax_appeals import load_ratio_table
    return load_ratio_table()


def test_short_stale_and_thin_files_make_no_report(db, tmp_path):
    _deeds(db, months=4)                                                  # 4 months: under min_months (6)
    res = _run(db, tmp_path)
    assert res.metrics["towns_with_data"] == 0 and res.metrics["towns_short"] == 1 and res.products == 0
    _deeds(db, mun="0705", months=16, today=WEEKDAY.date() - timedelta(days=400))   # a file a year old
    res = _run(db, tmp_path)
    assert res.metrics["towns_stale"] == 1 and res.metrics["towns_with_data"] == 0
    _deeds(db, mun="0709", months=16, per_month=1, condos=0)              # one sale a month: 12 in the window
    res = _run(db, tmp_path, min_sales=20)
    assert res.metrics["towns_thin"] == 1 and res.metrics["towns_with_data"] == 0


def test_the_pipeline_counts_only_what_our_records_prove(db, tmp_path):
    _deeds(db)
    make_lead(db, sheriff_days=20)                                        # Essex's CivilView list is current
    make_lead(db, sheriff_days=200)                                       # beyond the 90-day window
    for i, (days, town, doc) in enumerate(((120, "NEWARK", "LIS PENDENS FORECLOSURE"),
                                           (10, "NEWARK", "LIS PENDENS FORECLOSURE"),
                                           (20, "NEWARK", "LIS PENDENS FORECLOSURE AND RECOVERY"),
                                           (30, "NEWARK", "LIS PENDENS IN REM"),
                                           (5, "NEWARK", "LIS PENDENS DISCHARGE"),
                                           (6, "EAST ORANGE", "LIS PENDENS FORECLOSURE"),
                                           (1, "NEWARK", "LIS PENDENS FORECLOSURE"))):
        db.add(ClerkFiling(county="Essex", source="press", instrument=f"I-{i}", doc_type=doc,
                           recorded_date=WEEKDAY.date() - timedelta(days=days), town=town,
                           is_discharge="DISCHARGE" in doc, defendants=["QUINN Q SAMPLE"]))
    res = _run(db, tmp_path)
    text = _text(_report(tmp_path))
    assert "Sheriff foreclosure sales scheduled in the next 90 days (sheriff's notices) 1" in text
    assert "Foreclosure lis pendens filed in the last 90 days (county clerk index) 4" in text
    since = WEEKDAY.date() - timedelta(days=90)
    rep = _rep(db)
    _, body = s12.touch1(rep, "Pat Agent", WEEKDAY, "Sam")
    assert f"and 4 foreclosure cases were filed there since {since:%B} {since.day}." in body
    # a filing the clerk's index can't tie to a town makes the count unprovable: it is not shown
    db.add(ClerkFiling(county="Essex", source="press", instrument="I-x", doc_type="LIS PENDENS FORECLOSURE",
                       recorded_date=WEEKDAY.date() - timedelta(days=3), town="ESSEX COUNTY"))
    _run(db, tmp_path)
    text = _text(_report(tmp_path))
    assert "the Essex County clerk index doesn't give the town for every filing" in text
    rep = _rep(db)
    assert rep.lis_pendens is None
    until = WEEKDAY.date() + timedelta(days=90)
    assert (f"and 1 sheriff sale is set there before {until:%B} {until.day}."
            in s12.touch1(rep, "Pat Agent", WEEKDAY, "Sam")[1])
    assert res.metrics["towns_with_data"] == 1
    # a sale CivilView couldn't tie to a parcel, whose notice names a village, may be in Newark: the count is a floor
    # in the report and Touch 1 states no sheriff count at all
    make_lead(db, sheriff_days=15, parcel_id="ESSEX-UNMATCHED-77", mun_code=None, municipality=None,
              city="Hunt Avenue Vauxhall", property_class=None)
    make_lead(db, sheriff_days=16, parcel_id="ESSEX-UNMATCHED-78", mun_code=None, municipality=None,
              city="Sample Avenue South Orange", property_class=None)    # placed in South Orange, not Newark
    _run(db, tmp_path)
    text = _text(_report(tmp_path))
    assert ("at least 1 (1 more in Essex County have a notice that doesn't make the town clear)" in text)
    rep = _rep(db)
    assert rep.sheriff == 1 and rep.sheriff_unplaced == 1
    body = s12.touch1(rep, "Pat Agent", WEEKDAY, "Sam")[1]
    assert "sheriff" not in body and "foreclosure" not in body


def test_unmatched_sheriff_sales_are_placed_only_by_a_town_name_at_the_end_and_names_are_spelled_out():
    towns = [("ORANGE", "0717"), ("SOUTH ORANGE VILLAGE", "0719"), ("NEWARK", "0714"), ("WEST ORANGE", "0722")]
    assert s12.place_unmatched("Wyoming Avenue South Orange", towns) == "0719"     # never plain Orange
    assert s12.place_unmatched("12 Orange Street Newark NJ", towns) == "0714"
    assert s12.place_unmatched("Main St West Orange", towns) == "0722"
    assert s12.place_unmatched("Park Ave Orange", towns) == "0717"
    for text in ("East Orange", "Hunt Avenue Vauxhall", "South Brunwsick", ""):
        assert s12.place_unmatched(text, towns) == "", text
    assert s12.town_label("E RUTHERFORD BORO") == "East Rutherford"
    assert s12.town_label("HASBROUCK HGHTS BORO") == "Hasbrouck Heights"
    assert s12.town_label("ELIZABETH CITY") == "Elizabeth"


def _rep(db) -> s12.TownReport:
    from collections import Counter
    names = s12.town_names(db, _table())
    reps = s12.collect_towns(db, WEEKDAY.date(), dict(s12.DEFAULTS), names, {"Essex"}, Counter())
    s12.sheriff_counts(db, reps, WEEKDAY.date(), dict(s12.DEFAULTS), names)
    s12.lis_pendens_counts(db, reps, WEEKDAY.date(), dict(s12.DEFAULTS), names)
    s12.tax_lines(db, reps, _table())
    [rep] = reps.values()
    return rep


# -- Touch 1 / Touch 2 --------------------------------------------------------------------------------------------
def test_an_agent_gets_a_plain_touch1_and_the_full_report_waits_as_the_sample(db, tmp_path):
    from engine import firm_outbox as fo
    from engine import human_copy as hc
    _deeds(db)
    _agent(db)
    res = _run(db, tmp_path)
    [o] = res.outbound
    rep = _rep(db)
    a, b = rep.long_months[0], rep.long_months[-1]
    assert o.to == AGENT and o.stream == "s12" and o.kind == "pitch" and o.priority == 3 and o.commercial
    assert o.key == f"s12:pitch:{AGENT}:{WEEKDAY:%Y-%m}" and o.attachments == [] and o.in_reply_to is None
    assert o.subject == "[Newark] home sales by public record"
    assert o.body.startswith("Hi Pat,\n\n") and o.body.rstrip().endswith("\nSam")
    assert _sentences(o.body) == [
        f"{rep.long.n} homes sold in Newark from {s12.month_name(a, year=False)} to {s12.month_name(b, year=False)} "
        f"by public record, with a median of {s12.money(rep.long.median_price)}.",
        f"I put that into a 1-page monthly Newark report agents can send to their sphere; want {WEEKDAY:%B}'s, free?"]
    head = " ".join(_sentences(o.body))
    assert 25 <= hc.word_count(head) <= 45 and hc.lint(o.body) == [] and head.count("$") == 1
    for never in ("QUINN", "SAMPLE HOLDINGS", "Sample St", "http", "attached", "!", "Ad:", "worth"):
        assert never not in o.body, never
    t = db.get(FirmTarget, o.target_id)
    assert (t.email, t.kind, t.stream, t.stage, t.town, t.county, t.org) == (
        AGENT, "farm_agent", "s12", "NEW", "Newark", "Essex", "Sample Realty")
    assert t.data_json["s12_mun"] == "0714" and t.data_json["s12_listings"] == 3
    # Touch 2: the full report (no addresses in it, so nothing to mask) and both flat prices
    assert o.sample_attachments == [_report(tmp_path)]
    assert ("It's $79 a month plus NJ sales tax per town, or $149 a month plus NJ sales tax to be the only brokerage "
            "getting it there, with no commission or per-deal fee." in o.sample_body)
    assert o.sample_body.startswith("Attached is the Newark report (the e-mail box at the end is ready to paste).")
    assert 'Reply "start" and I\'ll set it up.' in o.sample_body and hc.lint(o.sample_body) == []
    assert res.metrics["agents_seen"] == 1 and res.metrics["targets_synced"] == 1 and res.metrics["pitches"] == 1
    # through the outbox: Touch 1 carries no file; the report waits for their yes
    row = fo.enqueue(db, o)
    held = db.query(FirmOutbox).filter_by(key=f"{o.key}:sample").one()
    assert row.status == "queued" and row.attachments_json == []
    assert held.status == "held" and [x["name"] for x in held.attachments_json] == [_report(tmp_path).name]
    # the same month brings no second pitch
    db.commit()
    assert _run(db, tmp_path).outbound == []


def test_touch1_drops_what_it_cannot_say_and_fits_45_words():
    d = date(2026, 3, 1)
    months = [(2025, m) for m in range(7, 13)] + [(2026, m) for m in range(1, 7)]
    sales = [s12.Sale(f"P{i}", date(2025, 7 + i % 6, 5), 300_000 + 1000 * i, company=False) for i in range(20)]
    rep = s12.TownReport(mun="0000", town="Point Pleasant Beach Borough Sample", county="Ocean", months=months,
                         data_as_of=d, sales=sales, sheriff=7)
    now = datetime(2026, 11, 10, 10)
    _, body = s12.touch1(rep, "Sample Realty Group", now)
    assert body.startswith("Hello,\n\n") and "sheriff" not in body              # too long with it: dropped first
    head = " ".join(_sentences(body))
    assert len(_sentences(body)) == 2 and len(head.split()) <= 45
    few = s12.TownReport(mun="0000", town="Sampletown", county="Essex", months=months, data_as_of=d,
                         sales=[s12.Sale(f"P{i}", date(2025, 7, 5), 1000.0 * (i + 1), company=None,
                                         package=True) for i in range(6)])
    _, body = s12.touch1(few, "Pat Agent", now)
    assert "$" not in body and "with a median" not in body                    # a suppressed median is never stated


def test_agents_are_picked_by_listings_in_one_town_and_stop_rules_hold(db, tmp_path):
    from db.models import InboundReply
    from engine import agent_outreach as ao
    _deeds(db)
    _agent(db, "two@brokerage.example", name="Tia Two", listings=2)      # under 3 listings in the town
    _agent(db, "info@sampleoffice.example", name="Front Desk")            # a shared office inbox
    _agent(db, "deal@brokerage.example", name="Dee Deal")                 # writing to us about a house
    db.add(InboundReply(message_id="<deal@b.test>", from_email="deal@brokerage.example", status="needs_approval"))
    _agent(db, "gone@brokerage.example", name="Gail Gone")
    ao.record_bounce(db, "gone@brokerage.example")
    _agent(db, "stop@brokerage.example", name="Sid Stop", dnc_status="dnc")
    _agent(db, "owned@brokerage.example", name="Owen Owned")              # already another stream's target
    db.add(FirmTarget(kind="investor_agent", stream="s10", email="owned@brokerage.example", stage="NEW"))
    _agent(db, "amy@brokerage.example", name="Amy Able", listings=4)
    _agent(db, "lee@brokerage.example", name="Lee Lister", listings=3)
    res = _run(db, tmp_path)
    # one pitch per town per run: Amy (4 listings) ranks above Lee (3); the other agents never get one
    assert [o.to for o in res.outbound] == ["amy@brokerage.example"]
    assert res.outbound[0].body.startswith("Hi Amy,")
    assert db.query(FirmTarget).filter_by(stream="s12").count() == 1
    owned = db.query(FirmTarget).filter_by(email="owned@brokerage.example").one()
    assert owned.stream == "s10" and owned.data_json in (None, {})
    assert res.metrics["agents_seen"] == 6     # 3+ live listings in a target town (a stopped contact never counts)


def test_a_dry_run_and_a_weekend_write_no_target_and_pitch_nobody(db, tmp_path):
    _deeds(db)
    _agent(db)
    res = _run(db, tmp_path, send=False)
    assert res.outbound == [] and res.metrics["agents_seen"] == 1 and res.metrics["targets_synced"] == 0
    assert db.query(FirmTarget).count() == 0 and res.products == 1            # the report itself is still written
    saturday = WEEKDAY + timedelta(days=(5 - WEEKDAY.weekday()) % 7)
    res = _run(db, tmp_path, now=saturday)
    assert res.outbound == [] and "weekend: no pitches" in res.notes and db.query(FirmTarget).count() == 0


# -- subscribers --------------------------------------------------------------------------------------------------
def test_a_subscriber_gets_their_town_co_branded_once_a_month_and_exclusivity_holds(db, tmp_path):
    from engine import firm_outbox as fo
    _deeds(db)
    _agent(db, "lee@brokerage.example", name="Lee Lister", listings=3)    # another brokerage farming the same town
    sub = FirmTarget(kind="farm_agent", stream="s12", email="sub@brokerage.example", stage="PAYING", name="Pat Agent",
                     org="Sample Realty", town="Newark", county="Essex",
                     data_json={"s12_mun": "0714", "s12_exclusive": True})
    other = FirmTarget(kind="farm_agent", stream="s12", email="late@otherbrokerage.example", stage="TRIAL",
                       name="Lou Late", org="Other Homes", town="Newark", county="Essex", data_json={"s12_mun": "0714"})
    db.add_all([sub, other])
    res = _run(db, tmp_path)
    [d] = res.outbound                                                   # no pitch into the exclusive town
    assert d.to == "sub@brokerage.example" and d.kind == "delivery" and d.priority == 1 and not d.commercial
    assert d.key == f"s12:deliver:{sub.id}:{WEEKDAY:%Y-%m}"
    assert d.subject == f"Newark market report: {WEEKDAY:%B %Y}" and d.body.startswith("Hi Pat,\n\n")
    [path] = d.attachments
    assert path.parent.name == str(sub.id) and path.name == f"s12_market_essex_newark_{WEEKDAY:%Y-%m}.pdf"
    assert "Presented by Pat Agent, Sample Realty" in _text(path)
    assert "Presented by" not in _text(_report(tmp_path))               # the generic copy carries no brand
    assert res.metrics["deliveries"] == 1 and res.metrics["deliveries_held_exclusive"] == 1
    assert res.metrics["towns_exclusive"] == 1 and res.metrics["pitches"] == 0
    row = fo.enqueue(db, d)
    assert row.status == "queued" and [a["name"] for a in row.attachments_json] == [path.name]
    db.commit()
    assert _run(db, tmp_path).outbound == []                             # once a calendar month


def test_the_dead_zone_line_shows_only_where_s7s_daily_pass_covers_the_town(db, tmp_path):
    from db.models import Listing, SwarmState, utcnow
    from engine.streams import s7_highvalue_harvest as hv
    _deeds(db, mun="0713")                                               # Montclair: on s7's high-value town list
    # two homes inside a dead zone, one past its edge, and a commercial listing (no graduated fee) inside one
    for n, (price, kind) in enumerate(((1_005_000, "single_family"), (2_010_000, "condos"),
                                       (1_050_000, "single_family"), (1_004_000, "commercial"))):
        db.add(Listing(source="homeharvest", source_ref=f"HV{n}", status="for_sale", city="Montclair",
                       address=f"{n} Sample Ter", zip_code="07042", list_price=price, property_type=kind))
    db.flush()
    _run(db, tmp_path)
    assert "dead zone" not in _text(_report(tmp_path))                   # s7 hasn't answered for Montclair: unproven
    stamp = utcnow().isoformat()
    db.add(SwarmState(key=hv.HISTORY_KEY, value_json={"zips": {
        hv.Query("Montclair, NJ", lo, hi).key: [stamp] for lo, hi in hv.query_ranges(hv.bands())}}))
    res = _run(db, tmp_path)
    text = _text(_report(tmp_path))
    assert "Graduated transfer-fee dead zones ($1,000,000-$1,010,500" in text
    assert "2 for-sale home listings in Montclair priced inside one" in text and "public for-sale listings" in text
    assert "Sample Ter" not in text and "$2,010,000" not in text          # a count, never a listing
    assert res.metrics["towns_with_data"] == 1


def test_through_the_firm_runner_the_pitch_queues_and_the_weekly_county_delivery_finds_no_s12_file(db, tmp_path,
                                                                                                  monkeypatch):
    from engine import swarm_firm
    monkeypatch.setitem(swarm_firm.STREAMS, "s12", ("engine.streams.s12_town_market_reports", "Town market reports"))
    _deeds(db)
    _agent(db)
    db.add(FirmTarget(kind="farm_agent", stream="s12", email="sub@brokerage.example", stage="PAYING", name="Pat Agent",
                      org="Sample Realty", town="Newark", county="Essex", data_json={"s12_mun": "0714"}))
    db.commit()
    out = swarm_firm.run_streams(now=WEEKDAY, out_dir=tmp_path / "firm", only=["s12"], force=True, send=True)
    rec = out["s12"]
    assert "error" not in rec and rec["queued"] == 2 and rec["delivered"] == 0   # our own monthly delivery, not fo's
    db.expire_all()
    rows = {r.kind: r for r in db.query(FirmOutbox).filter_by(stream="s12")}
    assert set(rows) == {"pitch", "sample", "delivery"}
    assert rows["pitch"].to_email == AGENT and rows["pitch"].attachments_json == [] and rows["sample"].status == "held"
    assert rows["delivery"].to_email == "sub@brokerage.example" and rows["delivery"].status == "queued"
    assert [a["name"] for a in rows["delivery"].attachments_json] == [f"s12_market_essex_newark_{WEEKDAY:%Y-%m}.pdf"]
