"""Stream 1: the "buyers ready" buy-box intake (engine/streams/s1_acquisitions.py, operator 2026-11-10).

We line up cash buyers for our live written offers without marketing any house we don't have under contract: the
note carries only an aggregate (how many, which counties, a rounded band across all live offers), never an address,
a house's price or a spread, and goes only to buyer-type targets in those counties, at most twice, 14+ days apart."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from types import SimpleNamespace

from db.models import (
    CashBuyer,
    ContactType,
    FirmOutbox,
    FirmTarget,
    InboundReply,
    Interaction,
    LeadStage,
    Prospect,
    SignalType,
)
from engine import firm_outbox as fo
from engine import human_copy as hc
from engine.streams import s1_acquisitions as s1
from engine.streams.base import PRIORITY_PITCH, StreamContext
from tests.factories import add_contact, make_lead
from tests.test_command_center import setup_paths  # noqa: F401 - pytest fixture
from tests.test_gmail_autopilot import live  # noqa: F401 - pytest fixture

TUE = datetime(2026, 11, 10, 10, 0)              # a Tuesday (naive Eastern, the DB convention)
UNION = {"county": "Union", "municipality": "ELIZABETH CITY", "mun_code": "2004", "city": "Elizabeth",
         "zip_code": "07201"}
BERGEN = {"county": "Bergen", "municipality": "HACKENSACK CITY", "mun_code": "0223", "city": "Hackensack",
          "zip_code": "07601"}


def _ctx(tmp_path, now=TUE, **limits) -> StreamContext:
    return StreamContext(now=now, out_dir=tmp_path / "out", operator=hc.operator(), limits=limits)


def _offer(db, price: float, *, days: int = 1, reply: bool = True, stage: str | None = None, **prop):
    """A lead with our written offer out and the agent conversation `days` ago (their reply, or our offer e-mail)."""
    ld = make_lead(db, sheriff_days=None, stale_listing=True, list_price=price + 60000, **prop)
    ld.offer_amount = price
    if stage:
        ld.status_stage = stage
    add_contact(db, ld, contact_type=ContactType.LISTING_AGENT, name="Lee Broker", phone=None,
                email=f"lee{ld.id}@broker.test")
    if reply:
        db.add(InboundReply(message_id=f"<r{ld.id}@broker.test>", lead_id=ld.id, from_email=f"lee{ld.id}@broker.test",
                            status="needs_approval", created_at=TUE - timedelta(days=days)))
    else:
        db.add(Interaction(lead_id=ld.id, channel="email", outcome="OFFER_SENT_TO_AGENT",
                           occurred_at=TUE - timedelta(days=days)))
    db.flush()
    return ld


def _two_offers(db):
    a = _offer(db, 312000)                                   # Essex (Newark), the agent wrote back yesterday
    b = _offer(db, 587500, reply=False, days=3, **UNION)     # Union (Elizabeth), our offer e-mail three days ago
    db.commit()
    return a, b


def _text(body: str) -> str:
    paras = [p for p in body.split("\n\n") if p.strip()]
    assert re.fullmatch(r"(Hi [A-Z][A-Za-z]+|Hello),", paras[0]) and paras[-1] == hc.operator()["first"]
    return " ".join(paras[1:-1])


def _sentences(body: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.?!])\s+", _text(body)) if s]


def _target(db, email, kind="investor_friendly_broker", **kw):
    t = fo.upsert_target(db, kind=kind, stream=kw.pop("stream", "s2"), email=email, org=kw.pop("org", "Sample Realty"),
                         **kw)
    db.commit()
    return t


# -- live offers and the aggregate --------------------------------------------------------------------------------
def test_live_offers_are_written_offers_with_a_recent_agent_conversation(live):
    db = live
    a, b = _two_offers(db)
    no_offer = _offer(db, 300000)
    no_offer.offer_amount = None
    _offer(db, 300000, days=30)                              # the conversation went quiet
    _offer(db, 300000, stage=LeadStage.DEAD)
    _offer(db, 300000, stage=LeadStage.UNDER_CONTRACT)       # a signed contract goes through disposition instead
    gone = _offer(db, 300000)
    for s in gone.property.signals:
        if s.signal_type == SignalType.STALE_LISTING:
            s.active = False                                 # pending / sold: off the market
    db.commit()
    assert {ld.id for ld in s1.live_offers(db, TUE, 21)} == {a.id, b.id}
    book = s1.offer_book(s1.live_offers(db, TUE, 21))
    assert (book.count, book.counties, book.band, book.where) == (2, ["Essex", "Union"], "$300K-$600K",
                                                                  "Essex and Union counties")


def test_the_band_is_rounded_outward_and_never_narrower_than_50k():
    def lead(price, county):
        return SimpleNamespace(offer_amount=price, property=SimpleNamespace(county=county))
    book = s1.offer_book([lead(280000, "MONMOUTH"), lead(455000, "Monmouth"), lead(640000, "Union"),
                          lead(330000, "Essex")])
    assert book.band == "$275K-$650K" and book.counties == ["Monmouth", "Essex", "Union"]
    assert book.where == "Monmouth, Essex and Union counties"
    assert s1.offer_book([lead(300000, "Essex"), lead(310000, "Essex")]).band == "$300K-$350K"
    assert s1.offer_book([lead(950000, "Bergen"), lead(1_010_000, "Bergen")]).band == "$950K-$1.025M"
    assert s1.offer_book([]) is None
    assert s1.county_phrase(["A", "B", "C", "D", "E"]) == "A, B, C and 2 other counties"


# -- the note -----------------------------------------------------------------------------------------------------
def test_the_note_goes_only_to_buyer_targets_in_the_live_offer_counties_and_names_no_house(live, tmp_path):
    db = live
    a, _b = _two_offers(db)
    # a flipper family buying in Essex (deed records), reached through the business e-mail found for it
    db.add(CashBuyer(entity_key="RIVERHOMES", entity_name="RIVER HOMES LLC", mailing_address="9 RIVER RD",
                     cash_purchases_count=6, last_purchase_date=TUE.date(),
                     recent_purchases_json=[{"address": f"{i} SAMPLE AVE", "municipality": "NEWARK CITY",
                                             "county": "Essex", "mun_code": "0714", "price": 400000 + 1000 * i,
                                             "deed_date": f"2026-0{i + 1}-15", "units": 1} for i in range(6)]))
    db.add(Prospect(key="RIVERHOMESLLC", entity_name="RIVER HOMES LLC", email="deals@riverhomes.example",
                    stage="NEW", purchases_36m=6))
    union =_target(db, "info@unionbrokers.example", county="Union", org="Union Investment Brokers")
    town = _target(db, "info@newarkbrokers.example", town="Newark", org="Newark Investment Brokers")   # no county
    _target(db, "info@bergenbrokers.example", county="Bergen", org="Bergen Brokers")                    # elsewhere
    _target(db, "info@roofs.example", kind="roofer", county="Essex", org="Sample Roofing", stream="s4")  # not a buyer
    gone = _target(db, "old@flips.example", kind="flipper", county="Essex", org="Old Flips LLC")
    gone.stage = "OPTED_OUT"
    listing = make_lead(db, sheriff_days=None, stale_listing=True)                       # an Essex listing of hers
    add_contact(db, listing, contact_type=ContactType.LISTING_AGENT, name="Kim Seller", phone=None,
                email="kim@realty.example")
    kim = _target(db, "kim@realty.example", kind="investor_agent", name="Kim Seller", org=None, score=9)
    far = make_lead(db, sheriff_days=None, stale_listing=True, **BERGEN)
    add_contact(db, far, contact_type=ContactType.LISTING_AGENT, name="Bo Far", phone=None, email="bo@realty.example")
    _target(db, "bo@realty.example", kind="investor_agent", name="Bo Far", org=None, score=50)       # Bergen only
    _target(db, f"lee{a.id}@broker.test", kind="investor_agent", name="Lee Broker", org=None, score=99)  # in talks
    db.commit()
    r = s1.STREAM.run(db, _ctx(tmp_path, buyers_per_run=8))
    flipper = db.query(FirmTarget).filter_by(email="deals@riverhomes.example").one()
    assert [o.target_id for o in r.outbound] == [flipper.id, kim.id, union.id, town.id]   # buyers first
    assert (r.metrics["live_offers"], r.metrics["buyer_targets"], r.metrics["buyers_queued"]) == (2, 4, 4)
    assert all(isinstance(v, (int, float)) for v in r.metrics.values())                    # counts only
    streets = [ld.property.street_address for ld in db.query(type(a)).all()]
    for ob in r.outbound:
        assert ob.key == f"s1:buyers:{ob.target_id}:2026-W46" and ob.kind == "buyer_note" and ob.commercial
        assert ob.priority == PRIORITY_PITCH and ob.attachments == [] and ob.sample_attachments == []
        assert ob.sample_body == "" and ob.in_reply_to is None
        assert ob.subject == "As-is houses in Essex and Union counties"
        sents = _sentences(ob.body)
        assert len(sents) == 2 and hc.word_count(_text(ob.body)) <= 45 and hc.lint(ob.body) == [], sents
        assert sents[0] == ("I'm working on a couple of as-is houses in Essex and Union counties, $300K-$600K, "
                            "and may have one under contract soon.")
        assert re.findall(r"\$[\d,.]+[KM]?", ob.body) == ["$300K", "$600K"]                # the band, nothing else
        for street in streets:
            assert street.title() not in ob.body and street.upper() not in ob.body.upper()
        assert "312" not in ob.body and "587" not in ob.body and "assign" not in ob.body.lower()
        assert "ARV" not in ob.body and "fee" not in ob.body.lower()
    by = {o.target_id: o for o in r.outbound}
    assert by[flipper.id].body.startswith("Hello,\n\n")
    assert "Do you buy cash there, and what's your buy box (towns, price, rehab level)?" in by[flipper.id].body
    assert by[kim.id].body.startswith("Hi Kim,\n\n") and "Do you or your investor clients buy cash" in by[kim.id].body
    assert by[union.id].body.startswith("Hello,\n\n")
    assert sum(1 for _ in s1.STREAM.run(db, _ctx(tmp_path, buyers_per_run=2)).outbound) == 2


def test_once_per_14_days_at_most_twice_and_never_on_weekends(live, tmp_path):
    db = live
    _two_offers(db)
    t = _target(db, "info@unionbrokers.example", county="Union", org="Union Investment Brokers")
    assert s1.STREAM.run(db, _ctx(tmp_path, now=datetime(2026, 11, 14, 10))).outbound == []        # Saturday
    first = s1.STREAM.run(db, _ctx(tmp_path)).outbound[0]
    row = fo.enqueue(db, first, TUE)
    assert row.status == "queued" and db.query(FirmOutbox).filter_by(kind="sample").count() == 0   # nothing held
    assert s1.STREAM.run(db, _ctx(tmp_path)).outbound == []                 # queued already: never twice
    row.status, row.message_id, row.sent_at = "sent", "<s1a@desk.example>", TUE
    t.touches, t.stage, t.last_contact_at = 1, "CONTACTED", TUE
    db.commit()
    assert s1.STREAM.run(db, _ctx(tmp_path, now=TUE + timedelta(days=7))).outbound == []   # past the 6-day gap only
    later = TUE + timedelta(days=15)
    nudge = s1.STREAM.run(db, _ctx(tmp_path, now=later)).outbound
    assert len(nudge) == 1 and nudge[0].key == f"s1:buyers:{t.id}:2026-W48"
    ob = nudge[0]
    assert ob.in_reply_to == "<s1a@desk.example>" and ob.subject == "Re: " + first.subject and ob.attachments == []
    nudge_text = ("Do you or your investor clients buy cash in Essex and Union counties, and if so, what's the buy box "
                  "so I can send you the first as-is house once it's under contract?")
    assert _sentences(ob.body) == [nudge_text]
    row2 = fo.enqueue(db, ob, later)
    row2.status, row2.sent_at = "sent", later
    t.touches, t.last_contact_at = 0, later                 # even with cold touches to spare: 2 notes is the most
    db.commit()
    assert s1.STREAM.run(db, _ctx(tmp_path, now=later + timedelta(days=20), live_offer_days=90)).outbound == []


def test_a_target_in_another_funnel_or_with_one_live_offer_gets_nothing(live, tmp_path):
    db = live
    a = _offer(db, 312000)
    db.commit()
    t = _target(db, "info@newarkbrokers.example", county="Essex", org="Newark Investment Brokers")
    r = s1.STREAM.run(db, _ctx(tmp_path))
    assert r.outbound == [] and r.metrics["live_offers"] == 1      # one house can't be described without describing it
    _offer(db, 450000)
    db.add(FirmOutbox(key=f"s2:pitch:{t.id}:2026-W45", stream="s2", priority=PRIORITY_PITCH, kind="sample",
                      target_id=t.id, to_email=t.email, subject="Re: x", body="x", status="held"))
    db.commit()
    assert s1.STREAM.run(db, _ctx(tmp_path)).outbound == []        # a yes would release the other stream's sample
    db.query(FirmOutbox).delete()
    db.commit()
    assert [o.target_id for o in s1.STREAM.run(db, _ctx(tmp_path)).outbound] == [t.id]
    assert a.offer_amount == 312000


def test_a_yes_to_the_note_is_a_warm_reply_for_the_operator_and_sends_nothing_by_itself(live, tmp_path):
    db = live
    _two_offers(db)
    t = _target(db, "info@unionbrokers.example", county="Union", org="Union Investment Brokers")
    ob = s1.STREAM.run(db, _ctx(tmp_path)).outbound[0]
    row = fo.enqueue(db, ob, TUE)
    row.status, row.message_id = "sent", "<s1yes@desk.example>"
    t.touches = 1
    db.commit()
    mail = SimpleNamespace(message_id="<their@unionbrokers.example>", in_reply_to="<s1yes@desk.example>",
                           text="Yes, we buy in Union. 2-4 family under 500K, heavy rehab is fine.",
                           subject="Re: " + ob.subject, from_email=t.email, references=[], received=None,
                           auto_reply=False)
    thread, emails = fo.known(db)
    assert fo.match(thread, emails, mail) == t.id
    reply = fo.handle_reply(db, mail, t.id, TUE)
    db.commit()
    assert reply.intent == "interested" and reply.status == "new" and [w.id for w in fo.warm(db)] == [reply.id]
    assert db.query(FirmOutbox).filter(FirmOutbox.status == "queued").count() == 0
