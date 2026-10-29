"""B2B listing-agent offers: price caps, compliant wording, SMTP (fake server), rate limit, logging, opt-out."""
from datetime import datetime

import pytest

from core.config import settings
from engine import agent_outreach as ao
from engine import compliance
from tests.factories import add_contact, make_lead


@pytest.fixture()
def postal(monkeypatch):
    monkeypatch.setitem(settings()["operator"], "mailing_address", "PO Box 10, Newark, NJ 07101")
    monkeypatch.setitem(settings()["operator"], "name", "Jordan Operator")


def listed(db, list_price=360_000, **kw):
    lead = make_lead(db, sheriff_days=None, stale_listing=True, list_price=list_price, **kw)
    agent = add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100",
                        email="pat@broker.test", company="Garden Realty")
    return lead, agent


def test_offer_prices_never_exceed_ask(db):
    lead, _ = listed(db, list_price=240_000)          # MAO 70% (260K) is above this ask
    anchor, best = ao.offer_prices(lead)
    assert best <= 240_000 and anchor <= round(240_000 * 0.90, -2) and anchor <= best
    lead2, _ = listed(db, list_price=360_000)
    anchor2, best2 = ao.offer_prices(lead2)
    assert best2 == 260_000 and anchor2 == 228_800       # walk-away MAO and the anchor, both under ask


def test_packet_wording_and_loi(db, tmp_path, postal):
    lead, agent = listed(db)
    p = ao.build_packet(lead, session=db, out_root=tmp_path)
    assert p.kind == "offer" and p.agent_contact_id == agent.id and p.agent_first == "Pat"
    body = p.email_body
    assert "$228,800 to $260,000" in body and "on the market about 140 days" in body
    assert "not represented by a buyer's agent" in body and "not asking for any part of it" in body
    assert "may assign the contract" in body and "not licensed agents" in body
    assert "attorney review" in body and "PO Box 10" in body and "no more" in body
    assert "STOP" in p.sms_text and p.mailto.startswith("mailto:pat@broker.test?subject=")
    assert p.loi_files["pdf"].endswith("loi.pdf")
    for phrase in ("drive by", "stop by the house", "door-knock", "I'll come see it"):
        assert phrase not in body.lower()


def test_frfpa_presumed_exempt_listing_becomes_inquiry(db, postal):
    # A listed foreclosure with unconfirmed occupancy: the urgent fast-track packet asks who lives there first.
    lead = make_lead(db, absentee=True, stale_listing=True, list_price=360_000)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test")
    p = ao.build_packet(lead, generate_loi=False, session=db)
    assert p.kind == "inquiry" and p.anchor is None and p.track == "CONFIRM_OCCUPANCY"
    assert "living there" in p.email_body and p.email_body.count("Hi Pat") == 1
    assert ao.record_sent(db, lead, p, "email") == lead.status_stage            # an inquiry is not an offer
    assert lead.next_followup_at is not None


class FakeSMTP:
    sent: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_smtp_send_logs_stage_followup_and_limit(db, tmp_path, postal, monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    lead, _ = listed(db)
    p = ao.build_packet(lead, session=db, out_root=tmp_path)
    with pytest.raises(RuntimeError, match="SMTP_USER"):
        ao.send_smtp(db, lead, p, smtp_factory=FakeSMTP)
    monkeypatch.setenv("SMTP_USER", "me@example.test")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "app-password")
    now = datetime(2026, 10, 30, 9, 0)                       # Friday → follow-up Sunday moves to Monday 10:00
    FakeSMTP.sent = []
    stage = ao.send_smtp(db, lead, p, now=now, smtp_factory=FakeSMTP)
    msg = FakeSMTP.sent[0]
    assert stage == "OFFER_SENT_TO_AGENT" and msg["To"] == "pat@broker.test"
    assert any(part.get_filename() and part.get_filename().endswith(".pdf") for part in msg.iter_attachments())
    assert lead.next_followup_at == datetime(2026, 11, 2, 10, 0) and lead.offer_amount == p.anchor
    assert ao.sent_last_24h(db, now) == 1
    monkeypatch.setitem(settings()["outreach"], "daily_email_limit", 1)
    with pytest.raises(RuntimeError, match="daily limit"):
        ao.send_smtp(db, lead, p, now=now, smtp_factory=FakeSMTP)


def test_opt_out_suppresses_agent(db):
    lead, agent = listed(db)
    ao.opt_out(db, agent)
    assert agent.dnc_status == "dnc" and "internal" in compliance.dnc_sources(db, "9735550100")
    assert ao.agent_contact(lead) is None and not ao.eligible(lead)[0]


def test_candidates_and_batch_page(db, tmp_path, postal):
    good, _ = listed(db)
    far, _ = listed(db, list_price=900_000)            # 32% of ask: not a candidate
    db.flush()
    ids = [lead.id for lead in ao.candidates(db, 10)]
    assert good.id in ids and far.id not in ids
    index, packets = ao.write_batch(db, 10, tmp_path / "blast")
    html = index.read_text()
    assert index.exists() and "Listing-agent offers" in html and packets[0].lead_id == good.id


# -- two-step funnel: Touch 1 opener, nudge, Touch 2 --------------------------------------------------------
def test_opener_is_two_lines_with_only_the_required_footer(db, postal):
    from engine import human_copy as hc
    lead, agent = listed(db)
    p = ao.build_opener(lead)
    assert p.kind == "opener" and p.anchor is None and p.best is None and p.loi_files == {}
    body = p.email_body
    question, _, footer = body.partition("Thanks,")
    assert question.startswith("Hi Pat,") and "attorney review" in question and "available" in question
    assert "$" not in body and "http" not in body and "--- " not in body and "*" not in body
    assert len(question.split()) <= 32                     # the question itself stays short
    assert "PO Box 10" in footer and '"no more"' in footer and "Jordan" in footer
    assert hc.lint(body) == []
    assert "STOP" in p.sms_text and "available" in p.sms_text and len(p.sms_text) <= 160
    assert len(p.phone_script) == 3 and p.mailto.startswith("mailto:pat@broker.test?subject=")
    # structure varies per lead, subject too
    others = {ao.build_opener(listed(db)[0]).email_body.split("\n")[2] for _ in range(6)}
    assert len(others) >= 2


def test_first_touch_opener_then_offer_and_nudge_logging(db, postal):
    lead, agent = listed(db)
    assert ao.opener_enabled() and ao.prior_touches(db, lead) == 0 and ao.last_touch(db, lead) is None
    p1 = ao.first_touch(lead, db, generate_loi=False)
    assert p1.kind == "opener"
    stage = ao.record_sent(db, lead, p1, "email", now=datetime(2026, 10, 20, 9, 0))
    assert stage == lead.status_stage != "OFFER_SENT_TO_AGENT"          # an opener is not an offer
    assert ao.last_touch(db, lead) == "opener" and ao.prior_touches(db, lead) == 1
    assert lead.next_followup_at.date() == datetime(2026, 10, 22).date() and lead.next_followup_at.hour == 10
    p2 = ao.first_touch(lead, db, generate_loi=False)                    # already touched: the priced note
    assert p2.kind == "offer" and p2.anchor and "--- Offer Summary ---" in p2.email_body


def test_urgent_listed_foreclosure_skips_the_opener(db, postal):
    lead = make_lead(db, sheriff_days=9, upset=150_000, vacant=True, stale_listing=True, list_price=360_000)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test")
    assert not ao.opener_applies(db, lead, days_to_sale=9)
    p = ao.first_touch(lead, db, generate_loi=False)
    assert p.kind in ("offer", "inquiry") and p.sale_date is not None
    later = make_lead(db, sheriff_days=40, upset=150_000, vacant=True, stale_listing=True, list_price=360_000)
    add_contact(db, later, contact_type="LISTING_AGENT", name="Sam Agent", phone="9735550101", email="sam@broker.test")
    assert ao.first_touch(later, db, generate_loi=False).kind == "opener"


def test_first_touch_setting_can_send_the_offer_first(db, postal, monkeypatch):
    monkeypatch.setitem(settings(), "autopilot", {**dict(settings().get("autopilot", {})), "first_touch": "offer"})
    lead, _ = listed(db)
    assert not ao.opener_enabled() and ao.first_touch(lead, db, generate_loi=False).kind == "offer"
