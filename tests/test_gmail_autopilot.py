"""Phase 8 Gmail autopilot: read-only IMAP reader, reply matching, thread headers, automatic Touch 2, the
Action Needed approvals, opt-outs and bounces, the paced outbound batch, the simple-home API and the deck rules.

Gmail itself is never contacted: a fake IMAP connection serves RFC 5322 messages and a fake SMTP server records
what would have been sent."""
from __future__ import annotations

import imaplib
import time
from datetime import datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from db.models import Contact, InboundReply, Interaction, Lead
from engine import autopilot as ap
from engine import gmail_autopilot as gm
from engine import operator_setup
from engine.crm import TZ
from tests.factories import add_contact
from tests.test_agent_outreach import FakeSMTP
from tests.test_command_center import GOOD, setup_paths  # noqa: F401 - pytest fixture
from tests.test_reply_negotiator import listed

T0 = datetime(2026, 11, 4, 9, 30, tzinfo=TZ)          # a Wednesday morning, inside send hours
ME = "jordan@example.test"


class FakeIMAP:
    """Enough of imaplib.IMAP4_SSL for Mailbox: login, read-only select, UID SEARCH / FETCH (PEEK only)."""

    def __init__(self, messages: dict[int, bytes], uidvalidity: int = 7, password_ok: bool = True):
        self.messages, self.uidvalidity, self.password_ok = dict(messages), uidvalidity, password_ok
        self.readonly = None
        self.fetches: list[tuple[str, str]] = []

    def login(self, user, password):
        if not self.password_ok:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [str(self.uidvalidity).encode()]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            if args[1] == "UID":
                lo = int(args[2].split(":")[0])
                uids = [u for u in self.messages if u >= lo] or ([max(self.messages)] if self.messages else [])
            else:
                uids = list(self.messages)
            return "OK", [" ".join(str(u) for u in sorted(uids)).encode()]
        if cmd == "FETCH":
            assert "PEEK" in args[1], "the operator's mail must never be marked as read"
            self.fetches.append((args[0], args[1]))
            data = []
            for u in (int(x) for x in args[0].split(",")):
                raw = self.messages[u]
                if "HEADER.FIELDS" in args[1]:
                    raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                data += [(f"{u} (UID {u} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
            return "OK", data
        raise AssertionError(cmd)

    def close(self):
        return "OK", []

    def logout(self):
        return "BYE", []


def mail(uid: int, body: str, *, frm: str = "Pat Agent <pat@broker.test>", subject: str = "Re: 1 Test St",
         in_reply_to: str | None = None, refs: list[str] | None = None, when: datetime = T0 + timedelta(minutes=40),
         headers: dict[str, str] | None = None) -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, ME, subject
    m["Date"] = format_datetime(when)
    m["Message-ID"] = f"<agent-{uid}@broker.test>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = " ".join(refs or [in_reply_to])
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    return m.as_bytes(policy=policy.SMTP)


@pytest.fixture()
def live(db, setup_paths, tmp_path, monkeypatch):  # noqa: F811
    """My Setup done (Gmail + app password), offers written to a temp folder, a quiet campaign, no real IMAP."""
    import engine.contracts as contracts
    monkeypatch.setattr(contracts, "DEALS_DIR", tmp_path / "deals")
    operator_setup.save({**GOOD, "app_password": "abcdefghijklmnop"})
    monkeypatch.setitem(settings(), "gmail_autopilot", {**settings().get("gmail_autopilot", {}),
                                                        "touch2_delay_minutes": [4, 6]})
    FakeSMTP.sent = []
    gm.campaign.__init__()
    yield db
    gm.set_imap_factory(None)


def opener(db, lead, when=T0):
    """Touch 1 over (fake) SMTP, so our Message-ID is on record."""
    db.commit()
    ap.send(db, lead.id, "smtp", now=when, smtp_factory=FakeSMTP)
    db.commit()
    return FakeSMTP.sent[-1]


# -- parsing ----------------------------------------------------------------------------------------------------
def test_strip_quoted_keeps_only_the_agents_words():
    gmail = "Yes, still available.\nWhat's your number?\n\nOn Mon, Nov 2, 2026 at 9:14 AM Jordan <j@x.com> wrote:\n> Hi Pat"
    assert gm.strip_quoted(gmail) == "Yes, still available.\nWhat's your number?"
    wrapped = "Send it over.\n\nOn Mon, Nov 2, 2026 at 9:14 AM Jordan Operator <jordan@example.test>\nwrote:\n> hi"
    assert gm.strip_quoted(wrapped) == "Send it over."
    outlook = "Seller wants 310.\n\nFrom: Jordan <j@x.com>\nSent: Monday, November 2, 2026 9:14 AM\nTo: Pat\nSubject: hi"
    assert gm.strip_quoted(outlook) == "Seller wants 310."
    phone = "Still active!\n\nSent from my iPhone\n\n> On Nov 2 ..."
    assert gm.strip_quoted(phone) == "Still active!"
    html = EmailMessage()
    html["From"], html["Subject"], html["Message-ID"] = "a@b.test", "Re: x", "<h1@b.test>"
    html.set_content('<div>Yes <b>available</b></div><div class="gmail_quote">On Mon wrote: old stuff</div>',
                     subtype="html")
    parsed = gm.parse_message(html.as_bytes(policy=policy.SMTP), 9)
    assert parsed.text == "Yes available" and parsed.message_id == "<h1@b.test>"


def test_auto_replies_and_bounces_are_recognised():
    ooo = gm.parse_headers(mail(1, "I'm out until Monday", headers={"Auto-Submitted": "auto-replied"}), 1)
    assert ooo.auto_reply
    assert gm.parse_headers(mail(2, "x", subject="Out of Office: back Monday"), 2).auto_reply
    assert not gm.parse_headers(mail(3, "Yes available"), 3).auto_reply
    bounce = EmailMessage()
    bounce["From"], bounce["Subject"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification"
    bounce["Message-ID"] = "<dsn-1@google.test>"
    bounce.set_content("Address not found.\n\nFinal-Recipient: rfc822; pat@broker.test\n\n"
                       "Message-ID: <ours-1@example.test>\nSubject: 1 Test St")
    b = gm.parse_message(bounce.as_bytes(policy=policy.SMTP), 4)
    assert b.bounce and b.bounced_ids == ["<ours-1@example.test>"] and b.bounced_addresses == ["pat@broker.test"]
    assert gm.reply_subject("Re: 1 Test St") == "Re: 1 Test St" and gm.reply_subject("1 Test St") == "Re: 1 Test St"


# -- the main path: opener -> "still available" -> Touch 2 in the thread, by itself ---------------------------------
def test_still_available_reply_sends_touch2_in_thread_by_itself(live):
    db = live
    lead = listed(db)
    first = opener(db, lead)
    ours = first["Message-ID"]
    inter = db.query(Interaction).filter_by(lead_id=lead.id, outcome="AGENT_OPENER_SENT").one()
    assert inter.email_message_id == ours and ours.endswith("@example.test>")
    reply = mail(41, "Yes, it's still available. What's your offer?\n\nOn Wed, Nov 4, 2026 Jordan wrote:\n> Hi Pat,",
                 subject=f"Re: {first['Subject']}", in_reply_to=ours)
    news = mail(40, "50% off staging this week", frm="Promo <deals@stagers.test>", subject="Staging sale")
    fake = FakeIMAP({40: news, 41: reply})
    gm.set_imap_factory(lambda: fake)
    r = gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    assert r["ok"] and r["checked"] == 2 and r["matched"] == 1 and r["auto_queued"] == 1 and r["sent"] == 0
    assert fake.readonly is True
    assert [f[0] for f in fake.fetches if "HEADER.FIELDS" not in f[1]] == ["41"]     # the promo stays unread
    db.expire_all()
    row = db.query(InboundReply).one()
    assert row.status == "auto_queued" and row.match_method == "thread" and row.lead_id == lead.id
    assert row.body_text == "Yes, it's still available. What's your offer?" and row.send_after > row.received_at
    replied = db.query(Interaction).filter_by(lead_id=lead.id, outcome="AGENT_REPLIED").one()
    assert replied.email_message_id == "<agent-41@broker.test>"
    db.commit()
    assert lead.id not in {c["lead_id"] for c in ap.deck(db)["cards"]}       # the queue owns it, not the deck
    db.commit()
    # A few minutes later (a person's pace), inside send hours: the priced note + LOI go out in their thread.
    assert gm.send_due(now=T0 + timedelta(minutes=58), smtp_factory=FakeSMTP) == 1
    sent = FakeSMTP.sent[-1]
    assert sent["In-Reply-To"] == "<agent-41@broker.test>"
    assert ours in sent["References"] and "<agent-41@broker.test>" in sent["References"]
    assert sent["Subject"].startswith("Re: ") and sent["To"] == "pat@broker.test"
    text = sent.get_body(preferencelist=("plain",)).get_content()
    assert "--- Offer Summary ---" in text and "PO Box 10, Edison" in text and '"no more"' in text
    assert any(p.get_filename().endswith(".pdf") for p in sent.iter_attachments())
    db.expire_all()
    row = db.get(InboundReply, row.id)
    assert row.status == "auto_sent" and row.sent_message_id == sent["Message-ID"] and row.price
    offer = db.query(Interaction).filter_by(lead_id=lead.id, outcome="OFFER_SENT_TO_AGENT").one()
    assert offer.email_message_id == sent["Message-ID"] and offer.channel == "email_smtp"
    lead = db.get(Lead, lead.id)
    assert lead.status_stage == "OFFER_SENT_TO_AGENT" and lead.next_followup_at > datetime(2026, 11, 6)
    db.commit()
    # The next check reads only newer mail: nothing is processed twice.
    again = gm.poll_gmail_replies(now=T0 + timedelta(hours=1), smtp_factory=FakeSMTP)
    assert again["checked"] == 0 and again["matched"] == 0 and db.query(InboundReply).count() == 1


def test_touch2_waits_outside_send_hours_and_when_auto_send_is_off(live, monkeypatch):
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    gm.set_imap_factory(lambda: FakeIMAP({5: mail(5, "Still available!", in_reply_to=ours)}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    n = len(FakeSMTP.sent)
    assert gm.send_due(now=datetime(2026, 11, 4, 22, 30, tzinfo=TZ), smtp_factory=FakeSMTP) == 0   # 10:30 PM
    assert len(FakeSMTP.sent) == n and db.query(InboundReply).one().status == "auto_queued"
    other = listed(db)
    ours2 = opener(db, other)["Message-ID"]
    monkeypatch.setitem(settings()["gmail_autopilot"], "auto_send_touch2", False)
    gm.set_imap_factory(lambda: FakeIMAP({6: mail(6, "Yes, still available", in_reply_to=ours2)}, uidvalidity=8))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)
    db.expire_all()
    row = db.query(InboundReply).filter_by(lead_id=other.id).one()
    assert row.status == "needs_approval" and "off" in row.note


@pytest.mark.parametrize("text, reason", [
    ("Yes it's available. Is your buyer using a mortgage?", "question"),
    ("Available, but there's an oil tank in the yard.", "oil tank"),
    ("It's still available. Can you send proof of funds?", "pof"),
])
def test_anything_beyond_plain_available_waits_for_the_operator(live, text, reason):
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    gm.set_imap_factory(lambda: FakeIMAP({7: mail(7, text, in_reply_to=ours)}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    db.expire_all()
    row = db.query(InboundReply).one()
    assert row.status == "needs_approval" and row.note
    assert gm.send_due(now=T0 + timedelta(hours=2), smtp_factory=FakeSMTP) == 0


# -- counters: Action Needed, one click ---------------------------------------------------------------------------
def test_counter_lands_in_action_needed_and_one_click_sends_reply_and_loi(live):
    db = live
    lead = listed(db)
    opener(db, lead)
    # The agent writes a fresh e-mail (no thread headers) from the address on the listing.
    gm.set_imap_factory(lambda: FakeIMAP({8: mail(8, "Seller would take $285,000 if you can close fast.",
                                                  subject="1 Test St")}))
    r = gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    assert r["needs_approval"] == 1
    db.expire_all()
    row = db.query(InboundReply).one()
    assert row.match_method == "agent_email" and row.verdict == "GREEN" and row.counter_amount == 285000
    st = gm.status(db, now=T0 + timedelta(minutes=46))
    card = st["action_needed"][0]
    assert card["light"]["text"].startswith("🟢 DEAL WORKS AT $285K") and "$17,000" in card["light"]["text"]
    assert card["loi"] and card["phone"]["display"] and "285,000" in card["reply_text"]
    assert st["today"]["replies_found"] == 1 and st["today"]["emails_sent"] == 1
    out = gm.approve(db, row.id, now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)
    db.commit()
    sent = FakeSMTP.sent[-1]
    assert out["sent"] and out["price"] == 285000 and out["loi_attached"]
    assert sent["In-Reply-To"] == "<agent-8@broker.test>" and sent["Subject"] == "Re: 1 Test St"
    assert any(p.get_filename().endswith(".pdf") for p in sent.iter_attachments())
    db.expire_all()
    assert db.get(InboundReply, row.id).status == "approved" and db.get(Lead, lead.id).offer_amount == 285000
    with pytest.raises(gm.ApprovalError, match="already approved"):
        gm.approve(db, row.id, smtp_factory=FakeSMTP)
    with pytest.raises(gm.ApprovalError, match="mailing address"):
        row2 = InboundReply(message_id="<x@y>", lead_id=lead.id, status="needs_approval", verdict="INFO",
                            analysis_json={"reply_text": "Hi Pat, thanks."})
        db.add(row2)
        db.flush()
        gm.approve(db, row2.id, smtp_factory=FakeSMTP)


def test_opt_out_suppresses_and_a_bounce_takes_the_address_off(live):
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    gm.set_imap_factory(lambda: FakeIMAP({9: mail(9, "Please remove me from your list. No more.", in_reply_to=ours)}))
    r = gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    assert r["opted_out"] == 1
    db.expire_all()
    agent = db.query(Contact).filter_by(lead_id=lead.id, contact_type="LISTING_AGENT").one()
    assert agent.dnc_status == "dnc" and db.query(InboundReply).one().status == "opted_out"
    other = listed(db)
    ours2 = opener(db, other)["Message-ID"]
    dsn = EmailMessage()
    dsn["From"], dsn["Subject"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification"
    dsn["Message-ID"], dsn["Date"] = "<dsn-9@google.test>", format_datetime(T0 + timedelta(minutes=46))
    dsn.set_content(f"Address not found.\n\nMessage-ID: {ours2}\nTo: pat@broker.test")
    gm.set_imap_factory(lambda: FakeIMAP({1: dsn.as_bytes(policy=policy.SMTP)}, uidvalidity=99))
    r = gm.poll_gmail_replies(now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)
    assert r["bounce"] == 1
    db.expire_all()
    c = db.query(Contact).filter_by(lead_id=other.id, contact_type="LISTING_AGENT").one()
    assert c.email is None and "bounced" in c.notes and c.phone        # the phone stays: call them


def test_unknown_senders_are_ignored_unless_they_name_our_street(live):
    db = live
    lead = listed(db)
    opener(db, lead)
    street = lead.property.street_address.title()
    msgs = {11: mail(11, "Lunch Friday?", frm="Friend <friend@mail.test>", subject="lunch"),
            12: mail(12, "Hi, I'm the co-listing agent. Still available.", frm="Co Agent <co@broker2.test>",
                     subject=f"Re: {street}")}
    fake = FakeIMAP(msgs)
    gm.set_imap_factory(lambda: fake)
    r = gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    assert r["matched"] == 1 and [f[0] for f in fake.fetches if "HEADER.FIELDS" not in f[1]] == ["12"]
    db.expire_all()
    row = db.query(InboundReply).one()
    assert row.match_method == "address" and row.status == "needs_approval" and "street" in row.note


def test_wrong_app_password_is_a_plain_error(live):
    gm.set_imap_factory(lambda: FakeIMAP({}, password_ok=False))
    with pytest.raises(gm.GmailError, match="app password"):
        gm.poll_gmail_replies(now=T0)
    gm.poller.run_once()
    assert "app password" in gm.poller.last["error"]
    gm.set_imap_factory(None)                     # the real client, pointed at a closed local port by conftest
    with pytest.raises(gm.GmailError, match="can't reach Gmail"):
        gm.poll_gmail_replies(now=T0)


# -- the outbound batch --------------------------------------------------------------------------------------------
def test_daily_batch_is_paced_logged_and_respects_hours(live):
    db = live
    leads = [listed(db) for _ in range(3)]
    phone_only = listed(db)
    db.query(Contact).filter_by(lead_id=phone_only.id, contact_type="LISTING_AGENT").one().email = None
    db.commit()
    with pytest.raises(gm.CampaignError, match="between"):
        gm.start_campaign(2, now=datetime(2026, 11, 4, 21, 0, tzinfo=TZ), smtp_factory=FakeSMTP, background=False)
    waits: list[float] = []
    out = gm.start_campaign(2, now=T0, smtp_factory=FakeSMTP, sleep=waits.append, background=False)
    assert out["total"] == 2 and gm.campaign.sent == 2 and not gm.campaign.running
    lo, hi = settings()["gmail_autopilot"]["pace_seconds"]
    assert len(waits) == 1 and lo <= waits[0] <= hi                     # one gap between two e-mails
    assert phone_only.id not in {x["lead_id"] for x in gm.campaign.log}
    db.expire_all()
    sent = db.query(Interaction).filter(Interaction.channel == "email_smtp").all()
    assert len(sent) == 2 and all(i.email_message_id for i in sent)
    assert {i.lead_id for i in sent} <= {lead.id for lead in leads}
    assert "Done: 2 e-mails sent" in gm.campaign.message


def test_deck_shows_only_cards_with_a_way_to_reach_someone(live):
    db = live
    ok = listed(db)
    dead = listed(db)
    agent = db.query(Contact).filter_by(lead_id=dead.id, contact_type="LISTING_AGENT").one()
    agent.email, agent.phone = None, "9735550177"
    from engine import compliance
    compliance.add_internal(db, agent.phone, notes="asked to stop")
    agent.dnc_status = "clear"                       # stale flag: the stop list itself must still win
    db.commit()
    d = ap.deck(db)
    ids = {c["lead_id"] for c in d["cards"]}
    assert ok.id in ids and dead.id not in ids and d["hidden_no_contact"] >= 1


def test_interested_on_a_call_turns_the_card_into_touch2(live):
    db = live
    lead = listed(db)
    card = ap.build_card(db, lead)
    assert card["packet_kind"] == "opener"
    r = ap.outcome(db, lead.id, "interested")
    assert r["card"]["kind"] == "touch2" and r["card"]["packet_kind"] == "offer" and r["card"]["touch"] == 2
    assert "--- Offer Summary ---" in r["card"]["body"]


def test_calendar_alert_for_a_reply_waiting_on_the_operator(live):
    from engine import gcal_sync
    db = live
    lead = listed(db)
    row = InboundReply(message_id="<cal@b.test>", lead_id=lead.id, status="needs_approval", verdict="YELLOW",
                       headline="COUNTER AT $290,000", counter_amount=310000, received_at=datetime(2026, 11, 4, 11, 0),
                       created_at=datetime(2026, 11, 4, 16, 0),         # read by the desk at 11:00 Eastern (UTC)
                       body_text="The seller Quinn Sample wants 310", note="they named a number: your call",
                       analysis_json={"numbers": {"mao_walkaway": 292000}})
    db.add(row)
    db.flush()
    ev = gcal_sync.reply_events(db, lead, datetime(2026, 11, 4, 11, 1))
    assert len(ev) == 1 and ev[0].key == f"reply:{lead.id}:r{row.id}" and ev[0].kind == "reply"
    body = ev[0].body
    assert body["summary"].startswith("📞 COUNTER-OFFER RECEIVED: call Pat Agent about ")
    assert "$310,000" in body["description"] and "--- Call script (word for word) ---" in body["description"]
    assert "Best and final:" in body["description"] or "Don't name a price" in body["description"]
    assert "Quinn" not in body["description"] and ev[0].starts_at == datetime(2026, 11, 4, 11, 10)
    row.status = "approved"
    db.flush()
    assert gcal_sync.reply_events(db, lead, datetime(2026, 11, 4, 11, 1)) == []


def test_a_reply_the_mac_reads_hours_late_still_gets_its_alert(live):
    """2026-11-03: replies sent at 9 AM were read at 11:59 (the daemon wasn't running): the alert, timed off the agent's
    send time, landed in the past and was never created. It is timed off when the Mac read the reply."""
    from engine import gcal_sync
    db = live
    lead = listed(db)
    row = InboundReply(message_id="<late@b.test>", lead_id=lead.id, status="needs_approval", verdict="INFO",
                       headline="ANSWER THEM", received_at=datetime(2026, 11, 4, 9, 0),
                       created_at=datetime(2026, 11, 4, 16, 59))            # 11:59 AM Eastern, stored in UTC
    db.add(row)
    db.flush()
    ev = gcal_sync.reply_events(db, lead, datetime(2026, 11, 4, 12, 0))
    assert len(ev) == 1 and ev[0].starts_at == datetime(2026, 11, 4, 12, 9)


# -- the simple-home API -------------------------------------------------------------------------------------------
class FakeSSL(FakeSMTP):
    def __init__(self, *a, **kw):
        pass


def test_simple_home_endpoints(live, monkeypatch):
    import web.app as webapp
    db = live
    client = TestClient(webapp.app)
    lead = listed(db)
    db.commit()
    st = client.get("/api/simple/status").json()
    assert st["configured"] and st["account"] == ME and st["action_needed"] == [] and st["goal"] == 30
    assert st["today"] == {"emails_sent": 0, "replies_found": 0, "offers_delivered": 0}
    monkeypatch.setitem(settings()["gmail_autopilot"], "send_hours", [0, 24])
    monkeypatch.setitem(settings()["gmail_autopilot"], "pace_seconds", [0, 0])
    monkeypatch.setattr(gm.ao.smtplib, "SMTP_SSL", FakeSSL)
    r = client.post("/api/simple/run", json={"limit": 1})
    assert r.status_code == 200 and r.json()["total"] == 1 and r.json()["running"]
    for _ in range(100):
        if not gm.campaign.running:
            break
        time.sleep(0.05)
    assert gm.campaign.sent == 1 and FakeSMTP.sent[-1]["To"] == "pat@broker.test"
    again = client.post("/api/simple/run", json={})
    assert again.status_code == 409                              # nothing left to send (it was contacted)
    assert client.post("/api/replies/999/approve", json={}).status_code == 409
    assert client.post("/api/replies/999/dismiss").status_code == 409
    row = InboundReply(message_id="<d@b.test>", lead_id=lead.id, status="needs_approval", verdict="INFO")
    db.add(row)
    db.commit()
    assert client.post(f"/api/replies/{row.id}/dismiss").json()["status"] == "dismissed"
    assert client.post("/api/simple/stop").status_code == 200
    page = client.get("/").text
    assert "Advanced Tools" in page and "🏠 Home" in page
    monkeypatch.delenv("SMTP_APP_PASSWORD")
    assert client.post("/api/simple/check-inbox").status_code == 409


# -- real-world replies (2026-11-02; names, phones and addresses replaced) -----------------------------------------
BROKER_SIGNATURE_REPLY = """Yes. Attorney 

Sincerely,

Pat Sample
Sample Real Estate
Owner/Broker
100 Sample Ave
Sampletown NJ 07000
Cell: (973)555-0178
pat@samplerealestate.test

COMMERCIAL AND RESIDENTIAL SPECIALIST 

Top 1% in New Jersey
Recipient of NJAR Circle of Excellence PLATINUM Award Over 20 Million Dollar Sales Volume YEARLY 2004-2008,2013-2026


Sorry for any errors, this email was sent from my IPhone 
From: jordan@example.test <jordan@example.test>
Sent: Monday, 02 November 2026 13:38:09
To: pat samplerealestate.test <pat@samplerealestate.test>
Subject: question on 1 Test St

Hi Pat, Quick question on your listing. Is it still available, or are you already in attorney review?"""


def test_broker_signature_is_not_read_as_a_20_million_counter(live):
    from engine import reply_negotiator as rn
    kept = gm.strip_quoted(BROKER_SIGNATURE_REPLY)
    assert kept == "Yes. Attorney"
    p = rn.parse_reply(kept)
    assert p.counter_amount is None and "ESTATE" not in p.intents and p.intents == {"BACKUP"}
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    gm.set_imap_factory(lambda: FakeIMAP({3: mail(3, BROKER_SIGNATURE_REPLY, frm="Pat Sample <pat@broker.test>",
                                                  in_reply_to=ours)}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    db.expire_all()
    row = db.query(InboundReply).one()
    # "Yes. Attorney" answers "still available, or already in attorney review?": the offer goes as a backup.
    assert row.status == "auto_queued" and row.counter_amount is None and row.body_text == "Yes. Attorney"
    assert row.headline == "SEND A BACKUP OFFER NOW (Touch 2)"
    draft = row.analysis_json["reply_text"]
    assert "backup offer in case the current contract doesn't go through" in draft and "congratulations" not in draft.lower()
    assert '"no more"' in draft and "PO Box 10" in draft


@pytest.mark.parametrize("text, counter", [
    ("Seller would take 310k.\n\nThanks,\nPat\nSample Realty\nOver 20 Million Dollar Sales Volume", 310000),
    ("We did 20 million in sales last year, the seller wants 310k", 310000),
])
def test_signature_and_brag_numbers_never_become_the_counter(live, text, counter):
    from engine import reply_negotiator as rn
    lead = listed(live)
    an = rn.analyze(live, lead, gm.strip_quoted(text))
    assert an.parsed.counter_amount == counter and an.numbers.get("counter") == counter


def test_a_number_that_cant_be_this_houses_price_is_ignored(live):
    from engine import reply_negotiator as rn
    lead = listed(live)
    an = rn.analyze(live, lead, "Seller would take 20 million.")
    assert an.parsed.counter_amount is None and "COUNTER" not in an.parsed.intents
    assert any("isn't a price for this house" in w for w in an.warnings)


def test_touch2_after_their_reply_thanks_them_and_signs_once(live, monkeypatch):
    from engine import agent_outreach
    db = live
    monkeypatch.setitem(settings()["operator"], "company_name", "Jordan Operator")    # a sole investor
    lead = listed(db)
    db.add(Interaction(lead_id=lead.id, channel="email_in", outcome="AGENT_REPLIED", notes="still available",
                       occurred_at=datetime.now()))
    db.flush()
    p = agent_outreach.build_packet(lead, generate_loi=False, session=db)
    head = p.email_body.split("--- Offer Summary ---")[0]
    assert head.startswith("Hi Pat,\n\nThanks for getting back to me on ") and "Quick question" not in head
    assert "Jordan Operator | Jordan Operator" not in p.email_body and "Jordan Operator\n" in head
    with_loi = agent_outreach.build_packet(lead, generate_loi=True, session=db)
    assert "The 1-page written offer is attached" in with_loi.email_body and with_loi.loi_files.get("pdf")


# -- one sure way to send the offer (2026-11-02: a logged "still available" with no offer out yet) -----------------
def _queued_reply(db, lead):
    ours = opener(db, lead)["Message-ID"]
    gm.set_imap_factory(lambda: FakeIMAP({21: mail(21, "Hello, it's still available.", in_reply_to=ours,
                                                   subject="Re: question on 1 Test St")}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    db.expire_all()
    row = db.query(InboundReply).one()
    assert row.status == "auto_queued"
    return row, ours


def test_sending_from_the_lead_page_threads_the_offer_and_closes_the_queue(live):
    db = live
    lead = listed(db)
    row, ours = _queued_reply(db, lead)
    import web.app as webapp
    client = TestClient(webapp.app)
    card = client.get(f"/api/autopilot/{lead.id}/card").json()
    assert card["kind"] == "touch2" and card["threaded"] and card["subject"] == "Re: question on 1 Test St"
    assert card["body"].startswith("Hi Pat,\n\nThanks for getting back to me on ")
    n = len(FakeSMTP.sent)
    db.commit()
    r = ap.send(db, lead.id, "smtp", subject=card["subject"], now=T0 + timedelta(minutes=47), smtp_factory=FakeSMTP)
    db.commit()
    sent = FakeSMTP.sent[-1]
    assert len(FakeSMTP.sent) == n + 1 and r["threaded"] and r["touch"] == 2
    assert sent["In-Reply-To"] == "<agent-21@broker.test>" and ours in sent["References"]
    assert sent["Subject"] == "Re: question on 1 Test St"
    assert any(p.get_filename().endswith(".pdf") for p in sent.iter_attachments())
    db.expire_all()
    assert db.get(InboundReply, row.id).status == "approved"          # the queue won't send a second offer
    assert gm.send_due(now=T0 + timedelta(hours=1), smtp_factory=FakeSMTP) == 0 and len(FakeSMTP.sent) == n + 1


def test_send_now_skips_the_human_paced_wait(live):
    db = live
    lead = listed(db)
    row, _ = _queued_reply(db, lead)
    out = gm.send_now(row.id, now=T0 + timedelta(minutes=46), smtp_factory=FakeSMTP)
    assert out["sent"] and out["status"] == "auto_sent"
    assert FakeSMTP.sent[-1]["In-Reply-To"] == "<agent-21@broker.test>"
    with pytest.raises(gm.ApprovalError, match="auto sent"):
        gm.send_now(row.id, smtp_factory=FakeSMTP)


def test_nothing_else_goes_out_while_the_autopilot_is_sending(live):
    db = live
    lead = listed(db)
    row, _ = _queued_reply(db, lead)
    db.get(InboundReply, row.id).status = "sending"
    db.commit()
    with pytest.raises(ap.AutopilotError, match="going out from the inbox autopilot"):
        ap.send(db, lead.id, "smtp", smtp_factory=FakeSMTP)


def test_a_reply_misread_by_an_older_version_is_read_again(live):
    db = live
    lead = listed(db)
    lead.last_counter_amount = 20_000_000
    row = InboundReply(message_id="<old@b.test>", lead_id=lead.id, status="needs_approval", verdict="RED", match_method="thread",
                       headline="HOLD FIRM AT $292,000 / SET 14-DAY DRIP", counter_amount=20_000_000,
                       intents_json=["COUNTER", "ESTATE"], body_text=BROKER_SIGNATURE_REPLY.split("\nFrom:")[0],
                       received_at=datetime(2026, 11, 4, 9, 41), analysis_json={"reply_text": "$20,000,000 doesn't work"})
    db.add(row)
    db.flush()
    assert gm.refresh_open_replies(db) == 1
    assert row.counter_amount is None and row.body_text == "Yes. Attorney" and lead.last_counter_amount is None
    assert row.headline == "SEND A BACKUP OFFER NOW (Touch 2)" and "20,000,000" not in row.analysis_json["reply_text"]
    assert row.status == "auto_queued" and "re-read" in row.note      # released: the backup offer goes out
    assert gm.refresh_open_replies(db) == 0                           # idempotent
    row.status = "needs_approval"
    db.flush()
    assert gm.refresh_open_replies(db, promote=False) == 0 and row.status == "needs_approval"


def test_lead_page_points_a_counter_to_action_needed(live):
    import web.app as webapp
    db = live
    lead = listed(db)
    opener(db, lead)
    gm.set_imap_factory(lambda: FakeIMAP({31: mail(31, "Seller would take $285,000.", subject="1 Test St")}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    card = TestClient(webapp.app).get(f"/api/autopilot/{lead.id}/card").json()
    assert card["waiting"]["numbers"]["counter"] == 285000 and card["waiting"]["status"] == "needs_approval"


def test_a_thank_you_after_a_no_price_note_needs_nothing(live):
    """2026-11-04: "Sounds good! Thank you!" after our opener / backup note became a 📞 call and "waiting on you"."""
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]                       # the no-price opener was our last e-mail
    gm.set_imap_factory(lambda: FakeIMAP({7: mail(7, "Sounds good! Thank you!\n\nGet Outlook for Android",
                                                  in_reply_to=ours)}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    db.expire_all()
    row = db.query(InboundReply).one()
    assert row.status == "dismissed" and row.headline.startswith("THANKS") and row.lead_id == lead.id
    assert not db.query(Interaction).filter_by(lead_id=lead.id, outcome="AGENT_REPLIED").count()


def _answered_far_apart(db, lead, counter=300000):
    row = InboundReply(message_id="<far@b.test>", lead_id=lead.id, status="auto_sent", verdict="RED",
                       match_method="thread", counter_amount=counter,
                       headline=f"TOO FAR APART AT ${counter:,} (our final $292,000): NO NUMBER, CHECK BACK IN 30 DAYS",
                       received_at=T0 + timedelta(minutes=20), resolved_at=T0 + timedelta(minutes=25),
                       analysis_json={"reply_text": "Hi Pat, ... mind if I check back in about 30 days?"})
    db.add(row)
    db.commit()
    return row


def test_a_yes_to_our_check_in_needs_nothing(live):
    """2026-11-04: "Sure, you can absolutely" after our "mind if I check back in about 30 days?" was read as "still
    available", and a second "what would the seller need to net?" went out right after the agent gave a number."""
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    _answered_far_apart(db, lead)
    sent_before = len(FakeSMTP.sent)
    gm.set_imap_factory(lambda: FakeIMAP({8: mail(8, "Sure, you can absolutely", in_reply_to=ours)}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    db.expire_all()
    row = db.query(InboundReply).filter_by(message_id="<agent-8@broker.test>").one()
    assert row.status == "dismissed" and row.headline.startswith("OK TO CHECK BACK")
    assert len(FakeSMTP.sent) == sent_before


def test_no_automatic_touch2_once_they_gave_a_number(live):
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    db.add(InboundReply(message_id="<num@b.test>", lead_id=lead.id, status="dismissed", match_method="thread",
                        counter_amount=300000, received_at=T0 + timedelta(minutes=20)))
    db.commit()
    gm.set_imap_factory(lambda: FakeIMAP({9: mail(9, "Yes, still available", in_reply_to=ours)}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    db.expire_all()
    row = db.query(InboundReply).filter_by(message_id="<agent-9@broker.test>").one()
    assert row.status == "needs_approval" and "already gave a number" in row.note
