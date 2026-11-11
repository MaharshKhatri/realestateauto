"""The B2B playbook rules (2026-11-10): one in-thread sequence per target (Touch 1, a nudge on day 3-4, close-the-loop
on day 9-10), the weekly ramp, the bounce / Gmail-block pauses, no attorney or CPA cold mail after 6 PM or on a Friday
afternoon, and "plus NJ sales tax" on every price."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from core.config import settings
from db.models import FirmOutbox, FirmTarget, utcnow
from engine import cold_guard
from engine import firm_outbox as fo
from engine.crm import TZ
from engine.streams.base import PRIORITY_PITCH, PRIORITY_REPLY, Outbound
from tests.test_agent_outreach import FakeSMTP
from tests.test_command_center import setup_paths  # noqa: F401 - pytest fixture
from tests.test_firm import TUE, TUE_ET, pitch, target
from tests.test_gmail_autopilot import live  # noqa: F401 - pytest fixture


@pytest.fixture()
def quick(monkeypatch):
    monkeypatch.setitem(settings(), "research_firm", {**settings()["research_firm"], "pace_seconds": [0, 0],
                                                      "cold_gap_minutes": [0, 0]})


def _sent_pitch(db, t, days_ago: float, mid="<p1@desk>"):
    row = FirmOutbox(key=f"s6:pitch:{t.id}", stream=t.stream, priority=3, kind="pitch", target_id=t.id,
                     to_email=t.email, subject="[Union] sales near Elizabeth", body="Hi.", status="sent",
                     message_id=mid, sent_at=utcnow() - timedelta(days=days_ago))
    db.add(row)
    t.stage, t.touches, t.last_contact_at = "CONTACTED", 1, TUE - timedelta(days=days_ago)
    db.commit()
    return row


# -- the sequence -----------------------------------------------------------------------------------------------------
def test_the_nudge_and_close_the_loop_go_in_the_same_thread(live, quick):
    db = live
    t = target(db, name="Dana Sample")
    p = _sent_pitch(db, t, 2)
    assert fo.queue_nudges(db, TUE_ET) == 0                              # day 2: too early
    t.last_contact_at = TUE - timedelta(days=3, hours=1)
    db.commit()
    assert fo.queue_nudges(db, TUE_ET) == 1
    db.commit()
    nudge = db.query(FirmOutbox).filter_by(key=f"nudge:{p.id}:2").one()
    assert nudge.in_reply_to == "<p1@desk>" and nudge.subject == "Re: [Union] sales near Elizabeth"
    assert nudge.body.startswith("Hi Dana,") and "want me to send the comparable-sales packet?" in nudge.body
    assert nudge.attachments_json == [] and nudge.kind == "followup"
    FakeSMTP.sent = []
    assert fo.send_due(now=TUE_ET, smtp_factory=FakeSMTP, pace=False)["sent"] == 1
    msg = FakeSMTP.sent[-1]
    assert msg["In-Reply-To"] == "<p1@desk>"
    assert "A business solicitation from" in msg.get_body(preferencelist=("plain",)).get_content()
    db.expire_all()
    t = db.get(FirmTarget, t.id)
    assert t.touches == 2
    assert fo.queue_nudges(db, TUE_ET) == 0                              # close-the-loop waits 6 more days
    t.last_contact_at = TUE - timedelta(days=6, hours=1)
    db.commit()
    assert fo.queue_nudges(db, TUE_ET) == 1
    db.commit()
    close = db.query(FirmOutbox).filter_by(key=f"nudge:{p.id}:3").one()
    assert "Last note from me" in close.body and "won't follow up" in close.body
    t.touches = 3
    t.last_contact_at = TUE - timedelta(days=30)
    db.commit()
    assert fo.queue_nudges(db, TUE_ET) == 0                              # three touches: the sequence is over


def test_a_reply_or_a_weekend_ends_or_pauses_the_sequence(live, quick):
    db = live
    t = target(db)
    _sent_pitch(db, t, 5)
    sat = datetime(2026, 11, 14, 10, 0, tzinfo=TZ)
    assert fo.queue_nudges(db, sat) == 0
    t.stage = "REPLIED"
    db.commit()
    assert fo.queue_nudges(db, TUE_ET) == 0


def test_a_streams_own_repitch_gives_way_to_the_nudges(live):
    db = live
    t = target(db)
    _sent_pitch(db, t, 7)
    row = fo.enqueue(db, Outbound(stream="s6", key="s6:follow:x", to=t.email, subject="New sheet", body="Hi.",
                                  priority=3, kind="followup", target_id=t.id), TUE)
    assert row.status == "skipped" and "nudges" in row.reason


# -- ramp --------------------------------------------------------------------------------------------------------------
def test_first_touches_ramp_by_week(live):
    db = live
    assert cold_guard.first_touch_cap(db, 30, date(2026, 11, 10)) == 20       # nothing sent yet: week 1
    t = target(db)
    _sent_pitch(db, t, 2)
    first = db.query(FirmOutbox).filter_by(kind="pitch").one().sent_at.date()
    assert cold_guard.first_touch_cap(db, 30, first + timedelta(days=6)) == 20
    assert cold_guard.first_touch_cap(db, 30, first + timedelta(days=7)) == 30
    assert cold_guard.first_touch_cap(db, 25, first + timedelta(days=30)) == 25   # never above the caps


# -- pauses ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, code", [
    ("550 5.7.1 [x] Our system has detected that this message is likely unsolicited mail", "5.7.1"),
    ("421 4.7.28 Gmail has detected an unusual rate of unsolicited mail", "4.7.28"),
    ("550 5.4.5 Daily user sending limit exceeded", "5.4.5"),
    ("550 5.7.1 Unable to relay for intake@lawfirm.example", None),         # a recipient policy, not a spam block
    ("550 5.1.1 The email account that you tried to reach does not exist", None),
])
def test_which_gmail_codes_stop_cold_mail(text, code):
    assert cold_guard.block_code(text) == code


def test_a_block_code_pauses_cold_mail_for_a_week_but_replies_still_go(live, quick):
    db = live
    a = target(db, email="a@firm.example", org="A LLC")
    fo.enqueue(db, pitch(a, key="b1"), TUE)
    fo.enqueue(db, Outbound(stream="s6", key="b2", to=a.email, subject="Re: x", body="Answer.", priority=PRIORITY_REPLY,
                            kind="reply", target_id=a.id, in_reply_to="<m@x>"), TUE)
    assert cold_guard.note_error(db, "SMTPDataError: 421 4.7.28 unusual rate", TUE_ET) == "4.7.28"
    db.commit()
    assert "paused until" in cold_guard.paused(db, TUE_ET)
    FakeSMTP.sent = []
    r = fo.send_due(now=TUE_ET, smtp_factory=FakeSMTP, pace=False)
    assert r["sent"] == 1 and r["cold_paused"] and FakeSMTP.sent[0]["Subject"] == "Re: x"
    assert cold_guard.paused(db, TUE_ET + timedelta(days=8)) is None


def test_bounces_pause_cold_mail_only_at_five_and_over_eight_percent(live, quick):
    """Operator 2026-11-10: dead domains are skipped before sending, so only 5+ bounces AND > 8% of the window's cold
    sends pause cold mail; a reset starts the count over (the addresses stay bounced)."""
    db = live
    from engine import agent_outreach as ao
    for i in range(3):
        ao.record_bounce(db, f"gone{i}@firm.example")
    db.commit()
    assert cold_guard.paused(db) is None                         # 3 bounces: no longer a pause
    for i in range(3, 5):
        ao.record_bounce(db, f"gone{i}@firm.example")
    db.commit()
    why = cold_guard.paused(db)                                  # 5 bounces, 0 cold sends: over 8%
    assert why and "5 bounces" in why
    for i in range(60):                                          # 5 of 60 sends = 8.3%: still over
        db.add(FirmOutbox(key=f"c{i}", stream="s4", priority=3, kind="pitch", to_email=f"x{i}@firm.example",
                          subject="s", body="b", status="sent", sent_at=utcnow()))
    db.commit()
    assert cold_guard.paused(db)
    for i in range(60, 70):                                      # 5 of 70 = 7.1%: under 8%, cold mail goes
        db.add(FirmOutbox(key=f"c{i}", stream="s4", priority=3, kind="pitch", to_email=f"x{i}@firm.example",
                          subject="s", body="b", status="sent", sent_at=utcnow()))
    db.commit()
    assert cold_guard.paused(db) is None
    assert cold_guard.paused(db, datetime.now(TZ) + timedelta(days=8)) is None


def test_a_reset_restarts_the_bounce_count(live, quick):
    db = live
    from engine import agent_outreach as ao
    for i in range(5):
        ao.record_bounce(db, f"gone{i}@firm.example")
    db.commit()
    assert cold_guard.paused(db)
    cold_guard.reset(db)
    db.commit()
    assert cold_guard.paused(db) is None and "gone0@firm.example" in ao.bounced_emails(db)
    assert cold_guard.note_error(db, "421 4.7.28 unusual rate", TUE_ET) == "4.7.28"
    db.commit()
    assert "paused until" in cold_guard.paused(db, TUE_ET)       # a Gmail block code is never cleared by a reset


# -- timing and price ------------------------------------------------------------------------------------------------
def test_no_attorney_cold_mail_on_a_friday_afternoon(live, quick):
    db = live
    lawyer = target(db, email="intake@lawfirm.example")                       # a tax-appeal attorney
    roofer = target(db, email="info@roofs.example", kind="roofer", stream="s4", org="Sample Roofing")
    fo.enqueue(db, pitch(lawyer, key="f1"), TUE)
    fo.enqueue(db, pitch(roofer, key="f2", prio=PRIORITY_PITCH), TUE)
    db.commit()
    FakeSMTP.sent = []
    fri = datetime(2026, 11, 13, 13, 30, tzinfo=TZ)
    fo.send_due(now=fri, smtp_factory=FakeSMTP, pace=False)
    assert [m["To"] for m in FakeSMTP.sent] == ["info@roofs.example"]
    db.expire_all()
    assert db.query(FirmOutbox).filter_by(key="f1").one().status == "queued"


def test_every_price_says_plus_nj_sales_tax():
    assert fo.price_line("s4").endswith("plus NJ sales tax")
    assert "plus NJ sales tax" in fo.start_reply_text("s2", "https://pay.example/x")


def test_a_copy_fix_reaches_mail_already_queued(live):
    db = live
    t = target(db)
    row = fo.enqueue(db, Outbound(stream="s1", key="s1:buyers:x", to=t.email, subject="As-is houses",
                                  body="I'm working on a few as-is houses and expect to have one under contract soon.",
                                  priority=PRIORITY_PITCH, kind="buyer_note", target_id=t.id), TUE)
    db.commit()
    assert fo.rewrite_queued(db) == 1
    assert "may have one under contract soon" in db.get(FirmOutbox, row.id).body
    assert fo.rewrite_queued(db) == 0


def _fake_dns(monkeypatch, table):
    """table: (domain, rtype) -> status; anything missing is ("ok")."""
    monkeypatch.setattr(cold_guard, "_DOMAIN_CACHE", {})
    monkeypatch.setattr(cold_guard, "_PROBE", {})

    def q(domain, rtype):
        st = table.get((domain, rtype), "ok")
        return (st, ["mx.example.net"] if st == "ok" and rtype == "MX" else [domain] if st == "ok" else [])
    monkeypatch.setattr(cold_guard, "_query", q)


def test_mx_check_decides_which_domains_are_dead(monkeypatch):
    _fake_dns(monkeypatch, {("gone-firm.com", "MX"): "nxdomain",
                            ("site-only.com", "MX"): "noanswer",                        # no MX, but an A record: fine
                            ("no-mail.com", "MX"): "noanswer", ("no-mail.com", "A"): "noanswer",
                            ("no-mail.com", "AAAA"): "noanswer",
                            ("slow-dns.com", "MX"): "error"})
    assert cold_guard.dead_domain("a@gone-firm.com") == "the domain doesn't exist"
    assert cold_guard.dead_domain("a@site-only.com") is None
    assert cold_guard.dead_domain("a@no-mail.com") == "the domain has no mail server (no MX or A record)"
    assert cold_guard.dead_domain("a@slow-dns.com") is None                       # can't tell: send
    assert cold_guard.dead_domain("a@firm.example") is None                       # reserved: never looked up


def test_a_resolver_that_cant_answer_mx_falls_back_to_a_records(monkeypatch):
    """Some resolvers (a test proxy) answer A but never MX: then only an A-record NXDOMAIN counts."""
    _fake_dns(monkeypatch, {("gmail.com", "MX"): "noanswer", ("x.com", "MX"): "noanswer", ("x.com", "A"): "noanswer",
                            ("x.com", "AAAA"): "noanswer", ("gone.com", "MX"): "noanswer", ("gone.com", "A"): "nxdomain"})
    assert cold_guard.dead_domain("a@x.com") is None
    assert cold_guard.dead_domain("a@gone.com") == "the domain doesn't exist"


def test_a_domain_that_does_not_exist_is_never_mailed(live, quick, monkeypatch):
    """2026-11-10: "the domain atbnj.com couldn't be found" was the third bounce in 48 hours and paused all cold mail.
    An e-mail to a domain DNS says can't take mail is never sent and never counts as a bounce."""
    from engine import agent_outreach as ao
    _fake_dns(monkeypatch, {("gone-firm.com", "MX"): "nxdomain"})
    db = live
    t = target(db, email="info@gone-firm.com", org="Gone LLC")
    fo.enqueue(db, pitch(t, key="g1"), TUE)
    db.commit()
    FakeSMTP.sent = []
    fo.send_due(now=TUE_ET, smtp_factory=FakeSMTP, pace=False)
    assert FakeSMTP.sent == []
    db.expire_all()
    row = db.query(FirmOutbox).filter_by(key="g1").one()
    assert row.status == "skipped" and "doesn't exist" in row.reason
    assert "info@gone-firm.com" in ao.bounced_emails(db)
    assert cold_guard.bounces_since(db, utcnow() - timedelta(days=1)) == []      # never sent: no bounce pause


def test_a_sealed_note_resets_the_bounce_pause(live, quick):
    from engine import agent_outreach as ao
    from engine import cloud_desk
    db = live
    for i in range(5):
        ao.record_bounce(db, f"gone{i}@firm.example")
    db.commit()
    assert cold_guard.paused(db)
    key = "k" * 32
    out = cloud_desk.apply_note(cloud_desk.seal_note([{"cold_guard": "reset"}], cloud_desk.public_hex(key)), key)
    assert out["note_done"] == 1 and not out.get("note_errors"), out
    db.expire_all()
    assert cold_guard.paused(db) is None


def test_a_sealed_note_undoes_a_bounce_logged_by_mistake(live, quick):
    """Delay notices counted as bounces before 2026-11-11: {"unbounce": [...]} takes them back out."""
    from engine import agent_outreach as ao
    from engine import cloud_desk
    db = live
    t = target(db, email="info@slow.example", kind="hvac", stream="s4")
    t.stage, t.notes = "LOST", "bounced 2026-11-11"
    for i in range(4):
        ao.record_bounce(db, f"gone{i}@firm.example")
    ao.record_bounce(db, "info@slow.example")
    db.commit()
    assert cold_guard.paused(db)
    key = "k" * 32
    out = cloud_desk.apply_note(cloud_desk.seal_note([{"unbounce": ["info@slow.example"]}],
                                                     cloud_desk.public_hex(key)), key)
    assert out["note_done"] == 1 and not out.get("note_errors"), out
    db.expire_all()
    assert cold_guard.paused(db) is None and "info@slow.example" not in ao.bounced_emails(db)
    assert "gone0@firm.example" in ao.bounced_emails(db)
    assert db.get(FirmTarget, t.id).stage == "CONTACTED"
