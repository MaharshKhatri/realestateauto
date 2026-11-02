"""The Claude-native autopilot (2026-11-02): today's real agent replies (names, phones, addresses replaced), replies
never held back by the cold-outreach cap, Gmail's Sent folder as the record, `manage.py inbox`, `/reply`, the daemon."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from db.models import InboundReply, Interaction, Lead
from engine import agent_outreach as ao
from engine import gmail_autopilot as gm
from engine import human_copy as hc
from engine import reply_negotiator as rn
from tests.test_agent_outreach import FakeSMTP
from tests.test_command_center import GOOD, setup_paths  # noqa: F401 - pytest fixture
from tests.test_gmail_autopilot import T0, FakeIMAP, live, mail, opener  # noqa: F401 - live is a fixture
from tests.test_reply_negotiator import listed

TODAY = {   # what each agent wrote back to the two-line opener (names, phones and addresses replaced)
    "role": ("Yes it is. Are you a buyer or a realtor looking to show it to someone else?\n\n\nPat Sample\n\n"
             "Principal Agent\n\nSAMPLE REALTY | Direct: 201-555-0154 | License #: 0000000"),
    "backup": "Hello we have a contract but not sure it's going through - Did you want to submit an offer",
    "call": ("Hello Jordan,\n\n   We are still active.\n   Please call me to discuss.\n\nPat Sample\nBroker/Salesperson\n"
             "Sample Select\n100 Sample Ave.\nSampletown, NJ 07000\nCell     732.555.0143"),
    "showing": "Yes, it is still available. Are you interested in seeing it?",
    "plain": "It is available\nSent from my iPhone\n\n> On Nov 2, 2026, at 1:35 PM, jordan@example.test wrote:\n>\n> Hi Pat,",
    "attorney": "Yes. Attorney \n\nSincerely,\n\nPat Sample\nSample Real Estate\nOwner/Broker\nCell: (973)555-0178",
    "available": "Hello Jordan, it's still available.",
    "expired": "Listing expired",
}
FIRST_LINE = {
    "role": "I'm the buyer: an investor buying for myself or a partner investor, not a realtor",
    "backup": "I'd like to put in a clean backup offer in case the current contract doesn't go through",
    "call": "I'll give you a call; so you have the numbers in front of you, here they are in writing",
    "showing": "As for seeing it, a few interior photos or a short video would help first",
    "plain": "Thanks for getting back to me on",
    "attorney": "I'd like to put in a clean backup offer",
    "available": "Thanks for getting back to me on",
}


@pytest.mark.parametrize("kind", list(TODAY))
def test_todays_replies_are_read_right(kind):
    p = rn.parse_reply(gm.strip_quoted(TODAY[kind]))
    expect = {"role": {"AVAILABLE", "ASKS_ROLE"}, "backup": {"BACKUP", "AVAILABLE"}, "call": {"AVAILABLE", "CALL_ME"},
              "showing": {"AVAILABLE", "ASKS_SHOWING"}, "plain": {"AVAILABLE"}, "attorney": {"BACKUP"},
              "available": {"AVAILABLE"}, "expired": {"EXPIRED"}}[kind]
    assert p.intents == expect and p.counter_amount is None, (p.intents, p.notes)


def test_each_reply_gets_its_own_offer_by_itself_in_its_thread(live):
    db = live
    leads, ours = {}, {}
    msgs = {}
    for i, kind in enumerate(TODAY, start=1):
        lead = listed(db)
        leads[kind] = lead
        ours[kind] = opener(db, lead, when=T0 + timedelta(minutes=i))["Message-ID"]
        msgs[100 + i] = mail(100 + i, TODAY[kind], in_reply_to=ours[kind], subject=f"Re: question {i}",
                             frm=f"Pat Sample <pat{i}@broker.test>")
    # 25 cold openers already went out today: the answers to agents who wrote back are never held back by that.
    for _ in range(25 - len(TODAY)):
        db.add(Interaction(lead_id=leads["plain"].id, channel="email_smtp", outcome="AGENT_OPENER_SENT",
                           occurred_at=datetime(2026, 11, 4, 9, 0), debrief_json={"packet": {"kind": "opener"}}))
    db.commit()
    assert ao.sent_last_24h(db, T0 + timedelta(minutes=30), cold_only=True) >= 25
    gm.set_imap_factory(lambda: FakeIMAP(msgs))
    out = gm.run_inbox(now=T0 + timedelta(minutes=40), smtp_factory=FakeSMTP)
    assert out["ok"] and out["poll"]["matched"] == len(TODAY)
    db.expire_all()
    by_lead = {r.lead_id: r for r in db.query(InboundReply)}
    sent = {m["In-Reply-To"]: m for m in FakeSMTP.sent if m["In-Reply-To"]}
    for i, (kind, lead) in enumerate(leads.items(), start=1):
        row = by_lead[lead.id]
        if kind == "expired":
            assert row.status == "needs_approval" and "LISTING EXPIRED" in row.headline
            assert "if the owner still wants to sell" in row.analysis_json["reply_text"].lower()
            continue
        assert row.status == "auto_sent", (kind, row.status, row.note)
        msg = sent[f"<agent-{100 + i}@broker.test>"]
        assert ours[kind] in msg["References"] and msg["Subject"] == f"Re: question {i}"
        assert msg["To"] == f"pat{i}@broker.test"
        body = msg.get_body(preferencelist=("plain",)).get_content()
        head = body.split("--- Offer Summary ---")[0]
        assert FIRST_LINE[kind] in head, (kind, head)
        assert hc.lint(body) == [], (kind, hc.lint(body))
        assert any(p.get_filename().endswith(".pdf") for p in msg.iter_attachments())
        assert row.analysis_json["sent"]["anchor"] and row.analysis_json["sent"]["best"]
    call_lead = db.get(Lead, leads["call"].id)
    assert call_lead.next_followup_at < T0.replace(tzinfo=None) + timedelta(hours=24)    # a call, not 48 hours
    table = {r["lead_id"]: r for r in out["rows"]}
    assert table[leads["call"].id]["you_do"].startswith("call them")
    assert table[leads["backup"].id]["offer_sent"].count("$") == 2


def test_cold_cap_still_holds_cold_email(live):
    db = live
    lead = listed(db)
    for _ in range(25):
        db.add(Interaction(lead_id=lead.id, channel="email_smtp", outcome="AGENT_OPENER_SENT",
                           occurred_at=datetime(2026, 11, 4, 9, 0), debrief_json={"packet": {"kind": "opener"}}))
    db.commit()
    p = ao.build_opener(listed(db))
    with pytest.raises(RuntimeError, match="25 cold e-mails"):
        ao.deliver(p, session=db, now=T0, smtp_factory=FakeSMTP)
    assert ao.deliver(p, session=db, now=T0, smtp_factory=FakeSMTP, engaged=True)       # a reply goes out


class FakeGmail(FakeIMAP):
    """INBOX + the Sent folder (found by its \\Sent special-use flag, as Gmail names it per language)."""

    def __init__(self, inbox, sent, **kw):
        super().__init__(inbox, **kw)
        self.boxes = {"INBOX": dict(inbox), '"[Gmail]/Gesendet"': dict(sent)}

    def list(self, *a):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Gesendet"']

    def select(self, mailbox, readonly=False):
        self.messages = self.boxes[mailbox]
        return super().select(mailbox, readonly)


def test_a_reply_written_in_gmail_is_imported_and_never_doubled(live):
    from email import policy
    from email.message import EmailMessage
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    agent_msg = mail(51, "Yes, still available.", in_reply_to=ours, subject="Re: question")
    by_hand = EmailMessage()
    by_hand["From"], by_hand["To"], by_hand["Subject"] = "jordan@example.test", "pat@broker.test", "Re: question"
    by_hand["Message-ID"], by_hand["In-Reply-To"] = "<typed-on-phone@mail.gmail.com>", "<agent-51@broker.test>"
    by_hand["References"] = f"{ours} <agent-51@broker.test>"
    by_hand["Date"] = "Wed, 04 Nov 2026 10:20:00 -0500"
    by_hand.set_content("Great, thanks Pat. Option 1: $240,000 cash, as-is. I'll send the written offer today.")
    fake = FakeGmail({51: agent_msg}, {})
    gm.set_imap_factory(lambda: fake)
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)   # queued (4-11 minute wait)
    fake.boxes['"[Gmail]/Gesendet"'] = {7: by_hand.as_bytes(policy=policy.SMTP)}
    n = len(FakeSMTP.sent)
    out = gm.run_inbox(now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)
    assert out["sent_import"]["imported"] == 1 and len(FakeSMTP.sent) == n               # no second offer
    db.expire_all()
    inter = db.query(Interaction).filter_by(email_message_id="<typed-on-phone@mail.gmail.com>").one()
    assert inter.outcome == "OFFER_SENT_TO_AGENT" and inter.channel == "email_gmail"
    assert db.get(Lead, lead.id).offer_amount == 240000
    assert db.query(InboundReply).one().status == "approved"
    assert gm.run_inbox(now=T0 + timedelta(minutes=55), smtp_factory=FakeSMTP)["sent_import"]["imported"] == 0


def test_reply_in_plain_words_threads_signs_and_attaches(live):
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    gm.set_imap_factory(lambda: FakeIMAP({61: mail(61, "Seller wants 310k, what can you do?", in_reply_to=ours,
                                                   subject="Re: question")}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=45), smtp_factory=FakeSMTP)
    found = gm.find_leads(db, lead.property.street_address[:8])
    assert found and found[0]["lead_id"] == lead.id and found[0]["waiting_reply"]
    assert gm.find_leads(db, "pat@broker.test")[0]["agent_email"] == "pat@broker.test"
    db.commit()
    out = gm.send_custom_reply(db, lead.id, "Hi Pat,\n\nThanks. I can do $290,000 cash, as-is, quick close.",
                               price=290000, now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)
    db.commit()
    msg = FakeSMTP.sent[-1]
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert out["threaded"] and out["loi_attached"] and msg["In-Reply-To"] == "<agent-61@broker.test>"
    assert "PO Box 10" in body and '"no more"' in body and "Jordan" in body
    db.expire_all()
    assert db.get(Lead, lead.id).offer_amount == 290000
    assert db.query(InboundReply).one().status == "approved"


def test_daemon_and_launchd_plist(live, capsys, monkeypatch):
    import manage
    monkeypatch.setattr(manage.sys, "platform", "linux")
    manage.main(["install-daemon", "--print"])
    out = capsys.readouterr().out
    assert "com.khatri.wholesale.autopilot" in out and "autopilot-daemon" in out and "<key>KeepAlive</key><true/>" in out
    gm.set_imap_factory(lambda: FakeIMAP({}))
    from engine.crm import TZ
    import datetime as dt

    class Noon(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 11, 4, 12, 0, tzinfo=TZ)
    monkeypatch.setattr(dt, "datetime", Noon)
    manage.main(["autopilot-daemon", "--once"])                     # one cycle, no web server


@pytest.mark.parametrize("text, intent", [("No, sorry it sold", "UNDER_CONTRACT"), ("Nope - pending", "UNDER_CONTRACT"),
                                          ("It is not under contract", "AVAILABLE")])
def test_a_leading_no_answers_the_question_it_doesnt_negate(text, intent):
    p = rn.parse_reply(text)
    assert intent in p.intents and (intent == "AVAILABLE" or "AVAILABLE" not in p.intents)


def test_a_listing_matched_to_a_parcel_in_another_town_is_held_and_never_priced(live):
    """2026-11-02: '22 Grand Ave, Iselin' was linked to a 22 Grand Ave parcel in South River, 10 miles away."""
    from db.models import Listing
    from engine import preflight
    db = live
    lead = listed(db)
    ref = next(sg.source_ref for sg in lead.property.signals if sg.signal_type == "STALE_LISTING")
    db.add(Listing(source="homeharvest", source_ref=ref, address="1 Test St", city="Iselin", lat=40.576, lon=-74.307))
    db.flush()
    pf = preflight.check(lead)
    assert pf.status == "HOLD" and "wrong parcel" in pf.issues[0] and "miles apart" in pf.issues[0]
    an = rn.analyze(db, lead, "Hello Jordan, We are still active. Please call me to discuss.")
    assert an.headline.startswith("NUMBERS ON HOLD") and "bedrooms" in an.reply_text
    assert "$" not in an.reply_text.split("--- Details ---")[0]
    assert gm.auto_touch2_block(db, lead, an, "thread", "We are still active. Please call me to discuss.")


def test_the_listing_agent_refuses_a_parcel_miles_from_the_listing():
    from agents.scrapers.stale_listing_distress_agent import MAX_PARCEL_MILES, _miles_apart
    south_river, iselin = {"lat": 40.434, "lon": -74.373}, {"lat": 40.576, "lon": -74.307}
    assert _miles_apart(south_river, iselin) > 10 > MAX_PARCEL_MILES
    assert _miles_apart({"lat": 40.5761, "lon": -74.3075}, iselin) < 0.1
    assert _miles_apart({"lat": None, "lon": None}, iselin) is None


def test_a_reply_logged_by_an_older_version_still_gets_its_tailored_first_line(live):
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    # queued by an older reader that didn't know BACKUP: the stored intents say only AVAILABLE
    replied = Interaction(lead_id=lead.id, channel="email_in", outcome="AGENT_REPLIED", occurred_at=datetime(2026, 11, 4, 9, 41),
                          notes="SEND THE OFFER NOW (Touch 2): " + TODAY["backup"], debrief_json={"intents": ["AVAILABLE"]},
                          email_message_id="<old-backup@b.test>")
    db.add(replied)
    db.flush()
    db.add(InboundReply(message_id="<old-backup@b.test>", lead_id=lead.id, status="auto_queued", verdict="GREEN",
                        headline="SEND THE OFFER NOW (Touch 2)", intents_json=["AVAILABLE"], match_method="thread",
                        body_text=TODAY["backup"], received_at=datetime(2026, 11, 4, 9, 40), subject="Re: question",
                        from_email="pat@broker.test", in_reply_to=ours, send_after=datetime(2026, 11, 4, 9, 45),
                        interaction_id=replied.id))
    db.commit()
    gm.set_imap_factory(lambda: FakeIMAP({}))
    gm.run_inbox(now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)
    body = FakeSMTP.sent[-1].get_body(preferencelist=("plain",)).get_content()
    assert "backup offer in case the current contract doesn't go through" in body
    assert ao.reply_flags_for(db, lead) >= {"BACKUP"}
