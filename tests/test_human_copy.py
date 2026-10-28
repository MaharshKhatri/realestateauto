"""Every outgoing e-mail and SMS reads like a person wrote it: short, plain, no stock phrases, no em-dashes, no
markdown, and every required disclosure present under the summary line below the signature."""
from datetime import date, datetime, timedelta

import pytest

from core.config import settings
from db.models import CashBuyer
from engine import agent_outreach as ao
from engine import crm
from engine import disposition as dp
from engine import fast_track as ft
from engine import human_copy as hc
from engine import inbound as ib
from engine import virtual_inspector as vi
from tests.factories import add_contact, make_lead


@pytest.fixture(autouse=True)
def operator(monkeypatch):
    for k, v in {"name": "Jordan Operator", "phone": "9735550199", "mailing_address": "PO Box 10, Newark, NJ 07101",
                 "company_name": "Garden State Home Buyers LLC", "email": "jordan@example.test"}.items():
        monkeypatch.setitem(settings()["operator"], k, v)


def listed(db, **kw):
    lead = make_lead(db, stale_listing=True, list_price=kw.pop("list_price", 360_000),
                     sheriff_days=kw.pop("sheriff_days", 12), **kw)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test",
                company="Garden Realty")
    return lead


def check_email(text: str, *, summary: str = hc.DIVIDER) -> tuple[str, str]:
    """The DoD rules: < 110 words above the signature, no forbidden phrase, no em-dash, no '**'."""
    assert hc.lint(text) == [], (hc.lint(text), text)
    head, tail = hc.split_signature(text)
    assert hc.word_count(head) < 110
    assert "—" not in text and "**" not in text
    for phrase in hc.FORBIDDEN:
        assert phrase not in text.lower(), phrase
    assert summary in tail, "disclosures belong under the summary line below the signature"
    assert "Jordan Operator | Garden State Home Buyers LLC" in tail and "(973) 555-0199" in tail
    return head, tail


def check_sms(text: str) -> None:
    assert hc.lint(text, sms=True) == [], (hc.lint(text, sms=True), text)


def test_lint_catches_spam_tells():
    assert hc.lint("Hi Pat,\n\nI hope this email finds you well — a great opportunity!") == [
        "em-dash", "forbidden phrase: 'i hope this email finds you well'", "forbidden phrase: 'opportunity'"]
    assert "markdown" in hc.lint("Hi,\n\n**Price:** $200,000")
    assert "bullet list above the signature" in hc.lint("Hi,\n\n• 3 bed\n• 2 bath\n\nThanks,\nJordan")
    long = "Hi,\n\n" + "word " * 120 + "\n\nThanks,\nJordan"
    assert any(p.startswith("121 words") for p in hc.lint(long))
    assert hc.lint("Hi Pat, quick question on 12 Main St. Reply STOP to opt out.", sms=True) == []
    assert hc.lint("Hi Pat, quick question.", sms=True) == ["SMS has no STOP opt-out"]
    assert hc.lint("We are excited to offer this formal letter of intent.") == [
        "forbidden phrase: 'excited'", "forbidden phrase: 'formal'", "forbidden phrase: 'letter of intent'"]


def test_helpers():
    assert hc.street("12 SAMPLE ST, Newark") == "12 Sample St" and hc.street("306 13TH STREET") == "306 13th Street"
    assert hc.first_name("PAT AGENT") == "Pat" and hc.first_name("") == "there"
    assert hc.pick(["a", "b"], 3) == "b" and hc.pick(["a", "b"], 3) == hc.pick(["a", "b"], 3)
    assert hc.plain("one — two **three**") == "one, two three"
    assert hc.short_date(date(2026, 11, 12)) == "Nov 12"


def test_stale_listing_offer_follows_the_template(db, tmp_path):
    lead = listed(db, sheriff_days=None)
    p = ao.build_packet(lead, session=db, out_root=tmp_path)
    st = hc.street(p.address)
    assert p.subject in (f"question on {st}", f"{st} - still available?")
    head, tail = check_email(p.email_body)
    assert head.startswith("Hi Pat,\n\nQuick question on your listing at") and "in Newark" in head
    assert "on the market about 140 days" in head and "$228,800 to $260,000" in head
    assert "not working with a buyer's agent" in head and "handle both sides if your listing agreement allows" in head
    assert "send over the written offer today" in head
    # Everything required, under the line: terms, unrepresented-buyer wording, may-assign, postal address, opt-out.
    for needed in ("Option 1: $228,800 cash", "Option 2: $260,000", "3-business-day attorney review",
                   "not asking for any part of it", "not licensed agents", "may assign the contract",
                   "PO Box 10, Newark", 'Reply "no more"'):
        assert needed in tail, needed
    assert tail.index(hc.DIVIDER) > tail.index("Jordan Operator")
    check_sms(p.sms_text)
    assert p.sms_text.startswith("Hi Pat, it's Jordan with Garden State Home Buyers LLC")
    assert len(p.phone_script) == 3


def test_stale_listing_inquiry_when_occupancy_unknown(db):
    # In foreclosure with only a presumed FRFPA exemption: no price until the agent confirms who lives there.
    lead = listed(db, absentee=True, sheriff_days=20)
    p = ao.build_packet(lead, generate_loi=False)
    assert p.kind == "inquiry" and p.anchor is None
    head, tail = check_email(p.email_body, summary=hc.DETAILS)
    assert "living there" in head and "$" not in head and "No price yet" in tail
    check_sms(p.sms_text)


@pytest.mark.parametrize("track", ["CASH_OFFER", "FRFPA_FLOOR_OFFER", "CONFIRM_OCCUPANCY", "SHORT_SALE",
                                   "EQUITY_MONITOR", "NO_OFFER"])
def test_listed_foreclosure_every_track(db, track):
    kw = {"CASH_OFFER": {"absentee": True}, "FRFPA_FLOOR_OFFER": {}, "CONFIRM_OCCUPANCY": {"absentee": True},
          "SHORT_SALE": {"absentee": True, "verdict": "SHORT_SALE_ONLY", "upset": 450_000},
          "EQUITY_MONITOR": {"verdict": "EQUITY_RESCUE_REFERRAL"}, "NO_OFFER": {"verdict": "FRFPA_BLOCKED"}}[track]
    lead = listed(db, **kw)
    if track in ("CASH_OFFER", "SHORT_SALE"):
        crm.confirm_occupancy(db, lead, "VACANT", "agent on call")
    p = ft.build_packet(db, lead, generate_loi=False)
    assert p.track == track
    head, tail = check_email(p.email_body, summary=hc.DIVIDER if p.kind == "offer" else hc.DETAILS)
    sale = hc.short_date(date.today() + timedelta(days=12))
    st = hc.street(p.address)
    assert p.subject in (f"{st} (saw the {sale} sale date)", f"quick question re: {st}")
    assert f"I was looking at your listing at {st}" in head
    owed = "$450,000" if track == "SHORT_SALE" else "$150,000"
    assert f"Essex County sheriff sale list for {sale} with around {owed} owed" in head
    assert "(973) 555-0199" in head                                    # "Call or text me at ..."
    if p.kind == "offer":
        assert "$" in head.split("owed")[1] and "Offer:" in tail
    else:
        assert "$" not in head.split("owed")[1]                         # inquiries never name a price
    for needed in ("can't stop or postpone the sale", "not asking for any part of it", "may assign the contract",
                   "PO Box 10"):
        assert needed in tail, needed
    if track == "FRFPA_FLOOR_OFFER":
        assert "10 business days to cancel" in head and "82% of fair market value" in tail
    if track == "SHORT_SALE":
        assert p.kind == "offer"
        assert "submit to the lender" in head and "stays with the agent and the seller's attorney" in tail
    for banned in ("stop the sale", "postpone it", "negotiate with your bank", "we'll negotiate"):
        assert banned not in head.lower()
    check_sms(p.sms_text)
    assert all("—" not in line for line in p.phone_script) and len(p.phone_script) == 3


def test_buyer_invite_walkthrough_and_followup(db):
    lead = make_lead(db, sheriff_days=None, dwellings=2, sqft=2100)
    buyer = CashBuyer(entity_key="RIVER", entity_name="RIVER HOMES LLC", contact_name="Sam Rivera")
    db.add(buyer)
    db.flush()
    slots = [datetime(2026, 11, 3, 11), datetime(2026, 11, 4, 16)]
    inv = dp.buyer_invite(lead, buyer, slots)
    st = hc.street(f"{lead.property.street_address}, Newark")
    assert inv["subject"] == f"off-market fixer in Newark ({st})"
    head, tail = check_email(inv["body"], summary="--- Deal Summary ---")
    assert head.startswith("Hi Sam,") and "2-family in Newark under contract" in head and "Want a slot?" in head
    assert "Tuesday November 3 at 11am" in head
    for needed in ("not a licensed agent", "my contract (equitable interest)", "proof of funds", "PO Box 10", "STOP"):
        assert needed in tail, needed
    assert inv["email"].startswith(f"Subject: {inv['subject']}\n\n")
    check_sms(inv["sms"])
    listed_lead = make_lead(db, sheriff_days=None, stale_listing=True)
    assert dp.buyer_invite(listed_lead, buyer, slots)["subject"].startswith("fixer under contract in Newark")
    req = dp.access_request(lead, "agent", slots)
    assert "—" not in req and hc.lint(req, max_words=120) == []
    assert "my buying partner, the investor I may assign the contract to," in req
    follow = dp.post_walk_followup(lead, buyer)
    assert "—" not in follow and hc.lint(follow) == [] and follow.endswith("Jordan")


def test_inbound_reply_and_photo_requests():
    email, sms = ib.response_drafts("Robin Testcase", "41 Sample Ave, Newark", True)
    subject, _, body = email.partition("\n\n")
    assert subject == "Subject: about 41 Sample Ave"
    head, tail = check_email(body, summary="--- About Us ---")
    assert head.startswith("Hi Robin,") and "not agents" in tail and "assign our contract" in tail
    check_sms(sms)
    req = vi.request_messages("41 Sample Ave, Newark, NJ 07106")
    _, _, photo_body = req["email"].partition("\n\n")
    assert hc.lint(photo_body) == [] and "PO Box 10" in photo_body
    check_sms(req["sms_owner"])
    assert "—" not in req["sms_agent"] and hc.lint(req["sms_agent"]) == []


def test_agent_blast_page_uses_plain_copy(db, tmp_path):
    listed(db, sheriff_days=None)
    index, packets = ao.write_batch(db, 5, tmp_path / "blast")
    assert packets and all(hc.lint(p.email_body) == [] for p in packets)
    assert "Offer Summary" in index.read_text()
