"""S7 Mansion-tax cliff net sheets: the dead-zone math, the gates and the one-page sheet."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pypdf
import pytest

from db.models import FirmOutbox, Interaction
from engine.quant_pricing import costs, graduated_percent_fee, realty_transfer_fee
from engine.streams import s7_mansion_tax_cliff as s7
from engine.streams.base import PRIORITY_SPECIALTY, StreamContext
from tests.factories import add_contact, make_lead

NOW = datetime(2026, 11, 9, 10, 0)
OP = {"name": "Max Tester", "first": "Max", "company": "", "phone": "(973) 555-0100", "email": "max@example.com",
      "postal": "1 Main St, Newark NJ 07102"}
AGENT_EMAIL = "pat.agent@brokerage.example"


def ctx(tmp_path, send: bool = True, **limits) -> StreamContext:
    return StreamContext(now=NOW, out_dir=tmp_path / "s7", operator=OP, send=send, limits=limits)


def listed(db, price: float = 1_007_198, email: str | None = AGENT_EMAIL, **kw):
    lead = make_lead(db, sheriff_days=None, stale_listing=True, list_price=price,
                     listing_text="Estate sale, sold as-is, needs work.", owner_name="QUINN Q SAMPLE", **kw)
    if email:
        add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone=None, email=email)
    return lead


def pdf_text(path) -> tuple[int, str]:
    r = pypdf.PdfReader(str(path))
    return len(r.pages), " ".join(p.extract_text() or "" for p in r.pages)


# -- math ---------------------------------------------------------------------------------------------------------
def test_transfer_tax_uses_the_yaml_functions():
    for p in (350_000, 999_999, 1_000_000, 1_000_500, 2_000_000, 2_000_500, 3_600_000):
        assert s7.transfer_tax(p) == pytest.approx(realty_transfer_fee(p) + graduated_percent_fee(p))
    assert s7.thresholds() == [float(t["over"]) for t in
                               costs()["realty_transfer_fee"]["graduated_percent_fee"]["tiers"]]


def test_every_threshold_has_a_dead_zone_solved_on_the_500_grid():
    zones = s7.dead_zones()
    assert [t for t, _ in zones] == [1_000_000, 2_000_000, 2_500_000, 3_000_000, 3_500_000]
    for t, edge in zones:
        assert edge > t and (edge - t) % 500 == 0
        assert s7.seller_net(edge) >= s7.seller_net(t) > s7.seller_net(edge - 500)
        assert s7.seller_net(t + 500) < s7.seller_net(t)
        assert s7.cliff(t) is None                       # exactly at the line: no graduated jump
        assert s7.cliff(t - 1000) is None
        assert s7.cliff(edge) is None                    # the edge nets at least what T does
        assert s7.cliff(t + 1000) is not None
    # the 1% cliff at $1M closes within ~$10.5K (the RTF's 1.21% top rate eats into the recovery)
    assert 1_010_000 < zones[0][1] <= 1_011_000


def test_cliff_report_numbers():
    c = s7.cliff(1_007_198)
    assert c is not None and c.threshold == 1_000_000
    assert c.net_price == pytest.approx(1_007_198 - realty_transfer_fee(1_007_198) - graduated_percent_fee(1_007_198))
    assert c.net_threshold == pytest.approx(1_000_000 - realty_transfer_fee(1_000_000))
    assert c.gain == pytest.approx(c.net_threshold - c.net_price) and c.gain > 2_000
    assert c.width == c.edge - 1_000_000
    c2 = s7.cliff(2_005_000)
    assert c2.threshold == 2_000_000 and c2.pct_price == pytest.approx(0.02) and c2.pct_threshold == pytest.approx(0.01)


def test_residential_class_and_note_copy():
    assert s7.residential("2", "multi_family") and not s7.residential("4A", "single_family")
    assert s7.residential(None, "condos") and not s7.residential(None, "multi_family")
    assert not s7.residential("", "land")
    body = s7.note_body(s7.cliff(1_007_198), "12 Sample St", "Max")
    from engine import human_copy as hc
    assert hc.lint(body) == [] and "—" not in body and s7.word_count(body) < 110
    sentences = [x for x in re.split(r"(?<=[.?!])\s+", body.split("\n\n")[1]) if x.strip()]
    assert 3 <= len(sentences) <= 5
    assert body.splitlines()[-1] == "Max" and "$1,000,000" in body and "$2,965" in body
    for word in ("opportunity", "guaranteed", "referral", "commission", "excited"):
        assert word not in body.lower()


# -- the run ------------------------------------------------------------------------------------------------------
def test_run_builds_one_page_sheet_and_note(db, tmp_path):
    lead = listed(db)
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.found == 1 and res.products == 1 and len(res.outbound) == 1
    ob = res.outbound[0]
    assert ob.key == f"s7:sheet:{lead.id}:1007198" and ob.to == AGENT_EMAIL and ob.lead_id == lead.id
    assert ob.priority == PRIORITY_SPECIALTY and ob.commercial and not ob.subject.lower().startswith("re:")
    pages, text = pdf_text(ob.attachments[0])
    assert pages == 1
    assert "not tax or legal advice" in text and "GraduatedPercentFeeNotice.pdf" in text and "feeschedover350" in text
    assert "QUINN" not in text.upper() and "Pat Agent" not in text and AGENT_EMAIL not in text
    assert all(isinstance(v, (int, float)) for v in res.metrics.values())
    # stable key: a second run (nothing enqueued) proposes the very same key
    again = s7.STREAM.run(db, ctx(tmp_path))
    assert [o.key for o in again.outbound] == [ob.key]


def test_dry_run_and_empty_database(db, tmp_path):
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.processed == 0 and res.outbound == [] and res.products == 0
    listed(db)
    dry = s7.STREAM.run(db, ctx(tmp_path, send=False))
    assert dry.outbound == [] and dry.products == 1


def test_out_of_zone_and_wrong_class_are_skipped(db, tmp_path):
    listed(db, price=1_000_000)                          # at the line
    listed(db, price=999_000)                            # under it
    listed(db, price=1_015_000)                          # past the edge
    listed(db, price=1_006_000, property_class="4A")     # commercial: no graduated fee
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.outbound == [] and res.metrics["skipped_class"] == 1
    assert res.metrics["just_under"] >= 2 and res.metrics["zone_listings"] == 1


def test_suppression_stop_list_and_recent_mail_are_honored(db, tmp_path):
    from engine.compliance import suppress_address
    sup = listed(db, price=1_005_000, email="a1@brokerage.example")
    suppress_address(db, f"{sup.property.street_address}, Newark NJ 07106", source="daniels_law")
    stopped = listed(db, price=1_006_000, email="a2@brokerage.example")
    add_contact(db, stopped, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550199",
                email="a2@brokerage.example", dnc_status="dnc")
    recent = listed(db, price=1_007_000, email="a3@brokerage.example")
    db.add(Interaction(lead_id=recent.id, channel="email_smtp", outcome="OFFER_SENT_TO_AGENT",
                       occurred_at=NOW - timedelta(days=3)))
    before = listed(db, price=1_008_000, email="a4@brokerage.example")
    db.add(FirmOutbox(key=f"s7:sheet:{before.id}:1009000", stream="s7", to_email="a4@brokerage.example",
                      subject="x", body="x", status="sent"))
    db.flush()
    res = s7.STREAM.run(db, ctx(tmp_path))
    assert res.outbound == [] and res.products == 0
    m = res.metrics
    assert (m["skipped_suppressed"], m["skipped_stop"], m["skipped_recent"], m["skipped_sent_before"]) == (1, 1, 1, 1)


def test_cap_per_run_and_one_sheet_per_agent(db, tmp_path):
    for i in range(4):
        listed(db, price=1_003_000 + 1000 * i, email=f"agent{i}@brokerage.example")
    listed(db, price=2_004_000, email="agent0@brokerage.example")      # the same agent again: next run
    res = s7.STREAM.run(db, ctx(tmp_path, pitches_per_run=3))
    assert len(res.outbound) == 3 and len({o.to for o in res.outbound}) == 3
    assert len({o.key for o in res.outbound}) == 3
