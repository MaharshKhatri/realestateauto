"""S7's high-value listing pass: the price bands, the provider call, refusals, budgets, the daily cadence, the stores
and the hand-off to s7's courtesy note. The provider and NJGIN are fakes: no test sends a request anywhere."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from db.models import Contact, DistressSignal, Lead, Listing, SignalType, SwarmState, utcnow
from engine import homeharvest_provider as hh
from engine.streams import s7_highvalue_harvest as hv
from engine.streams import s7_mansion_tax_cliff as s7
from engine.streams.base import StreamContext

DAY1 = datetime(2026, 11, 10, 7, 5)
OP = {"name": "Max Tester", "first": "Max", "company": "", "phone": "(973) 555-0100", "email": "max@example.com",
      "postal": "1 Main St, Newark NJ 07102"}
TOWNS = ["Sampleton, NJ", "Exampleville, NJ"]
LIM = {"harvest_towns": TOWNS, "harvest_cache_hours": 0}


@pytest.fixture(autouse=True)
def _no_block_left_behind():
    hh.clear_block()
    yield
    hh.clear_block()


class FakeGIS:
    """NJGIN answers nothing: the listing keeps its own point (an unmatched parcel), as on a failed match."""
    def __init__(self, fail: bool = False):
        self.fail = fail

    def resolve_municipality(self, county, name):
        if self.fail:
            raise ConnectionError("NJGIN down")
        return None

    def find_by_address(self, county, street, mun_code=None):
        return None


def row(n: int, price: float = 1_005_000, status: str = "for_sale", ptype: str = "single_family",
        email: str | None = "pat.agent@brokerage.example", agent: str = "Pat Agent") -> dict:
    adv = {"type": "seller", "name": agent, "email": email, "broker": {"name": "Sample Realty"},
           "phones": [{"number": "9735550100", "type": "Mobile"}, {"number": "9735550111", "type": "Fax"}]}
    return {"property_id": f"P{n}", "url": f"https://listing.example/P{n}", "status": status,
            "address": f"{n} Sample St", "city": "Sampleton", "state": "NJ", "zip": "07999",
            "property_type": ptype, "beds": 4, "baths": 3.5, "sqft": 3200, "year_built": 1990, "list_price": price,
            "list_date": "2026-10-20", "days_on_market": 21, "price_reduced": False,
            "description": "Bright colonial on a quiet street.", "lat": 40.75, "lon": -74.3,
            "original_list_price": None, "mls": "SAMPLEMLS", "estimated_value": None,
            "raw": {"advertisers": [adv], "location": {"county": {"name": "Essex"}}, "tags": []}}


class FakeProvider:
    """fetch(location, price_min, price_max): answers from a {(location, range index): rows} script."""
    def __init__(self, answers=None, blocked_at: int | None = None, fail_at: set[int] | None = None, clock=None):
        self.answers, self.blocked_at, self.fail_at, self.clock = answers or {}, blocked_at, fail_at or set(), clock
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, location, price_min, price_max):
        self.calls.append((location, price_min, price_max))
        if self.clock is not None:
            self.clock.t += 300
        n = len(self.calls)
        if n == self.blocked_at:
            raise hh.ProviderBlocked("Realtor.com refused the request (status=403)")
        if n in self.fail_at:
            raise hh.ProviderError("ConnectionError: reset")
        band = 0 if price_min < 1_500_000 else 1
        return list(self.answers.get((location, band), []))


def live(db, days_ago: float = 1.0) -> None:
    """The stale-listing harvest answered on this database: this is the machine that harvests listings."""
    stamp = (utcnow() - timedelta(days=days_ago)).isoformat()
    db.add(SwarmState(key="stale_listing_harvests", value_json={"zips": {"07102": [stamp]}}))
    db.flush()


def run(db, now=DAY1, provider=None, gis=None, **limits):
    return hv.harvest(db, now=now, limits={**LIM, **limits}, fetch=provider or FakeProvider(), gis=gis or FakeGIS())


# -- the plan -----------------------------------------------------------------------------------------------------
def test_one_band_per_dead_zone_with_a_margin_and_the_upper_bands_share_one_query():
    bands = hv.bands()
    assert bands == [(990_000, 1_070_000), (1_980_000, 2_080_000), (2_480_000, 2_570_000), (2_980_000, 3_070_000),
                     (3_480_000, 3_570_000)]
    for (lo, hi), (t, edge) in zip(bands, s7.dead_zones()):
        assert lo <= t - 10_000 and hi >= edge + 40_000                 # each band contains its zone with margin
        assert lo % 10_000 == 0 and hi % 10_000 == 0
    assert hv.query_ranges(bands) == [(990_000, 1_070_000), (1_980_000, 3_570_000)]
    assert hv.query_ranges(bands, merge_gap=0) == bands
    plan = hv.plan(["A, NJ", "B, NJ", "A, NJ"], hv.query_ranges(bands))
    assert [(q.location, q.price_min) for q in plan] == [("A, NJ", 990_000), ("B, NJ", 990_000),
                                                         ("A, NJ", 1_980_000), ("B, NJ", 1_980_000)]
    assert len(hv.TOWNS) >= 40 and len(set(hv.TOWNS)) == len(hv.TOWNS)
    assert all(t.endswith(", NJ") for t in hv.TOWNS)


def test_fetch_band_asks_the_provider_for_one_price_range_and_keeps_one_row_per_house(monkeypatch):
    seen = []

    def scrape(**kw):
        seen.append(kw)
        base = {"property_id": "P1", "href": "https://listing.example/P1", "status": "for_sale", "list_price": 1005000,
                "description": {"type": "single_family", "sqft": 3000, "text": None},
                "location": {"address": {"line": "1 Sample St", "city": "Sampleton", "postal_code": "07999",
                                         "state_code": "NJ", "coordinate": {"lat": 40.7, "lon": -74.3}},
                             "county": {"name": "Essex", "fips_code": "34013"}},
                "advertisers": [{"name": "Pat Agent", "email": "pat.agent@brokerage.example", "phones": []}],
                "photos": [{"href": "x"}] * 3}
        richer = {**base, "description": {**base["description"], "text": "Colonial."}}
        return [base, richer]
    monkeypatch.setattr(hh, "_scrape", scrape)
    rows = hv.fetch_band("Sampleton, NJ", 990_000, 1_070_000, limit=300, cache_hours=0)
    assert seen == [{"location": "Sampleton, NJ", "listing_type": "for_sale", "price_min": 990_000,
                     "price_max": 1_070_000, "extra_property_data": False, "limit": 300}]
    assert len(rows) == 1 and rows[0]["description"] == "Colonial." and rows[0]["list_price"] == 1005000
    assert set(rows[0]["raw"]) == {"advertisers", "location", "tags"}             # slim: nothing else is kept
    assert rows[0]["raw"]["location"]["county"]["name"] == "Essex"


def test_the_pass_calls_the_provider_with_price_min_and_max_for_every_town_and_range(db, monkeypatch):
    live(db)
    seen = []
    monkeypatch.setattr(hh, "_scrape", lambda **kw: seen.append(kw) or [])
    out = hv.harvest(db, now=DAY1, limits=LIM, gis=FakeGIS())
    assert out["status"] == "ran" and out["queries"] == 4
    assert [(k["location"], k["price_min"], k["price_max"]) for k in seen] == [
        ("Sampleton, NJ", 990_000, 1_070_000), ("Exampleville, NJ", 990_000, 1_070_000),
        ("Sampleton, NJ", 1_980_000, 3_570_000), ("Exampleville, NJ", 1_980_000, 3_570_000)]
    assert all(k["listing_type"] == "for_sale" and k["extra_property_data"] is False for k in seen)


# -- when it runs -------------------------------------------------------------------------------------------------
def test_it_runs_only_where_the_listing_harvest_runs(db):
    p = FakeProvider()
    assert run(db, provider=p) == {"status": "not_live"} and p.calls == []
    live(db, days_ago=5)
    assert run(db, provider=p) == {"status": "not_live"} and p.calls == []
    assert hv.listing_harvest_live(db) is False
    db.get(SwarmState, "stale_listing_harvests").value_json = {"zips": {"07102": [utcnow().isoformat()]}}
    db.flush()
    assert hv.listing_harvest_live(db) is True
    assert run(db, provider=p)["status"] == "ran" and len(p.calls) == 4


def test_once_a_day_and_the_next_day_starts_where_a_cut_off_stopped(db):
    live(db)
    p = FakeProvider()
    assert run(db, provider=p, harvest_max_queries=3)["queries"] == 3
    assert run(db, now=DAY1 + timedelta(hours=6), provider=p) == {"status": "done_today"}
    assert len(p.calls) == 3
    run(db, now=DAY1 + timedelta(days=1), provider=p, harvest_max_queries=2)
    # day 2 begins with the query day 1 never asked, then wraps around
    assert p.calls[3:] == [("Exampleville, NJ", 1_980_000, 3_570_000), ("Sampleton, NJ", 990_000, 1_070_000)]
    assert hv.state(db)["cursor"] == 1


def test_the_time_budget_stops_the_pass(db):
    live(db)

    class Clock:
        t = 0.0

        def __call__(self):
            return self.t
    clock = Clock()
    p = FakeProvider(clock=clock)                       # each query takes 5 minutes
    out = hv.harvest(db, now=DAY1, limits={**LIM, "harvest_max_minutes": 12}, fetch=p, gis=FakeGIS(), clock=clock)
    assert len(p.calls) == 3 and out["cut_off"] == 1 and out["status"] == "ran"


def test_a_refusal_stops_at_once_is_recorded_for_every_harvest_and_is_honored_the_next_day(db):
    live(db)
    p = FakeProvider({("Sampleton, NJ", 0): [row(1)]}, blocked_at=2)
    out = run(db, provider=p)
    assert out["status"] == "blocked" and out["blocked"] == 1 and len(p.calls) == 2
    block = db.get(SwarmState, "listing_provider_blocked").value_json      # the stale-listing harvest reads it too
    assert block["zip"] == "Exampleville, NJ" and block["zips_harvested"] == 1 and block["until"]
    assert hh.blocked()
    # what came back before the refusal is kept
    assert db.scalar(select(Listing).where(Listing.source_ref == "P1")) is not None
    assert hv.state(db)["cursor"] == 1                                     # tomorrow starts with the refused query
    hh.clear_block()                                                       # a new process: only the database knows
    again = run(db, now=DAY1 + timedelta(days=1), provider=p)
    assert again == {"status": "blocked_before"} and len(p.calls) == 2 and hh.blocked()


def test_three_failures_in_a_row_stop_the_pass(db):
    live(db)
    p = FakeProvider(fail_at={2, 3, 4})
    out = run(db, provider=p)
    assert out["status"] == "errors" and out["errors"] == 3 and len(p.calls) == 4
    assert hv.state(db)["cursor"] == 1                                     # the failed streak is asked again first


# -- what it stores -----------------------------------------------------------------------------------------------
def test_dead_zone_listings_become_leads_with_their_agents_and_nothing_else_is_stored(db):
    live(db)
    answers = {("Sampleton, NJ", 0): [row(1), row(2, price=1_050_000), row(3, status="pending"),
                                      row(4, ptype="land"), row(5, price=995_000)],
               ("Exampleville, NJ", 1): [row(6, price=2_010_000, email="lee.broker@brokerage.example",
                                             agent="Lee Broker")]}
    out = run(db, provider=FakeProvider(answers))
    assert out["rows"] == 6 and out["in_zone"] == 4 and out["new_leads"] == 2
    refs = set(db.scalars(select(Listing.source_ref)))
    assert refs == {"P1", "P6"}                                            # out of zone, pending, land: not stored
    sig = db.scalar(select(DistressSignal).where(DistressSignal.source_ref == "P1"))
    assert sig.signal_type == SignalType.STALE_LISTING and sig.active
    assert sig.raw_metadata_json["harvest"] == "s7_highvalue"
    assert sig.raw_metadata_json["listing_agent_email"] == "pat.agent@brokerage.example"
    lead = db.scalar(select(Lead).where(Lead.property_id == sig.property_id))
    agents = db.scalars(select(Contact).where(Contact.lead_id == lead.id)).all()
    assert {(c.contact_type, c.email, c.phone) for c in agents} == {
        ("LISTING_AGENT", "pat.agent@brokerage.example", "9735550100")}  # the fax line is never a phone
    assert lead.property.county == "Essex" and lead.property.lat == 40.75
    assert set(hv.state(db)["refs"]) == {"P1", "P6"}


def test_s7_writes_its_courtesy_note_on_what_the_pass_found(db, tmp_path):
    live(db)
    run(db, provider=FakeProvider({("Sampleton, NJ", 0): [row(1)]}))
    res = s7.STREAM.run(db, StreamContext(now=DAY1, out_dir=tmp_path, operator=OP, limits={"harvest": False}))
    [ob] = res.outbound
    assert ob.to == "pat.agent@brokerage.example" and ob.attachments == [] and ob.sample_attachments
    assert ob.body.startswith("Hi Pat,\n\nQuick heads-up on 1 Sample St: at $1,005,000 your seller crosses")
    assert ob.subject == "1 Sample St and the $1M transfer-fee line"


def test_listings_that_leave_the_zone_or_the_market_leave_outreach(db):
    live(db)
    day = [{("Sampleton, NJ", 0): [row(1), row(2), row(3)]}]
    p = FakeProvider(day[0])
    run(db, provider=p)
    active = lambda ref: db.scalar(select(DistressSignal.active).where(DistressSignal.source_ref == ref))  # noqa: E731
    assert active("P1") and active("P2") and active("P3")
    # day 2: P1 went pending, P2 dropped its price under the line, P3 is missing
    p.answers = {("Sampleton, NJ", 0): [row(1, status="pending"), row(2, price=999_000)]}
    out = run(db, now=DAY1 + timedelta(days=1), provider=p)
    assert out["retired"] == 2 and not active("P1") and not active("P2") and active("P3")
    assert db.scalar(select(Listing.status).where(Listing.source_ref == "P1")) == "pending"
    assert db.scalar(select(Listing.list_price).where(Listing.source_ref == "P2")) == 999_000
    # P3 missing from three answered harvests of its query: off the market
    run(db, now=DAY1 + timedelta(days=2), provider=p)
    assert active("P3")
    out = run(db, now=DAY1 + timedelta(days=3), provider=p)
    assert out["swept"] == 1 and not active("P3")
    assert db.scalar(select(Listing.status).where(Listing.source_ref == "P3")) == "off_market"
    assert "P3" not in hv.state(db)["refs"]
    # P2 back in the zone: the signal comes back
    p.answers = {("Sampleton, NJ", 0): [row(2, price=1_004_000)]}
    run(db, now=DAY1 + timedelta(days=4), provider=p)
    assert active("P2")


def test_a_listing_whose_town_left_the_list_goes_once_nobody_has_seen_it_for_days(db):
    live(db)
    run(db, provider=FakeProvider({("Sampleton, NJ", 0): [row(1)]}))
    p = FakeProvider()
    run(db, now=DAY1 + timedelta(days=1), provider=p, harvest_towns=["Exampleville, NJ"])
    assert "P1" in hv.state(db)["refs"]                                    # seen yesterday: still tracked
    db.scalar(select(Listing).where(Listing.source_ref == "P1")).last_seen_at = utcnow() - timedelta(days=4)
    db.flush()
    out = run(db, now=DAY1 + timedelta(days=2), provider=p, harvest_towns=["Exampleville, NJ"])
    assert out["swept"] == 1 and "P1" not in hv.state(db)["refs"]
    assert not db.scalar(select(DistressSignal.active).where(DistressSignal.source_ref == "P1"))


def test_one_bad_listing_is_skipped_and_the_new_lead_cap_holds(db):
    live(db)
    out = run(db, provider=FakeProvider({("Sampleton, NJ", 0): [row(1), row(2)]}), gis=FakeGIS(fail=True))
    assert out["store_errors"] == 2 and out.get("new_leads", 0) == 0
    assert db.scalar(select(DistressSignal.id)) is None                    # the savepoint dropped the half-made lead
    out = run(db, now=DAY1 + timedelta(days=1), provider=FakeProvider({("Sampleton, NJ", 0): [row(1), row(2), row(3)]}),
              harvest_max_new_leads=2)
    assert out["new_leads"] == 2 and out["over_cap"] == 1
