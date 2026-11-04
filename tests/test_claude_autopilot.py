"""The Claude-native autopilot (2026-11-02): today's real agent replies (names, phones, addresses replaced), replies
never held back by the cold-outreach cap, Gmail's Sent folder as the record, `manage.py inbox`, `/reply`, the daemon."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from email import policy

import pytest

from core.config import settings
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
    cap = int(settings()["outreach"]["daily_email_limit"])
    for _ in range(cap):
        db.add(Interaction(lead_id=lead.id, channel="email_smtp", outcome="AGENT_OPENER_SENT",
                           occurred_at=datetime(2026, 11, 4, 9, 0), debrief_json={"packet": {"kind": "opener"}}))
    db.commit()
    p = ao.build_opener(listed(db))
    with pytest.raises(RuntimeError, match=f"{cap} cold e-mails"):
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
    # The no-price note answers them and asks for the facts by itself (2026-11-03): it carries no number and no LOI.
    assert an.kind == "fact_check" and not an.style, an.style
    assert gm.auto_touch2_block(db, lead, an, "thread", "We are still active. Please call me to discuss.") is None


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


class HangsUp(FakeSMTP):
    """Gmail closing the connection at sign-in, every time (2026-11-02, the 4th offer of the first inbox run)."""
    logins = 0

    def login(self, user, pw):
        HangsUp.logins += 1
        import smtplib
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


def test_gmail_hanging_up_stops_the_run_and_the_next_check_sends_it(live):
    db = live
    msgs = {}
    for i in (1, 2):
        lead = listed(db)
        ours = opener(db, lead, when=T0 + timedelta(minutes=i))["Message-ID"]
        msgs[70 + i] = mail(70 + i, "It is still available.", in_reply_to=ours, subject=f"Re: question {i}",
                            frm=f"Pat Sample <pat{i}@broker.test>")
    gm.set_imap_factory(lambda: FakeIMAP(msgs))
    n = len(FakeSMTP.sent)
    HangsUp.logins = 0
    out = gm.run_inbox(now=T0 + timedelta(minutes=40), smtp_factory=HangsUp)
    assert out["sent_now"] == 0 and "closed the connection" in out["send_error"]
    assert HangsUp.logins == 3                       # one offer tried 3 times, then the run stops (no 3 x 2 sign-ins)
    assert len(FakeSMTP.sent) == n
    rows = {r["id"]: r for r in out["rows"]}
    tried = [r for r in rows.values() if r["note"].startswith("not sent yet")]
    assert len(tried) == 1 and "retries on the next inbox check" in tried[0]["you_do"]
    assert sum(r["status"] == "auto_queued" for r in rows.values()) == 1
    db.expire_all()
    assert db.query(Interaction).filter_by(outcome="OFFER_SENT_TO_AGENT").count() == 0
    # The next check (the daemon 5 minutes later, or the operator running inbox again): both offers go, once each.
    out = gm.run_inbox(now=T0 + timedelta(minutes=46), smtp_factory=FakeSMTP)
    assert out["sent_now"] + out["poll"]["sent"] == 2 and not out.get("send_error"), out["rows"]
    assert len(FakeSMTP.sent) == n + 2
    assert {r["status"] for r in out["rows"]} == {"auto_sent"}
    out = gm.run_inbox(now=T0 + timedelta(minutes=52), smtp_factory=FakeSMTP)
    assert out["sent_now"] + out["poll"]["sent"] == 0 and len(FakeSMTP.sent) == n + 2


# -- 2026-11-03: the second morning's replies (names, phones and addresses replaced) ---------------------------------
@pytest.mark.parametrize("text, counter, intents", [
    ("Never happen…\n\nWe are at 470.00 Sorry.", 470000, {"COUNTER", "REJECT"}),
    ("We're under contract at 469.9", 469900, {"COUNTER", "UNDER_CONTRACT"}),
    ("Not happening at that price, seller won't go below 455k", 455000, {"COUNTER", "REJECT"}),
    ("The HOA is $150.00 a month and taxes are $6,200.", None, set()),
    ("HOA is $220.00, no special assessment.", None, set()),
])
def test_agent_shorthand_and_flat_noes_are_read_right(text, counter, intents):
    p = rn.parse_reply(text)
    assert p.counter_amount == counter and intents <= p.intents, (p.counter_amount, p.intents, p.notes)


def test_a_no_after_our_backup_offer_keeps_us_the_backup_never_above_the_walkaway(live):
    db = live
    lead = listed(db)
    db.add(InboundReply(message_id="<backup-1@broker.test>", lead_id=lead.id, from_email="pat@broker.test",
                        status="approved", intents_json=["BACKUP"], body_text="We have a contract but not sure"))
    lead.offer_amount = 280_000                          # our backup offer went out in writing
    db.flush()
    an = rn.analyze(db, lead, gm.strip_quoted("Never happen…\n\nWe are at 470.00 Sorry."))
    assert an.verdict == "RED" and an.headline.startswith("BACKUP AT")
    walk = an.numbers["adjusted_walkaway"]
    body = an.reply_text.split("--- Offer Summary ---")[0]
    assert "$470,000" in body and "backup spot" in body and hc.money(walk) in body
    assert an.numbers["our_counter"] == walk <= an.numbers["mao_walkaway"]
    assert not hc.lint(an.reply_text), hc.lint(an.reply_text)


def _pf(status, issue):
    from engine import preflight
    return lambda lead, today=None: preflight.Preflight(status=status, issues=[issue])


CONDO = ("condo / townhouse unit valued against other units' recorded sales without living area: ask the agent for "
         "recent sales of same-size units in the building before quoting")
UNITS = "12 of 18 comps have a different unit count than this 3-unit house: confirm the unit count"


@pytest.mark.parametrize("issue, reply, words, answer", [
    (CONDO, "Yes, it is still available. Are you interested in seeing it?", ["same-size units", "HOA"],
     "a few photos or a short video would help first"),
    (UNITS, "Yes it is. Are you a buyer or a realtor looking to show it to someone else?", ["rent roll", "vacant at closing"],
     "I'm the buyer: an investor"),
])
def test_a_price_the_facts_dont_support_yet_becomes_a_question_not_an_offer(live, monkeypatch, issue, reply, words,
                                                                               answer):
    from engine import preflight
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    monkeypatch.setattr(preflight, "check", _pf("VERIFY", issue))
    an = rn.analyze(db, lead, reply)
    above = an.reply_text.split("--- Details ---")[0]
    assert an.kind == "fact_check" and an.headline.startswith("CONFIRM THE FACTS FIRST")
    assert "$" not in above and all(w in above for w in words) and answer in above
    assert not hc.lint(an.reply_text), hc.lint(an.reply_text)
    # ... and it goes out by itself in their thread, with no LOI and no price, logged as a follow-up (not an offer).
    gm.set_imap_factory(lambda: FakeIMAP({81: mail(81, reply, in_reply_to=ours, subject="Re: question")}))
    n = len(FakeSMTP.sent)
    out = gm.run_inbox(now=T0 + timedelta(minutes=40), smtp_factory=FakeSMTP)
    assert out["sent_now"] + out["poll"]["sent"] == 1 and len(FakeSMTP.sent) == n + 1
    msg = FakeSMTP.sent[-1]
    assert msg["In-Reply-To"] == "<agent-81@broker.test>" and not list(msg.iter_attachments())
    assert "$" not in msg.get_content().split("--- Details ---")[0]
    db.expire_all()
    row = db.query(InboundReply).one()
    assert row.status == "auto_sent" and "no price yet" in row.note
    assert db.query(Interaction).filter_by(lead_id=lead.id, outcome="OFFER_SENT_TO_AGENT").count() == 0
    assert db.get(Lead, lead.id).offer_amount is None
    assert "wait for their facts" in next(r for r in out["rows"] if r["id"] == row.id)["you_do"]


def test_approving_a_no_price_note_never_attaches_an_offer(live, monkeypatch):
    """Before 2026-11-03, approving the wrong-parcel note attached an LOI priced off the other house."""
    from engine import preflight
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    monkeypatch.setattr(preflight, "check", _pf("HOLD", "wrong parcel: the MLS listing (Iselin) and the parcel record "
                                                        "(Sampletown) are 10.4 miles apart"))
    monkeypatch.setitem(settings(), "gmail_autopilot", {**settings()["gmail_autopilot"], "auto_send_fact_check": False})
    gm.set_imap_factory(lambda: FakeIMAP({82: mail(82, "We are still active. Please call me to discuss.",
                                                   in_reply_to=ours, subject="Re: question")}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=40), smtp_factory=FakeSMTP)
    db.expire_all()
    row = db.query(InboundReply).one()
    assert row.status == "needs_approval" and row.analysis_json["kind"] == "fact_check"
    r = gm.approve(db, row.id, now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)
    assert r["sent"] and r["price"] is None and not r["loi_attached"]
    assert not list(FakeSMTP.sent[-1].iter_attachments())
    assert db.query(Interaction).filter_by(lead_id=lead.id, outcome="OFFER_SENT_TO_AGENT").count() == 0


def test_commands_take_the_street_instead_of_an_id(live):
    db = live
    a, b = listed(db), listed(db)
    opener(db, a)
    street = a.property.street_address
    assert gm.resolve_lead(db, str(a.id)) == a.id
    assert gm.resolve_lead(db, street.split(" ")[0] + " " + street.split(" ")[1]) == a.id     # the e-mailed one wins
    with pytest.raises(gm.ApprovalError, match="no lead matches"):
        gm.resolve_lead(db, "999 Nowhere Rd")
    with pytest.raises(gm.ApprovalError, match="nothing from the agent is waiting"):
        gm.resolve_reply(db, street)
    assert b.id != a.id


def test_the_daemon_sends_one_paced_batch_per_weekday_after_its_start_time(live, monkeypatch):
    from engine.crm import TZ
    db = live
    calls = []

    def fake_campaign(limit, **kw):
        calls.append(limit)
        gm.campaign.total, gm.campaign.sent, gm.campaign.message = 3, 3, "Sent 3 of 3"
        return {}
    monkeypatch.setattr(gm, "start_campaign", fake_campaign)
    mon_early, mon = datetime(2026, 11, 9, 9, 0, tzinfo=TZ), datetime(2026, 11, 9, 10, 45, tzinfo=TZ)
    assert gm.daily_openers(15, at="10:30", now=mon_early) is None                     # before the start time
    assert gm.daily_openers(0, at="10:30", now=mon) is None                            # off unless asked for
    assert gm.daily_openers(15, at="10:30", now=mon)["sent"] == 3 and calls == [15]
    assert gm.daily_openers(15, at="10:30", now=mon + timedelta(hours=2)) is None      # once a day
    assert gm.daily_openers(15, at="10:30", now=datetime(2026, 11, 14, 11, 0, tzinfo=TZ)) is None   # Saturday
    assert gm.daily_openers(15, at="10:30", now=datetime(2026, 11, 10, 21, 0, tzinfo=TZ)) is None   # after send hours
    assert gm.daily_openers(15, at="10:30", now=datetime(2026, 11, 10, 10, 31, tzinfo=TZ))["sent"] == 3
    assert calls == [15, 15]


def test_the_mac_reports_the_day_on_the_calendar(live):
    """📊 one all-day event per day: what went out, what came back, what waits on you (with the command)."""
    from engine import gcal_sync
    db = live
    lead = listed(db)
    ours = opener(db, lead)["Message-ID"]
    gm.set_imap_factory(lambda: FakeIMAP({91: mail(91, "Seller wants 355k, can you do that?", in_reply_to=ours,
                                                   subject="Re: question")}))
    gm.run_inbox(now=T0 + timedelta(minutes=40), smtp_factory=FakeSMTP)
    db.expire_all()
    d = gcal_sync.desk_status_event(db, (T0 + timedelta(minutes=45)).replace(tzinfo=None))
    body = d.body
    street = hc.street(lead.property.street_address)
    assert d.key == f"desk:{T0:%Y%m%d}" and body["start"].get("date") and body.get("transparency") == "transparent"
    assert body["summary"].startswith("📊 Deal desk:") and "1 waiting on you" in body["summary"]
    assert "WAITING ON YOU (1)" in body["description"] and "call them" in body["description"] and "send #" in body["description"]
    assert street.split(" ")[1] in body["description"]
    assert "Seller wants" not in body["description"]                   # never the agent's own words
    assert gcal_sync.desk_status_event.__name__ in {x.__name__ for x in [gcal_sync.desk_status_event]}
    assert d.key in gcal_sync.desired_events(db, None, (T0 + timedelta(minutes=45)).replace(tzinfo=None))


def test_set_gmail_saves_a_new_password_from_a_hidden_prompt(setup_paths, monkeypatch):  # noqa: F811
    import getpass
    import manage
    from engine import operator_setup
    (setup_paths / ".env").write_text("DB_SYNC_KEY=keep-me\nSMTP_USER=jordan@example.test\nSMTP_APP_PASSWORD=old\n")
    operator_setup.sync_from_disk()
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "abcd efgh ijkl mnop")
    monkeypatch.setattr(gm, "test_connection", lambda **kw: {"ok": True, "account": "jordan@example.test",
                                                             "smtp": {"ok": True, "detail": "signed in"},
                                                             "imap": {"ok": True, "detail": "signed in"}})
    manage.cmd_set_gmail(type("A", (), {"email": "jordan@example.test"})())
    env = (setup_paths / ".env").read_text()
    assert "SMTP_APP_PASSWORD=abcdefghijklmnop" in env and "DB_SYNC_KEY=keep-me" in env and "=old" not in env
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "too short")
    with pytest.raises(SystemExit):
        manage.cmd_set_gmail(type("A", (), {"email": "jordan@example.test"})())
    assert "SMTP_APP_PASSWORD=abcdefghijklmnop" in (setup_paths / ".env").read_text()   # nothing saved on a typo


def test_every_waiting_answer_goes_out_after_one_yes(live, monkeypatch):
    """2026-11-03: the operator came back to a full inbox. One confirmed command sends every drafted answer (each
    through approve(): LOI at the drafted price, CAN-SPAM lines, stop list); street-only matches stay for a look."""
    db = live
    a, b = listed(db), listed(db)
    oa, ob = opener(db, a)["Message-ID"], opener(db, b, when=T0 + timedelta(minutes=1))["Message-ID"]
    gm.set_imap_factory(lambda: FakeIMAP({
        101: mail(101, "Seller wants 355k, can you get there?", in_reply_to=oa, subject="Re: question a"),
        102: mail(102, "Who is the buyer and how did you get my info?", in_reply_to=ob, subject="Re: question b",
                  frm="Pat Sample <pat2@broker.test>")}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=40), smtp_factory=FakeSMTP)
    db.add(InboundReply(message_id="<street-only@x.test>", lead_id=a.id, status="needs_approval", match_method="address",
                        analysis_json={"reply_text": "hi"}, subject="Re: something on your street"))
    db.commit()
    ok, held = gm.approvable(db)
    assert len(ok) == 2 and [why for _, why in held] == ["matched only by the street in the subject: check it's about "
                                                          "this listing"]
    n = len(FakeSMTP.sent)
    r = gm.approve_all(now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)
    assert len(r["sent"]) == 2 and not r["failed"] and len(FakeSMTP.sent) == n + 2
    assert {m["In-Reply-To"] for m in FakeSMTP.sent[-2:]} == {"<agent-101@broker.test>", "<agent-102@broker.test>"}
    db.expire_all()
    assert {x.status for x in db.query(InboundReply)} == {"approved", "needs_approval"}       # the street-only one waits


def test_the_desk_answers_the_safe_replies_by_itself_and_leaves_the_rest_for_a_call(live):
    """Calls only for the operator (2026-11-03): no-price notes and holds on a plainly stated number go out by
    themselves; an accept, a question or a number read out of a signature waits for a call."""
    db = live
    leads = [listed(db) for _ in range(4)]
    texts = {0: "Never happen. We are at 470.00 Sorry.",              # RED hold, stated number: sends
             1: "Seller would take 250k, send it over.",             # GREEN accept: a call
             2: "Who is the buyer and how did you get my info?",     # a question: a call
             3: "Thanks.\n\nPat Sample | Lic 0897105 | 470"}          # nothing plainly stated: never priced
    ours = {i: opener(db, lead, when=T0 + timedelta(minutes=i))["Message-ID"] for i, lead in enumerate(leads)}
    gm.set_imap_factory(lambda: FakeIMAP({200 + i: mail(200 + i, t, in_reply_to=ours[i], subject=f"Re: q{i}",
                                                        frm=f"Pat Sample <pat{i}@broker.test>")
                                          for i, t in texts.items()}))
    gm.poll_gmail_replies(now=T0 + timedelta(minutes=40), smtp_factory=FakeSMTP)
    db.expire_all()
    rows = {r.lead_id: r for r in db.query(InboundReply)}
    n = len(FakeSMTP.sent)
    r = gm.auto_approve(now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)
    db.expire_all()
    sent_leads = {x["lead_id"] for x in r["sent"]}
    assert leads[0].id in sent_leads and leads[1].id not in sent_leads and leads[2].id not in sent_leads
    assert len(FakeSMTP.sent) == n + len(r["sent"])
    assert db.get(InboundReply, rows[leads[1].id].id).status == "needs_approval"
    assert db.get(InboundReply, rows[leads[2].id].id).status == "needs_approval"


def _voice(uid, body, *, number="(973) 555-0100", kind="text"):
    from email.message import EmailMessage
    m = EmailMessage()
    if kind == "text":
        m["From"] = f'"{number} (SMS)" <17325550000.1{re.sub(r"[^0-9]", "", number)}.Ab12Cd@txt.voice.google.com>'
        m["Subject"] = f"New text message from {number}"
        m.set_content(f"{body}\n\nTo respond to this text message, reply to this email or visit Google Voice.\n\n"
                      "YOUR ACCOUNT <https://voice.google.com> HELP CENTER <https://support.google.com/voice>")
    else:
        m["From"] = "Google Voice <voice-noreply@google.com>"
        m["Subject"] = f"New voicemail from {number} at 3:14 PM"
        m.set_content(f"Transcript: {body}\n\nPlay message <https://voice.google.com/voicemail>\n\nYOUR ACCOUNT")
    m["To"], m["Message-ID"] = "jordan@example.test", f"<voice-{uid}@google.com>"
    m["Date"] = "Wed, 04 Nov 2026 10:20:00 -0500"
    return m.as_bytes(policy=policy.SMTP)


def test_an_agents_google_voice_text_becomes_their_reply_a_call_and_a_drafted_text(live):
    """2026-11-03: the operator's Google Voice forwards texts and voicemails to the business Gmail. The desk reads
    those from agents we contacted (by their phone), never sends anything back through the Voice relay address, and
    turns each into a 📞 call with a drafted text the operator can send from their phone."""
    from engine import gcal_sync
    db = live
    lead = listed(db)                                          # the agent's phone on file: (973) 555-0100
    opener(db, lead)
    gm.set_imap_factory(lambda: FakeIMAP({
        301: _voice(301, "Yes still available, call me when you get a chance"),
        302: _voice(302, "Hi this is someone else entirely", number="(201) 555-0999"),
        303: _voice(303, "Hey it's Pat about the listing, seller would take 355", kind="voicemail")}))
    out = gm.poll_gmail_replies(now=T0 + timedelta(minutes=40), smtp_factory=FakeSMTP)
    assert out["matched"] == 2                                 # the stranger's text is never read or stored
    db.expire_all()
    rows = {r.match_method: r for r in db.query(InboundReply)}
    text, vm = rows["voice_text"], rows["voice_mail"]
    assert text.status == "needs_approval" and text.from_email is None and "Google Voice" not in text.body_text
    assert text.body_text.startswith("Yes still available") and "texted you" in text.note
    assert vm.body_text.startswith("Hey it's Pat") and vm.counter_amount == 355000
    n = len(FakeSMTP.sent)
    with pytest.raises(gm.ApprovalError, match="texted or called"):
        gm.approve(db, text.id, smtp_factory=FakeSMTP)
    assert gm.auto_approve(now=T0 + timedelta(minutes=50), smtp_factory=FakeSMTP)["sent"] == []
    assert len(FakeSMTP.sent) == n                             # nothing went out by itself
    [ev] = [e for e in gcal_sync.reply_events(db, lead, datetime(2026, 11, 4, 10, 0)) if f"r{text.id}" in e.key]
    assert "they texted you" in ev.body["summary"] and "text back from your phone" in ev.body["description"]
    assert gm.reply_thread(db, lead.id) is None or "voice" not in (gm.reply_thread(db, lead.id)["message_id"] or "")
    rows_out = {r["id"]: r for r in gm.inbox_rows(db, T0 + timedelta(minutes=45))}
    assert rows_out[text.id]["you_do"].startswith("call them back")


def test_openers_go_out_in_waves_and_a_missed_wave_catches_up(live, monkeypatch):
    """2026-11-03 volume plan: 30 openers a weekday in two waves (10:30, 14:00), so no desk run is busy for half an hour."""
    from engine.crm import TZ
    calls = []

    def fake_campaign(limit, **kw):
        calls.append(limit)
        gm.campaign.total, gm.campaign.sent, gm.campaign.message = limit, limit, "ok"
        return {}
    monkeypatch.setattr(gm, "start_campaign", fake_campaign)
    waves = "10:30,14:00"
    assert gm.daily_openers(31, at=waves, now=datetime(2026, 11, 9, 10, 40, tzinfo=TZ))["sent"] == 16
    assert gm.daily_openers(31, at=waves, now=datetime(2026, 11, 9, 12, 0, tzinfo=TZ)) is None      # wave 2 not due
    assert gm.daily_openers(31, at=waves, now=datetime(2026, 11, 9, 14, 5, tzinfo=TZ))["sent"] == 15
    assert gm.daily_openers(31, at=waves, now=datetime(2026, 11, 9, 16, 0, tzinfo=TZ)) is None      # both done
    # Tuesday: the desk first runs at 15:00, so it sends wave 1 now and wave 2 at its next run
    tue = datetime(2026, 11, 10, 15, 0, tzinfo=TZ)
    assert gm.daily_openers(30, at=waves, now=tue)["sent"] == 15
    assert gm.daily_openers(30, at=waves, now=tue + timedelta(minutes=15))["sent"] == 15
    assert gm.daily_openers(30, at=waves, now=tue + timedelta(minutes=30)) is None
    assert calls == [16, 15, 15, 15]


@pytest.mark.production_gates
def test_one_new_cold_email_per_agent_every_three_days(live, monkeypatch):
    """An agent with several listings gets one new note, not one per house."""
    from engine import autopilot
    db = live
    a = listed(db)
    b = listed(db)                                       # same agent e-mail (pat@broker.test) on a second listing
    cards = [{"lead_id": x.id, "address": f"{x.id} Main St", "kind": "opener", "touch": 1,
              "agent": {"name": "Pat Agent", "email": "pat@broker.test"}} for x in (a, b)]
    monkeypatch.setattr(autopilot, "deck", lambda s, limit=None, now=None: {"cards": cards})
    monkeypatch.setattr(gm, "auto_sendable", lambda c: None)
    picked, skipped = gm.pick_cards(db, 10)
    assert [p["lead_id"] for p in picked] == [a.id] and "last 3 days" in skipped[0]["reason"]


def test_a_polite_no_on_a_held_listing_is_a_no_not_another_fact_check(live):
    """2026-11-04: after 'No where close to what the seller needs. Best of luck', the desk sent the same
    fact-finding note again. A no is read as a no, whatever pre-flight says about the numbers."""
    from db.models import Listing
    db = live
    lead = listed(db)
    ref = next(sg.source_ref for sg in lead.property.signals if sg.signal_type == "STALE_LISTING")
    db.add(Listing(source="homeharvest", source_ref=ref, address="1 Test St", city="Iselin", lat=40.576, lon=-74.307))
    db.flush()
    for text in ("No where close to what the seller needs. Best of luck", "Nowhere close. Good luck with your search."):
        assert "REJECT" in rn.parse_reply(text).intents
        an = rn.analyze(db, lead, text)
        assert an.kind != "fact_check" and not an.headline.startswith("NUMBERS ON HOLD"), an.headline
        assert "bedrooms" not in an.reply_text
