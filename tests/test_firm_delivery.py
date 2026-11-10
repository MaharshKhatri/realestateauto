"""After the sample (2026-11-10): "start" gets the payment link in their thread when one is configured, and paying /
trial clients get their full weekly report by themselves (never a masked sample, never twice a week)."""
from __future__ import annotations

from types import SimpleNamespace

from core.config import settings
from db.models import FirmOutbox, FirmTarget
from engine import firm_outbox as fo
from engine.streams.base import PRIORITY_REPLY, render_table_pdf
from tests.test_firm import TUE, target
from tests.test_gmail_autopilot import live  # noqa: F401 - pytest fixture
from tests.test_command_center import setup_paths  # noqa: F401 - pytest fixture


def _mail(mid, text, frm):
    return SimpleNamespace(message_id=mid, text=text, subject="Re: Union County report", from_email=frm, references=[],
                           received=None, auto_reply=False)


def test_start_is_a_warm_payment_step_and_the_link_goes_out_only_when_configured(live, monkeypatch):
    db = live
    t = target(db, kind="foreclosure_surplus_attorney", stream="s3")
    r = fo.handle_reply(db, _mail("<s1@firm.example>", "Start", t.email), t.id, TUE)
    db.commit()
    assert r.intent == "start" and [w.id for w in fo.warm(db)] == [r.id]
    assert db.query(FirmOutbox).filter(FirmOutbox.key.like("start:%")).count() == 0      # no link set: operator's call
    monkeypatch.setitem(settings(), "research_firm", {**settings()["research_firm"],
                                                      "payment_links": {"s3": "https://pay.example/s3"}})
    r2 = fo.handle_reply(db, _mail("<s2@firm.example>", "Let's start. How do I pay?", t.email), t.id, TUE)
    db.commit()
    row = db.query(FirmOutbox).filter(FirmOutbox.key.like("start:%")).one()
    assert r2.intent == "start" and row.status == "queued" and row.priority == PRIORITY_REPLY
    assert "https://pay.example/s3" in row.body and "$400 a month" in row.body and row.in_reply_to == "<s2@firm.example>"
    assert fo.classify("Sure, send it over") == "interested" and fo.classify("Start the trial please") == "start"
    assert fo.classify("Not interested") == "not_interested"


def test_paying_clients_get_the_full_report_once_a_week_never_the_sample(live, tmp_path):
    db = live
    out = tmp_path / "s3"
    render_table_pdf(out / "s3_surplus_union_2026-W46.pdf", title="Union", subtitle="x", head=["#", "Property"],
                     rows=[[str(i), f"{i} Sample St"] for i in range(5)])
    render_table_pdf(out / "s3_surplus_essex_2026-W46.pdf", title="Essex", subtitle="x", head=["#", "Property"],
                     rows=[[str(i), f"{i} Sample St"] for i in range(5)])
    paying = target(db, email="client@law.example", kind="foreclosure_surplus_attorney", stream="s3", county="Union")
    paying.stage = "PAYING"
    lead = target(db, email="prospect@law.example", kind="foreclosure_surplus_attorney", stream="s3", county="Union")
    other = target(db, email="lender@lend.example", kind="hard_money_lender", stream="s5")
    other.stage, other.data_json = "TRIAL", {"products": ["s3"]}
    db.commit()
    assert fo.deliver(db, "s3", "Surplus research", out, TUE) == 2
    db.commit()
    rows = {r.to_email: r for r in db.query(FirmOutbox).filter_by(kind="delivery")}
    assert set(rows) == {"client@law.example", "lender@lend.example"}            # never the prospect
    names = [a["name"] for a in rows["client@law.example"].attachments_json]
    assert names == ["s3_surplus_union_2026-W46.pdf"]                              # their county, full, no sample
    assert rows["client@law.example"].priority == PRIORITY_REPLY
    assert fo.deliver(db, "s3", "Surplus research", out, TUE) == 0                # once per ISO week
    assert db.get(FirmTarget, lead.id).stage == "NEW"
