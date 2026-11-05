"""The desk without the Mac (2026-11-03): GitHub Actions runs it; the Mac hands its database over through Gmail,
sealed so only the cloud can open it, then goes idle. Nothing here touches the real Gmail."""
from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from datetime import datetime
from email import policy

import pytest
from sqlalchemy import select
from cryptography.exceptions import InvalidTag

from engine import cloud_desk as cd
from engine import gmail_autopilot as gm
from tests.test_gmail_autopilot import FakeIMAP

KEY = "x" * 24 + "cloud-desk-test-key"
ME = "jordan@example.test"


class Courier:
    """SMTP that delivers to the account's own Sent folder, which the fake IMAP then serves."""

    def __init__(self):
        self.store: dict[int, bytes] = {}

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        self.store[len(self.store) + 1] = msg.as_bytes(policy=policy.SMTP)


@pytest.fixture()
def courier(monkeypatch):
    c = Courier()
    gm.set_imap_factory(lambda: FakeIMAP(c.store))
    monkeypatch.setenv("SMTP_USER", ME)
    monkeypatch.setenv("SMTP_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("DESK_KEY", KEY)
    monkeypatch.setattr(gm, "credentials", lambda: (ME, "abcdefghijklmnop"))
    yield c
    gm.set_imap_factory(None)


def _root(tmp_path, rows=3):
    root = tmp_path / "mac"
    (root / "config").mkdir(parents=True)
    con = sqlite3.connect(root / "wholesaling.db")
    con.execute("create table t (x)")
    con.executemany("insert into t values (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()
    (root / "config" / "settings.local.yaml").write_text("operator:\n  name: Jordan Operator\n")
    (root / "config" / "token.json").write_text('{"token": "t"}')
    return root


def test_only_the_cloud_can_open_what_the_mac_seals():
    sealed = cd.seal(b"the whole desk", cd.public_hex(KEY))
    assert cd.unseal(sealed, KEY) == b"the whole desk"
    with pytest.raises(InvalidTag):
        cd.unseal(sealed, "another-key-entirely-123")
    assert cd.unlock(cd.lock(b"state", KEY), KEY) == b"state"
    with pytest.raises(InvalidTag):
        cd.unlock(cd.lock(b"state", KEY), "another-key-entirely-123")


def test_a_bundle_carries_the_database_and_config_and_nothing_else(tmp_path):
    root = _root(tmp_path, rows=5)
    data = cd.make_bundle(root)
    dest = tmp_path / "cloud"
    assert sorted(cd.apply_bundle(data, dest)) == ["config/settings.local.yaml", "config/token.json", "wholesaling.db"]
    assert sqlite3.connect(dest / "wholesaling.db").execute("select count(*) from t").fetchone()[0] == 5
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:xz") as tar:
        for name in ("../escape.txt", ".env", "config/token.json"):
            info = tarfile.TarInfo(name)
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
    assert cd.apply_bundle(evil.getvalue(), dest) == ["config/token.json"]
    assert not (tmp_path / "escape.txt").exists() and not (dest / ".env").exists()


def test_gmail_carries_a_bundle_in_parts_and_checks_it(courier, monkeypatch):
    monkeypatch.setattr(cd, "PART_BYTES", 1000)
    blob = bytes(range(256)) * 13
    n = cd.send_bundle(ME, "pw", blob, "db", now=datetime(2026, 11, 3, 15, 0), smtp_factory=courier)
    assert n == 4 and len(courier.store) == 4
    assert cd.latest_bundle(ME, "pw", "db") == ("20261103T150000", blob)
    assert cd.latest_bundle(ME, "pw", "config") is None
    del courier.store[3]                                         # a part never arrived: that set is skipped
    assert cd.latest_bundle(ME, "pw", "db") is None


def test_the_mac_hands_off_once_the_cloud_is_ready_then_stays_idle(courier, tmp_path):
    root = _root(tmp_path)
    assert cd.mac_handoff(root=root, smtp_factory=courier)["state"] == "local"      # cloud not set up yet
    cd.post_ready(ME, "pw", KEY, smtp_factory=courier)
    assert cd.ready_key(ME, "pw") == cd.public_hex(KEY)
    h = cd.mac_handoff(root=root, smtp_factory=courier, now=datetime(2026, 11, 3, 16, 0))
    assert h["state"] == "handed_off" and (root / "data" / "runtime_cloud.json").exists()
    stamp, sealed = cd.latest_bundle(ME, "pw", "db")
    assert "wholesaling.db" in cd.apply_bundle(cd.unseal(sealed, KEY), tmp_path / "check")
    assert cd.mac_handoff(root=root, smtp_factory=courier) == {"state": "idle"}
    import os
    t = root / "config" / "token.json"
    os.utime(t, (t.stat().st_mtime + 60, t.stat().st_mtime + 60))           # a new calendar sign-in on the Mac
    assert cd.mac_handoff(root=root, smtp_factory=courier, now=datetime(2026, 11, 4, 9, 0))["sent"] == "config"
    assert cd.latest_bundle(ME, "pw", "config")[0] == "20261104T090000"


def test_the_cloud_waits_for_the_handoff_then_runs_from_its_own_encrypted_state(courier, tmp_path, monkeypatch):
    import db.database as database
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    monkeypatch.setenv("WHOLESALING_DATABASE_URL", f"sqlite:///{cloud / 'wholesaling.db'}")
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_SessionFactory", None)
    monkeypatch.setattr(cd, "_reload", lambda root: None)
    ran = []
    monkeypatch.setattr(cd, "_work", lambda task, **kw: ran.append(task) or {"sent": 0})
    assert cd.cloud_run(root=cloud, smtp_factory=courier) == {"state": "waiting_for_handoff", "ready_posted": True}
    assert cd.cloud_run(root=cloud, smtp_factory=courier) == {"state": "waiting_for_handoff"}       # asks once
    mac = _root(tmp_path)
    cd.mac_handoff(root=mac, smtp_factory=courier)
    r = cd.cloud_run(root=cloud, smtp_factory=courier)
    assert r["state"] == "ok" and r["restored"] and ran == ["run"]
    assert (cloud / "data" / "cloud" / "state.enc").exists()
    assert b"Jordan Operator" not in (cloud / "data" / "cloud" / "state.enc").read_bytes()     # encrypted at rest
    # The next run on a fresh runner: only the cached state.enc, no bundle needed.
    for p in (cloud / "wholesaling.db", cloud / "config" / "settings.local.yaml"):
        p.unlink()
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_SessionFactory", None)
    monkeypatch.setattr(cd, "latest_bundle", lambda user, pw, kind, **kw: None)
    r = cd.cloud_run(root=cloud, smtp_factory=courier)
    assert r["state"] == "ok" and "restored" not in r and (cloud / "config" / "settings.local.yaml").exists()
    assert sqlite3.connect(cloud / "wholesaling.db").execute("select count(*) from t").fetchone()[0] == 3


def test_a_gmail_drop_while_looking_for_a_config_bundle_doesnt_fail_the_run(courier, tmp_path, monkeypatch):
    """2026-11-05: three runs failed with TimeoutError / IMAP "abort" in the config-bundle check before any work."""
    import imaplib
    import db.database as database
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    monkeypatch.setenv("WHOLESALING_DATABASE_URL", f"sqlite:///{cloud / 'wholesaling.db'}")
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_SessionFactory", None)
    monkeypatch.setattr(cd, "_reload", lambda root: None)
    ran = []
    monkeypatch.setattr(cd, "_work", lambda task, **kw: ran.append(task) or {"sent": 0})
    cd.cloud_run(root=cloud, smtp_factory=courier)                     # posts "ready"
    cd.mac_handoff(root=_root(tmp_path), smtp_factory=courier)
    real = cd.latest_bundle

    def flaky(user, pw, kind, **kw):
        if kind == "config":
            raise imaplib.IMAP4.abort("socket error: EOF")
        return real(user, pw, kind, **kw)
    monkeypatch.setattr(cd, "latest_bundle", flaky)
    r = cd.cloud_run(root=cloud, smtp_factory=courier)
    assert r["state"] == "ok" and r["config_check"] == "abort" and ran == ["run"]


def test_calendar_errors_reach_the_public_log_as_safe_codes():
    assert cd.calendar_code("Google is busy (403); queued for the next sync") == "busy-403"
    assert cd.calendar_code("can't reach Google (TimeoutError); queued for the next sync") == "offline-TimeoutError"
    assert cd.calendar_code("Google sign-in expired or was revoked: run python manage.py setup-calendar") == "auth"
    assert cd.calendar_code("Google said 400: <event body with a street>") == "error-400"


def test_without_its_secrets_the_cloud_desk_does_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("DESK_KEY", raising=False)
    assert cd.cloud_run(root=tmp_path) == {"state": "not_configured"}


def test_after_a_call_claude_can_send_the_offer_or_close_the_reply_with_ids_only(db, monkeypatch, tmp_path):
    """The cloud chat reaches the desk only through the public workflow inputs: a lead # ("send the offer") or a reply #
    ("handled on a call"). No price travels, so the offer is the desk's own, under the walk-away."""
    from db.models import InboundReply
    from engine import autopilot
    from tests.factories import make_lead
    lead = make_lead(db)
    row = InboundReply(message_id="<r1@example.test>", lead_id=lead.id, status="needs_approval")
    db.add(row)
    db.commit()
    sent = []
    monkeypatch.setattr(autopilot, "send", lambda s, lead_id, channel, **kw: sent.append((lead_id, channel)) or
                        {"kind": "touch2", "touch": 2, "to": "agent@example.test"})
    early = datetime(2026, 11, 3, 7, 0)                        # before the inbox hours: only the task itself runs
    kw = dict(now=early, smtp_factory=None, user=ME, pw="p", desk_key=KEY, root=tmp_path)
    assert cd._work("offer", approve_id=None, lead_id=lead.id, **kw)["offer_sent"] == 1
    assert sent == [(lead.id, "smtp")]
    assert cd._work("dismiss", approve_id=row.id, **kw)["dismissed"] == 1
    db.refresh(row)
    assert row.status == "dismissed" and row.note == "handled on a call"
    assert cd._work("dismiss", approve_id=row.id, **kw)["dismiss_error"] == "ApprovalError"     # only once


def test_a_new_calendar_sign_in_on_the_mac_goes_to_the_cloud_at_once_asking_for_a_new_password_if_needed(monkeypatch):
    """2026-11-03: the Mac's Gmail app password had been pasted into a chat and had to be revoked, so the hand-over of
    the next calendar sign-in can't count on it: setup-calendar asks for a new one, then hands the sign-in over."""
    import manage
    calls = iter([RuntimeError("535 bad credentials"), {"state": "idle", "sent": "config"}])

    def handoff():
        r = next(calls)
        if isinstance(r, Exception):
            raise r
        return r
    asked = []
    monkeypatch.setattr(cd, "mac_handoff", handoff)
    monkeypatch.setattr(gm, "test_connection", lambda **kw: {"ok": False, "smtp": {"ok": False}, "imap": {"ok": False}})
    monkeypatch.setattr(manage, "cmd_set_gmail", lambda args: asked.append(args))
    manage._hand_calendar_to_cloud()
    assert len(asked) == 1


def test_a_sealed_note_from_the_chat_logs_the_calls_and_sends_the_replies(db, monkeypatch):
    """2026-11-03: the operator reports calls in the cloud chat ("Carlo picked up: seller needs 497 net...").
    Claude seals a note to the desk's public key; only the desk opens it; replies go through send_custom_reply and
    calls through crm.log_call, whose follow-up becomes the next call on the calendar."""
    from db.models import Interaction
    from engine import gcal_sync
    from tests.factories import add_contact, make_lead
    from db.models import ContactType
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    add_contact(db, lead, contact_type=ContactType.LISTING_AGENT, name="PAT AGENT", phone="9735550100",
                email="pat@brokerage.example")
    db.commit()
    sent = []
    monkeypatch.setattr(gm, "send_custom_reply", lambda s, lead_id, text, **kw: sent.append((lead_id, text, kw)))
    note = cd.seal_note([{"lead": lead.id, "reply": "Hi Pat, thanks for the call.", "call": "SPOKE_WARM_FOLLOWUP",
                          "notes": "seller needs about 497K net; tank sweep clean", "followup_at": "2026-12-03T11:00"},
                         {"lead": 999999, "call": "NO_ANSWER"}], cd.public_hex(KEY))
    assert "seller" not in note                                       # ciphertext only
    r = cd.apply_note(note, KEY)
    assert r == {"note_done": 1, "note_errors": "ValueError"}
    assert sent[0][0] == lead.id and sent[0][2]["allow_over_walkaway"] is False
    db.expire_all()
    call = db.scalars(select(Interaction).where(Interaction.lead_id == lead.id, Interaction.channel == "call")).one()
    assert call.outcome == "SPOKE_WARM_FOLLOWUP" and call.next_followup_at == datetime(2026, 12, 3, 11, 0)
    [ev] = gcal_sync.followup_events(db, db.get(type(lead), lead.id), datetime(2026, 11, 3, 18, 0))
    assert ev.body["summary"].startswith("📞 Call Pat Agent back about") and "497K net" in ev.body["description"]
    assert "Optional call" not in ev.body["description"] and "voicemail" in ev.body["description"]
    assert cd.apply_note("not a note", KEY) == {"note_error": "unreadable"}


def test_new_pricing_rules_reprice_every_valuation_once_before_anything_is_sent(db, monkeypatch, tmp_path):
    """2026-11-03: stored valuations carried the old fees and rehab tiers. The first run on new rules re-prices them all."""
    from engine import offer_math
    from tests.factories import make_lead
    make_lead(db, stale_listing=True)
    db.commit()
    early = datetime(2026, 11, 3, 7, 0)                        # before the inbox hours: no Gmail
    kw = dict(now=early, smtp_factory=None, user=ME, pw="p", desk_key=KEY, root=tmp_path)
    first = cd._work("run", approve_id=None, **kw)
    assert first["repriced"] >= 1 and first["reprice_errors"] == 0
    assert "repriced" not in cd._work("run", approve_id=None, **kw)                   # once per rules version
    monkeypatch.setattr(offer_math, "PRICING_VERSION", "next")
    assert cd._work("run", approve_id=None, **kw)["repriced"] >= 1
    assert cd._work("recalibrate", approve_id=None, **kw)["repriced"] >= 1           # on demand (workflow task)


def test_a_cell_number_heard_on_a_call_goes_on_the_agents_card(db, monkeypatch):
    """2026-11-04: Lou's office said he wasn't in and gave his cell; the next call should dial the cell."""
    from tests.factories import add_contact, make_lead
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Lou Agent", phone="7325550100", company="Sample Realty")
    db.commit()
    key = "k" * 32
    note = cd.seal_note([{"lead": lead.id, "agent_phone": "(732) 555-0177", "call": "NO_ANSWER",
                          "notes": "office gave his cell"}], cd.public_hex(key))
    assert cd.apply_note(note, key)["note_done"] == 1
    db.expire_all()
    cell = [c for c in lead.contacts if c.phone == "7325550177"]
    assert cell and cell[0].phone_type == "wireless" and cell[0].name == "Lou Agent" and cell[0].company == "Sample Realty"
    from engine import call_sheet as cs
    assert cs.agent_line(db, lead)[0] == "(732) 555-0177"                      # the sheet dials the cell first


def test_the_office_names_the_real_listing_agent(db, monkeypatch):
    """2026-11-04: the brokerage office said Patricia handles 1802 Clinton St and gave her number."""
    from tests.factories import add_contact, make_lead
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Premier", phone="9085550100", email="office@b.test")
    db.commit()
    key = "k" * 32
    note = cd.seal_note([{"lead": lead.id, "agent_phone": "908-555-0178", "agent_name": "Patricia Agent"}],
                        cd.public_hex(key))
    assert cd.apply_note(note, key)["note_done"] == 1
    db.expire_all()
    new = [c for c in lead.contacts if c.phone == "9085550178"]
    assert new and new[0].name == "Patricia Agent" and new[0].email is None
    assert any(c.name == "Premier" and c.phone == "9085550100" for c in lead.contacts)     # the office card stays


def test_a_note_closes_the_replies_it_settled_and_logs_an_email_outcome(db, monkeypatch):
    """2026-11-04: Claude read the business inbox (Jon: 'received the offer, sharing with the sellers'). The note
    closes the waiting reply so the desk sends nothing more, and logs what was heard as an e-mail outcome."""
    from db.models import InboundReply, Interaction
    from tests.factories import add_contact, make_lead
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Jon Agent", email="jon@brokerage.example")
    db.add(InboundReply(message_id="<jon@b.test>", lead_id=lead.id, status="needs_approval", verdict="INFO",
                        from_email="jon@brokerage.example", subject="Re: 1 Test St", body_text="x"))
    db.commit()
    key = "k" * 32
    note = cd.seal_note([{"lead": lead.id, "close_replies": "sharing the offer with the sellers",
                          "call": "SPOKE_WARM_FOLLOWUP", "channel": "email", "notes": "received the offer",
                          "followup_at": "2026-11-06T10:00:00"}], cd.public_hex(key))
    assert cd.apply_note(note, key) == {"note_done": 1}
    db.expire_all()
    row = db.scalars(select(InboundReply).where(InboundReply.lead_id == lead.id)).one()
    assert row.status == "dismissed" and row.note == "closed from the chat: sharing the offer with the sellers"
    inter = db.scalars(select(Interaction).where(Interaction.lead_id == lead.id)).one()
    assert inter.channel == "email" and inter.next_followup_at == datetime(2026, 11, 6, 10, 0)
    from engine import call_sheet as cs
    t0 = cs.Row(code="A01", tier="T0", lead_id=lead.id, phone="", who="Jon Agent", streets=["1 Test St"], facts="", say="")
    assert cs.refreshed(db, t0).say.startswith("SKIP TODAY: settled by e-mail (sharing the offer")


def test_a_note_changes_the_operators_phone_on_every_email(db, monkeypatch, tmp_path):
    """2026-11-04: the operator moved to a new business number; it reaches the signature, scripts and voicemail line."""
    from core.config import settings
    from engine import human_copy as hc
    from engine import operator_setup
    local = tmp_path / "settings.local.yaml"
    monkeypatch.setattr(operator_setup, "LOCAL_SETTINGS", local)
    monkeypatch.setitem(settings()["operator"], "phone", "(732) 555-0100")
    key = "k" * 32
    note = cd.seal_note([{"operator": {"phone": "908-555-0142", "ssn": "x"}}], cd.public_hex(key))
    assert cd.apply_note(note, key) == {"note_done": 1}
    assert settings()["operator"]["phone"] == "(908) 555-0142" and "(908) 555-0142" in hc.signature_lines()
    assert "ssn" not in local.read_text() and "(908) 555-0142" in local.read_text()
    bad = cd.seal_note([{"operator": {"phone": "12345"}}], cd.public_hex(key))
    assert cd.apply_note(bad, key) == {"note_done": 0, "note_errors": "ValueError"}


def test_a_refresh_that_runs_long_is_cut_off_and_the_rest_of_the_run_still_happens(db, monkeypatch, tmp_path):
    """2026-11-04: with live listings the pipeline outgrew 45 minutes and the whole run failed; a cut-off now keeps
    what was committed and the run goes on (re-price, sheet, inbox, calendar). 80 minutes before 7 AM, 40 in the day."""
    import subprocess
    seen = []

    def slow(cmd, **kw):
        seen.append(kw["timeout"])
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    monkeypatch.setattr(cd.subprocess, "run", slow)
    kw = dict(smtp_factory=None, user=ME, pw="p", desk_key=KEY, root=tmp_path, approve_id=None)
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    early = cd._work("refresh", now=datetime(2026, 11, 5, 5, 17, tzinfo=et), **kw)
    assert early["refresh"] == "cut off at 80 min" and "repriced" in early
    day = cd._work("refresh", now=datetime(2026, 11, 5, 13, 0, tzinfo=et), **kw)
    assert day["refresh"] == "cut off at 40 min" and seen == [80 * 60, 40 * 60]


def test_gmail_dropping_the_line_costs_one_inbox_check_not_the_run(db, monkeypatch, tmp_path):
    """2026-11-05: a TimeoutError and an IMAP "abort" from Gmail each failed a whole desk run (no calendar sync, a red
    run). The inbox step reports it in the counts; the calendar still syncs and the run finishes."""
    import imaplib
    from engine import gcal_sync, gmail_autopilot as gm
    synced = []
    monkeypatch.setattr(gcal_sync, "configured", lambda: True)
    monkeypatch.setattr(gcal_sync, "sync", lambda s, now=None: synced.append(now) or type("R", (), {"error": None})())
    kw = dict(smtp_factory=None, user=ME, pw="p", desk_key=KEY, root=tmp_path, approve_id=None)
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    for exc in (TimeoutError("timed out"), imaplib.IMAP4.abort("socket error: EOF")):
        def boom(*a, _exc=exc, **k):
            raise _exc
        monkeypatch.setattr(gm, "run_inbox", boom)
        out = cd._work("dismiss", now=datetime(2026, 11, 5, 16, 20, tzinfo=et), **kw)
        assert out["gmail_error"] == type(exc).__name__ and out["calendar"] == "ok"
    assert len(synced) == 2


def test_the_night_pricing_run_values_the_new_listings(db, monkeypatch, tmp_path):
    """2026-11-04: the first cloud harvest brought 1,567 unvalued listings; task `value` prices them (70 minutes at
    night, 30 in the day) and every daytime run prices a few more."""
    import swarm.orchestrator as orch
    calls = []

    class Run:
        metrics_json = {"valued": 250, "left_for_next_run": 1300}
    monkeypatch.setattr(orch, "run_agent", lambda name, **kw: calls.append((name, kw)) or Run())
    kw = dict(smtp_factory=None, user=ME, pw="p", desk_key=KEY, root=tmp_path, approve_id=None)
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    night = cd._work("value", now=datetime(2026, 11, 5, 5, 0, tzinfo=et), **kw)      # before 7 AM (no backup)
    assert night["new_valued"] == 250 and night["unvalued_left"] == 1300
    assert calls[0] == ("value_new_listings", {"cap": 600, "max_minutes": 70})
    cd._work("value", now=datetime(2026, 11, 5, 13, 0, tzinfo=et), **kw)
    assert calls[1][1]["max_minutes"] == 30


def test_a_note_hands_the_listing_to_the_colleague_the_agent_named(db, monkeypatch):
    """2026-11-04: "Please contact Geri, who I've CC'd" (the agent was travelling): the note finds the lead by its
    street, saves Geri as the listing agent's e-mail and sends the answer to her in the same thread."""
    from tests.factories import add_contact, make_lead
    lead = make_lead(db, sheriff_days=None, stale_listing=True)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Jacqui Agent", email="jacqui@broker.example",
                company="Sample Realty")
    db.commit()
    sent = []
    monkeypatch.setattr(gm, "send_custom_reply", lambda s, lead_id, text, **kw: sent.append((lead_id, kw["to"])))
    monkeypatch.setattr(gm, "resolve_lead", lambda s, ref: lead.id if ref == "1 Test St" else 0)
    key = "k" * 32
    note = cd.seal_note([{"street": "1 Test St", "agent_email": "Geri@Broker.Example", "agent_name": "Geri Agent",
                          "reply": "Hi Geri, Jacqui pointed me to you."}], cd.public_hex(key))
    assert cd.apply_note(note, key) == {"note_done": 1}
    assert sent == [(lead.id, "geri@broker.example")]
    db.expire_all()
    geri = [c for c in lead.contacts if c.email == "geri@broker.example"]
    assert geri and geri[0].name == "Geri Agent" and geri[0].company == "Sample Realty"
    bad = cd.seal_note([{"lead": lead.id, "agent_email": "not-an-address"}], cd.public_hex(key))
    assert cd.apply_note(bad, key)["note_errors"] == "ValueError"
