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
