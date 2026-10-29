"""engine/clipboard_skiptrace.py: the queue, parsing the three people-search layouts (synthetic fixtures with invented
names), ranking, the DNC scrub, saving and the READY_TO_CALL flip."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from db.models import Contact, SignalType
from db.repository import upsert_signal
from engine import clipboard_skiptrace as cs
from engine import compliance
from tests.factories import add_contact, make_lead

FIX = Path(__file__).parent / "fixtures" / "skiptrace"
NOON = datetime(2026, 10, 20, 12, 0)      # a Tuesday at noon: inside calling hours


def page(name: str) -> str:
    return (FIX / f"{name}.txt").read_text()


# -- parsing --------------------------------------------------------------------------------------------
def test_parse_truepeoplesearch_layout():
    p = cs.parse_page(page("truepeoplesearch_detail"))
    assert p.name == "Quinn Q Sample" and p.age == 67 and not p.deceased and p.site == "truepeoplesearch"
    assert p.current_address == "12 Test St, Newark, NJ 07106"
    assert p.previous_addresses == ["8 Old Mill Rd, Edison, NJ 08817", "44 Harbor Ln Apt 2B, Tampa, FL 33602"]
    by = {h.digits: h for h in p.phones}
    assert by["9735550101"].kind == "wireless" and by["9735550101"].last_reported == "Jan 2026" and by["9735550101"].primary
    assert by["9735550102"].kind == "landline" and by["9735550102"].reported_key == 201903 and not by["9735550102"].primary
    assert by["2015550103"].kind == "wireless" and by["2015550103"].reported_key == 202108
    assert p.emails == ["qsample58@example.net", "quinn.sample@example.com"]
    assert p.relatives[:2] == [{"name": "Jordan Example", "age": 41}, {"name": "Morgan Sample", "age": 70}]


def test_parse_fastpeoplesearch_and_cyberbackgroundchecks_layouts():
    f = cs.parse_page(page("fastpeoplesearch_detail"))
    by = {h.digits: h for h in f.phones}
    assert f.name == "Quinn Q Sample" and f.age == 67 and f.site == "fastpeoplesearch"
    assert by["9735550101"].primary and by["9735550101"].kind == "wireless"
    assert not by["9735550102"].primary and by["9735550102"].kind == "landline"
    assert by["8625550104"].kind == "voip" and by["8625550104"].last_reported == "Nov 2024"
    assert f.relatives == [{"name": "Jordan Example", "age": None}, {"name": "Morgan Sample", "age": None}]
    c = cs.parse_page(page("cyberbackgroundchecks_detail"))
    by = {h.digits: h for h in c.phones}
    assert c.site == "cyberbackgroundchecks" and by["9735550101"].reported_key == 202601 and by["9735550101"].primary
    assert by["9735550105"].kind == "wireless" and by["9735550105"].reported_key == 201607
    assert by["9735550102"].kind == "landline" and not by["9735550102"].primary
    assert c.relatives == [{"name": "Jordan Example", "age": 41}, {"name": "Morgan Sample", "age": 70}]
    assert c.previous_addresses[0] == "8 Old Mill Rd, Edison, NJ 08817"


def test_parse_deceased_and_junk():
    d = cs.parse_page(page("deceased_relatives"))
    assert d.deceased and d.age == 88 and d.relatives == [{"name": "Jordan Example", "age": 61}, {"name": "Morgan Sample", "age": 58}]
    assert [h.kind for h in d.phones] == ["landline"] and any("deceased" in n for n in d.notes)
    j = cs.parse_page(page("junk_paste"))
    assert j.phones == [] and j.name is None and j.emails == [] and any("no phone numbers" in n for n in j.notes)
    assert cs.parse_page("").phones == []


def test_rank_wireless_recent_primary_first():
    p = cs.parse_page(page("truepeoplesearch_detail"))
    assert [h.digits for h in cs.rank(p)] == ["9735550101", "2015550103", "9735550102"]
    f = cs.parse_page(page("fastpeoplesearch_detail"))
    assert [h.kind for h in cs.rank(f)] == ["wireless", "landline", "voip"]
    dup = cs.parse_page("(973) 555-0101 - Wireless\nLast reported Jan 2026\n973-555-0101\n973.555.0101 Mobile")
    assert len(cs.rank(dup)) == 1


# -- scrub / save / ready flip ----------------------------------------------------------------------------
def test_scrub_marks_internal_dnc_and_unscrubbed(db):
    p = cs.parse_page(page("truepeoplesearch_detail"))
    hits = cs.scrub(db, cs.rank(p), now=NOON)
    assert {h.status for h in hits} == {"not_checked"} and not any(h.callable_now for h in hits)   # no FTC data loaded
    assert all("unscrubbed" in h.reason for h in hits)
    compliance.add_internal(db, "9735550101", notes="asked us to stop")
    hits = cs.scrub(db, cs.rank(p), now=NOON)
    top = next(h for h in hits if h.hit.digits == "9735550101")
    assert top.status == "dnc" and not top.callable_now and "do-not-call" in top.reason


def test_save_creates_contacts_flags_dnc_and_flips_ready(db):
    lead = make_lead(db, sheriff_days=25, upset=150000, vacant=True, verdict="STRONG")
    lead.property.owner_name = "QUINN Q SAMPLE"
    db.flush()
    compliance.add_internal(db, "2015550103", notes="Daniel's Law request", source="daniels_law")
    assert not lead.ready_to_call
    parsed = cs.parse_page(page("truepeoplesearch_detail"))
    res = cs.save(db, lead, parsed, now=NOON)
    phones = {c.phone: c for c in lead.contacts if c.phone}
    assert set(phones) == {"9735550101", "2015550103", "9735550102"} and res.contacts_created >= 5
    assert phones["9735550101"].phone_type == "wireless" and phones["9735550101"].confidence_score >= 0.85
    assert phones["2015550103"].dnc_status == "dnc" and phones["2015550103"].confidence_score == 0.0
    assert phones["9735550102"].phone_type == "landline" and phones["9735550102"].confidence_score < 0.6
    assert "last reported Jan 2026" in phones["9735550101"].notes and "age 67" in phones["9735550101"].notes
    assert res.best_phone == "(973) 555-0101" and res.best_status == "not_checked"
    assert res.ready_to_call and lead.ready_to_call and "phone" in res.via
    assert lead.status_stage == "SKIP_TRACE"
    emails = [c for c in lead.contacts if c.email]
    assert len(emails) == 2 and all(c.contact_type == "OWNER" for c in emails)
    assert not [c for c in lead.contacts if c.contact_type == "RELATIVE"]          # not an estate: relatives stay out


def test_save_deceased_owner_adds_relatives_and_note(db):
    lead = make_lead(db, sheriff_days=None, upset=None, verdict="STRONG", is_estate_owned=True)
    lead.property.owner_name = "QUINN Q SAMPLE"
    db.flush()
    res = cs.save(db, lead, cs.parse_page(page("deceased_relatives")), now=NOON)
    rel = [c for c in lead.contacts if c.contact_type == "RELATIVE"]
    assert res.deceased_flagged and res.relatives_saved == 2 and {c.name for c in rel} == {"Jordan Example", "Morgan Sample"}
    assert "deceased" in lead.notes and any("age 61" in (c.notes or "") for c in rel)
    assert res.best_phone == "(973) 555-0102"


def test_save_with_only_dnc_numbers_says_letter(db):
    lead = make_lead(db, sheriff_days=25, upset=150000, vacant=True, verdict="STRONG")
    compliance.add_internal(db, "9735550102", notes="stop")
    res = cs.save(db, lead, cs.parse_page(page("deceased_relatives")), now=NOON)
    assert res.best_phone is None and any("letter" in n for n in res.notes) and not res.ready_to_call


# -- the queue -----------------------------------------------------------------------------------------
def test_queue_orders_estates_vacant_sheriff_lis_pendens_absentee_and_skips_listed(db):
    estate = make_lead(db, sheriff_days=None, upset=None, verdict="STRONG", is_estate_owned=True)
    estate.frfpa_track, estate.priority_rank_score = "EXEMPT_NOT_IN_FORECLOSURE", 50
    vacant = make_lead(db, sheriff_days=20, upset=150000, vacant=True, verdict="STRONG")
    vacant.priority_rank_score = 90
    lp = make_lead(db, sheriff_days=None, upset=None, verdict="WORKABLE")
    upsert_signal(db, lp.property, signal_type=SignalType.LIS_PENDENS, source="clerk", source_ref="LP-1")
    lp.priority_rank_score = 80
    absentee = make_lead(db, sheriff_days=None, upset=None, verdict="STRONG", absentee=True)
    absentee.priority_rank_score = 70
    listed = make_lead(db, sheriff_days=None, upset=None, verdict="STRONG", stale_listing=True)
    add_contact(db, listed, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100")
    has_phone = make_lead(db, sheriff_days=20, upset=150000, vacant=True, verdict="STRONG")
    add_contact(db, has_phone, name="Quinn Sample", phone="9735551234")
    passed = make_lead(db, sheriff_days=20, upset=150000, vacant=True, verdict="PASS")
    for lead in (estate, vacant, lp, absentee, listed, has_phone, passed):
        lead.property.owner_name = lead.property.owner_name or "QUINN Q SAMPLE"
    db.flush()
    rows = cs.queue(db, limit=10, now=NOON)
    ids = [r["lead_id"] for r in rows]
    assert ids[:4] == [estate.id, vacant.id, lp.id, absentee.id]
    assert listed.id not in ids and has_phone.id not in ids and passed.id not in ids
    first = rows[0]
    assert first["tags"] == ["EXEMPT_ESTATE"] and "heir or executor" in first["why"] and first["target"]["name"]
    assert set(first["links"]) >= {"truepeoplesearch", "fastpeoplesearch", "cyberbackgroundchecks"}
    assert all(not v.startswith("http") or "truepeoplesearch.com" in v or "fastpeoplesearch.com" in v
               or "cyberbackgroundchecks.com" in v for v in first["links"].values())
    assert rows[1]["tags"] == ["VACANT_SHERIFF"] and rows[1]["days_to_sale"] == 20 and rows[1]["numbers"]["verdict"] == "STRONG"


def test_trace_end_to_end_returns_next_lead(db):
    a = make_lead(db, sheriff_days=20, upset=150000, vacant=True, verdict="STRONG")
    b = make_lead(db, sheriff_days=22, upset=150000, vacant=True, verdict="STRONG")
    a.priority_rank_score, b.priority_rank_score = 90, 80
    for lead in (a, b):
        lead.property.owner_name = "QUINN Q SAMPLE"
    db.flush()
    out = cs.trace(db, a.id, page("fastpeoplesearch_detail"), now=NOON)
    assert out["saved"]["best_phone"] == "(973) 555-0101" and out["ready_to_call"] and out["stage"] == "SKIP_TRACE"
    assert out["next"]["lead_id"] == b.id and out["parsed"]["name"] == "Quinn Q Sample"
    assert [h["kind"] for h in out["hits"]] == ["wireless", "landline", "voip"]
    with pytest.raises(ValueError):
        cs.trace(db, 999999, "x")
    assert cs.queue(db, limit=10, now=NOON)[0]["lead_id"] == b.id          # a now has a phone: off the queue


def test_module_never_fetches_the_sites():
    src = Path(cs.__file__).read_text()
    assert "requests" not in src and "httpx" not in src and "urlopen" not in src and "PoliteSession" not in src
