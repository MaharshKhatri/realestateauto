"""The daily call sheet (2026-11-03: "I can make ... like 100 a day"): tiers, one row per phone, one number per row,
frozen codes, the block script, calendar blocks around classes, and the codes the operator reports in the chat."""
from datetime import date, datetime, timedelta

import pytest

from core.config import settings
from db.models import Interaction, SwarmState
from engine import call_sheet as cs
from engine import gcal_sync as gs
from tests.factories import add_contact, make_lead

NOW = datetime(2026, 11, 4, 8, 0)          # a Wednesday


def listed(db, list_price=340_000, phone="9735550100", email="pat@broker.test", **kw):
    kw.setdefault("assumptions", {"sqft_source": "record"})
    lead = make_lead(db, sheriff_days=None, stale_listing=True, list_price=list_price, **kw)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone=phone, email=email,
                company="Garden Realty")
    return lead


@pytest.fixture()
def operator(monkeypatch):
    monkeypatch.setitem(settings()["operator"], "name", "Jordan Operator")
    monkeypatch.setitem(settings()["operator"], "phone", "7325550100")
    monkeypatch.setitem(settings()["operator"], "mailing_address", "PO Box 10, Edison, NJ 08817")


@pytest.mark.production_gates
def test_rows_carry_one_number_one_phone_and_frozen_codes(db, operator):
    firm = listed(db, list_price=340_000)                               # final 285K = 84%: a firm number
    ask = listed(db, list_price=400_000, phone="9735550111", email="sam@broker.test")   # 71%: ask theirs
    far = listed(db, list_price=600_000, phone="9735550122", email="kim@broker.test")   # 48%: not called
    twin = listed(db, list_price=345_000)                               # same agent phone as `firm`: same row
    fax = listed(db, list_price=340_000, phone="9735550133", email=None)
    fax.contacts[0].phone_type = "fax"
    db.flush()
    rows = cs.build(db, NOW)
    by_lead = {r.lead_id: r for r in rows}
    assert far.id not in by_lead and fax.id not in by_lead
    r = by_lead[firm.id]
    assert r.tier == "T3" and r.say == "OFFER $280,000 · best and final $285,000" and twin.id in r.lead_ids
    assert len(r.streets) == 2 and r.code == "A01"
    a = by_lead[ask.id]
    assert a.tier == "T4" and a.say.startswith("ASK THEIR NUMBER") and "$285,000" in a.say and a.offer is None
    assert "(973) 555-0111" in a.line() and f"lead {ask.id}" in a.line()
    # frozen: a re-run the same day returns the same codes even when the pool changes
    first = cs.today(db, NOW)
    listed(db, list_price=330_000, phone="9735550144", email="lee@broker.test")
    again = cs.today(db, NOW + timedelta(hours=3))
    assert [(x.code, x.lead_id) for x in again] == [(x.code, x.lead_id) for x in first]
    assert cs.lead_ids_on(db, NOW.date()) >= {firm.id, twin.id, ask.id}


def test_capacity_blocks_and_sunday(db):
    assert cs.capacity(date(2026, 11, 4)) == 100 and cs.capacity(date(2026, 11, 7)) == 25
    assert cs.capacity(date(2026, 11, 8)) == 0
    script = "\n".join(cs.block_script("B"))
    assert "B01 vm" in script and "Never: go above the best and final" in script and "Voicemail:" in script
    assert "not a realtor" in script and "assign the contract" in script and "drive by" not in script.lower()


def test_blocks_fit_around_classes():
    d = date(2026, 11, 4)
    busy = [(datetime(2026, 11, 4, 10, 20), datetime(2026, 11, 4, 11, 40)),
            (datetime(2026, 11, 4, 12, 25), datetime(2026, 11, 4, 13, 20)),
            (datetime(2026, 11, 4, 14, 0), datetime(2026, 11, 4, 15, 20))]
    placed = gs.place_blocks(d, cs.blocks(), busy)
    assert placed["A"] == (datetime(2026, 11, 4, 9, 30), 50)            # shrinks to end before the class
    assert placed["B"] == (datetime(2026, 11, 4, 11, 40), 45)
    assert placed["C"][0] == datetime(2026, 11, 4, 15, 20)               # 13:30-14:00 is too short: after class
    assert placed["D"][0] >= placed["C"][0] + timedelta(minutes=placed["C"][1])
    for start, mins in placed.values():
        end = start + timedelta(minutes=mins)
        assert not any(a < end and b > start for a, b in busy)


@pytest.mark.production_gates
def test_calendar_shows_four_blocks_and_no_second_event_for_sheet_leads(db, operator):
    lead = listed(db)
    lead.next_followup_at = datetime(2026, 11, 4, 13, 36)                # a 48-hour nudge due today
    db.flush()
    cs.today(db, NOW)
    want = gs.desired_events(db, now=NOW)
    blocks = [d for k, d in want.items() if k.startswith("callblock:20261104:")]
    assert len(blocks) == 1 and blocks[0].body["summary"].startswith("📞 Call block A: 1 calls")
    text = blocks[0].body["description"]
    assert "--- Script for every call in this block" in text and "A01 |" in text and "OFFER $280,000" in text
    assert not any(k.startswith("followup:") and d.lead_id == lead.id for k, d in want.items())


@pytest.mark.production_gates
def test_reported_codes_log_calls_send_offers_and_stop_numbers(db, operator, monkeypatch):
    from engine import autopilot, compliance
    from engine import gmail_autopilot as gm
    firm = listed(db, list_price=340_000)
    twin = listed(db, list_price=345_000)                                # same phone: same row as firm
    ask = listed(db, list_price=400_000, phone="9735550111", email="sam@broker.test")
    stop = listed(db, list_price=410_000, phone="9735550122", email="kim@broker.test")
    db.flush()
    rows = {r.lead_id: r for r in cs.today(db, NOW)}
    sent, replies = [], []
    monkeypatch.setattr(autopilot, "send", lambda s, lead_id, channel, **kw: sent.append(lead_id) or {"kind": "touch2"})
    monkeypatch.setattr(gm, "send_custom_reply", lambda s, lead_id, text, price=None, **kw:
                        replies.append((lead_id, price, text)) or {})
    when = datetime(2026, 11, 4, 9, 40)
    items = [{"code": rows[firm.id].code, "result": "send"},
             {"code": rows[ask.id].code, "result": "270"},               # their number, under our 285K final
             {"code": rows[stop.id].code, "result": "stop"},
             {"code": "Z99", "result": "vm"}]
    out = cs.apply_codes(db, items, now=when)
    assert out["unknown"] == 1 and sent == [firm.id]
    assert replies and replies[0][0] == ask.id and replies[0][1] == 270_000      # the written offer at their number
    assert "dnc" in compliance.classify_number(db, "9735550122", b2b=True)
    outcomes = {i.lead_id: i.outcome for i in db.query(Interaction).all()}
    assert outcomes[firm.id] == "SPOKE_NEEDS_OFFER" and outcomes[stop.id] == "DNC" and twin.id not in outcomes
    assert all((i.debrief_json or {}).get("source") == "chat_note" for i in db.query(Interaction).all())


@pytest.mark.production_gates
def test_a_number_on_uncertain_data_is_held_until_fresh_comps_confirm_it(db, operator, monkeypatch):
    from engine import gmail_autopilot as gm
    from engine import offer_math
    lead = listed(db, list_price=400_000, assumptions={})                # living area not on record: uncertain
    db.flush()
    row = cs.today(db, NOW)[0]
    calls = []
    monkeypatch.setattr(offer_math, "firm_up", lambda s, lead, **kw: calls.append(lead.id) or
                        offer_math.firm_offer(lead, s))                   # the live source found nothing new
    monkeypatch.setattr(gm, "send_custom_reply", lambda *a, **kw: pytest.fail("nothing goes in writing"))
    out = cs.apply_codes(db, [{"code": row.code, "result": "270 needs a roof"}], now=NOW)
    assert out["held"] == 1 and calls == [lead.id]
    assert db.query(Interaction).one().outcome == "SPOKE_NEEDS_OFFER"


def test_amounts_read_like_the_operator_types_them():
    assert cs.parse_amount("340") == 340_000 and cs.parse_amount("469.9") == 469_900
    assert cs.parse_amount("$1.2m") == 1_200_000 and cs.parse_amount("450k") == 450_000
    assert cs.parse_amount("vm") is None and cs.parse_amount("3") is None


def test_the_cloud_desk_applies_codes_from_a_sealed_note(db, monkeypatch):
    from engine import cloud_desk as cd
    seen = []
    monkeypatch.setattr(cs, "apply_codes", lambda s, items, **kw: seen.extend(items) or {"logged": len(items)})
    key = "k" * 32
    note = cd.seal_note([{"code": "A01", "result": "vm"}, {"code": "A02", "result": "na"}], cd.public_hex(key))
    out = cd.apply_note(note, key)
    assert out["note_done"] == 2 and [i["code"] for i in seen] == ["A01", "A02"]
    assert db.get(SwarmState, "call_sheet") is None
