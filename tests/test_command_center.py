"""Phase 5 command center: health, KPI pills, My Setup (settings.local.yaml + .env, no password echo), swarm
refresh runner, step-by-step autopilot (deck, send with edits, outcomes, progress), OPRA requests, tax-list and
lis-pendens uploads, and attaching an address to a clerk filing."""
import io
import os
import time
from datetime import date, datetime

import pytest
import yaml
from fastapi.testclient import TestClient

from core.config import Section, settings
from db.models import ClerkFiling, DistressSignal, Interaction, OpraRequest, SignalType
from engine import autopilot as ap
from engine import operator_setup
from engine import opra_generator as og
from engine.tax_list_import import import_tax_list, parse_rows, read_table
from swarm import runner
from tests.factories import add_contact, make_lead
from tests.test_agent_outreach import FakeSMTP

GOOD = {"name": "Jordan Operator", "company_name": "Garden State Home Buyers LLC", "phone": "908-555-0100",
        "email": "jordan@example.test", "mailing_address": "PO Box 10, Edison, NJ 08817"}


@pytest.fixture()
def setup_paths(tmp_path, monkeypatch):
    """Isolate My Setup: a copy of the operator section, temp settings.local.yaml / .env, clean SMTP env."""
    monkeypatch.setitem(settings(), "operator", Section(dict(settings()["operator"])))
    monkeypatch.setitem(settings(), "scrapers", Section(dict(settings().get("scrapers", {}))))
    monkeypatch.setattr(operator_setup, "LOCAL_SETTINGS", tmp_path / "settings.local.yaml")
    monkeypatch.setattr(operator_setup, "ENV_FILE", tmp_path / ".env")
    for k in ("SMTP_USER", "SMTP_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    yield tmp_path
    for k in ("SMTP_USER", "SMTP_APP_PASSWORD"):
        os.environ.pop(k, None)


def test_setup_validates_writes_local_files_and_never_returns_password(setup_paths):
    with pytest.raises(operator_setup.SetupError) as exc:
        operator_setup.save({**GOOD, "name": "Operator", "phone": "555", "mailing_address": "Edison"})
    assert set(exc.value.errors) == {"name", "phone", "mailing_address"}
    out = operator_setup.save({**GOOD, "app_password": "abcd efgh ijkl mnop"})
    assert out["live"] and out["auto_email"] and out["has_app_password"] and "app_password" not in out
    local = yaml.safe_load((setup_paths / "settings.local.yaml").read_text())
    assert local["operator"]["phone"] == "(908) 555-0100" and local["operator"]["name"] == "Jordan Operator"
    assert local["scrapers"]["contact_email"] == "jordan@example.test"
    env = (setup_paths / ".env").read_text()
    assert "SMTP_APP_PASSWORD=abcdefghijklmnop" in env and "SMTP_USER=jordan@example.test" in env
    assert oct((setup_paths / ".env").stat().st_mode)[-3:] == "600"
    assert settings().operator["mailing_address"] == GOOD["mailing_address"]      # live in this process now
    operator_setup.save({**GOOD, "company_name": "Garden State Homes LLC"})       # blank password keeps the old one
    assert "SMTP_APP_PASSWORD=abcdefghijklmnop" in (setup_paths / ".env").read_text()
    with pytest.raises(operator_setup.SetupError, match="16 letters"):
        operator_setup.save({**GOOD, "app_password": "short"})


def test_swarm_runner_reports_progress_and_refuses_overlap():
    calls = []

    class Run:
        status, records_processed, new_leads_found, error_count = "success", 3, 1, 0

    def slow(name, **opts):
        calls.append((name, opts))
        time.sleep(0.01)
        return Run()
    st = runner.trigger("quick", runner=slow, wait=True)
    st = runner.status()
    assert not st["running"] and st["mode"] == "quick" and st["finished_at"]
    assert [s["agent"] for s in st["steps"]] == runner.QUICK and all(s["status"] == "success" for s in st["steps"])
    assert ("outlier_guard", {"offline": True}) in calls
    with pytest.raises(ValueError):
        runner.trigger("everything", runner=slow)
    runner._state["running"] = True
    try:
        with pytest.raises(RuntimeError, match="already running"):
            runner.trigger("quick", runner=slow)
    finally:
        runner._state["running"] = False


def deal(db, **kw):
    lead = make_lead(db, sheriff_days=None, stale_listing=True, list_price=360_000, **kw)
    add_contact(db, lead, contact_type="LISTING_AGENT", name="Pat Agent", phone="9735550100", email="pat@broker.test",
                company="Garden Realty")
    return lead


def test_deck_cards_explain_the_deal_and_send_logs_progress(db, setup_paths, tmp_path, monkeypatch):
    operator_setup.save(GOOD)
    import engine.contracts as contracts
    monkeypatch.setattr(contracts, "DEALS_DIR", tmp_path / "deals")
    lead = deal(db)
    d = ap.deck(db)
    assert d["goal"] == 15 and d["done"] == 0 and d["setup_complete"] and not d["auto_email"]
    c = next(x for x in d["cards"] if x["lead_id"] == lead.id)
    # A cold agent gets Touch 1: the two-line availability question, no price, no summary block.
    assert c["kind"] == "mls_fixer" and c["packet_kind"] == "opener" and c["touch"] == 1 and c["style"] == []
    assert "Newark" in c["what"] and "140 days" in c["what"]
    assert any("$260,000 walk-away" in w for w in c["why"]) and "E-mail Pat Agent (Garden Realty)" in c["action"]
    assert len(c["phone_script"]) == 3 and c["subject"] and "--- Offer Summary ---" not in c["body"]
    assert "available" in c["body"] and "PO Box 10" in c["body"]
    with pytest.raises(ap.AutopilotError, match="mailing address"):
        ap.send(db, lead.id, "mailto", body="Hi Pat, short note with no footer.")
    edited = c["body"].replace("still available", "still on the market")
    r = ap.send(db, lead.id, "mailto", subject="question on your listing", body=edited)
    assert r["stage"] == "NEW" and r["done"] == 1 and r["followup_at"]
    inter = db.query(Interaction).filter_by(lead_id=lead.id, outcome="AGENT_OPENER_SENT").one()
    assert inter.channel == "email"
    assert all(x["lead_id"] != lead.id for x in ap.deck(db)["cards"])       # contacted: off today's deck
    # The agent answers: Touch 2 is the priced note with the full summary block.
    db.add(Interaction(lead_id=lead.id, channel="email_in", outcome="AGENT_REPLIED", notes="yes, still available",
                       occurred_at=datetime.now()))
    db.flush()
    t2 = ap.build_card(db, lead)
    assert t2["kind"] == "touch2" and t2["packet_kind"] == "offer" and "--- Offer Summary ---" in t2["body"]
    r2 = ap.send(db, lead.id, "mailto", subject="offer on your listing", body=t2["body"])
    assert r2["stage"] == "OFFER_SENT_TO_AGENT"
    assert db.query(Interaction).filter_by(lead_id=lead.id, outcome="OFFER_SENT_TO_AGENT").count() == 1


def test_smtp_send_from_autopilot_attaches_offer(db, setup_paths, tmp_path, monkeypatch):
    import engine.contracts as contracts
    monkeypatch.setattr(contracts, "DEALS_DIR", tmp_path / "deals")
    operator_setup.save({**GOOD, "app_password": "abcdefghijklmnop"})
    lead = deal(db)
    FakeSMTP.sent = []
    r = ap.send(db, lead.id, "smtp", smtp_factory=FakeSMTP)             # Touch 1 over SMTP: plain, no attachment
    msg = FakeSMTP.sent[0]
    assert r["stage"] == "NEW" and msg["To"] == "pat@broker.test"
    text = msg.get_body(preferencelist=("plain",)).get_content()
    assert "attorney review" in text and "PO Box 10, Edison" in text and "--- Offer Summary ---" not in text
    assert not list(msg.iter_attachments())
    db.add(Interaction(lead_id=lead.id, channel="email_in", outcome="AGENT_REPLIED", notes="still available",
                       occurred_at=datetime.now()))
    db.flush()
    r2 = ap.send(db, lead.id, "smtp", smtp_factory=FakeSMTP)            # Touch 2: the offer with the 1-page LOI
    msg2 = FakeSMTP.sent[1]
    text2 = msg2.get_body(preferencelist=("plain",)).get_content()
    assert r2["stage"] == "OFFER_SENT_TO_AGENT" and "--- Offer Summary ---" in text2
    assert any(p.get_filename() for p in msg2.iter_attachments())


def test_outcomes_interested_generates_offer_and_sold_kills(db, setup_paths, tmp_path, monkeypatch):
    import engine.contracts as contracts
    monkeypatch.setattr(contracts, "DEALS_DIR", tmp_path / "deals")
    operator_setup.save(GOOD)
    a, b = deal(db), deal(db)
    r = ap.outcome(db, a.id, "interested", out_root=tmp_path / "deals")
    assert r["outcome"] == "SPOKE_NEEDS_OFFER" and r["files"]["pdf"].endswith(".pdf") and r["done"] == 1
    r2 = ap.outcome(db, b.id, "sold")
    assert r2["stage"] == "DEAD" and b.status_stage == "DEAD"
    with pytest.raises(ap.AutopilotError):
        ap.outcome(db, a.id, "maybe")


def test_followup_card_after_offer_is_due(db, setup_paths):
    from engine import agent_outreach as ao
    operator_setup.save(GOOD)
    lead = deal(db)
    ao.record_sent(db, lead, ao.build_packet(lead, generate_loi=False, session=db), "email",
                   now=datetime(2026, 10, 20, 9, 0))
    card = ap.build_card(db, lead)
    assert card["kind"] == "followup" and card["subject"].startswith("following up on")
    assert card["body"].startswith("Hi Pat,\n\nFollowing up on my note") and "--- Offer Summary ---" in card["body"]
    r = ap.send(db, lead.id, "sms")
    assert db.query(Interaction).filter_by(lead_id=lead.id, outcome="AGENT_FOLLOWUP_SENT").count() == 1
    assert r["stage"] == "OFFER_SENT_TO_AGENT"


def test_kpis(db):
    deal(db)
    make_lead(db, stale_listing=True, list_price=360_000, absentee=True, sheriff_days=9)
    k = ap.kpis(db)
    assert k["open_leads"] == 2 and k["listed_foreclosures"] == 1 and k["goal"] == 15
    assert set(k) >= {"ready_to_call", "offers_sent_30d", "active_cash_buyers", "pipeline_value"}


def test_opra_drafts_certify_commercial_purpose_and_track_due_date(db, setup_paths):
    operator_setup.save(GOOD)
    make_lead(db, sheriff_days=None)
    d = og.drafts(db, 20, extra_counties=["Mercer"])
    newark = next(x for x in d if x.town == "NEWARK CITY")
    assert newark.channel == "verify" and newark.leads == 1 and "OPRA Unit" in newark.clerk
    assert "I WILL use the requested records for a commercial purpose" in newark.body
    assert "City of Newark (Essex County) only" in newark.body and "PO Box 10, Edison" in newark.body
    assert "not seeking these records in connection with a legal proceeding" in newark.body
    assert {x.town for x in d if x.county == "Mercer"} == {"TRENTON", "HAMILTON", "EWING"}
    trenton = next(x for x in d if x.town == "TRENTON")
    assert trenton.channel == "portal" and trenton.portal.startswith("https://") and trenton.mailto is None
    ewing = next(x for x in d if x.town == "EWING")
    assert ewing.mailto.startswith("mailto:") and "OPRA%20request" in ewing.mailto
    row = og.mark_sent(db, "Essex", "NEWARK CITY", "email", when=datetime(2026, 10, 28, 10))
    assert row.due_date == date(2026, 11, 19)                  # 14 NJ business days (skips Election Day, Veterans Day)
    og.mark_received(db, "Essex", "Newark")
    assert db.query(OpraRequest).one().status == "received"


TAX_CSV = ("Tax Collector Delinquent Report\n"
           "Block,Lot,Qual,Owner Name,Property Location,Tax Year,Total Due\n"
           "100,5,,SAMPLE QUINN,5 Sample St,2025,\"$4,210.55\"\n"
           "100,5,,SAMPLE QUINN,5 Sample St,2026,1200\n"
           "200,9,C0002,TESTER CASEY,9 Test Ave,2026,800.00\n"
           "999,1,,NOBODY,1 Nowhere,2026,50\n")


def test_tax_list_import_matches_parcels_and_stacks(db, tmp_path, monkeypatch):
    import engine.tax_list_import as tli
    monkeypatch.setattr(tli, "IMPORT_DIR", tmp_path / "tax_lists")
    rows = parse_rows(read_table(TAX_CSV.encode(), "list.csv"))
    assert [(r.block, r.lot, r.qual, r.amount) for r in rows][0] == ("100", "5", "", 4210.55)
    a = make_lead(db, sheriff_days=20, block="100", lot="5")
    b = make_lead(db, sheriff_days=None, block="200", lot="9", qual="C0002")
    r = import_tax_list(db, TAX_CSV.encode(), "list.csv", county="Essex", town="Newark")
    assert (r.rows, r.parcels, r.matched_local, r.unmatched) == (4, 3, 2, 1) and r.saved_as
    sig = db.query(DistressSignal).filter_by(property_id=a.property_id, signal_type=SignalType.TAX_DELINQUENT).one()
    assert sig.amount_owed == 5410.55 and sig.raw_metadata_json["tax_years"] == ["2025", "2026"]
    assert db.query(DistressSignal).filter_by(property_id=b.property_id, signal_type=SignalType.TAX_DELINQUENT).count() == 1
    import_tax_list(db, TAX_CSV.encode(), "list.csv", county="Essex", town="Newark")       # idempotent
    assert db.query(DistressSignal).filter_by(signal_type=SignalType.TAX_DELINQUENT).count() == 2
    with pytest.raises(ValueError, match="Block and Lot"):
        parse_rows(read_table(b"name,amount\nx,1\n", "x.csv"))


def test_tax_list_reads_excel(db):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["BLOCK", "LOT", "QUALIFIER", "OWNER", "LOCATION", "BALANCE"])
    ws.append([100, 5, None, "SAMPLE QUINN", "5 Sample St", 812.5])
    buf = io.BytesIO()
    wb.save(buf)
    rows = parse_rows(read_table(buf.getvalue(), "list.xlsx"))
    assert len(rows) == 1 and rows[0].block == "100" and rows[0].amount == 812.5 and rows[0].owner == "SAMPLE QUINN"


@pytest.fixture()
def client(db, monkeypatch, tmp_path):
    import engine.contracts as contracts
    import engine.tax_list_import as tli
    import web.app as webapp
    monkeypatch.setattr(webapp, "records_gis", lambda: None)
    monkeypatch.setattr(contracts, "DEALS_DIR", tmp_path / "deals")
    monkeypatch.setattr(tli, "IMPORT_DIR", tmp_path / "tax_lists")
    return TestClient(webapp.app)


def test_command_center_endpoints(db, client, setup_paths, monkeypatch):
    assert client.get("/api/health").json()["app"] == "nj-wholesale-cockpit"
    r = client.post("/api/settings", json={**GOOD, "phone": "12"})
    assert r.status_code == 400 and "phone" in r.json()["detail"]["errors"]
    s = client.post("/api/settings", json={**GOOD, "app_password": "abcdefghijklmnop"}).json()
    assert s["live"] and s["has_app_password"] and "abcdefghijklmnop" not in client.get("/api/settings").text
    lead = deal(db)
    db.commit()
    k = client.get("/api/kpis").json()
    assert k["open_leads"] == 1
    d = client.get("/api/autopilot").json()
    assert d["cards"][0]["lead_id"] == lead.id and d["auto_email"]
    assert client.post(f"/api/autopilot/{lead.id}/send", json={"channel": "fax"}).status_code == 409
    out = client.post(f"/api/autopilot/{lead.id}/outcome", json={"outcome": "voicemail"}).json()
    assert out["outcome"] == "LEFT_VM" and out["done"] == 1
    monkeypatch.setattr(runner, "trigger", lambda mode: {"running": True, "mode": mode, "steps": []})
    assert client.post("/api/swarm/trigger", json={"mode": "quick"}).json()["mode"] == "quick"
    st = client.get("/api/swarm/status").json()
    assert "recent" in st and "steps" in st


def test_records_endpoints(db, client, setup_paths, tmp_path, monkeypatch):
    import agents.scrapers.lis_pendens_agent as lp
    monkeypatch.setattr(lp, "IMPORT_DIR", tmp_path / "lis_pendens")
    make_lead(db, sheriff_days=None, block="100", lot="5")
    target = make_lead(db, sheriff_days=None, street_address="77 SAMPLE PL")
    db.commit()
    o = client.get("/api/opra").json()
    assert o["requests"][0]["town"] == "NEWARK CITY" and o["rules"]["commercial_business_days"] == 14
    assert client.post("/api/opra/sent", json={"county": "Essex", "town": "NEWARK CITY"}).json()["status"] == "sent"
    up = client.post("/api/imports/tax-list", data={"county": "Essex", "town": "Newark"},
                     files={"file": ("list.csv", TAX_CSV.encode(), "text/csv")}).json()
    assert up["matched_local"] == 1 and up["unmatched"] == 2
    assert client.get("/api/opra").json()["requests"][0]["status"] == "received"
    csv_text = ("Town,Defendant,Plaintiff,Recorded,Instrument,Type\n"
                f"NEWARK,TESTER CASEY,BIG BANK NA,{date.today():%m/%d/%Y},2026090001,LIS PENDENS FORECLOSURE\n")
    lpr = client.post("/api/imports/lis-pendens", data={"county": "Essex"},
                      files={"file": ("export.csv", csv_text.encode(), "text/csv")}).json()
    assert lpr["status"] == "success" and lpr["filings"] == 1
    cf = client.get("/api/clerk-filings").json()
    assert cf["counts"] == {"total": 1, "matched": 0} and cf["unmatched"][0]["defendants"] == ["TESTER CASEY"]
    fid = cf["unmatched"][0]["id"]
    assert client.post(f"/api/clerk-filings/{fid}/locate", json={"address": "1 Unknown Rd"}).status_code == 409
    hit = client.post(f"/api/clerk-filings/{fid}/locate", json={"address": "77 Sample Pl"}).json()
    assert hit["lead_id"] == target.id and hit["match"] == "operator_address"
    db.expire_all()
    assert db.get(ClerkFiling, fid).property_id == target.property_id
    assert db.query(DistressSignal).filter_by(property_id=target.property_id,
                                              signal_type=SignalType.LIS_PENDENS).count() == 1


def test_kpis_survive_a_stage_change_from_another_session(db):
    """Regression: /api/kpis 500'd when another request moved a lead's stage between two queries."""
    from db.database import session_scope
    from db.models import Lead
    lead = deal(db)
    db.commit()
    assert db.get(Lead, lead.id).status_stage == "NEW"                       # cached in this session
    with session_scope() as other:
        other.get(Lead, lead.id).status_stage = "OFFER_SENT_TO_AGENT"
    k = ap.kpis(db)
    assert k["open_leads"] == 1 and k["pipeline_value"] >= 0


def test_tax_list_import_falls_back_when_njgin_is_down(db, tmp_path, monkeypatch):
    import engine.tax_list_import as tli
    from core.http import FetchError
    monkeypatch.setattr(tli, "IMPORT_DIR", tmp_path / "tax_lists")

    class DownGIS:
        def resolve_municipality(self, county, town):
            return ("NEWARK CITY", "0714")

        def query(self, where):
            raise FetchError("GET parcels failed after retries: read timed out")
    a = make_lead(db, sheriff_days=None, block="100", lot="5")
    r = import_tax_list(db, TAX_CSV.encode(), "list.csv", county="Essex", town="Newark", gis=DownGIS())
    assert r.matched_local == 1 and r.matched_njgin == 0 and any("didn't answer" in n for n in r.notes)
    assert db.query(DistressSignal).filter_by(property_id=a.property_id, signal_type=SignalType.TAX_DELINQUENT).count() == 1


def test_two_step_funnel_cards_opener_nudge_then_touch2(db, setup_paths):
    from engine import human_copy as hc
    operator_setup.save(GOOD)
    lead = deal(db)
    card = ap.build_card(db, lead)
    assert card["kind"] == "mls_fixer" and card["packet_kind"] == "opener" and card["touch"] == 1
    assert "two-line opener" in card["action"] and "$" not in card["body"] and card["style"] == []
    assert any("Buyer math" in w or "walk-away" in w for w in card["why"])
    with pytest.raises(ap.AutopilotError, match="mailing address"):
        ap.send(db, lead.id, "mailto", body="Hi Pat, is it still available?")
    r = ap.send(db, lead.id, "mailto", now=datetime(2026, 10, 20, 9, 0))
    assert r["stage"] != "OFFER_SENT_TO_AGENT" and r["done"] == 1
    assert db.query(Interaction).filter_by(lead_id=lead.id, outcome="AGENT_OPENER_SENT").count() == 1
    # 48 hours of silence: the one-line nudge, still no price
    nudge = ap.build_card(db, lead, now=datetime(2026, 10, 23, 15, 0))
    assert nudge["kind"] == "followup" and nudge["packet_kind"] == "opener" and nudge["touch"] == 1
    assert "$" not in nudge["body"] and "nudge" in nudge["action"] and hc.lint(nudge["body"]) == []
    assert nudge["subject"].startswith("re: ")
    # the agent answers: Touch 2 is the priced note
    db.add(Interaction(lead_id=lead.id, channel="email_in", outcome="AGENT_REPLIED", notes="still available",
                       occurred_at=datetime(2026, 10, 23, 16, 0)))
    db.flush()
    t2 = ap.build_card(db, lead, now=datetime(2026, 10, 23, 17, 0))
    assert t2["kind"] == "touch2" and t2["packet_kind"] == "offer" and t2["touch"] == 2
    assert "--- Offer Summary ---" in t2["body"] and "answered your opener" in t2["action"]
    r2 = ap.send(db, lead.id, "mailto", now=datetime(2026, 10, 23, 17, 5))
    assert r2["stage"] == "OFFER_SENT_TO_AGENT"


def test_phase6_endpoints_reply_structure_skiptrace_and_first_touch(db, client, setup_paths):
    from pathlib import Path as _P
    operator_setup.save(GOOD)
    lead = deal(db)
    db.commit()
    r = client.post("/api/autopilot/first-touch", json={"mode": "offer"}).json()
    assert r["first_touch"] == "offer"
    assert yaml.safe_load((setup_paths / "settings.local.yaml").read_text())["autopilot"]["first_touch"] == "offer"
    assert client.get("/api/autopilot").json()["first_touch"] == "offer"
    assert client.post("/api/autopilot/first-touch", json={"mode": "shout"}).status_code == 422
    client.post("/api/autopilot/first-touch", json={"mode": "opener"})
    preview = client.post(f"/api/leads/{lead.id}/analyze-reply", json={"text": "Seller would take 250k", "save": False}).json()
    assert preview["verdict"] == "GREEN" and "interaction_id" not in preview and preview["numbers"]["counter"] == 250000
    saved = client.post(f"/api/leads/{lead.id}/analyze-reply", json={"text": "Seller would take 250k"}).json()
    assert saved["interaction_id"] and saved["stage"] == "NEGOTIATING" and saved["last_counter_amount"] == 250000
    assert saved["reply_text"].startswith("Hi Pat,") and saved["headline"].startswith("ACCEPT")
    d = client.get(f"/api/leads/{lead.id}").json()
    assert d["funnel"]["last_touch"] == "reply" and d["funnel"]["first_touch"] == "opener"
    assert d["partner"]["partners_needed"] == 3 and d["structure"]["recommended"] in ("assignment", "double_close")
    assert "rider" in d["gates"] and d["property"]["mun_code"] == "0714" and d["deal_cfg"]["pricing_model"] == "quant"
    s = client.get(f"/api/leads/{lead.id}/structure?seller_refuses=true").json()
    assert s["recommended"] == "double_close" and s["entity_option"]["conditions"] and s["rider_gate"]["allowed"]
    c = client.post("/api/structure/compare", json={"contract_price": 250000, "buyer_price": 270000}).json()
    assert c["recommended"] == "assignment" and c["assignment"]["net"] == 20000 - 150
    owner = make_lead(db, sheriff_days=20, upset=150000, vacant=True, verdict="STRONG")
    owner.property.owner_name = "QUINN Q SAMPLE"
    db.commit()
    q = client.get("/api/skiptrace/queue").json()
    row = next(x for x in q["rows"] if x["lead_id"] == owner.id)
    assert row["tags"] == ["VACANT_SHERIFF"] and row["links"]["truepeoplesearch"].startswith("https://www.truepeoplesearch.com")
    text = _P("tests/fixtures/skiptrace/truepeoplesearch_detail.txt").read_text()
    t = client.post(f"/api/leads/{owner.id}/skiptrace-paste", json={"text": text}).json()
    assert t["saved"]["best_phone"] == "(973) 555-0101" and t["ready_to_call"] and t["stage"] == "SKIP_TRACE"
    assert client.post("/api/leads/999999/skiptrace-paste", json={"text": text}).status_code == 404
    assert all(x["lead_id"] != owner.id for x in client.get("/api/skiptrace/queue").json()["rows"])
