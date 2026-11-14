"""The operator's partner note (2026-11-14): one short note to a county's best buyers and investor agents, queued from
a sealed note, sent any day 8-8 through the firm outbox's stop lists and CAN-SPAM footer."""
from __future__ import annotations

from datetime import datetime, timedelta

from core.config import settings
from db.models import ContactType, FirmOutbox, FirmReply, FirmTarget, InboundReply
from engine import firm_outbox as fo
from engine import partner_blast as pb
from engine.crm import TZ
from tests.factories import add_contact, make_lead
from tests.test_agent_outreach import FakeSMTP
from tests.test_command_center import setup_paths  # noqa: F401 - pytest fixture
from tests.test_gmail_autopilot import live  # noqa: F401 - pytest fixture

SAT = datetime(2026, 11, 14, 9, 0)                # Saturday 9:00 AM, naive Eastern
SAT_ET = SAT.replace(tzinfo=TZ)
MON = {"county": "Monmouth", "municipality": "ASBURY PARK CITY", "mun_code": "1304", "city": "Asbury Park",
       "zip_code": "07712", "lat": 40.22, "lon": -74.01}
SPEC = {"county": "Monmouth", "limit": 15, "key": "asbury-1114", "subject": "Two as-is Asbury Park houses",
        "buyer_body": ("I have two as-is Asbury Park deals in the high $400Ks to low $500Ks, one with the seller's yes "
                       "on price. Would you co-buy with me, funding the deposit/purchase for an attorney-reviewed "
                       "profit split?"),
        "agent_body": ("Hi {first}, I have two as-is Asbury Park deals in the high $400Ks to low $500Ks, one with the "
                       "seller's yes on price. Would you (or an investor client you represent on resales) co-buy with "
                       "me, funding the deposit/purchase for an attorney-reviewed profit split?")}


def _target(db, email, kind, county="Monmouth", **kw):
    return fo.upsert_target(db, kind=kind, stream="s2", email=email, org=kw.pop("org", None), county=county, **kw)


def _world(db):
    """Our live offer in Asbury Park (listing agent at listbroker.example) and the county's buyers and agents."""
    ld = make_lead(db, sheriff_days=None, stale_listing=True, list_price=535000, **MON)
    ld.offer_amount = 475900
    stacy = add_contact(db, ld, contact_type=ContactType.LISTING_AGENT, name="Stacy Lister", phone=None,
                        email="stacy@listbroker.example")
    stacy.company = "Shore Gateway Realty - Broker"
    db.add(InboundReply(message_id="<s1@listbroker.example>", lead_id=ld.id, from_email="stacy@listbroker.example",
                        status="answered", created_at=SAT - timedelta(days=1)))
    other = make_lead(db, sheriff_days=None, stale_listing=True, list_price=420000, **{**MON, "lat": 40.221})
    add_contact(db, other, contact_type=ContactType.LISTING_AGENT, name="Lee Coast", phone=None,
                email="lee@coastrealty.example")
    buyer = _target(db, "deals@shorebuyers.example", "flipper", org="Shore Buyers LLC", score=10)
    _target(db, "deals@farbuyers.example", "flipper", county="Bergen", org="Far Buyers LLC", score=99)
    agent = _target(db, "ann@shorerealty.example", "investor_agent", name="Ann Shore", score=5)
    _target(db, "bob@listbroker.example", "investor_agent", name="Bob Colleague", score=50)   # Stacy's office
    out = _target(db, "out@optout.example", "flipper", org="Out LLC", score=40)
    out.stage = "OPTED_OUT"
    waiting = _target(db, "ken@waiting.example", "investor_agent", name="Ken Waiting", score=30)
    db.add(FirmReply(message_id="<k@waiting>", target_id=waiting.id, stream="s1", from_email="ken@waiting.example",
                     status="new"))
    mate = make_lead(db, sheriff_days=None, stale_listing=True, list_price=389000, **{**MON, "lat": 40.223})
    c = add_contact(db, mate, contact_type=ContactType.LISTING_AGENT, name="Al Mate", phone=None,
                    email="almate@yahoo.com")                     # Stacy's office, writing from yahoo.com
    c.company = "SHORE GATEWAY REALTY"
    talking = _target(db, "kim@talking.example", "investor_agent", name="Kim Talking", score=30)
    talking.stage = "INTERESTED"                                    # answered already: in a conversation
    lst = make_lead(db, sheriff_days=None, stale_listing=True, list_price=399000, **{**MON, "lat": 40.222})
    add_contact(db, lst, contact_type=ContactType.LISTING_AGENT, name="Kim Talking", phone=None,
                email="kim@talking.example")
    db.flush()
    pending = fo.enqueue(db, fo_pitch(buyer), SAT)
    db.commit()
    return buyer, agent, pending


def fo_pitch(t):
    from engine.streams.base import Outbound
    return Outbound(stream="s1", key=f"s1:buyers:{t.id}:2026-W46", to=t.email, subject="As-is houses",
                    body="Hello,\n\nA buy-box note.\n\nJordan", kind="buyer_note", target_id=t.id)


def test_the_list_is_the_countys_buyers_then_agents_never_our_deal_agents_or_their_office(live):
    db = live
    _world(db)
    picked = pb.pick(db, "Monmouth", 15, now=SAT, key="asbury-1114")
    emails = [c.email for c in picked]
    assert emails[0] == "deals@shorebuyers.example"                          # buyers first
    assert set(emails) == {"deals@shorebuyers.example", "ann@shorerealty.example", "lee@coastrealty.example"}
    # never: another county, the agent on our live offer, her brokerage, an opted-out firm, a reply waiting on us
    assert {c.audience for c in picked if c.email != "deals@shorebuyers.example"} == {"agent"}
    # researched addresses go first, inside the limit, but never past a rule (Stacy's office stays out)
    two = pb.pick(db, "Monmouth", 2, now=SAT, key="asbury-1114",
                  include=["LEE@coastrealty.example", "bob@listbroker.example"])
    assert len(two) == 2 and two[0].email == "lee@coastrealty.example"
    assert "bob@listbroker.example" not in [c.email for c in two]


def test_a_preview_queues_nothing_and_a_run_queues_one_note_each_in_the_operators_words(live):
    db = live
    buyer, agent, pending = _world(db)
    res = pb.queue(db, {**SPEC, "preview": True}, now=SAT_ET)
    assert res["blast_picked"] == 3 and res["blast_queued"] == 0
    assert "Ann Shore <ann@shorerealty.example>" in res["preview_text"] and "nothing was sent" in res["preview_text"]
    assert db.query(FirmOutbox).filter_by(kind="partner_note").count() == 0
    res = pb.queue(db, SPEC, now=SAT_ET)
    db.commit()
    assert (res["blast_queued"], res["blast_buyers"], res["blast_agents"]) == (3, 1, 2)
    rows = {r.to_email: r for r in db.query(FirmOutbox).filter_by(kind="partner_note")}
    assert rows["ann@shorerealty.example"].body.startswith("Hi Ann,\n\nI have two as-is Asbury Park deals")
    assert "(or an investor client you represent on resales)" in rows["ann@shorerealty.example"].body
    assert rows["deals@shorebuyers.example"].body.startswith("Hello,\n\nI have two")
    assert "investor client" not in rows["deals@shorebuyers.example"].body
    assert all(r.body.endswith("\n\nJordan") and r.subject == SPEC["subject"] for r in rows.values())
    db.refresh(pending)
    assert (pending.status, pending.reason) == ("skipped", "partner note sent instead")   # one cold note a week
    assert pb.queue(db, SPEC, now=SAT_ET)["blast_queued"] == 0                           # never twice (same key)
    lee = db.query(FirmTarget).filter_by(email="lee@coastrealty.example").one()
    assert (lee.kind, lee.stream, lee.county) == ("investor_agent", "s1", "Monmouth")


def test_partner_notes_go_on_a_saturday_with_the_ad_line_and_opt_out(live, monkeypatch):
    db = live
    monkeypatch.setitem(settings(), "research_firm", {**settings()["research_firm"], "partner_gap_seconds": [0, 0]})
    _world(db)
    pb.queue(db, SPEC, now=SAT_ET)
    db.commit()
    FakeSMTP.sent = []
    r = fo.send_due(now=SAT_ET, smtp_factory=FakeSMTP, pace=False)
    assert r["sent"] == 3 and len(FakeSMTP.sent) == 3                      # cold pitches wait for Monday; these don't
    text = FakeSMTP.sent[0].get_body(preferencelist=("plain",)).get_content()
    assert "A business solicitation from" in text and 'Reply "no more"' in text
    assert FakeSMTP.sent[0]["List-Unsubscribe"]
    db.expire_all()
    assert db.query(FirmTarget).filter_by(email="ann@shorerealty.example").one().stage == "CONTACTED"
    late = datetime(2026, 11, 14, 20, 30, tzinfo=TZ)
    pb.queue(db, {**SPEC, "key": "late"}, now=late)
    db.commit()
    assert fo.send_due(now=late, smtp_factory=FakeSMTP, pace=False)["sent"] == 0      # 8 AM-8 PM only


def test_partner_notes_wait_their_gap_between_sends(live, monkeypatch):
    db = live
    monkeypatch.setitem(settings(), "research_firm", {**settings()["research_firm"], "partner_gap_seconds": [60, 60]})
    _world(db)
    pb.queue(db, SPEC, now=SAT_ET)
    db.commit()
    FakeSMTP.sent = []
    assert fo.send_due(now=SAT_ET, smtp_factory=FakeSMTP, pace=False)["sent"] == 1   # the next waits 60 s


def test_a_sealed_note_previews_or_queues_the_partner_note(live):
    db = live
    from engine import cloud_desk as cd
    _world(db)
    key = "k" * 32
    sent = []
    cd_send = cd._send_self_files
    try:
        cd._send_self_files = lambda *a, **k: sent.append(a[2])
        out = cd.apply_note(cd.seal_note([{"partner_blast": {**SPEC, "preview": True}}], cd.public_hex(key)), key)
    finally:
        cd._send_self_files = cd_send
    assert out["note_done"] == 1 and out["blast_picked"] == 3 and out["blast_preview_mailed"] == 1
    assert sent and sent[0].startswith("[njw-blast] ") and sent[0].endswith("Monmouth preview")
    assert "preview_text" not in out                                         # the public log carries counts only
    out = cd.apply_note(cd.seal_note([{"firm_target": {"kind": "flipper", "email": "new@coastflips.example",
                                                       "org": "Coast Flips LLC", "county": "Monmouth"}},
                                      {"partner_blast": SPEC}], cd.public_hex(key)), key)
    assert out["note_done"] == 2 and out["blast_queued"] == 4               # the researched firm is on the list


def test_a_house_that_turns_warm_lines_up_a_co_buyer_by_itself(live, monkeypatch):
    """2026-11-14: accepted / within $25K of our final / proof of funds asked on a house we can price -> one partner
    note blast for its county (county, product, a rounded band: never the address or our number), once per house."""
    from core.config import settings
    from db.models import InboundReply
    db = live
    monkeypatch.setitem(settings(), "partner_auto", {**settings()["partner_auto"], "enabled": True})
    from engine import offer_math as om
    monkeypatch.setattr(om, "firm_offer", lambda lead, session=None, **k: om.FirmOffer(tier="ask_first", final=470000,
                                                                                      ask=520000))
    buyer, agent, _ = _world(db)
    warm = make_lead(db, sheriff_days=None, stale_listing=True, list_price=520000, dwellings=2,
                     **{**MON, "lat": 40.224})
    warm.offer_amount = 455000
    add_contact(db, warm, contact_type=ContactType.LISTING_AGENT, name="Wes Warm", phone=None, email="wes@warm.example")
    db.add(InboundReply(message_id="<w@warm>", lead_id=warm.id, from_email="wes@warm.example", status="dismissed",
                        intents_json=["POF_REQUEST"], created_at=SAT - timedelta(hours=1)))
    db.commit()
    out = pb.auto_run(db, SAT_ET)
    db.commit()
    assert out["auto_blasts"] == 1 and out["auto_queued"] >= 2
    rows = db.query(FirmOutbox).filter(FirmOutbox.key.like(f"partner:auto-{warm.id}:%")).all()
    body = rows[0].body
    assert "2-family in Monmouth County in the $425K-$500K range that I'm negotiating now" in body
    assert "455" not in body and "Asbury" not in body and "wes@warm.example" not in [r.to_email for r in rows]
    assert pb.auto_run(db, SAT_ET)["auto_blasts"] == 0                    # once per house
