"""The Mac runs the desk by itself (2026-11-03): it pulls fixes pushed from the cloud, runs an unattended Claude desk
agent while replies wait, and the price rules hold even when no person is watching."""
from __future__ import annotations

import os
import stat
import subprocess
from datetime import datetime, timedelta

import pytest

from db.models import Contact, InboundReply
from engine import gmail_autopilot as gm
from engine import mac_desk
from engine import reply_negotiator as rn
from tests.test_agent_outreach import FakeSMTP
from tests.test_command_center import GOOD, setup_paths  # noqa: F401 - pytest fixture
from tests.test_gmail_autopilot import T0, FakeIMAP, live, mail, opener  # noqa: F401 - live is a fixture
from tests.test_reply_negotiator import listed


def test_a_signature_license_number_is_not_a_counter():
    text = ("Yes it is. Are you a buyer or a realtor looking to show it to someone else?  Pat Sample Principal Agent "
            "SAMPLE REALTY | Direct: 201-555-0154 | License #: 0897105")
    p = rn.parse_reply(text)
    assert p.counter_amount is None and p.intents == {"AVAILABLE", "ASKS_ROLE"}
    assert rn.parse_reply("MLS # 3901234 is the listing, they'd take 410k").counter_amount == 410000
    assert rn.parse_reply("Seller wants 355k. Lic# 1234567").counter_amount == 355000


def test_no_one_can_reply_above_the_walkaway_but_the_operator(live):
    db = live
    lead = listed(db)
    opener(db, lead)
    walk = lead.property.current_valuation.mao_aggressive
    with pytest.raises(gm.ApprovalError, match="above your walk-away"):
        gm.send_custom_reply(db, lead.id, "We can do more.", price=walk + 5000, smtp_factory=FakeSMTP)
    r = gm.send_custom_reply(db, lead.id, "My best is below.", price=walk - 1000, smtp_factory=FakeSMTP)
    assert r["price"] == walk - 1000
    r = gm.send_custom_reply(db, lead.id, "Fine, I'll stretch.", price=walk + 5000, smtp_factory=FakeSMTP,
                             allow_over_walkaway=True)
    assert r["price"] == walk + 5000


def test_a_bounced_agent_with_a_phone_becomes_a_call_on_the_calendar(live):
    from email.message import EmailMessage
    from email import policy
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    lead.next_followup_at = None
    db.commit()
    b = EmailMessage()
    b["From"], b["To"], b["Subject"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "jordan@example.test", \
        "Delivery Status Notification (Failure)"
    b["Message-ID"], b["In-Reply-To"] = "<bounce-1@mail.gmail.com>", ours
    b.set_content("Address not found\n\nYour message wasn't delivered to pat@broker.test because the address couldn't "
                  "be found.\n\nFinal-Recipient: rfc822; pat@broker.test\nStatus: 5.1.1")
    gm.set_imap_factory(lambda: FakeIMAP({71: b.as_bytes(policy=policy.SMTP)}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=30), smtp_factory=FakeSMTP)
    db.expire_all()
    assert db.query(InboundReply).one().status == "bounce"
    agent = db.query(Contact).filter_by(lead_id=lead.id).one()
    assert agent.email is None and agent.phone
    assert db.get(type(lead), lead.id).next_followup_at is not None
    # The call is labelled as a bounce, not "they asked you to call" (2026-11-06, 344 Daniele Dr)
    from engine import gcal_sync as gc
    ev = gc.followup_events(db, db.get(type(lead), lead.id), (T0 + timedelta(minutes=31)).replace(tzinfo=None))
    assert ev, "the bounce's call slot is on the calendar"
    body = ev[0].body
    assert "(your e-mail bounced)" in body["summary"] and "asked you to call" not in body["summary"]
    assert "my e-mail to you bounced" in body["description"] and "following up on my e-mail" not in body["description"]


# -- self-update -------------------------------------------------------------------------------------------------
def _repo(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repos(tmp_path):
    """origin (bare) <- cloud (pushes fixes); mac = the operator's checkout."""
    origin, cloud, mac = tmp_path / "origin.git", tmp_path / "cloud", tmp_path / "mac"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(cloud)], check=True)
    for r in (cloud,):
        _repo(r, "config", "user.email", "t@example.test")
        _repo(r, "config", "user.name", "T")
        _repo(r, "checkout", "-q", "-b", "main")
    (cloud / "app.py").write_text("VERSION = 1\n")
    _repo(cloud, "add", ".")
    _repo(cloud, "commit", "-qm", "one")
    _repo(cloud, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(mac)], check=True)
    _repo(mac, "config", "user.email", "m@example.test")
    _repo(mac, "config", "user.name", "M")
    return cloud, mac


def test_the_mac_picks_up_pushed_fixes_by_itself(repos):
    cloud, mac = repos
    assert mac_desk.self_update(mac, pip=False)["reason"] == "up to date"
    (cloud / "app.py").write_text("VERSION = 2\n")
    _repo(cloud, "commit", "-qam", "fix")
    _repo(cloud, "push", "-q", "origin", "main")
    (mac / "app.py").write_text("VERSION = 1  # a local edit\n")          # uncommitted edit on the Mac
    u = mac_desk.self_update(mac, pip=False)
    assert u["updated"] and u["commits"] == 1 and u["stashed"] and (mac / "app.py").read_text() == "VERSION = 2\n"
    assert "local edits parked" in _repo(mac, "stash", "list")             # parked, never half-merged


def test_a_conflicting_local_commit_keeps_the_current_code(repos):
    cloud, mac = repos
    (cloud / "app.py").write_text("VERSION = 2\n")
    _repo(cloud, "commit", "-qam", "cloud fix")
    _repo(cloud, "push", "-q", "origin", "main")
    (mac / "app.py").write_text("VERSION = 3\n")
    _repo(mac, "commit", "-qam", "mac fix")
    u = mac_desk.self_update(mac, pip=False)
    assert not u["updated"] and "conflict" in u["reason"] and (mac / "app.py").read_text() == "VERSION = 3\n"
    assert _repo(mac, "status", "--porcelain") == ""                         # no half-done rebase left behind


# -- the unattended desk agent ----------------------------------------------------------------------------------
def test_the_desk_agent_runs_only_while_replies_wait_inside_hours_and_not_too_often(db):
    noon = datetime(2026, 11, 4, 12, 0)
    assert not mac_desk.agent_due(db, noon, waiting=0)
    assert not mac_desk.agent_due(db, datetime(2026, 11, 4, 21, 0), waiting=3)
    assert mac_desk.agent_due(db, noon, waiting=2)
    mac_desk._save(db, last_run=noon.isoformat())
    assert not mac_desk.agent_due(db, noon + timedelta(minutes=30), waiting=2)
    assert mac_desk.agent_due(db, noon + timedelta(minutes=91), waiting=2)


def test_the_desk_agent_gets_its_prompt_and_only_the_desk_commands(db, tmp_path):
    fake = tmp_path / "claude"
    seen = tmp_path / "args.txt"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {seen}\necho 'sent 2 answers; left 1 call for you'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert mac_desk.find_claude(str(fake)) == str(fake)
    root = tmp_path / "repo"
    (root / ".claude" / "commands").mkdir(parents=True)
    r = mac_desk.run_agent(db, str(fake), datetime(2026, 11, 4, 12, 0), root=root)
    assert r["ok"] and "left 1 call" in r["report"]
    args = seen.read_text().splitlines()
    assert args[0] == "-p" and "unattended agent" in args[1] and "--allowedTools" in args
    tools = args[args.index("--allowedTools") + 1:]
    assert tools == mac_desk.AGENT_TOOLS and not any(t.startswith("Write") or t.startswith("Edit") for t in tools)
    assert "left 1 call" in (root / "logs" / "agent.log").read_text()
    assert mac_desk._state(db)["last_ok"] is True
