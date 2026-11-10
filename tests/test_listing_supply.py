"""Wider stale-listing supply, safely (2026-11-03): ZIPs from settings + auto-expansion towns, a polite stop on a
provider refusal, listings that leave the market leave outreach, fax lines never callable, and every listing lead
gets a valuation. Offline: the listing provider, NJGIN and the underwriter are fakes."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

import agents.scrapers.stale_listing_distress_agent as sl
from agents.enrichment.listing_agent_enricher import ListingAgentEnricher, advertiser_contacts, scrub_fax_contacts
from agents.enrichment.signal_stacker import ListingIndex, SignalStackerAgent
from agents.enrichment.skiptrace_pipeline import import_found
from db.models import (Contact, DistressSignal, Interaction, Lead, Listing, Property, SignalType, SwarmState,
                       Valuation, utcnow)
from engine import agent_outreach as ao
from engine import autopilot as ap
from engine import compliance, crm
from engine import homeharvest_provider as hh
from engine.scoring import has_agent_contact, listing_off_market, ready_to_call
from tests.factories import add_contact, make_lead

QUANT = {"buyer_max": 300000, "mao_target": 270000, "mao_walkaway": 290000, "opening_offer": 237600,
         "fee_target": 20000, "alpha_effective": 0.72,
         "buyer": {"rehab": 60000, "rehab_contingency": 6000, "taxes_held": 4500, "carry": 2100, "capital_cost": 22000,
                   "holding_months": 6}}


@pytest.fixture(autouse=True)
def _no_provider_pause():
    hh.clear_block()
    yield
    hh.clear_block()


class FakeGIS:
    """No NJGIN calls: every listing stays on an UNMATCHED parcel."""

    def __init__(self, *a, **kw):
        self.http = SimpleNamespace(stats_summary=lambda: {})

    def resolve_municipality(self, county, city):
        return None

    def find_by_address(self, county, street, mun_code):
        return None


class FakeListings:
    """homeharvest.active_listings stand-in: pages per ZIP, a ZIP can raise, every call is recorded."""

    def __init__(self):
        self.pages: dict[str, list[dict] | Exception] = {}
        self.calls: list[str] = []

    def __call__(self, zip_code, past_days=None):
        self.calls.append(zip_code)
        page = self.pages.get(zip_code, [])
        if isinstance(page, Exception):
            raise page
        return [dict(r) for r in page]


@pytest.fixture()
def provider(monkeypatch):
    fake = FakeListings()
    monkeypatch.setattr(hh, "active_listings", fake)
    monkeypatch.setattr(sl, "NJGISClient", FakeGIS)
    return fake


def row(ref: str, zip_code: str = "07106", *, status: str = "for_sale", dom: int = 70, price: float = 300000,
        phones: list[dict] | None = None, email: str | None = "pat@broker.test", lat: float | None = 40.731,
        lon: float | None = -74.201) -> dict:
    n = int("".join(ch for ch in ref if ch.isdigit()) or 1)
    return {"property_id": ref, "url": f"https://listing.test/{ref}", "status": status, "address": f"{n} Sample Ave",
            "city": "Newark", "zip": zip_code, "property_type": "single_family", "beds": 3, "baths": 1.5,
            "sqft": 1400, "year_built": 1925, "list_price": price, "list_date": None, "days_on_market": dom,
            "price_reduced": None, "description": "Lovely colonial.", "lat": lat, "lon": lon, "mls": "GSNJ",
            "raw": {"location": {"county": {"name": "Essex"}},
                    "advertisers": [{"type": "seller", "name": "Pat Agent", "email": email,
                                     "phones": phones if phones is not None else [{"number": "973-555-0100",
                                                                                     "type": "Mobile"}],
                                     "broker": {"name": "Garden Realty"}}]}}


def stale_signal(db, ref: str) -> DistressSignal | None:
    db.expire_all()
    return db.query(DistressSignal).filter_by(signal_type=SignalType.STALE_LISTING, source_ref=ref).one_or_none()


def run_harvest(zips: list[str]):
    return sl.StaleListingDistressAgent(zips=zips).execute()


# -- 1. where we harvest ------------------------------------------------------------------------------
def test_settings_block_holds_the_zips_and_the_code_list_is_only_a_fallback():
    cfg = sl.stale_cfg()
    zips = [str(z) for z in cfg["zips"]]
    assert cfg["min_days_on_market"] == 60 and cfg["max_list_price"] == 900000
    assert len(zips) == len(set(zips)) == 117 and all(sl.ZIP_RE.match(z) for z in zips)
    assert set(sl.DEFAULT_ZIPS) <= set(zips)
    assert {"08081", "07470", "08854", "07205", "07003", "07666", "07087", "07748"} <= set(zips)
    assert {"08873", "08823", "07801", "07960"} <= set(zips)          # Somerset + Morris (2026-11-10)


def test_harvest_zips_merge_auto_expansion_towns_from_our_own_parcels(db):
    def parcel(i, zip_code, town="HILLSIDE TWP", county="Union"):
        db.add(Property(parcel_id=f"UNION-2009-{i}-1", county=county, municipality=town, zip_code=zip_code,
                        data_sources=[], raw_json={}))
    for i in range(6):
        parcel(i, "07205")
    parcel(50, "07112")                                    # 1 stray parcel: not a Hillside ZIP
    db.add(SwarmState(key="auto_expansion", value_json={"municipalities": [
        {"county": "UNION", "name": "HILLSIDE TWP"}, {"county": "HUDSON", "name": "GUTTENBERG TOWN"}]}))
    db.flush()
    assert sl.auto_expansion_zips(db, {}) == {"HILLSIDE TWP (Union)": ["07205"], "GUTTENBERG TOWN (Hudson)": []}
    zips, notes = sl.harvest_zips(db, {"zips": ["07106", "07106", 7106, "08830"]})
    assert zips == ["07106", "08830", "07205"]
    assert any("isn't" in n or "aren't" in n for n in notes) and any("GUTTENBERG" in n for n in notes)
    assert any("added 1 ZIPs: 07205" in n for n in notes)
    assert sl.harvest_zips(db, {"zips": ["07106"], "include_auto_expansion": False})[0] == ["07106"]
    assert sl.harvest_zips(db, {"zips": ["07106"]}, override=["07999"])[0] == ["07999"]   # --zip: exactly these
    assert sl.harvest_zips(db, {})[0][: len(sl.DEFAULT_ZIPS)] == sl.DEFAULT_ZIPS           # no block: the fallback


# -- 2. a refusal is honored -------------------------------------------------------------------------------
def test_provider_refusal_stops_politely_and_keeps_what_was_harvested(db, provider):
    provider.pages = {"07106": [row("L101")], "07107": hh.ProviderBlocked("403 Forbidden"), "07108": [row("L301")]}
    run = run_harvest(["07106", "07107", "07108"])
    assert provider.calls == ["07106", "07107"]                         # the third ZIP was never asked
    assert run.status == "partial" and "Stopped politely" in (run.self_improvement_notes or "")
    assert run.metrics_json["provider_blocked"]["zip"] == "07107" and run.metrics_json["zips_harvested"] == 1
    assert stale_signal(db, "L101").active                              # the ZIP harvested before the refusal stays
    assert hh.blocked()                                                 # nothing else in this process asks either
    st = db.get(SwarmState, sl.BLOCK_KEY)
    assert st.value_json["zips_not_asked"] == 2 and st.value_json["until"] > st.value_json["at"]
    # The next run (another process: only the stored refusal) asks nothing until the pause is over.
    hh.clear_block()
    again = run_harvest(["07106", "07108"])
    assert provider.calls == ["07106", "07107"] and again.metrics_json["provider_blocked"]["skipped_run"]
    assert "asked nothing" in again.self_improvement_notes and hh.blocked()
    hh.clear_block()
    st = db.get(SwarmState, sl.BLOCK_KEY)
    st.value_json = {**st.value_json, "until": (utcnow() - timedelta(minutes=1)).isoformat(timespec="seconds")}
    db.commit()
    assert run_harvest(["07108"]).status == "success" and provider.calls[-1] == "07108"


def test_a_403_pauses_every_later_request_in_the_process(monkeypatch):
    homeharvest = pytest.importorskip("homeharvest")
    from homeharvest.exceptions import AuthenticationError
    calls = []

    def refuse(**kw):
        calls.append(kw)
        raise AuthenticationError("Forbidden", response=SimpleNamespace(status_code=403))
    monkeypatch.setattr(hh, "MIN_INTERVAL_S", 0)
    monkeypatch.setattr(homeharvest, "scrape_property", refuse)
    with pytest.raises(hh.ProviderBlocked, match="status=403"):
        hh.active_listings("07106")
    monkeypatch.setattr(homeharvest, "scrape_property", lambda **kw: calls.append(kw) or [])
    with pytest.raises(hh.ProviderBlocked, match="not requested"):
        hh.sold_comps("07106")
    assert len(calls) == 1                                              # the refusal was not asked again
    hh.clear_block()
    assert hh.active_listings("07106") == [] and len(calls) == 2


# -- 3. listings that leave the market leave outreach --------------------------------------------------
def test_harvest_stores_lead_unmatched_point_and_never_a_fax_line(db, provider):
    provider.pages = {"07106": [
        row("L101", phones=[{"number": "(973) 555-0199", "type": "Fax"}, {"number": "973-555-0100", "type": "Mobile"}]),
        row("L102", dom=30), row("L103", status="pending", dom=200)]}
    run = run_harvest(["07106"])
    assert run.status == "success" and run.metrics_json["flagged_by_zip"] == {"07106": 1}
    sig = stale_signal(db, "L101")
    meta = sig.raw_metadata_json
    assert sig.active and meta["listing_agent_phone"] == "973-555-0100" and meta["listing_agent_fax"] == ["9735550199"]
    assert stale_signal(db, "L102") is None and stale_signal(db, "L103") is None    # 30 DOM; pending
    prop = sig.property
    assert "UNMATCHED" in prop.parcel_id and (prop.lat, prop.lon) == (40.731, -74.201)   # valuable: comps need a point
    ListingAgentEnricher().execute()
    db.expire_all()
    phones = {c.phone for c in db.query(Contact).filter_by(lead_id=prop.lead.id)}
    assert "9735550100" in phones and "9735550199" not in phones


def test_pending_retires_the_signal_and_back_on_market_restores_it(db, provider):
    provider.pages = {"07106": [row("L101")]}
    run_harvest(["07106"])
    lead = stale_signal(db, "L101").property.lead
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test")
    db.add(Valuation(property=lead.property, arv_estimate=400000, as_is_value=320000, est_repair_cost=50000,
                     mao_conservative=235000, mao_aggressive=250000, anchor_offer=206800, confidence_score=80,
                     verdict="STRONG", is_current=True, assumptions_json={}, comps_json=[], comp_count=4))
    db.flush()
    db.refresh(lead.property)
    assert ready_to_call(lead) == (True, "listing agent phone")
    lead.ready_to_call = True
    db.commit()
    provider.pages = {"07106": [row("L101", status="contingent")]}
    run = run_harvest(["07106"])
    sig = stale_signal(db, "L101")
    assert not sig.active and sig.status == "contingent"
    assert "contingent" in sig.raw_metadata_json["deactivated"]["reason"]
    assert run.metrics_json["stale_signals_deactivated"] == 1
    assert listing_off_market(sig.property) and not sig.property.lead.ready_to_call
    provider.pages = {"07106": [row("L101", dom=80)]}
    run_harvest(["07106"])
    sig = stale_signal(db, "L101")
    assert sig.active and "deactivated" not in sig.raw_metadata_json and not listing_off_market(sig.property)


def test_listing_missing_from_three_harvests_goes_off_market(db, provider):
    provider.pages = {"07106": [row("L101"), row("L102")]}
    run_harvest(["07106"])
    provider.pages = {"07106": [row("L102")]}
    run_harvest(["07106"])
    run_harvest(["07106"])
    assert stale_signal(db, "L101").active                     # missed 2 harvests: not yet
    provider.pages = {"07106": []}
    run_harvest(["07106"])                                     # an empty answer proves nothing: not counted
    assert stale_signal(db, "L101").active
    provider.pages = {"07106": [row("L102")]}
    run = run_harvest(["07106"])
    sig = stale_signal(db, "L101")
    assert not sig.active and sig.status == "off_market" and run.metrics_json["listings_off_market"] == 1
    assert "not seen in the last 3 harvests" in sig.raw_metadata_json["deactivated"]["reason"]
    lst = db.query(Listing).filter_by(source_ref="L101").one()
    assert lst.status == "off_market" and lst.raw_json["status_before_off_market"] == "for_sale"
    assert stale_signal(db, "L102").active
    assert ListingIndex.build([lst]).by_key == {}               # the stacker no longer matches it


def test_stacker_retires_listed_distress_when_the_listing_goes_pending(db):
    fore = make_lead(db, street_address="12 TEST ST", zip_code="07106")
    lst = Listing(source="homeharvest", source_ref="L-12", address="12 Test Street", zip_code="07106",
                  status="for_sale", list_price=300000, days_on_market=60, url="https://listing.test/L-12")
    db.add(lst)
    db.commit()
    SignalStackerAgent(no_agents=True).execute()
    db.expire_all()
    sig = db.query(DistressSignal).filter_by(property_id=fore.property_id, signal_type=SignalType.ACTIVE_LISTING).one()
    assert sig.active
    db.get(Listing, lst.id).status = "pending"
    db.commit()
    SignalStackerAgent(no_agents=True).execute()
    db.expire_all()
    assert not db.get(DistressSignal, sig.id).active


def listed(db, list_price=380000, **kw):
    lead = make_lead(db, sheriff_days=None, upset=None, stale_listing=True, list_price=list_price,
                     assumptions={"quant": QUANT, "sqft_source": "record"}, **kw)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test")
    return lead


def retire(db, lead: Lead) -> None:
    for s in lead.property.signals:
        if s.signal_type == SignalType.STALE_LISTING:
            s.active = False
    db.flush()


def test_an_off_market_listing_drops_out_of_every_outreach_queue(db):
    live, gone = listed(db), listed(db)
    owner = listed(db)
    add_contact(db, owner, contact_type="OWNER", name="Quinn Sample", phone="9735551234")
    for lead in (live, gone, owner):
        assert ready_to_call(lead)[0]
    retire(db, gone)
    retire(db, owner)
    assert listing_off_market(gone.property) and not has_agent_contact(gone.property)
    assert ready_to_call(gone) == (False, None)                          # its agent has nothing to sell us
    assert ready_to_call(owner) == (True, "owner phone")                 # the owner is still a lead
    assert ready_to_call(live) == (True, "listing agent phone")
    ids = {lead.id for lead in ao.candidates(db, 20)}
    assert live.id in ids and gone.id not in ids
    assert crm.best_phone(db, gone) is not None                          # a follow-up already running keeps its number
    assert crm.best_phone(db, gone, live_listing_only=True) is None
    queued = {i.lead.id for i in crm.call_queue(db, limit=20)}
    assert live.id in queued and gone.id not in queued
    d = ap.deck(db)
    deck_ids = {c["lead_id"] for c in d["cards"]}
    assert live.id in deck_ids and gone.id not in deck_ids


def test_due_followup_on_an_off_market_listing_stays_only_after_the_agent_replied(db):
    gone = listed(db)
    retire(db, gone)
    gone.next_followup_at = utcnow() - timedelta(days=1)          # due in any time zone
    gone.last_contact_at = utcnow() - timedelta(days=3)
    db.flush()
    d = ap.deck(db)
    assert gone.id not in {c["lead_id"] for c in d["cards"]} and d["hidden_off_market"] == 1   # no cold nudge
    db.add(Interaction(lead_id=gone.id, channel="email_in", outcome="AGENT_REPLIED",
                       notes="under contract, send a backup", occurred_at=utcnow()))
    db.flush()
    d = ap.deck(db)
    assert d["hidden_off_market"] == 0


# -- 4. fax lines are never callable -----------------------------------------------------------------------
def test_advertiser_fax_lines_are_dropped():
    raw = {"advertisers": [
        {"type": "seller", "name": "Pat Agent", "email": "pat@broker.test",
         "phones": [{"number": "973-555-0190", "type": "Fax"}],
         "office": {"name": "Garden Realty", "phones": [{"number": "973-555-0191", "type": "Office Fax"},
                                                        {"number": "973-555-0200", "type": "Office"}]}},
        {"type": "seller", "name": "Fax Only", "phones": [{"number": "973-555-0192", "type": "fax"}]}]}
    out = advertiser_contacts(raw)
    assert {c["phone"] for c in out} == {None, "9735550200"}
    agent = next(c for c in out if c["name"] == "Pat Agent")
    assert agent["phone"] is None and agent["phone_type"] is None and agent["email"] == "pat@broker.test"
    assert not any(c["name"] == "Fax Only" for c in out)               # a fax and nothing else: nobody to reach


def test_saved_fax_numbers_leave_every_call_list(db):
    lead = make_lead(db)
    fax = add_contact(db, lead, contact_type="OWNER", name="Quinn Sample", phone="9735550190", phone_type="fax")
    assert compliance.is_fax_contact(fax) and not compliance.callable_phone(fax)
    assert compliance.can_call(db, fax)[0] is False and "fax" in compliance.can_call(db, fax)[1]
    assert ready_to_call(lead) == (False, None)
    assert crm.best_phone(db, lead) is None and lead.id not in {i.lead.id for i in crm.call_queue(db, limit=20)}
    phone = add_contact(db, lead, contact_type="OWNER", name="Quinn Sample", phone="9735551234", phone_type="wireless")
    assert crm.best_phone(db, lead).id == phone.id and ready_to_call(lead) == (True, "owner phone")


def test_scrub_moves_old_fax_numbers_off_contacts(db):
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    sig = next(s for s in lead.property.signals if s.signal_type == SignalType.STALE_LISTING)
    sig.raw_metadata_json = {**sig.raw_metadata_json, "listing_agent_fax": ["9735550193"]}
    with_email = add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550190",
                             phone_type="fax", email="pat@broker.test")
    bare = add_contact(db, lead, contact_type="LISTING_AGENT", name="Office", phone="9735550191", phone_type="fax")
    used = add_contact(db, lead, contact_type="LISTING_AGENT", name="Old Line", phone="9735550192", phone_type="fax")
    unlabeled = add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550193")
    real = add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100",
                       phone_type="wireless")
    db.add(Interaction(lead_id=lead.id, contact_id=used.id, channel="phone", outcome="NO_ANSWER", occurred_at=utcnow()))
    db.flush()
    ids = (with_email.id, bare.id, used.id, unlabeled.id)
    assert scrub_fax_contacts(db) == 4
    db.expire_all()
    a, b, c, d = (db.get(Contact, i) for i in ids)
    assert a.phone is None and a.email == "pat@broker.test" and a.raw_json["fax"] == "9735550190"
    assert b is None and d is None                                      # nothing left to reach, no history
    assert c is not None and c.phone is None and c.raw_json["fax"] == "9735550192"   # its call history stays
    assert db.get(Contact, real.id).phone == "9735550100" and scrub_fax_contacts(db) == 0


def test_skiptrace_import_skips_fax_numbers(db):
    lead = make_lead(db)
    import_found(db, lead, name="Quinn Sample", role="OWNER",
                 phones=[("9735550190", "fax"), ("9735551234", "wireless")], emails=[])
    db.flush()
    db.refresh(lead)
    assert {c.phone for c in lead.contacts} == {"9735551234"}


# -- 5. every listing lead gets a number -----------------------------------------------------------------
def test_value_new_listings_values_unvalued_listing_leads_by_priority(db, monkeypatch):
    from engine import underwriter as uw
    from swarm.orchestrator import JOBS, REGISTRY

    def fake_underwrite(self, session, prop):
        if prop.street_address.startswith("1 "):
            return None                                                 # no comps came back
        v = Valuation(property=prop, arv_estimate=420000, as_is_value=330000, est_repair_cost=50000,
                      rehab_tier="MEDIUM", target_assignment_fee=20000, mao_conservative=250000,
                      mao_aggressive=270000, anchor_offer=220000, confidence_score=70, verdict="WORKABLE",
                      is_current=True, assumptions_json={}, comps_json=[], comp_count=4, calculated_at=utcnow())
        session.add(v)
        session.flush()
        return v
    monkeypatch.setattr(uw.Underwriter, "underwrite", fake_underwrite)

    def fresh(street, priority, **kw):
        lead = make_lead(db, sheriff_days=None, verdict=None, stale_listing=True, street_address=street, **kw)
        lead.priority_rank_score = priority
        return lead
    nocomps, top, second, third = fresh("1 NOCOMP AVE", 90), fresh("20 FIRST AVE", 80), fresh("30 NEXT AVE", 60), \
        fresh("40 LAST AVE", 40)
    valued = make_lead(db, sheriff_days=None, stale_listing=True)        # already has a current valuation
    gone = fresh("50 GONE AVE", 99)
    retire(db, gone)                                                     # listing off the market: not worth a valuation
    plain = make_lead(db, sheriff_days=20, verdict=None)                 # not a listing lead
    db.commit()
    assert [lead.id for lead in uw.unvalued_listing_leads(db)] == [nocomps.id, top.id, second.id, third.id]

    run = uw.NewListingValuationAgent(public_only=True, cap=3, max_minutes=0).execute()
    m = run.metrics_json
    assert run.status == "success" and m["tried"] == 3 and m["valued"] == 2 and m["left_for_next_run"] == 1
    db.expire_all()
    cur = {lid: db.get(Lead, lid).property.current_valuation for lid in (nocomps.id, top.id, second.id, third.id)}
    assert cur[nocomps.id] is None and cur[top.id] and cur[second.id] and cur[third.id] is None
    assert db.get(Lead, top.id).score_breakdown_json["viability_basis"] == "valuation"   # re-scored at once
    assert str(nocomps.id) in db.get(SwarmState, uw.NEW_LISTING_STATE).value_json["no_valuation"]
    assert db.get(Lead, plain.id).property.current_valuation is None and valued.property.current_valuation

    again = uw.NewListingValuationAgent(public_only=True, cap=3, max_minutes=0).execute()
    assert again.metrics_json["waiting_retry"] == 1 and again.metrics_json["valued"] == 1   # the no-comps lead waits
    db.expire_all()
    assert db.get(Lead, third.id).property.current_valuation is not None

    assert "value_new_listings" in REGISTRY
    steps = JOBS["CompUnderwriterAgent"]
    assert steps.index("underwriter") < steps.index("value_new_listings") < steps.index("outlier_guard")
    from core.config import settings
    assert int(settings()["underwrite"]["new_listing_cap"]) == 300


def test_the_nightly_schedule_gives_value_new_listings_its_long_budget(monkeypatch):
    import apscheduler.schedulers.blocking as blocking

    from swarm import orchestrator
    jobs: dict[str, dict] = {}

    class FakeScheduler:
        def __init__(self, timezone=None):
            pass

        def add_job(self, func, trigger, args=None, kwargs=None, id=None, **kw):
            jobs[id] = {"args": args, "kwargs": kwargs or {}}

        def start(self):
            pass
    monkeypatch.setattr(blocking, "BlockingScheduler", FakeScheduler)
    orchestrator.daemon()
    assert jobs["value_new_listings"]["kwargs"] == {"max_minutes": 120}
    assert jobs["stale_listings"]["kwargs"] == {} and jobs["underwriter"]["args"] == ["underwriter"]
