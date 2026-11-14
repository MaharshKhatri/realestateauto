"""S7 Mansion-tax cliff net sheets: the dead-zone math, the gates, the one-page sheet and the 2-step courtesy note."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pypdf
import pytest
from sqlalchemy import select

from db.models import FirmOutbox, FirmTarget, InboundReply, Interaction, Listing, utcnow
from engine import firm_outbox as fo
from engine.quant_pricing import costs, graduated_percent_fee, realty_transfer_fee
from engine.streams import s7_mansion_tax_cliff as s7
from engine.streams.base import PRIORITY_SPECIALTY, StreamContext
from tests.factories import add_contact, make_lead

NOW = datetime(2026, 11, 9, 10, 0)
OP = {"name": "Max Tester", "first": "Max", "company": "", "phone": "(973) 555-0100", "email": "max@example.com",
      "postal": "1 Main St, Newark NJ 07102"}
AGENT_EMAIL = "pat.agent@brokerage.example"


def ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=NOW, out_dir=tmp_path / "s7", operator=OP, send=send, limits=limits)


def listed(db, price: float = 1_007_198, email: str | None = AGENT_EMAIL, agent_name: str = "Pat Agent", **kw):
    lead = make_lead(db, sheriff_days=None, stale_listing=True, list_price=price,
                     listing_text="Estate sale, sold as-is, needs work.", owner_name="QUINN Q SAMPLE", **kw)
    if email:
        add_contact(db, lead, contact_type="LISTING_AGENT", name=agent_name, phone=None, email=email)
    return lead


def pdf_text(path) -> tuple[int, str]:
    r = pypdf.PdfReader(str(path))
    return len(r.pages), " ".join(p.extract_text() or "" for p in r.pages)


def sentences(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.?!])\s+", text.strip()) if x.strip()]


# -- math ---------------------------------------------------------------------------------------------------------
def test_transfer_tax_uses_the_yaml_functions():
    for p in (350_000, 999_999, 1_000_000, 1_000_500, 2_000_000, 2_000_500, 3_600_000):
        assert s7.transfer_tax(p) == pytest.approx(realty_transfer_fee(p) + graduated_percent_fee(p))
    assert s7.thresholds() == [float(t["over"]) for t in
                               costs()["realty_transfer_fee"]["graduated_percent_fee"]["tiers"]]


def test_every_threshold_has_a_dead_zone_solved_on_the_500_grid():
    zones = s7.dead_zones()
    assert [t for t, _ in zones] == [1_000_000, 2_000_000, 2_500_000, 3_000_000, 3_500_000]
    for t, edge in zones:
        assert edge > t and (edge - t) % 500 == 0
        assert s7.seller_net(edge) >= s7.seller_net(t) > s7.seller_net(edge - 500)
        assert s7.seller_net(t + 500) < s7.seller_net(t)
        assert s7.cliff(t) is None                       # exactly at the line: no graduated jump
        assert s7.cliff(t - 1000) is None
        assert s7.cliff(edge) is None                    # the edge nets at least what T does
        assert s7.cliff(t + 1000) is not None
    # the 1% cliff at $1M closes within ~$10.5K (the RTF's 1.21% top rate eats into the recovery)
    assert 1_010_000 < zones[0][1] <= 1_011_000


def test_cliff_report_numbers():
    c = s7.cliff(1_007_198)
    assert c is not None and c.threshold == 1_000_000
    assert c.net_price == pytest.approx(1_007_198 - realty_transfer_fee(1_007_198) - graduated_percent_fee(1_007_198))
    assert c.net_threshold == pytest.approx(1_000_000 - realty_transfer_fee(1_000_000))
    assert c.gain == pytest.approx(c.net_threshold - c.net_price) and c.gain > 2_000
    assert c.width == c.edge - 1_000_000
    # the extra transfer tax is the fee difference; the seller's net loss is that minus the higher price
    assert c.tax_extra == pytest.approx(s7.transfer_tax(1_007_198) - s7.transfer_tax(1_000_000))
    assert c.tax_extra - (c.price - c.threshold) == pytest.approx(c.gain)
    c2 = s7.cliff(2_005_000)
    assert c2.threshold == 2_000_000 and c2.pct_price == pytest.approx(0.02) and c2.pct_threshold == pytest.approx(0.01)


def test_residential_class():
    assert s7.residential("2", "multi_family") and not s7.residential("4A", "single_family")
    assert s7.residential(None, "condos") and not s7.residential(None, "multi_family")
    assert not s7.residential("", "land")


# -- the 2-step copy ----------------------------------------------------------------------------------------------
def test_touch_one_is_a_greeting_and_two_plain_sentences_with_the_fee_arithmetic():
    from engine import human_copy as hc
    c = s7.cliff(1_007_198)
    body = s7.note_body(c, "12 Sample St", "Pat Agent")
    greet, rest = body.split("\n\n", 1)
    assert greet == "Hi Pat,"
    assert len(sentences(rest)) == 2 and s7.word_count(rest) <= 45
    assert hc.lint(body) == [] and "—" not in body and "http" not in body and "attach" not in body.lower()
    assert rest.startswith("Quick heads-up on 12 Sample St: at $1,007,198 your seller crosses New Jersey's graduated "
                           "transfer-fee line and pays about ")
    # the numbers are the message: the fee difference at the list price vs the line, never a value opinion
    assert s7.usd(c.tax_extra) in rest and "than at $1,000,000." in rest
    assert rest.endswith("Want the 1-page net sheet showing both prices?")
    for word in ("opportunity", "guaranteed", "referral", "commission", "excited", "worth", "value", "free", "ad:",
                 "subscription", "month"):
        assert word not in body.lower()
    # an upper line names its own threshold
    assert "than at $2,000,000." in s7.note_body(s7.cliff(2_005_000), "9 Sample Ln", "Pat Agent")


def test_greeting_uses_a_first_name_only_for_a_person():
    c = s7.cliff(1_006_000)
    assert s7.note_body(c, "12 Sample St", "Pat Agent").startswith("Hi Pat,\n\n")
    for name in ("Sample Realty Group", "The Sample Team", "Keller Sample", "Pat", None, ""):
        assert s7.note_body(c, "12 Sample St", name).startswith("Hello,\n\n"), name


def test_subject_and_sample_body():
    from engine import human_copy as hc
    c = s7.cliff(1_007_198)
    subj = s7.note_subject(c, "12 Sample St")
    assert subj == "12 Sample St and the $1M transfer-fee line"
    assert s7.note_subject(s7.cliff(2_505_000), "9 Sample Ln") == "9 Sample Ln and the $2.5M transfer-fee line"
    assert "!" not in subj and "free" not in subj.lower()
    sample = s7.sample_body("12 Sample St")
    assert hc.lint(sample) == [] and 1 <= len(sentences(sample)) <= 2
    assert "12 Sample St" in sample and "no charge" in sample and "$" not in sample


# -- the run ------------------------------------------------------------------------------------------------------
def test_run_builds_one_page_sheet_and_a_plain_touch_one(db, tmp_path):
    lead = listed(db)
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.found == 1 and res.products == 1 and len(res.outbound) == 1
    ob = res.outbound[0]
    assert ob.key == f"s7:sheet:{lead.id}:1007198" and ob.to == AGENT_EMAIL and ob.lead_id == lead.id
    assert ob.priority == PRIORITY_SPECIALTY and ob.commercial and ob.kind == "pitch"
    assert not ob.subject.lower().startswith("re:") and ob.subject.endswith("and the $1M transfer-fee line")
    # Touch 1: no attachment; the sheet waits as the sample, with a no-charge note
    assert ob.attachments == [] and len(ob.sample_attachments) == 1
    assert ob.body.startswith("Hi Pat,\n\nQuick heads-up on ") and "$1,007,198" in ob.body
    assert "no charge" in ob.sample_body and "$" not in ob.sample_body
    target = db.get(FirmTarget, ob.target_id)
    assert target.email == AGENT_EMAIL and target.stream == "s7" and target.kind == "listing_agent"
    pages, text = pdf_text(ob.sample_attachments[0])
    assert pages == 1
    assert "not tax or legal advice" in text and "GraduatedPercentFeeNotice.pdf" in text and "feeschedover350" in text
    assert "QUINN" not in text.upper() and "Pat Agent" not in text and AGENT_EMAIL not in text
    assert not list((tmp_path / "s7").rglob("*.sample.pdf"))          # one listing: the sheet itself is the sample
    assert all(isinstance(v, (int, float)) for v in res.metrics.values())
    # stable key: a second run (nothing enqueued) proposes the very same key
    again = s7.STREAM.run(db, ctx(tmp_path))
    assert [o.key for o in again.outbound] == [ob.key]


def test_the_outbox_holds_the_sheet_until_the_agent_says_yes(db, tmp_path):
    listed(db)
    ob = s7.STREAM.run(db, ctx(tmp_path)).outbound[0]
    row = fo.enqueue(db, ob, NOW)
    assert row is not None and row.status == "queued" and row.kind == "pitch" and row.attachments_json == []
    held = db.scalar(select(FirmOutbox).where(FirmOutbox.key == f"{ob.key}:sample"))
    assert held.status == "held" and held.kind == "sample" and held.body == ob.sample_body
    assert [a["name"] for a in held.attachments_json] == [ob.sample_attachments[0].name]
    row.status, row.message_id = "sent", "<s7-pitch@example>"
    db.flush()
    yes = SimpleNamespace(message_id="<yes@brokerage.example>", text="Yes please, send it over.",
                          from_email=AGENT_EMAIL, subject=f"Re: {ob.subject}", received=NOW, references=[],
                          auto_reply=False)
    reply = fo.handle_reply(db, yes, ob.target_id, NOW)
    assert reply.status == "sample_sent"
    db.refresh(held)
    assert held.status == "queued" and held.in_reply_to == "<yes@brokerage.example>"
    # one note per listing, ever
    assert s7.STREAM.run(db, ctx(tmp_path)).outbound == []


def test_dry_run_and_empty_database(db, tmp_path):
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.processed == 0 and res.outbound == [] and res.products == 0
    listed(db)
    dry = s7.STREAM.run(db, ctx(tmp_path, send=False))
    assert dry.outbound == [] and dry.products == 1
    assert db.scalar(select(FirmTarget.id)) is None                       # a dry run creates no target


def test_out_of_zone_and_wrong_class_are_skipped(db, tmp_path):
    listed(db, price=1_000_000)                          # at the line
    listed(db, price=999_000)                            # under it
    listed(db, price=1_015_000)                          # past the edge
    listed(db, price=1_006_000, property_class="4A")     # commercial: no graduated fee
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.outbound == [] and res.metrics["skipped_class"] == 1
    assert res.metrics["just_under"] >= 2 and res.metrics["zone_listings"] == 1


def test_a_listing_nobody_has_seen_for_days_gets_no_note(db, tmp_path):
    lead = listed(db)
    lst = db.scalar(select(Listing).where(Listing.property_id == lead.property_id))
    lst.last_seen_at = utcnow() - timedelta(days=5)
    db.flush()
    assert s7.STREAM.run(db, ctx(tmp_path)).outbound == []
    assert len(s7.STREAM.run(db, ctx(tmp_path, fresh_days=7)).outbound) == 1


def test_suppression_stop_list_and_recent_mail_are_honored(db, tmp_path):
    from engine.compliance import suppress_address
    sup = listed(db, price=1_005_000, email="a1@brokerage.example")
    suppress_address(db, f"{sup.property.street_address}, Newark NJ 07106", source="daniels_law")
    stopped = listed(db, price=1_006_000, email="a2@brokerage.example")
    add_contact(db, stopped, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550199",
                email="a2@brokerage.example", dnc_status="dnc")
    recent = listed(db, price=1_007_000, email="a3@brokerage.example")
    db.add(Interaction(lead_id=recent.id, channel="email_smtp", outcome="OFFER_SENT_TO_AGENT",
                       occurred_at=NOW - timedelta(days=3)))
    before = listed(db, price=1_008_000, email="a4@brokerage.example")
    db.add(FirmOutbox(key=f"s7:sheet:{before.id}:1009000", stream="s7", to_email="a4@brokerage.example",
                      subject="x", body="x", status="sent"))
    db.flush()
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.outbound == [] and res.products == 0
    m = res.metrics
    assert (m["skipped_suppressed"], m["skipped_stop"], m["skipped_recent"], m["skipped_sent_before"]) == (1, 1, 1, 1)


def test_no_note_to_an_agent_in_deal_talks_on_another_listing(db, tmp_path):
    listed(db, price=1_005_000, email="talks1@brokerage.example")
    listed(db, price=1_006_000, email="talks2@brokerage.example")
    listed(db, price=1_007_000, email="talks3@brokerage.example")
    listed(db, price=1_008_000, email="quiet@brokerage.example")
    other = make_lead(db, sheriff_days=None, stale_listing=True)
    db.add(InboundReply(message_id="<r@brokerage.example>", lead_id=other.id, from_email="Talks1@Brokerage.example",
                        subject="Re: 5 Other St", status="needs_approval"))
    c2 = add_contact(db, other, contact_type="LISTING_AGENT", name="Pat Agent", phone=None,
                     email="talks2@brokerage.example")
    db.add(Interaction(lead_id=other.id, contact_id=c2.id, channel="email_smtp", outcome="OFFER_SENT_TO_AGENT",
                       occurred_at=NOW - timedelta(days=10)))
    third = make_lead(db, sheriff_days=None, stale_listing=True)            # logged without the contact's id
    add_contact(db, third, contact_type="LISTING_AGENT", name="Pat Agent", phone=None, email="talks3@brokerage.example")
    db.add(Interaction(lead_id=third.id, channel="email_gmail", outcome="AGENT_FOLLOWUP_SENT",
                       occurred_at=NOW - timedelta(days=4)))
    old = make_lead(db, sheriff_days=None, stale_listing=True)              # a conversation that ended 2 months ago
    add_contact(db, old, contact_type="LISTING_AGENT", name="Pat Agent", phone=None, email="quiet@brokerage.example")
    db.add(Interaction(lead_id=old.id, channel="email_smtp", outcome="OFFER_SENT_TO_AGENT",
                       occurred_at=NOW - timedelta(days=60)))
    db.flush()
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.metrics["skipped_talks"] == 3 and [o.to for o in res.outbound] == ["quiet@brokerage.example"]


def test_an_agent_another_stream_pitched_this_week_waits_without_using_up_the_listing(db, tmp_path):
    lead = listed(db)
    t = fo.upsert_target(db, kind="investor_agent", stream="s2", email=AGENT_EMAIL, org="Sample Realty")
    t.touches, t.stage, t.last_contact_at = 1, "CONTACTED", NOW - timedelta(days=2)
    db.flush()
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.outbound == [] and res.metrics["skipped_target"] == 1
    assert db.scalar(select(FirmOutbox.id).where(FirmOutbox.key.like(f"s7:sheet:{lead.id}:%"))) is None
    t.last_contact_at = NOW - timedelta(days=8)                           # past the gap, still in s2's sequence
    db.flush()
    assert s7.STREAM.run(db, ctx(tmp_path)).outbound == []               # one person, one sequence at a time
    t.touches, t.last_contact_at = 0, NOW - timedelta(days=8)            # only an s1 buy-box note so far: no touch
    db.flush()
    [ob] = s7.STREAM.run(db, ctx(tmp_path)).outbound
    assert ob.target_id == t.id and db.get(FirmTarget, t.id).stream == "s2"   # their row and stream stay


def test_cap_per_run_and_one_sheet_per_agent(db, tmp_path):
    for i in range(4):
        listed(db, price=1_003_000 + 1000 * i, email=f"agent{i}@brokerage.example")
    listed(db, price=2_004_000, email="agent0@brokerage.example")      # the same agent again: next run
    res = s7.STREAM.run(db, ctx(tmp_path, pitches_per_run=3))
    assert len(res.outbound) == 3 and len({o.to for o in res.outbound}) == 3
    assert len({o.key for o in res.outbound}) == 3


# -- the high-value pass inside the run ---------------------------------------------------------------------------
def test_the_run_starts_with_the_daily_high_value_pass(db, tmp_path, monkeypatch):
    from engine.streams import s7_highvalue_harvest as hv
    calls = []
    monkeypatch.setattr(hv, "harvest", lambda s, now, limits: calls.append(now) or {"status": "ran", "queries": 3,
                                                                                     "in_zone": 1})
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert calls == [NOW]
    assert res.metrics["hv_ran"] == 1 and res.metrics["hv_queries"] == 3 and res.metrics["hv_in_zone"] == 1
    assert "hv_status" not in res.metrics and all(isinstance(v, (int, float)) for v in res.metrics.values())
    s7.STREAM.run(db, ctx(tmp_path, harvest=False))                       # switched off: not asked
    assert calls == [NOW]

    def boom(s, now, limits):
        raise RuntimeError("bad listing data")
    monkeypatch.setattr(hv, "harvest", boom)
    listed(db)
    db.commit()
    res = s7.STREAM.run(db, ctx(tmp_path))                                # a broken pass never stops the notes
    assert res.metrics["hv_failed"] == 1 and len(res.outbound) == 1


def test_no_listing_request_leaves_a_machine_that_does_not_harvest_listings(db, tmp_path, monkeypatch):
    from engine import homeharvest_provider as hh

    def no_network(**kw):
        raise AssertionError("the provider was asked")
    monkeypatch.setattr(hh, "_scrape", no_network)
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.metrics["hv_ran"] == 0 and res.metrics["hv_blocked"] == 0
