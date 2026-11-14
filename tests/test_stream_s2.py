"""Stream 2: the AI Deal Desk sold through the 2-step funnel (engine/streams/s2_deal_desk_saas.py, 2026-11-10).

Touch 1 is two plain sentences with one real street and true counts, no attachment and no subscription price; the
masked digest sample with the flat price waits for their yes; the follow-up is one sentence in the same thread."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from db.models import CashBuyer, ContactType, FirmOutbox, FirmTarget, Prospect, utcnow
from engine import firm_outbox as fo
from engine import human_copy as hc
from engine.streams import s2_deal_desk_saas as s2
from engine.streams.base import PRIORITY_PITCH, StreamContext, sample_path
from tests.factories import add_contact, make_lead
from tests.test_command_center import setup_paths  # noqa: F401 - pytest fixture
from tests.test_gmail_autopilot import live  # noqa: F401 - pytest fixture

TUE = datetime(2026, 11, 10, 10, 0)              # a Tuesday, inside business hours (naive Eastern)
LIMITS = {"pitches_per_run": 4, "agents_top": 0, "price_trial": 500, "price_monthly": 1000}
SAMPLE_LINE = ("$249 a month for one county or $499 for all eleven, plus NJ sales tax")


def _ctx(tmp_path, now=TUE, **limits) -> StreamContext:
    return StreamContext(now=now, out_dir=tmp_path / "out", operator=hc.operator(), limits={**LIMITS, **limits})


def _text(body: str) -> str:
    """The note above the sign-off without the greeting line ("Hi Pat," / "Hello,") and the first-name sign-off."""
    paras = [p for p in body.split("\n\n") if p.strip()]
    assert re.fullmatch(r"(Hi [A-Z][A-Za-z]+|Hello),", paras[0]) and paras[-1] == hc.operator()["first"]
    return " ".join(paras[1:-1])


def _sentences(body: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.?!])\s+", _text(body)) if s]


def _pdf(path) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text() for p in PdfReader(str(path)).pages)


def _has_street(text: str, street: str) -> bool:
    return re.search(rf"(?<!\d){re.escape(street)}\b", text) is not None


def _street(lead) -> str:
    return lead.property.street_address.title()


def _agent_world(db, n_others: int = 4, **kw):
    """The agent's own listing plus `n_others` fixer listings by other agents in the same town (Newark, Essex)."""
    own = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000,
                    assumptions={"quant": {"buyer_max": 440000}}, listing_text="estate sale, sold as-is")
    add_contact(db, own, contact_type=ContactType.LISTING_AGENT, name="Pat Agent", phone=None,
                email="pat@broker.example", company="SAMPLE REALTY")
    others = []
    for i in range(n_others):
        ld = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000,
                       assumptions={"quant": {"buyer_max": 420000}}, listing_text="needs work, as-is")
        add_contact(db, ld, contact_type=ContactType.LISTING_AGENT, name="Lee Broker", phone=None,
                    email=f"lee{i}@broker.example")
        others.append(ld)
    t = fo.upsert_target(db, kind="investor_agent", stream="s2", email="pat@broker.example", name="Pat Agent",
                         org="SAMPLE REALTY", score=50, data={"fixers": 3, "resales": 1, "towns": "Newark"}, **kw)
    db.commit()
    return t, own, others


def _flipper_world(db, *, sheriff: bool = False, n: int = 4):
    """A flipper family buying in Newark (deed records) and `n` deals there (listings, or sheriff sales only)."""
    buys = [{"address": f"{i} SAMPLE AVE", "municipality": "NEWARK CITY", "county": "Essex", "mun_code": "0714",
             "price": 400000, "deed_date": (date.today() - timedelta(days=60 + i)).isoformat(), "units": 1,
             "block": "900", "lot": str(i + 1)} for i in range(6)]
    db.add(CashBuyer(entity_key="RIVERHOMES", entity_name="RIVER HOMES LLC", mailing_address="9 RIVER RD",
                     cash_purchases_count=6, recent_purchases_json=buys, last_purchase_date=date.today()))
    db.add(Prospect(key="RIVERHOMESLLC", entity_name="RIVER HOMES LLC", email="deals@riverhomes.example",
                    stage="NEW", purchases_36m=6))
    deals = []
    for _ in range(n):
        if sheriff:
            deals.append(make_lead(db, sheriff_days=20, upset=150000, assumptions={"quant": {"buyer_max": 420000}}))
        else:
            deals.append(make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000,
                                   assumptions={"quant": {"buyer_max": 420000}}, listing_text="needs work, as-is"))
    db.commit()
    return deals


# -- Touch 1 ------------------------------------------------------------------------------------------------------
def test_agent_touch1_is_two_plain_sentences_with_one_street_and_no_attachment(live, tmp_path):
    db = live
    t, own, others = _agent_world(db)
    r = s2.STREAM.run(db, _ctx(tmp_path))
    ob = next(o for o in r.outbound if o.target_id == t.id)
    assert ob.key == f"s2:pitch:{t.id}:2026-W46" and ob.kind == "pitch" and ob.priority == PRIORITY_PITCH
    assert ob.attachments == [] and ob.commercial and ob.in_reply_to is None
    assert ob.body.startswith("Hi Pat,\n\n") and hc.lint(ob.body) == []
    sents = _sentences(ob.body)
    assert len(sents) == 2 and hc.word_count(_text(ob.body)) <= 45, sents
    assert sents[0] == "I saw your 3 fixer listings and 1 investor resale around Newark."
    named = [ld for ld in others if _has_street(ob.body, _street(ld))]
    assert len(named) == 1 and not _has_street(ob.body, _street(own))          # never the agent's own listing
    assert re.findall(r"\$[\d,]+[KkMm]?", ob.body) == ["$450,000"] and "$450,000 ask" in ob.body   # the public ask only
    assert "and 3 other fixer listings in Essex County into this week's investor sheet; want the 1-page sheet?" in ob.body
    low = ob.body.lower()
    for word in ("commission", "a month", "trial", "attached", "http", "free", "ad:"):
        assert word not in low
    assert ob.subject == f"{_street(named[0])} + 3 more in Essex County" and "!" not in ob.subject
    assert r.metrics["pitches"] == 1 and r.metrics["followups"] == 0


def test_the_sample_waits_for_a_yes_masked_after_two_rows_with_the_flat_price(live, tmp_path):
    db = live
    t, own, others = _agent_world(db)
    ob = next(o for o in s2.STREAM.run(db, _ctx(tmp_path)).outbound if o.target_id == t.id)
    assert len(ob.sample_attachments) == 1
    sample = ob.sample_attachments[0]
    full = sample.parent.parent / sample.name.replace(".sample", "")
    assert sample.exists() and full.exists() and sample == sample_path(full)
    stext, ftext = _pdf(sample), _pdf(full)
    streets = [_street(ld) for ld in others]
    assert all(_has_street(ftext, s) for s in streets)                      # the weekly digest: every row in full
    assert sum(_has_street(stext, s) for s in streets) == 2                 # the sample: 2 rows in full ...
    # ... the rest with the street hidden (the bullet is WinAnsi code 0x7F in Helvetica; pypdf hands back that code)
    assert re.search("[•\x7f]{2}", stext) and "(sample)" in stext
    agents = [f"lee{i}@broker.example" for i in range(len(others))]
    assert sum(a in ftext for a in agents) == 4 and sum(a in stext for a in agents) == 2   # listing agents hidden too
    assert not _has_street(stext, _street(own)) and not _has_street(ftext, _street(own))
    assert SAMPLE_LINE in ob.sample_body and hc.lint(ob.sample_body) == []
    assert "the first 2 deals in full, the other 2 with the street hidden" in ob.sample_body
    assert "for your buyer clients to approve" in ob.sample_body and ob.sample_body.startswith("Hi Pat,")
    # through the outbox: Touch 1 queued with nothing attached, the sample held for their yes
    row = fo.enqueue(db, ob, TUE)
    db.commit()
    held = db.query(FirmOutbox).filter_by(key=f"{ob.key}:sample").one()
    assert row.status == "queued" and row.attachments_json == []
    assert held.status == "held" and [a["name"] for a in held.attachments_json] == [sample.name]


def test_never_names_a_house_we_have_an_offer_on_or_one_under_a_suppression(live, tmp_path):
    db = live
    from engine import deal_desk as dd
    from engine.compliance import suppress_address
    t, _own, others = _agent_world(db, n_others=5)
    others[0].offer_amount = 300000                                        # our own live negotiation
    p = others[1].property
    suppress_address(db, f"{p.street_address}, {p.city} NJ {p.zip_code}", source="daniels_law")
    db.commit()
    ob = next(o for o in s2.STREAM.run(db, _ctx(tmp_path)).outbound if o.target_id == t.id)
    assert not _has_street(ob.body, _street(others[0])) and not _has_street(ob.body, _street(others[1]))
    assert sum(_has_street(ob.body, _street(ld)) for ld in others[2:]) == 1
    assert not _has_street(_pdf(ob.sample_attachments[0]), _street(others[1]))
    # the guard itself, even on a sheet built without the pool's own suppression filter
    prof, mine = s2.agent_profile(db, t)
    pool = {ld.id: row for ld in others[1:2] + others[3:4] if (row := dd.deal_row(db, ld))}
    d = dd.Digest(prof, sorted(pool.values(), key=lambda r: r.lead_id != others[1].id))
    assert d.rows[0].lead_id == others[1].id
    assert s2.touch_facts(db, t, d, mine).street == _street(others[3])


def test_a_flipper_hears_what_the_deed_records_show_and_a_sheriff_sale_shows_no_debt(live, tmp_path):
    db = live
    deals = _flipper_world(db, sheriff=True)
    r = s2.STREAM.run(db, _ctx(tmp_path))
    t = db.query(FirmTarget).filter_by(email="deals@riverhomes.example").one()
    ob = next(o for o in r.outbound if o.target_id == t.id)
    assert t.kind == "flipper" and ob.attachments == [] and ob.body.startswith("Hello,\n\n")
    sents = _sentences(ob.body)
    assert len(sents) == 2 and hc.word_count(_text(ob.body)) <= 45 and hc.lint(ob.body) == []
    assert sents[0] == "I saw in the public deed records that River Homes LLC has been buying around Newark."
    assert "$" not in ob.body                                              # an upset is a defendant's debt figure
    m = re.search(r"underwrote (.+?) \(sheriff sale (\d\d/\d\d)\) plus 3 other sheriff sales in your towns this week",
                  ob.body)
    assert m and m.group(1) in {_street(ld) for ld in deals}
    sale = (date.today() + timedelta(days=20)).strftime("%m/%d")
    assert m.group(2) == sale and "QUINN" not in ob.body.upper()            # never the defendant
    assert SAMPLE_LINE in ob.sample_body and "for you to approve" in ob.sample_body


def test_a_flipper_is_never_shown_a_house_it_owns(live, tmp_path):
    db = live
    deals = _flipper_world(db)
    mine = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000, block="900", lot="1",
                     assumptions={"quant": {"buyer_max": 445000}}, listing_text="needs work, as-is")
    db.commit()
    r = s2.STREAM.run(db, _ctx(tmp_path))
    ob = next(o for o in r.outbound if o.to == "deals@riverhomes.example")
    assert not _has_street(ob.body, _street(mine)) and not _has_street(_pdf(ob.sample_attachments[0]), _street(mine))
    assert sum(_has_street(ob.body, _street(ld)) for ld in deals) == 1


def test_the_follow_up_is_the_outboxs_nudge_in_the_same_thread_and_a_buyer_note_is_no_touch(live, tmp_path):
    """2026-11-10 (the B2B playbook): s2 proposes Touch 1 only; the outbox sends the nudge (day 3-4) and the
    close-the-loop note (day 9-10) in the prospect's own thread. An s1 buy-box note leaves the sequence untouched."""
    from engine.crm import TZ
    db = live
    t, _own, _others = _agent_world(db)
    first = next(o for o in s2.STREAM.run(db, _ctx(tmp_path)).outbound if o.target_id == t.id)
    row = fo.enqueue(db, first, TUE)
    row.status, row.message_id, row.sent_at = "sent", "<t1@desk.example>", utcnow() - timedelta(days=7)
    t.touches, t.stage, t.last_contact_at = 1, "CONTACTED", TUE
    db.commit()
    later = TUE + timedelta(days=7)
    r = s2.STREAM.run(db, _ctx(tmp_path / "later", now=later))
    assert [o for o in r.outbound if o.target_id == t.id] == []           # in a sequence: no second Touch 1
    assert fo.queue_nudges(db, later.replace(tzinfo=TZ)) == 1
    db.commit()
    nudge = db.query(FirmOutbox).filter_by(target_id=t.id, kind="followup").one()
    assert nudge.in_reply_to == "<t1@desk.example>" and nudge.subject == "Re: " + first.subject
    assert nudge.attachments_json == [] and "deal sheet" in nudge.body
    # a buy-box note from stream 1 is not a touch: the target still gets the full first note
    t2, *_ = _agent_world_other(db)
    t2.touches, t2.stage, t2.last_contact_at = 0, "CONTACTED", TUE
    db.commit()
    r = s2.STREAM.run(db, _ctx(tmp_path / "other", now=later))
    ob2 = next(o for o in r.outbound if o.target_id == t2.id)
    assert ob2.kind == "pitch" and len(_sentences(ob2.body)) == 2


def _agent_world_other(db):
    ld = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000,
                   assumptions={"quant": {"buyer_max": 420000}}, listing_text="needs work, as-is")
    add_contact(db, ld, contact_type=ContactType.LISTING_AGENT, name="Kim Seller", phone=None,
                email="kim@broker.example")
    t = fo.upsert_target(db, kind="investor_agent", stream="s2", email="kim@broker.example", name="Kim Seller",
                         score=40, data={"fixers": 1, "towns": "Newark"})
    db.commit()
    return t, ld


def test_targets_in_stream_1s_buyer_conversation_or_with_mail_queued_wait(live, tmp_path):
    db = live
    t, _own, _others = _agent_world(db)
    db.add(FirmOutbox(key=f"s1:buyers:{t.id}:2026-W45", stream="s1", priority=PRIORITY_PITCH, kind="pitch",
                      target_id=t.id, to_email=t.email, subject="As-is houses in Essex County", body="x",
                      status="sent", sent_at=utcnow() - timedelta(days=8), message_id="<b@desk.example>"))
    db.commit()
    assert s2.due_targets(db, TUE, 5, 6, 2) == []
    assert s2.buyer_note_targets(db, TUE, 30) == {t.id}
    db.query(FirmOutbox).delete()
    db.add(FirmOutbox(key="s6:pitch:x", stream="s6", priority=3, kind="pitch", target_id=t.id, to_email=t.email,
                      subject="x", body="x", status="queued"))
    db.commit()
    assert s2.due_targets(db, TUE, 5, 6, 2) == []                          # it would only be skipped by the outbox


def test_pitch_body_claims_only_what_the_records_show():
    # (a person filed as the company is covered in tests/test_firm.py)
    from types import SimpleNamespace
    f = s2.TouchFacts(street="12 Test St", price="$389,900 ask", others=4, mix="fixer listings and sheriff sales",
                      where="in your towns", sheet_for="Bergen County", near="Rutherford",
                      subject_where="in Bergen County", rows=5)
    shop = SimpleNamespace(kind="flipper", org="SAMPLE HOME BUYERS", name=None, data_json={})
    subject, body = s2.pitch_body(shop, f, {"first": "Sam"}, follow=False)
    assert subject == "12 Test St + 4 more in Bergen County" and body.startswith("Hello,\n\n")
    assert "that Sample Home Buyers has been buying around Rutherford." in body and body.endswith("\n\nSam")
    agent = SimpleNamespace(kind="investor_agent", org="Sample Realty", name="Coldwell Banker Realty", data_json={})
    _, body = s2.pitch_body(agent, f, {"first": "Sam"}, follow=False)
    assert body.startswith("Hello,\n\nI saw your listings around Rutherford.")   # no count known, no claim made
    resale = SimpleNamespace(kind="investor_agent", org=None, name="Pat Agent", data_json={"resales": 2})
    _, body = s2.pitch_body(resale, f, {"first": "Sam"}, follow=False)
    assert "I saw you're listing 2 investor resales around Rutherford." in body
    _, nudge = s2.pitch_body(resale, f, {"first": "Sam"}, follow=True)
    assert nudge == "Hi Pat,\n\nShould I send over this week's 1-page numbers sheet for Bergen County?\n\nSam"


def test_deal_mix_and_counties_say_only_what_the_rows_are():
    from types import SimpleNamespace as R
    assert s2.deal_mix([R(kind="MLS fixer"), R(kind="MLS fixer")]) == "fixer listings"
    assert s2.deal_mix([R(kind="MLS listing"), R(kind="Sheriff sale")]) == "stale listings and sheriff sales"
    assert s2.deal_mix([R(kind="MLS fixer"), R(kind="MLS listing"), R(kind="Pre-foreclosure")]) == \
        "stale and fixer listings and pre-foreclosures"
    assert s2.county_phrase(["Union"]) == "Union County"
    assert s2.county_phrase(["Essex", "Union"]) == "Essex and Union counties"
    assert s2.greeting("Pat Agent") == "Hi Pat," and s2.greeting("Keller Williams Team") == "Hello,"
    assert s2.greeting(None) == "Hello,"


def test_a_researched_flipper_or_brokerage_gets_a_stated_area_sheet_and_an_honest_hook(live, tmp_path):
    """A target from config/b2b_targets.yaml with no deed or listing records of ours: the county printed on their own
    site sets the area; the copy says only how we found them, never "your deed records" or "your listings"."""
    db = live
    for _ in range(4):
        ld = make_lead(db, sheriff_days=None, stale_listing=True, list_price=450000,
                       assumptions={"quant": {"buyer_max": 420000}}, listing_text="needs work, as-is")
        add_contact(db, ld, contact_type=ContactType.LISTING_AGENT, name="Lee Broker", phone=None,
                    email=f"lee{ld.id}@broker.example")
    flip = fo.upsert_target(db, kind="flipper", stream="s2", email="deals@samplebuyers.example",
                            org="Sample Home Buyers LLC", county="Essex", score=30)
    brok = fo.upsert_target(db, kind="investor_friendly_broker", stream="s2", email="info@sampleinvest.example",
                            org="Sample Investment Realty", county="Essex County", score=20)
    db.commit()
    res = s2.STREAM.run(db, _ctx(tmp_path))
    by = {o.target_id: o for o in res.outbound}
    assert flip.id in by and brok.id in by
    f, b = by[flip.id], by[brok.id]
    assert "I came across Sample Home Buyers LLC while putting together this week's investor sheet" in f.body
    assert "deed records" not in f.body and "numbers sheet" in f.body
    assert "I came across Sample Investment Realty" in b.body and "into it; want the 1-page sheet?" in b.body
    assert "underwrote" not in b.body                                     # an agent never hears our numbers
    for o in (f, b):
        assert o.attachments == [] and len(_sentences(o.body)) == 2 and hc.lint(o.body) == []
    from pypdf import PdfReader
    btext = PdfReader(str(b.sample_attachments[0])).pages[0].extract_text().replace("\n", " ")
    assert "Max buy" not in btext and "ARV" not in btext                  # the broker's sheet is the agent sheet
    ftext = PdfReader(str(f.sample_attachments[0])).pages[0].extract_text().replace("\n", " ")
    assert "Towns:" in ftext and "recorded deeds" not in ftext.split("Max buy")[0]
