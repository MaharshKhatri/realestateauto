"""Listed-foreclosure fast track (tracks, offers, scripts, packets) and portfolio-owner pitch/letters."""
import json
from datetime import date, timedelta

import pytest

from core.config import settings
from db.models import SignalType
from db.repository import upsert_signal
from engine import human_copy as hc
from engine import agent_outreach as ao
from engine import briefing, crm
from engine import direct_mail as dm
from engine import fast_track as ft
from tests.factories import add_contact, make_lead

BANNED = ("stop the sale", "postpone the sale", "negotiate with your bank", "drive by", "stop by the house")


@pytest.fixture()
def operator(monkeypatch):
    for k, v in {"name": "Jordan Operator", "phone": "9735550199", "mailing_address": "PO Box 10, Newark, NJ 07101",
                 "company_name": "Garden State Home Buyers LLC"}.items():
        monkeypatch.setitem(settings()["operator"], k, v)


def listed(db, list_price=360_000, **kw):
    lead = make_lead(db, stale_listing=True, list_price=list_price, sheriff_days=kw.pop("sheriff_days", 12), **kw)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test",
                company="Garden Realty")
    return lead


def test_tracks_follow_the_numbers_and_frfpa(db):
    presumed = listed(db, absentee=True)
    assert ft.is_listed_foreclosure(presumed.property)
    assert ft.deal_track(presumed)[0] == "CONFIRM_OCCUPANCY"
    crm.confirm_occupancy(db, presumed, "VACANT", "listing agent on 10/27 call")
    assert ft.deal_track(presumed)[0] == "CASH_OFFER"
    covered = listed(db)                                       # presumed owner-occupied → FRFPA applies
    assert ft.deal_track(covered)[0] == "FRFPA_FLOOR_OFFER"
    assert ft.deal_track(listed(db, verdict="SHORT_SALE_ONLY", absentee=True))[0] == "SHORT_SALE"
    assert ft.deal_track(listed(db, verdict="EQUITY_RESCUE_REFERRAL"))[0] == "EQUITY_MONITOR"
    assert ft.deal_track(listed(db, verdict="FRFPA_BLOCKED"))[0] == "NO_OFFER"
    assert ft.deal_track(listed(db, verdict=None))[0] == "NO_OFFER"


def test_offers_capped_floor_respected_and_far_below_ask_demoted(db):
    vacant = listed(db, absentee=True, list_price=240_000)
    crm.confirm_occupancy(db, vacant, "VACANT", "agent")
    anchor, best, floor = ft.offer_numbers(vacant)
    assert best <= 240_000 and anchor <= round(240_000 * 0.9, -2) and floor is None
    covered = listed(db)
    a2, b2, f2 = ft.offer_numbers(covered)
    assert f2 and a2 >= f2 and b2 >= a2                       # never below 82% of FMV
    far = listed(db, absentee=True, list_price=1_000_000)
    crm.confirm_occupancy(db, far, "TENANT_OCCUPIED", "agent")
    lf = ft.listed_foreclosure(db, far)
    assert lf.track == "NO_OFFER" and "re-check comps" in lf.reasons[0] and lf.anchor is None


def test_queue_order_countdown_and_script(db, operator):
    short = listed(db, verdict="SHORT_SALE_ONLY", absentee=True, sheriff_days=5)
    cash = listed(db, absentee=True, sheriff_days=30)
    crm.confirm_occupancy(db, cash, "VACANT", "agent")
    past = listed(db, absentee=True, sheriff_days=-3)
    q = ft.listed_foreclosures(db)
    ids = [r.lead_id for r in q]
    assert ids.index(cash.id) < ids.index(short.id) and past.id not in ids
    lf = q[ids.index(cash.id)]
    assert lf.days_to_sale == 30 and lf.upset == 150_000 and lf.list_price == 360_000 and lf.dom == 140
    sc = ft.agent_script(lf)
    text = " ".join([sc["opening"], sc["offer"], sc["close"], *sc["questions"]]).lower()
    sale = hc.short_date(date.today() + timedelta(days=30)).lower()
    assert "unrepresented" in text and f"sheriff sale list for {sale}" in text and "around $150,000 owed" in text
    assert not any(b in text for b in BANNED)
    ss = ft.agent_script(q[ids.index(short.id)])
    assert "lender" in ss["offer"] and "stays with you and the seller's attorney" in ss["offer"]
    rows = briefing.listed_rows(db)
    assert {r["lead_id"] for r in rows} == {cash.id, short.id}


def test_packets_offer_vs_inquiry_and_agent_blast_delegation(db, tmp_path, operator):
    cash = listed(db, absentee=True)
    crm.confirm_occupancy(db, cash, "VACANT", "agent")
    p = ft.build_packet(db, cash, out_root=tmp_path)
    assert p.kind == "offer" and p.track == "CASH_OFFER" and p.loi_files["pdf"].endswith("loi.pdf")
    assert p.sale_date == (date.today() + timedelta(days=12)).isoformat() and p.upset == 150_000
    assert p.subject in (f"{hc.street(p.address)} (saw the {hc.short_date(date.today() + timedelta(days=12))} sale date)",
                         f"quick question re: {hc.street(p.address)}")
    assert "can't stop or postpone the sale" in p.email_body and hc.lint(p.email_body) == []
    assert "not asking for any part of it" in p.email_body and "STOP" in p.sms_text and len(p.phone_script) == 3
    confirm = listed(db, absentee=True)
    q = ft.build_packet(db, confirm, generate_loi=False)
    assert q.kind == "inquiry" and q.anchor is None and "living there" in q.email_body
    delegated = ao.build_packet(cash, generate_loi=False, session=db)
    assert delegated.track == "CASH_OFFER" and delegated.days_to_sale == 12
    assert ao.record_sent(db, cash, delegated, "email") == "OFFER_SENT_TO_AGENT"


def portfolio(db, owner="BRICK CITY HOLDINGS LLC"):
    leads = [make_lead(db, sheriff_days=None, absentee=True, owner_name=owner, owner_mailing_address="1 OCEAN DR",
                       owner_city_state_zip="MIAMI, FL 33101") for _ in range(3)]
    ids = sorted(lead.property_id for lead in leads)
    for lead in leads:
        upsert_signal(db, lead.property, signal_type=SignalType.PORTFOLIO_OWNER, source="signal_stacker",
                      source_ref=f"portfolio|{lead.property_id}",
                      raw_metadata_json={"properties": 3, "property_ids": ids, "owner": owner,
                                         "mailing": "1 OCEAN DR, MIAMI, FL 33101"})
    db.flush()
    return leads


def test_portfolio_summary_script_and_letters(db, tmp_path, operator):
    leads = portfolio(db)
    ps = ft.portfolios(db)
    assert len(ps) == 1 and ps[0].size == 3 and ps[0].owner == "BRICK CITY HOLDINGS LLC"
    opener = ft.portfolio_script(ps[0])["opening"]
    assert all(lead.property.street_address.title().split()[0] in opener for lead in leads)
    assert "not an agent" in opener
    out, letters = ft.write_portfolio_batch(db, 5, tmp_path / "pf")
    assert len(letters) == 1 and letters[0].kind == "portfolio" and letters[0].address_lines[0] == "1 Ocean Dr"
    assert (out / "portfolio.pdf").read_bytes().startswith(b"%PDF")
    manifest = json.loads((out / "manifest.json").read_text())
    assert sorted(x["lead_id"] for x in manifest["letters"]) == sorted(lead.id for lead in leads)
    assert dm.mark_mailed(db, out) == 3
