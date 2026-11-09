"""S8 Contract fallout: status transitions, quiet "under contract" replies, the follow-up's one allowed number."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pypdf
from sqlalchemy import select

from db.models import Contact, FirmOutbox, InboundReply, Interaction, Listing, SwarmState
from engine.streams import s8_contract_fallout as s8
from engine.streams.base import PRIORITY_PITCH, StreamContext
from tests.factories import add_contact, make_lead

NOW = datetime(2026, 11, 9, 10, 0)
TODAY = NOW.date().isoformat()
OP = {"name": "Max Tester", "first": "Max", "company": "", "phone": "(973) 555-0100", "email": "max@example.com",
      "postal": "1 Main St, Newark NJ 07102"}
_n = iter(range(1, 1000))


def ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=NOW, out_dir=tmp_path / "s8", operator=OP, send=send, limits=limits)


def listed(db, *, status: str = "pending", contacted: bool = True, email: str | None = None, **kw):
    """A listed lead (its listing in `status`) whose agent we e-mailed 20 days ago."""
    lead = make_lead(db, sheriff_days=None, stale_listing=True, listing_text="Estate sale, sold as-is, needs work.",
                     owner_name="QUINN Q SAMPLE", **kw)
    email = email or f"agent{next(_n)}@brokerage.example"
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone=None, email=email)
    lst = db.scalar(select(Listing).where(Listing.property_id == lead.property_id))
    lst.status = status
    if contacted:
        db.add(Interaction(lead_id=lead.id, channel="email_smtp", outcome="OFFER_SENT_TO_AGENT",
                           occurred_at=NOW - timedelta(days=20)))
    db.flush()
    return lead, lst


def reply(db, lead, *, days: float, intents: list[str], status: str = "dismissed", headline: str = "") -> InboundReply:
    r = InboundReply(message_id=f"<r{next(_n)}@mail.example>", lead_id=lead.id, from_email="agent@brokerage.example",
                     subject="12 Test St", received_at=NOW - timedelta(days=days), status=status, intents_json=intents,
                     headline=headline, analysis_json={})
    db.add(r)
    db.flush()
    return r


def back_on_market(db, tmp_path, *leads_lists, **limits):
    """First run records the snapshot, then the listings come back for sale and the second run sees them."""
    s8.STREAM.run(db, ctx(tmp_path, **limits))
    for _, lst in leads_lists:
        lst.status = "for_sale"
    db.flush()
    return s8.STREAM.run(db, ctx(tmp_path, **limits))


def sentences(body: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.?!])\s+", body.split("\n\n")[1]) if x.strip()]


# -- pure helpers -------------------------------------------------------------------------------------------------
def test_contract_status_and_transitions():
    assert s8.contract_status("Pending") == "pending" and s8.contract_status("contingent") == "contingent"
    assert s8.contract_status("for_sale") is None and s8.contract_status("sold") is None
    assert s8.contract_status("off_market", {"status_before_off_market": "pending"}) == "pending"
    assert s8.contract_status("off_market", {"status_before_off_market": "for_sale"}) is None
    prev = {"1": "pending", "2": "contingent", "3": "pending"}
    assert s8.transitions(prev, {"1": "for_sale", "2": "contingent", "3": "sold", "4": "for_sale"}) == ["1"]
    assert s8.transitions({}, {"1": "for_sale"}) == []


def test_said_under_contract_reads_intents_then_our_headline():
    assert s8.said_under_contract(["UNDER_CONTRACT"]) and s8.said_under_contract(["BACKUP", "ASKS_ROLE"])
    assert not s8.said_under_contract(["AVAILABLE"], {}, "OFF THE TABLE FOR NOW: under contract")
    assert s8.said_under_contract([], {"headline": "OFF THE TABLE FOR NOW: under contract"})
    assert s8.said_under_contract([], {"kind": "under_contract"})
    assert s8.said_under_contract([], {}, "SEND A BACKUP OFFER NOW (Touch 2)")
    for neg in ("it is not under contract", "it isn't under contract", "no longer under contract",
                "it was under contract, back on the market"):
        assert not s8.said_under_contract([], {}, neg), neg


def test_followup_copy_states_only_the_standing_offer():
    from engine import human_copy as hc
    for via in ("status", "reply"):
        for offer in (None, 250_000):
            body = s8.followup_body("12 Test St", "Max", via, offer)
            assert hc.lint(body) == [] and "—" not in body and hc.word_count(body) < 110
            assert 3 <= len(sentences(body)) <= 5 and body.splitlines()[-1] == "Max"
            nums = re.findall(r"\$[\d,]+", body)
            assert nums == (["$250,000"] if offer else [])
    assert "what would the seller need" in s8.followup_body("12 Test St", "Max", "status", None)


def test_merge_fallouts_dedupes_and_prunes():
    old = [{"lead_id": 1, "detected": "2026-09-01", "via": "status"},
           {"lead_id": 2, "detected": "2026-11-08", "via": "reply", "reply_id": 9}]
    new = [{"lead_id": 2, "detected": "2026-11-08", "via": "reply"}, {"lead_id": 3, "detected": TODAY, "via": "status"}]
    out = s8.merge_fallouts(old, new, NOW.date(), keep_days=30)
    assert [(e["lead_id"], e["detected"]) for e in out] == [(3, TODAY), (2, "2026-11-08")]


# -- the run ------------------------------------------------------------------------------------------------------
def test_stream_attributes_and_empty_database(db, tmp_path):
    assert (s8.STREAM.key, s8.STREAM.priority, s8.STREAM.cadence_hours, s8.STREAM.run_hours) == ("s8", PRIORITY_PITCH,
                                                                                                  1.0, (8, 20))
    first = s8.STREAM.run(db, ctx(tmp_path))
    second = s8.STREAM.run(db, ctx(tmp_path))
    assert first.metrics["first_run"] == 1 and second.metrics["first_run"] == 0
    assert first.outbound == second.outbound == [] and second.products == 0 and second.found == 0


def test_first_run_records_only_then_transition_gets_followup(db, tmp_path):
    lead, lst = listed(db)
    r1 = s8.STREAM.run(db, ctx(tmp_path))
    assert r1.metrics["first_run"] == 1 and r1.metrics["transitions"] == 0 and r1.outbound == []
    assert db.get(SwarmState, s8.STATE_KEY).value_json == {str(lst.id): "pending"}
    lst.status = "for_sale"
    db.flush()
    r2 = s8.STREAM.run(db, ctx(tmp_path))
    assert (r2.metrics["transitions"], r2.metrics["fallouts"], r2.metrics["followups"]) == (1, 1, 1)
    ob = r2.outbound[0]
    assert ob.key == f"s8:back:{lead.id}:{TODAY}" and ob.kind == "followup" and ob.priority == PRIORITY_PITCH
    assert not ob.commercial and ob.in_reply_to is None and not ob.subject.lower().startswith("re:")
    assert "$" not in ob.body and "what would the seller need" in ob.body
    items = db.get(SwarmState, s8.FALLOUT_KEY).value_json["items"]
    assert items[0]["lead_id"] == lead.id and items[0]["detected"] == TODAY and items[0]["via"] == "status"
    assert items[0]["listing_id"] == lst.id and items[0]["was"] == "pending"
    pdfs = list((tmp_path / "s8").glob("*.pdf"))
    assert r2.products == 1 and len(pdfs) == 1
    reader = pypdf.PdfReader(str(pdfs[0]))
    text = " ".join(p.extract_text() or "" for p in reader.pages)
    assert len(reader.pages) == 1 and "Back on market today" in text
    assert "QUINN" not in text.upper() and "Pat Agent" not in text and "brokerage.example" not in text
    assert all(isinstance(v, (int, float)) for v in r2.metrics.values())
    # a re-run proposes the same key (the outbox dedupes it); once it is in the outbox, nothing more
    r3 = s8.STREAM.run(db, ctx(tmp_path))
    assert [o.key for o in r3.outbound] == [ob.key] and r3.products == 0 and r3.metrics["transitions"] == 0
    db.add(FirmOutbox(key=ob.key, stream="s8", to_email=ob.to, subject=ob.subject, body=ob.body, status="queued",
                      lead_id=lead.id))
    db.flush()
    assert s8.STREAM.run(db, ctx(tmp_path)).outbound == []


def test_written_offer_that_still_fits_is_restated_in_the_thread(db, tmp_path):
    lead, lst = listed(db, contacted=False)
    lead.offer_amount = 250_000
    r = reply(db, lead, days=25, intents=["AVAILABLE"])
    res = back_on_market(db, tmp_path, (lead, lst))
    ob = res.outbound[0]
    assert ob.in_reply_to == r.message_id and ob.subject.lower().startswith("re:")
    assert re.findall(r"\$[\d,]+", ob.body) == ["$250,000"] and "still stands" in ob.body
    assert res.metrics["offer_stands"] == 1


def test_offer_above_todays_final_is_never_stated(db, tmp_path):
    lead, lst = listed(db)
    lead.offer_amount = 400_000                       # today's final is $285,000 on the shared fixture
    res = back_on_market(db, tmp_path, (lead, lst))
    assert len(res.outbound) == 1 and "$" not in res.outbound[0].body and res.metrics["offer_stands"] == 0


def test_quiet_under_contract_reply_gets_a_check(db, tmp_path):
    lead, _ = listed(db, status="for_sale")
    r = reply(db, lead, days=20, intents=["UNDER_CONTRACT"])
    res = s8.STREAM.run(db, ctx(tmp_path))
    assert res.metrics["reply_checks"] == 1 and len(res.outbound) == 1
    ob = res.outbound[0]
    assert ob.in_reply_to == r.message_id and "did the contract go through" in ob.body
    items = db.get(SwarmState, s8.FALLOUT_KEY).value_json["items"]
    assert items[0]["via"] == "reply" and items[0]["reply_id"] == r.id
    assert s8.STREAM.run(db, ctx(tmp_path)).metrics["reply_checks"] == 0     # recorded once


def test_reply_check_waits_for_the_review_window_and_respects_newer_mail(db, tmp_path):
    fresh, _ = listed(db, status="for_sale")
    reply(db, fresh, days=5, intents=["UNDER_CONTRACT"])                 # still inside attorney review / inspection
    answered, _ = listed(db, status="for_sale")
    reply(db, answered, days=20, intents=["UNDER_CONTRACT"])
    reply(db, answered, days=2, intents=["AVAILABLE"])                   # they wrote since
    gone, _ = listed(db, status="pending")
    reply(db, gone, days=20, intents=["BACKUP"])
    for sg in gone.property.signals:                                    # the listing left the market
        sg.active = False
    db.flush()
    res = s8.STREAM.run(db, ctx(tmp_path))
    assert res.metrics["reply_checks"] == 0 and res.outbound == []


def test_recent_mail_stop_list_cold_and_suppressed_are_honored(db, tmp_path):
    from engine.compliance import suppress_address
    recent = listed(db)
    db.add(Interaction(lead_id=recent[0].id, channel="email_gmail", outcome="AGENT_FOLLOWUP_SENT",
                       occurred_at=NOW - timedelta(days=3)))
    stopped = listed(db, email="stopped@brokerage.example")
    add_contact(db, stopped[0], contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550199",
                email="stopped@brokerage.example", dnc_status="dnc")
    cold = listed(db, contacted=False)
    hidden = listed(db)
    suppress_address(db, f"{hidden[0].property.street_address}, Newark NJ 07106", source="daniels_law")
    res = back_on_market(db, tmp_path, recent, stopped, cold, hidden)
    m = res.metrics
    assert res.outbound == [] and m["transitions"] == 4 and m["fallouts"] == 3
    assert (m["skipped_recent"], m["skipped_stop"], m["skipped_cold"], m["skipped_suppressed"]) == (1, 1, 1, 1)
    ids = {e["lead_id"] for e in db.get(SwarmState, s8.FALLOUT_KEY).value_json["items"]}
    assert hidden[0].id not in ids and {recent[0].id, stopped[0].id, cold[0].id} <= ids
    assert db.scalar(select(Contact).where(Contact.dnc_status == "dnc")) is not None


def test_cap_per_run_and_dry_run(db, tmp_path):
    pairs = [listed(db) for _ in range(7)]
    res = back_on_market(db, tmp_path, *pairs)
    assert res.metrics["fallouts"] == 7 and len(res.outbound) == 5 and len({o.key for o in res.outbound}) == 5
    dry = s8.STREAM.run(db, ctx(tmp_path, send=False))
    assert dry.outbound == []
