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
    assert nudge.body.startswith("Hi Dana,") and "want me to send the recorded-sales sheet?" in nudge.body
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


def test_three_bounces_in_two_days_pause_cold_mail_and_agent_openers(live, quick):
    db = live
    from engine import agent_outreach as ao
    for i in range(3):
        ao.record_bounce(db, f"gone{i}@firm.example")
    db.commit()
    why = cold_guard.paused(db)
    assert why and "3 bounces" in why
    assert cold_guard.paused(db, datetime.now(TZ) + timedelta(hours=49)) is None


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
