"""The forensic audit (2026-11-14): outcomes per house the desk e-mailed, rates by offer-to-ask band and house kind,
firm-stream sends against replies, and the threads where the agent wrote last."""
from __future__ import annotations

from datetime import datetime, timedelta

from db.models import ContactType, FirmOutbox, FirmReply, InboundReply, Interaction, utcnow
from engine import ceo_audit as ca
from tests.factories import add_contact, make_lead
from tests.test_command_center import setup_paths  # noqa: F401 - pytest fixture
from tests.test_gmail_autopilot import live  # noqa: F401 - pytest fixture


def _lead(db, *, units=1, ask=500000, offer=None, intents=(), counter=None, outcome=None, hours_ago=30, text=None):
    ld = make_lead(db, sheriff_days=None, stale_listing=True, list_price=ask, dwellings=units,
                   listing_text=text or "Sold as-is, needs work.")
    if offer:
        ld.offer_amount = offer
    add_contact(db, ld, contact_type=ContactType.LISTING_AGENT, name="Pat Agent", phone=None,
                email=f"pat{ld.id}@broker.example")
    db.add(Interaction(lead_id=ld.id, channel="email", outcome=outcome or "OFFER_SENT_TO_AGENT",
                       occurred_at=utcnow() - timedelta(hours=hours_ago + 2)))
    if intents or counter:
        db.add(InboundReply(message_id=f"<r{ld.id}@b>", lead_id=ld.id, from_email=f"pat{ld.id}@broker.example",
                            status="dismissed", intents_json=list(intents), counter_amount=counter,
                            created_at=utcnow() - timedelta(hours=hours_ago)))
    db.flush()
    return ld


def test_outcomes_and_rates_by_band_units_and_tenants(live):
    db = live
    won = _lead(db, units=2, ask=535000, offer=475900, intents=("ACCEPT",), text="Tenant occupied 2-family, as-is.")
    warm = _lead(db, units=2, ask=625000, offer=511800, intents=("POF_REQUEST",))
    dead = _lead(db, ask=600000, offer=390000, intents=("REJECT",))
    far = _lead(db, ask=500000, offer=320000, intents=("COUNTER",), counter=480000)
    silent = _lead(db, ask=450000, offer=None, outcome="AGENT_OPENER_SENT")
    db.commit()
    rows = {r.lead_id: r for r in ca.lead_rows(db)}
    assert rows[won.id].outcome == "accepted" and rows[won.id].tenants and rows[won.id].ratio == 0.89
    assert rows[warm.id].outcome == "warm" and "POF_REQUEST" in rows[warm.id].why
    assert rows[dead.id].outcome == "dead" and rows[far.id].outcome == "dead"
    assert rows[silent.id].outcome == "silent" and rows[silent.id].openers == 1
    bands = {g: (n, pos) for g, n, _, pos, _ in ca.rates(list(rows.values()), lambda r: ca._band(r.ratio))}
    assert bands["85-90%"] == (1, 100.0) and bands["80-85%"] == (1, 100.0) and bands["<65%"][1] == 0


def test_the_report_names_houses_but_the_log_counts_only(live):
    db = live
    _lead(db, units=2, ask=535000, offer=475900, intents=("ACCEPT",))
    _lead(db, ask=500000, offer=440000, intents=("AVAILABLE",), hours_ago=20)           # both wrote last: dropped
    db.add(FirmOutbox(key="p1", stream="s6", kind="pitch", to_email="a@law.example", subject="s", body="b",
                      status="sent", sent_at=utcnow()))
    db.add(FirmOutbox(key="p2", stream="s1", kind="partner_note", to_email="k@cb.example", subject="s", body="b",
                      status="sent", sent_at=utcnow()))
    db.add(FirmReply(message_id="<k>", stream="s1", from_email="k@cb.example", intent="interested"))
    db.commit()
    text, counts = ca.build(db, utcnow())
    assert "Test St" in text and "[accepted]" in text and "Dropped threads" in text
    assert "s6   pitch" in text and "s1   partner_note" in text
    assert counts["audit_leads"] == 2 and counts["audit_warm"] == 1 and counts["audit_dropped"] == 2
    assert all(isinstance(v, int) for v in counts.values())


def test_the_desk_task_mails_the_report_and_logs_counts(db, tmp_path, monkeypatch):
    """2026-11-14: the first live run died on a missing import in the task path; this runs the task itself."""
    from engine import cloud_desk as cd
    _lead(db, units=2, ask=535000, offer=475900, intents=("ACCEPT",))
    db.commit()
    sent = []
    monkeypatch.setenv("SMTP_USER", "jordan@example.test")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "p")
    monkeypatch.setattr(cd, "_send_self_files", lambda *a, **k: sent.append(a[2]))
    kw = dict(now=datetime(2026, 11, 14, 7, 0), smtp_factory=None, user="jordan@example.test", pw="p",
              desk_key="x" * 40, root=tmp_path)
    out = cd._work("audit", approve_id=None, **kw)
    assert "audit_error" not in out and out["audit_leads"] == 1 and out["audit_mailed"] == 1
    assert sent and sent[0].startswith("[njw-audit] ")
