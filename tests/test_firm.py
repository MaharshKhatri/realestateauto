"""The 10-stream research firm's framework (2026-11-09): the outbox (dedupe, stop lists, caps, CAN-SPAM footer),
B2B replies read from Gmail, sealed-note controls, the orchestrator, the clean calendar and streams s1 / s2."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from email import message_from_bytes
from pathlib import Path

import pytest

from core.config import settings
from db.models import (CalendarEvent, Contact, ContactType, FirmOutbox, FirmReply, FirmStreamRun, FirmTarget,
                       InboundReply, Interaction, utcnow)
from engine import firm_outbox as fo
from engine import gmail_autopilot as gm
from engine import swarm_firm
from engine.streams.base import PRIORITY_PITCH, PRIORITY_REPLY, PRIORITY_SPECIALTY, Outbound, Stream, StreamResult
from tests.factories import add_contact, make_lead
from tests.test_agent_outreach import FakeSMTP
from tests.test_command_center import setup_paths  # noqa: F401 - pytest fixture
from tests.test_gmail_autopilot import ME, T0, FakeIMAP, live, mail  # noqa: F401 - pytest fixture

TUE = datetime(2026, 11, 10, 10, 0)              # a Tuesday, inside business hours (naive Eastern, the DB convention)
from engine.crm import TZ  # noqa: E402
TUE_ET = TUE.replace(tzinfo=TZ)                  # what the outbox takes (a naive time there means UTC)


def target(db, email="intake@lawfirm.example", kind="property_tax_appeal_attorney", stream="s6", **kw):
    t = fo.upsert_target(db, kind=kind, stream=stream, email=email, org=kw.pop("org", "Sample Tax Law LLC"), **kw)
    db.commit()
    return t


def pitch(t, key="s6:pitch:1:2026-W46", prio=PRIORITY_SPECIALTY, **kw):
    return Outbound(stream=t.stream, key=key, to=t.email, subject="Comparable-sales data for appeals",
                    body="Hi there,\n\nA short note.\n\nJordan", priority=prio, target_id=t.id, **kw)


# -- outbox -------------------------------------------------------------------------------------------------------
def test_an_outbound_is_queued_once_and_stop_lists_always_win(live):
    db = live
    from engine import agent_outreach as ao
    t = target(db)
    assert fo.enqueue(db, pitch(t), TUE).status == "queued"
    assert fo.enqueue(db, pitch(t), TUE) is None                     # same key: never twice
    gone = target(db, email="old@firm.example", org="Gone LLC")
    ao.record_bounce(db, "old@firm.example")
    assert fo.enqueue(db, pitch(gone, key="k2"), TUE).reason == "bounced before"
    out = target(db, email="out@firm.example", org="Out LLC")
    out.stage = "OPTED_OUT"
    assert fo.enqueue(db, pitch(out, key="k3"), TUE).reason == "target opted_out"
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    c = add_contact(db, lead, contact_type=ContactType.LISTING_AGENT, name="Pat Agent", phone=None,
                    email="stop@broker.example")
    c.dnc_status = "dnc"
    stop = target(db, email="stop@broker.example", kind="investor_agent", stream="s2", org=None)
    assert fo.enqueue(db, pitch(stop, key="k4"), TUE).reason == "on a stop list"
    again = target(db, email="recent@firm.example", org="Recent LLC")
    again.last_contact_at, again.touches = TUE - timedelta(days=2), 1
    assert fo.enqueue(db, pitch(again, key="k5"), TUE).reason == "pitched recently"


def test_every_message_carries_the_signature_postal_address_opt_out_and_ad_line(live, tmp_path):
    db = live
    t = target(db)
    pdf = tmp_path / "packet.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    row = fo.enqueue(db, pitch(t, attachments=[pdf]), TUE)
    db.commit()
    from engine import human_copy as hc
    msg = fo.compose(row, hc.operator(), "<x@y>", ME)
    text = msg.get_body(preferencelist=("plain",)).get_content()
    assert "PO Box 10, Edison, NJ 08817" in text and 'Reply "no more"' in text
    assert "Business offer from Garden State Home Buyers LLC." in text and "Jordan Operator" in text
    # 2026-11-10: a cold first touch never carries an attachment; the PDF waits as the held sample
    assert list(msg.iter_attachments()) == [] and msg["List-Unsubscribe"]
    held = db.query(FirmOutbox).filter_by(key=f"{row.key}:sample").one()
    assert held.status == "held" and held.kind == "sample" and held.attachments_json[0]["name"] == "packet.pdf"
    m3 = fo.compose(held, hc.operator(), "<s@y>", ME)
    assert [p.get_filename() for p in m3.iter_attachments()] == ["packet.pdf"]
    assert "Business offer" not in m3.get_body(preferencelist=("plain",)).get_content()   # they asked for it
    reply = Outbound(stream="s6", key="r1", to=t.email, subject="Re: data", body="Thanks, here it is.",
                     priority=PRIORITY_REPLY, kind="reply", target_id=t.id, in_reply_to="<their@msg>")
    r = fo.enqueue(db, reply, TUE)
    m2 = fo.compose(r, hc.operator(), "<z@y>", ME)
    assert "commercial message" not in m2.get_body().get_content() and m2["In-Reply-To"] == "<their@msg>"


def test_the_outbox_sends_by_priority_inside_caps_and_business_hours(live, monkeypatch):
    db = live
    monkeypatch.setitem(settings(), "research_firm", {**settings()["research_firm"], "caps": {"p1": 150, "p2": 25, "p3": 1},
                                                      "pace_seconds": [0, 0]})
    a, b = target(db, email="a@firm.example", org="A LLC"), target(db, email="b@firm.example", org="B LLC")
    fo.enqueue(db, pitch(a, key="p3a"), TUE)
    fo.enqueue(db, pitch(b, key="p3b"), TUE)
    db.commit()
    FakeSMTP.sent = []
    sat = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)
    assert fo.send_due(now=sat, smtp_factory=FakeSMTP, pace=False)["sent"] == 0          # cold pitches: weekdays only
    r = fo.send_due(now=TUE_ET, smtp_factory=FakeSMTP, pace=False)
    assert r["sent"] == 1 and len(FakeSMTP.sent) == 1                                     # p3 cap of 1 per 24 h
    db.expire_all()
    sent = db.query(FirmOutbox).filter_by(status="sent").one()
    t = db.get(FirmTarget, sent.target_id)
    assert sent.message_id and t.stage == "CONTACTED" and t.touches == 1


def test_the_outbox_refuses_to_send_without_the_operators_postal_address(live, monkeypatch):
    db = live
    from engine import human_copy as hc
    monkeypatch.setattr(hc, "operator", lambda: {"name": "Jordan Operator", "postal": "", "first": "Jordan"})
    t = target(db)
    fo.enqueue(db, pitch(t), TUE)
    db.commit()
    assert "postal" in fo.send_due(now=TUE_ET, smtp_factory=FakeSMTP, pace=False)["blocked"]


@pytest.mark.parametrize("text, intent", [
    ("Please remove me from your list", "opt_out"), ("no more", "opt_out"),
    ("Not interested, thanks", "not_interested"), ("We're all set.", "not_interested"),
    ("Yes, interested. How do we start?", "interested"), ("Sounds good, send me more", "interested"),
    ("What counties do you cover?", "question"), ("I'm not interested in this", "not_interested"),
])
def test_b2b_replies_are_classified(text, intent):
    assert fo.classify(text) == intent


def test_a_prospects_reply_sets_the_stage_stops_cold_touches_and_can_be_answered(live):
    db = live
    t = target(db)
    fo.enqueue(db, pitch(t, key="later"), TUE)
    db.commit()

    class M:
        message_id, in_reply_to, references, from_email = "<r1@firm.example>", None, [], "intake@lawfirm.example"
        subject, text, received, auto_reply = "Re: data", "Yes, interested. How do we start the trial?", None, False
    row = fo.handle_reply(db, M(), t.id, TUE)
    db.commit()
    assert row.intent == "interested" and db.get(FirmTarget, t.id).stage == "INTERESTED"
    assert db.query(FirmOutbox).filter_by(key="later").one().status == "skipped"
    assert fo.warm(db)[0].id == row.id
    out = fo.answer_reply(db, row.id, "Great: the trial starts Monday.", stage="TRIAL", now=TUE)
    assert out.priority == PRIORITY_REPLY and out.in_reply_to == "<r1@firm.example>" and out.kind == "reply"
    assert db.get(FirmTarget, t.id).stage == "TRIAL" and db.get(FirmReply, row.id).status == "answered"
    M.message_id, M.text = "<r2@firm.example>", "please stop emailing me"
    assert fo.handle_reply(db, M(), t.id, TUE).intent == "opt_out" and db.get(FirmTarget, t.id).stage == "OPTED_OUT"


def test_the_inbox_check_routes_a_prospects_reply_to_the_firm_not_to_a_lead(live):
    db = live
    t = target(db, email="ops@lender.example", kind="hard_money_lender", stream="s5", org="Sample Capital")
    row = fo.enqueue(db, pitch(t, key="s5:p"), TUE)
    row.status, row.message_id, row.sent_at = "sent", "<pitch-1@example.test>", utcnow()
    t.touches = 1
    db.commit()
    imap = FakeIMAP({1: mail(1, "Sounds good. Tell me more about the feed.", frm="Ops <ops@lender.example>",
                             subject="Re: borrower feed", in_reply_to="<pitch-1@example.test>")})
    gm.set_imap_factory(lambda: imap)
    r = gm.poll_gmail_replies(now=T0, smtp_factory=FakeSMTP)
    db.expire_all()
    assert r["firm_replies"] == 1 and r["matched"] == 0
    fr = db.query(FirmReply).one()
    assert fr.intent == "interested" and fr.stream == "s5" and db.query(InboundReply).count() == 0


def test_sealed_notes_answer_prospects_and_add_targets(live):
    db = live
    from engine import cloud_desk as cd
    t = target(db)
    rid = fo.handle_reply(db, type("M", (), {"message_id": "<q@f>", "in_reply_to": None, "references": [],
                                             "from_email": t.email, "subject": "Q", "text": "Which towns?",
                                             "received": None, "auto_reply": False})(), t.id, TUE).id
    db.commit()
    key = "k" * 32
    note = cd.seal_note([{"firm_reply": rid, "reply": "Middlesex and Union towns to start.", "stage": "REPLIED"},
                         {"firm_target": {"kind": "real_estate_cpa", "stream": "s9", "org": "Sample CPAs",
                                          "email": "info@samplecpa.example", "source": "samplecpa.example/contact"}},
                         {"firm_target": {"kind": "real_estate_cpa", "org": "No Contact"}}], cd.public_hex(key))
    assert cd.apply_note(note, key) == {"note_done": 2, "note_errors": "ValueError"}
    db.expire_all()
    assert db.query(FirmOutbox).filter_by(kind="reply").one().in_reply_to == "<q@f>"
    assert db.query(FirmTarget).filter_by(email="info@samplecpa.example").one().stream == "s9"


# -- orchestrator -------------------------------------------------------------------------------------------------
class FakeStream(Stream):
    key, name, cadence_hours, run_hours = "s3", "Fake", 24.0, (0, 24)

    def run(self, session, ctx):
        t = fo.upsert_target(session, kind="foreclosure_surplus_attorney", stream="s3", email="f@law.example",
                             org="F Law")
        return StreamResult(stream="s3", processed=5, found=2, products=1, metrics={"x": 1},
                            outbound=[Outbound(stream="s3", key="s3:pitch:f", to="f@law.example", subject="s",
                                               body="b", target_id=t.id)])


class Boom(Stream):
    key, name, run_hours = "s4", "Boom", (0, 24)

    def run(self, session, ctx):
        raise RuntimeError("bad data")


def test_the_orchestrator_runs_due_streams_records_crashes_and_reports_counts(live, monkeypatch, tmp_path):
    db = live
    monkeypatch.setattr(swarm_firm, "load", lambda k: {"s3": FakeStream(), "s4": Boom()}.get(k))
    r = swarm_firm.run_streams(now=TUE, out_dir=tmp_path)
    assert r["s3"]["queued"] == 1 and r["s4"]["error"] == "RuntimeError" and r["s5"] == {"status": "not installed"}
    again = swarm_firm.run_streams(now=TUE + timedelta(hours=1), out_dir=tmp_path)
    assert "s3" not in again                                                 # cadence: once a day
    db.expire_all()
    assert db.query(FirmStreamRun).filter_by(stream="s4").one().error == "RuntimeError"
    st = swarm_firm.status(db, TUE)
    row = next(x for x in st["streams"] if x["key"] == "s3")
    assert row["found"] == 2 and row["queued"] == 1 and row["targets"] == 1
    flat = swarm_firm.flat(st)
    assert flat["s3_found"] == 2 and flat["s4_error"] == "RuntimeError" and flat["s5_missing"] == 1
    assert any("Surplus research" in x for x in swarm_firm.table_lines(st))


def test_b2b_targets_are_seeded_from_the_yaml_once_per_address(live, tmp_path):
    db = live
    y = tmp_path / "targets.yaml"
    y.write_text("targets:\n"
                 "  - {kind: roofer, org: Sample Roofing, email: info@sampleroofing.example, county: Union,"
                 " source: 'https://sampleroofing.example/contact'}\n"
                 "  - {kind: roofer, org: Sample Roofing, email: INFO@sampleroofing.example}\n"
                 "  - {kind: astrologer, org: Nope, email: a@b.example}\n")
    assert swarm_firm.seed_targets(db, y) == 2
    db.commit()
    [t] = db.query(FirmTarget).all()
    assert (t.kind, t.stream, t.county) == ("roofer", "s4", "Union")


# -- calendar -----------------------------------------------------------------------------------------------------
def test_the_firm_calendar_holds_only_the_status_deadlines_and_warm_actions(live, monkeypatch):
    db = live
    from engine import gcal_sync
    monkeypatch.setitem(settings()["calendar"], "mode", "firm")
    monkeypatch.setenv("DAILY_OPENERS", "0")
    lead = make_lead(db, sheriff_days=10, stale_listing=True)
    lead.next_followup_at = TUE + timedelta(hours=2)
    db.add(InboundReply(message_id="<acc@b>", lead_id=lead.id, from_email="pat@broker.test", status="needs_approval",
                        intents_json=["ACCEPT"], created_at=TUE - timedelta(hours=1)))
    db.commit()
    want = gcal_sync.desired_events(db, now=TUE)
    kinds = {d.kind for d in want.values()}
    assert "desk" in kinds and "actions" in kinds
    assert not kinds & {"followup", "callback", "reply", "sheriff_sale", "daily_goal", "callblock"}
    desk = want[f"desk:{TUE:%Y%m%d}"]
    assert desk.body["summary"].startswith("📊 10-Stream Firm Status") and "10-STREAM FIRM" in desk.body["description"]
    warm = want[f"warm:{TUE:%Y%m%d}"]
    assert "accepted" in warm.body["description"]


# -- streams s1 / s2 ----------------------------------------------------------------------------------------------
def test_s1_matches_warm_conversations_to_cash_buyers(live, tmp_path):
    db = live
    from db.models import BuyerMatch, CashBuyer
    from engine import human_copy as hc
    from engine.streams import s1_acquisitions as s1
    from engine.streams.base import StreamContext
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    db.add(InboundReply(message_id="<w@b>", lead_id=lead.id, from_email="pat@broker.test", status="needs_approval",
                        created_at=TUE - timedelta(days=1)))
    db.add(CashBuyer(entity_key="RIVER", entity_name="RIVER HOMES LLC", cash_purchases_count=6,
                     recent_purchases_json=[{"address": f"{i} SAMPLE AVE", "municipality": "NEWARK CITY",
                                             "county": "Essex", "mun_code": "0714", "price": 400000,
                                             "deed_date": "2026-05-01", "units": 1} for i in range(6)],
                     last_purchase_date=date(2026, 5, 1)))
    db.commit()
    r = s1.STREAM.run(db, StreamContext(now=TUE, out_dir=tmp_path, operator=hc.operator()))
    assert r.outbound == [] and r.metrics["warm_leads"] == 1
    assert r.metrics["warm_matched"] == db.query(BuyerMatch).filter_by(lead_id=lead.id).count() > 0 or \
        r.metrics["warm_matched"] == 0


def test_s2_greets_people_not_brokerages_and_keeps_pitches_short():
    from engine.streams.s2_deal_desk_saas import first_name
    assert [first_name(x) for x in ("Pat Agent", "Coldwell Banker Realty", "JC Smith", "Keller Williams Team",
                                    "J. Doe", "")] == ["Pat", "there", "JC", "there", "there", "there"]


def test_s2_pitches_an_agent_a_digest_of_other_agents_deals_in_their_towns(live, tmp_path):
    db = live
    from engine import human_copy as hc
    from engine.streams import s2_deal_desk_saas as s2
    from engine.streams.base import StreamContext
    from engine.human_copy import lint
    own = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000,
                    assumptions={"quant": {"buyer_max": 420000}}, listing_text="estate sale, sold as-is")
    add_contact(db, own, contact_type=ContactType.LISTING_AGENT, name="Pat Agent", phone=None,
                email="pat@broker.example", company="SAMPLE REALTY")
    others = []
    for i in range(4):
        ld = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000,
                       assumptions={"quant": {"buyer_max": 420000}}, listing_text="needs work, as-is")
        add_contact(db, ld, contact_type=ContactType.LISTING_AGENT, name="Lee Broker", phone=None,
                    email=f"lee{i}@broker.example")
        others.append(ld.id)
    t = fo.upsert_target(db, kind="investor_agent", stream="s2", email="pat@broker.example", name="Pat Agent",
                         org="SAMPLE REALTY", score=50)
    db.commit()
    r = s2.STREAM.run(db, StreamContext(now=TUE, out_dir=tmp_path, operator=hc.operator(),
                                        limits={"pitches_per_run": 4, "agents_top": 0}))
    ob = next(o for o in r.outbound if o.target_id == t.id)
    assert ob.key == f"s2:pitch:{t.id}:2026-W46" and ob.priority == PRIORITY_PITCH and ob.attachments
    assert ob.body.startswith("Hi Pat,") and "$500" in ob.body and "commission" in ob.body
    assert "—" not in ob.body and len(ob.body.split()) < 110
    from pypdf import PdfReader
    text = PdfReader(str(ob.attachments[0])).pages[0].extract_text()
    assert "Towns you list in" in text and own.property.street_address.title() not in text
    sat = s2.STREAM.run(db, StreamContext(now=datetime(2026, 11, 14, 10), out_dir=tmp_path, operator=hc.operator()))
    assert sat.outbound == []



def test_s2_never_pitches_an_agent_we_are_negotiating_a_house_with(live):
    db = live
    from engine.streams import s2_deal_desk_saas as s2
    fo.upsert_target(db, kind="investor_agent", stream="s2", email="busy@broker.example", name="Busy Agent", score=99)
    fo.upsert_target(db, kind="investor_agent", stream="s2", email="free@broker.example", name="Free Agent", score=1)
    db.add(InboundReply(message_id="<deal@b>", from_email="busy@broker.example", status="needs_approval",
                        created_at=TUE - timedelta(days=3)))
    db.commit()
    assert [t.email for t in s2.due_targets(db, TUE, 5, 6, 2)] == ["free@broker.example"]


def test_s2_never_names_a_person_as_the_company():
    # 2026-11-09: a buyer family filed under a person went out as "Deals this week in Rutherford for Soine, Travis"
    from types import SimpleNamespace
    from engine.streams import s2_deal_desk_saas as s2
    prof = SimpleNamespace(towns=[(1, "Rutherford", "Bergen", 3)])
    d = SimpleNamespace(profile=prof, rows=[1, 2, 3])
    person = SimpleNamespace(kind="flipper", org="QUINN, SAMPLE", name=None, data_json={})
    subject, body = s2.pitch_body(person, d, {"first": "Sam"}, follow=False)
    assert subject == "Deals this week in Rutherford" and "Quinn" not in body and "the towns you have been buying" in body
    shop = SimpleNamespace(kind="flipper", org="Sample Home Buyers", name=None, data_json={})
    subject, body = s2.pitch_body(shop, d, {"first": "Sam"}, follow=False)
    assert subject.endswith("for Sample Home Buyers") and "the towns Sample Home Buyers has been buying" in body
    agent = SimpleNamespace(kind="investor_agent", org="Sample Realty", name="JC Agent", data_json={"resales": 1,
                                                                                                    "fixers": 3})
    subject, body = s2.pitch_body(agent, d, {"first": "Sam"}, follow=False)
    assert body.startswith("Hi JC,") and "3 fixer listings and 1 investor resale around Rutherford" in body


def test_targets_are_seeded_when_the_file_appears_or_changes_not_only_once_a_day(live, monkeypatch, tmp_path):
    # 2026-11-09: the first live run came before the target file was committed and marked the day "seeded"; every
    # specialty stream then had nobody to pitch
    db = live
    y = tmp_path / "targets.yaml"
    monkeypatch.setattr(swarm_firm, "TARGETS_FILE", y)
    monkeypatch.setattr(swarm_firm, "run_streams", lambda **kw: {})
    assert "targets_seeded" not in swarm_firm.tick(now=TUE, send=False)          # no file: nothing marked done
    y.write_text("targets:\n  - {kind: roofer, org: Sample Roofing, email: info@sampleroofing.example}\n")
    assert swarm_firm.tick(now=TUE + timedelta(minutes=6), send=False)["targets_seeded"] == 1
    assert "targets_seeded" not in swarm_firm.tick(now=TUE + timedelta(minutes=12), send=False)
    y.write_text(y.read_text() + "  - {kind: roofer, org: Other Roofing, email: info@otherroofing.example}\n")
    assert swarm_firm.tick(now=TUE + timedelta(minutes=18), send=False)["targets_seeded"] == 2
    db.expire_all()
    assert db.query(FirmTarget).count() == 2


def test_company_names_keep_their_initials():
    from engine.streams.base import nice_name
    assert nice_name("N.B. HOME IMPROVEMENT LLC") == "N.B. Home Improvement LLC"     # was "N.b." in a subject
    assert nice_name("JC HEIGHTS HOUSE LLC") == "JC Heights House LLC"
    assert nice_name("42 RUTGERS AVE., LLC") == "42 Rutgers Ave., LLC"
    assert nice_name("AL MAR LLC") == "Al Mar LLC"


def test_a_yes_releases_the_held_sample_in_their_thread_and_the_next_yes_is_warm(live, tmp_path, monkeypatch):
    """The 2-step funnel (2026-11-10): Touch 1 is two plain sentences; "yes, send it over" gets the sample (2 rows in
    full, the rest masked) by itself in their thread; a yes after the sample is the operator's warm action."""
    from types import SimpleNamespace
    from engine.streams.base import render_table_pdf, sample_path
    db = live
    t = target(db)
    pdf = render_table_pdf(tmp_path / "report.pdf", title="Report", subtitle="This week",
                           head=["#", "Property", "Upset"],
                           rows=[[str(i), f"{100 + i} Sample St<br/>Edison", "$100K"] for i in range(5)])
    assert sample_path(pdf).exists()
    ob = pitch(t, key="s3:pitch:9", sample_body="Here's the sheet; $400 a month flat.", sample_attachments=[sample_path(pdf)])
    row = fo.enqueue(db, ob, TUE)
    db.commit()
    held = db.query(FirmOutbox).filter_by(kind="sample").one()
    assert held.status == "held" and held.attachments_json[0]["name"] == "report.sample.pdf"
    mail = SimpleNamespace(message_id="<yes@firm.example>", text="Yes, please send it over.", subject="Re: " + row.subject,
                           from_email=t.email, references=[], received=None, auto_reply=False)
    r = fo.handle_reply(db, mail, t.id, TUE)
    db.commit()
    db.expire_all()
    held = db.get(FirmOutbox, held.id)
    assert r.intent == "interested" and r.status == "sample_sent" and fo.warm(db) == []
    assert held.status == "queued" and held.priority == PRIORITY_REPLY and held.in_reply_to == "<yes@firm.example>"
    assert held.subject == "Re: " + row.subject
    held.status = "sent"
    db.commit()
    again = SimpleNamespace(message_id="<yes2@firm.example>", text="Sounds good, how do we start?", subject="Re: x",
                            from_email=t.email, references=[], received=None, auto_reply=False)
    r2 = fo.handle_reply(db, again, t.id, TUE)
    db.commit()
    assert r2.intent == "interested" and r2.status == "new" and [w.id for w in fo.warm(db)] == [r2.id]
    # a no drops a held sample
    t2 = target(db, email="no@firm.example", org="No LLC")
    fo.enqueue(db, pitch(t2, key="s3:pitch:10", sample_attachments=[sample_path(pdf)]), TUE)
    db.commit()
    no = SimpleNamespace(message_id="<no@firm.example>", text="Not interested, thanks.", subject="Re: x",
                         from_email=t2.email, references=[], received=None, auto_reply=False)
    fo.handle_reply(db, no, t2.id, TUE)
    db.commit()
    assert db.query(FirmOutbox).filter_by(key="s3:pitch:10:sample").one().status == "skipped"


def test_cold_first_touches_are_spaced_but_replies_are_not(live, monkeypatch):
    db = live
    monkeypatch.setitem(settings(), "research_firm", {**settings()["research_firm"], "pace_seconds": [0, 0],
                                                      "cold_gap_minutes": 12})
    a, b = target(db, email="a@firm.example", org="A LLC"), target(db, email="b@firm.example", org="B LLC")
    fo.enqueue(db, pitch(a, key="g1"), TUE)
    fo.enqueue(db, pitch(b, key="g2"), TUE)
    fo.enqueue(db, Outbound(stream="s6", key="g3", to=a.email, subject="Re: x", body="Answer.", priority=PRIORITY_REPLY,
                            kind="reply", target_id=a.id, in_reply_to="<m@x>"), TUE)
    db.commit()
    FakeSMTP.sent = []
    r = fo.send_due(now=TUE_ET, smtp_factory=FakeSMTP, pace=False)
    assert r["sent"] == 2                         # the reply + one cold pitch; the second pitch waits for the gap
    db.expire_all()
    assert db.query(FirmOutbox).filter_by(key="g2").one().status == "queued"
