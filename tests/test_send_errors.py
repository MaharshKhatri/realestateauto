"""The ✉️ Send Email & Log button never shows a bare "Internal Server Error".

Gmail refusing the sign-in, a macOS certificate problem, a bad agent address, a busy database or an offer PDF that
won't build each come back as one sentence the operator can act on. App passwords are cleaned, the certificates come
from certifi, settings edited outside the running server are picked up, and the calendar hook can't break a save."""
from __future__ import annotations

import smtplib
import ssl
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from core import mail
from db.models import Interaction
from engine import agent_outreach as ao
from engine import autopilot as ap
from engine import gmail_autopilot as gm
from engine import operator_setup
from core.config import settings
from tests.test_command_center import GOOD, setup_paths  # noqa: F401 - pytest fixture
from tests.test_gmail_autopilot import FakeIMAP
from tests.test_reply_negotiator import listed


class Recorder:
    """smtplib.SMTP_SSL stand-in: records the login and the context it was built with."""
    logins: list = []
    sent: list = []
    contexts: list = []
    fail: BaseException | None = None

    def __init__(self, host=None, port=None, context=None, timeout=None):
        if Recorder.fail is not None and isinstance(Recorder.fail, (ssl.SSLError, OSError)) and \
                not isinstance(Recorder.fail, smtplib.SMTPException):
            raise Recorder.fail
        Recorder.contexts.append(context)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        Recorder.logins.append((user, password))
        if isinstance(Recorder.fail, smtplib.SMTPAuthenticationError):
            raise Recorder.fail

    def send_message(self, msg):
        if isinstance(Recorder.fail, smtplib.SMTPRecipientsRefused):
            raise Recorder.fail
        Recorder.sent.append(msg)


@pytest.fixture()
def app_client(db, setup_paths, tmp_path, monkeypatch):  # noqa: F811
    import engine.contracts as contracts
    import web.app as webapp
    monkeypatch.setattr(contracts, "DEALS_DIR", tmp_path / "deals")
    operator_setup.save({**GOOD, "app_password": "abcd efgh ijkl mnop"})
    monkeypatch.setattr(ao.smtplib, "SMTP_SSL", Recorder)
    Recorder.logins, Recorder.sent, Recorder.contexts, Recorder.fail = [], [], [], None
    return TestClient(webapp.app, raise_server_exceptions=False)


def send(client, lead):
    return client.post(f"/api/autopilot/{lead.id}/send", json={"channel": "smtp"})


AUTH = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted. For more information, go to "
                                            b"https://support.google.com/mail/?p=BadCredentials")
CERT = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get "
                                       "local issuer certificate (_ssl.c:1028)")


@pytest.mark.parametrize("failure, words", [
    (AUTH, ["Gmail refused the sign-in for jordan@example.test", "app password", "535"]),
    (CERT, ["certificate", "pip install -U certifi"]),
    (smtplib.SMTPRecipientsRefused({"pat@broker.test": (550, b"no such user")}), ["refused the agent's address"]),
    (ConnectionRefusedError(61, "Connection refused"), ["refused the connection"]),
])
def test_gmail_failures_come_back_as_one_plain_sentence(db, app_client, failure, words):
    lead = listed(db)
    db.commit()
    Recorder.fail = failure
    r = send(app_client, lead)
    assert r.status_code == 409 and r.headers["content-type"].startswith("application/json")
    detail = r.json()["detail"]
    assert all(w in detail for w in words), detail
    assert "abcdefghijklmnop" not in detail                        # the password never reaches the screen
    db.expire_all()
    assert db.query(Interaction).filter_by(lead_id=lead.id).count() == 0      # nothing logged as sent


def test_send_works_with_a_spaced_app_password_and_certifi(db, app_client, monkeypatch):
    seen = {}
    real = ssl.create_default_context

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)
    monkeypatch.setattr(mail.ssl, "create_default_context", spy)
    lead = listed(db)
    db.commit()
    r = send(app_client, lead)
    assert r.status_code == 200 and r.json()["done"] == 1
    assert Recorder.logins == [("jordan@example.test", "abcdefghijklmnop")]
    assert seen.get("cafile", "").endswith("cacert.pem")             # certifi's bundle, not the bare macOS store
    assert Recorder.sent[0]["To"] == "pat@broker.test"


def test_app_password_with_hyphens_is_accepted_and_cleaned(setup_paths):  # noqa: F811
    st = operator_setup.save({**GOOD, "app_password": "abcd-efgh-ijkl-mnop"})
    assert st["has_app_password"]
    assert "SMTP_APP_PASSWORD=abcdefghijklmnop" in (setup_paths / ".env").read_text()
    assert mail.clean_app_password(" abcd efgh-ijkl mnop ") == "abcdefghijklmnop"


def test_settings_and_env_edited_outside_the_server_are_picked_up(setup_paths, monkeypatch):  # noqa: F811
    operator_setup.save(GOOD)
    operator_setup.sync_from_disk()                                  # first look: records the files
    env, local = setup_paths / ".env", setup_paths / "settings.local.yaml"
    env.write_text("SMTP_USER=new@example.test\nSMTP_APP_PASSWORD=qrst uvwx yzab cdef\n")
    local.write_text(local.read_text().replace("PO Box 10, Edison, NJ 08817", "PO Box 22, Metuchen, NJ 08840"))
    import os
    t = env.stat().st_mtime + 5
    os.utime(env, (t, t))
    os.utime(local, (t, t))
    ok, _ = ao.smtp_ready()
    assert ok and os.environ["SMTP_USER"] == "new@example.test"
    assert settings()["operator"]["mailing_address"] == "PO Box 22, Metuchen, NJ 08840"
    assert gm.credentials() == ("new@example.test", "qrstuvwxyzabcdef")


def test_busy_database_and_unexpected_errors_are_json_not_500_pages(db, app_client, monkeypatch):
    lead = listed(db)
    db.commit()

    def locked(*a, **kw):
        raise OperationalError("INSERT INTO interactions ...", {}, Exception("database is locked"))
    monkeypatch.setattr(ap, "send", locked)
    r = send(app_client, lead)
    assert r.status_code == 503 and "database is busy" in r.json()["detail"]
    assert "Sent folder" in r.json()["detail"]

    def boom(*a, **kw):
        raise KeyError("agent")
    monkeypatch.setattr(ap, "send", boom)
    r = send(app_client, lead)
    assert r.status_code == 500 and r.headers["content-type"].startswith("application/json")
    assert "KeyError" in r.json()["detail"] and "logs/server.log" in r.json()["detail"]


def test_an_offer_pdf_that_wont_build_never_blocks_the_email(db, app_client, monkeypatch):
    import engine.contracts as contracts
    lead = listed(db)
    from datetime import datetime
    db.add(Interaction(lead_id=lead.id, channel="email_in", outcome="AGENT_REPLIED", notes="still available",
                       occurred_at=datetime.now()))
    db.commit()
    monkeypatch.setattr(contracts, "generate", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("font missing")))
    p = ao.build_packet(lead, session=db, generate_loi=True)
    assert p.kind == "offer" and not p.loi_files and any("couldn't be built" in w for w in p.warnings)
    assert "is attached" not in p.email_body                          # the text doesn't promise a missing PDF
    r = send(app_client, lead)
    assert r.status_code == 200 and r.json()["stage"] == "OFFER_SENT_TO_AGENT"
    assert not list(Recorder.sent[-1].iter_attachments())


def test_calendar_hook_errors_never_break_a_save(db, monkeypatch):
    from engine import gcal_sync
    gcal_sync.install_hooks()
    monkeypatch.setattr(gcal_sync, "_collect", lambda s: (_ for _ in ()).throw(RuntimeError("calendar down")))
    lead = listed(db)
    lead.notes = "saved even though the calendar hook failed"
    db.commit()
    db.expire_all()
    assert "saved" in db.get(type(lead), lead.id).notes


def test_refresh_can_be_stopped_between_steps():
    from swarm import runner
    gate, ran = threading.Event(), []

    def slow(name, **_):
        ran.append(name)
        gate.wait(5)
    runner.trigger("quick", runner=slow)
    time.sleep(0.1)
    st = runner.stop()
    assert st["running"] and st.get("stopping")
    gate.set()
    for _ in range(100):
        if not runner.status()["running"]:
            break
        time.sleep(0.05)
    st = runner.status()
    assert not st["running"] and len(ran) == 1 and "stopped by you" in st["error"]
    assert all(s["status"] == "skipped" for s in st["steps"][1:])


def test_connection_test_reports_both_sides(app_client, monkeypatch):
    gm.set_imap_factory(lambda: FakeIMAP({}))
    try:
        r = app_client.post("/api/setup/test-gmail").json()
        assert r["ok"] and r["smtp"]["ok"] and r["imap"]["ok"] and r["account"] == "jordan@example.test"
        assert Recorder.sent == []                                   # nothing sent by a plain test
        Recorder.fail = AUTH
        r = app_client.post("/api/setup/test-gmail").json()
        assert not r["ok"] and "refused the sign-in" in r["smtp"]["detail"] and r["imap"]["ok"]
        Recorder.fail = None
        gm.set_imap_factory(lambda: FakeIMAP({}, password_ok=False))
        r = app_client.post("/api/setup/test-gmail?send_to_self=true").json()
        assert r["smtp"]["ok"] and "test note" in r["smtp"]["detail"] and Recorder.sent[-1]["To"] == "jordan@example.test"
        assert not r["imap"]["ok"] and "app password" in r["imap"]["detail"]
    finally:
        gm.set_imap_factory(None)


def test_imap_certificate_failure_is_explained(monkeypatch):
    def cert_fail():
        raise CERT
    gm.set_imap_factory(cert_fail)
    try:
        with pytest.raises(gm.GmailError, match="certificate"):
            with gm.Mailbox("jordan@example.test", "abcdefghijklmnop"):
                pass
    finally:
        gm.set_imap_factory(None)


def test_batch_stops_at_the_first_sign_in_failure(db, app_client):
    from datetime import datetime
    from engine.crm import TZ
    for _ in range(3):
        listed(db)
    db.commit()
    Recorder.fail = AUTH
    waits: list[float] = []
    gm.campaign.__init__()
    gm.start_campaign(3, now=datetime(2026, 11, 4, 10, 0, tzinfo=TZ), sleep=waits.append, background=False)
    assert gm.campaign.sent == 0 and len(gm.campaign.log) == 1 and waits == []     # one try, no 15 bad logins
    assert gm.campaign.message.startswith("Stopped: Gmail refused the sign-in")
