"""Google Calendar sync against a fake Google service (same request objects and HttpErrors as googleapiclient):
the four event families, exact timing, idempotency across lost rows, offline queuing, the two-way rules, privacy,
the session hooks, the OAuth setup and the cockpit routes."""
import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httplib2
import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import select

from db.models import CalendarEvent, ContactType, DistressSignal, SignalType
from engine import agent_outreach as ao
from engine import compliance, crm
from engine import gcal_sync as gs
from engine.closing_coordinator import record_disapproval
from tests.factories import add_contact, make_lead

ET = ZoneInfo("America/New_York")


# -- a fake Google Calendar -------------------------------------------------------------------------
class _Req:
    def __init__(self, svc, fn):
        self.svc, self.fn = svc, fn

    def execute(self, num_retries=0):
        self.svc.calls += 1
        if self.svc.offline:
            raise httplib2.ServerNotFoundError("Unable to find the server at www.googleapis.com")
        return self.fn()


def _http(status, reason="error"):
    return HttpError(httplib2.Response({"status": status, "reason": reason}), json.dumps({"error": reason}).encode())


class FakeEvents:
    def __init__(self, svc):
        self.svc = svc

    def insert(self, calendarId, body, sendUpdates=None):
        def run():
            if body["id"] in self.svc.store:
                raise _http(409, "duplicate")
            return self.svc.put({**body, "status": "confirmed"})
        return _Req(self.svc, run)

    def update(self, calendarId, eventId, body, sendUpdates=None):
        def run():
            if eventId not in self.svc.store:
                raise _http(404, "notFound")
            return self.svc.put({**body, "id": eventId})
        return _Req(self.svc, run)

    def get(self, calendarId, eventId):
        def run():
            if eventId not in self.svc.store:
                raise _http(404, "notFound")
            return dict(self.svc.store[eventId])
        return _Req(self.svc, run)

    def delete(self, calendarId, eventId, sendUpdates=None):
        def run():
            ev = self.svc.store.get(eventId)
            if ev is None or ev["status"] == "cancelled":
                raise _http(410, "deleted")
            self.svc.put({**ev, "status": "cancelled"})
        return _Req(self.svc, run)

    def list(self, calendarId, maxResults=250, privateExtendedProperty=None, showDeleted=False, updatedMin=None,
             pageToken=None):
        def run():
            items = list(self.svc.store.values())
            if privateExtendedProperty:
                k, v = privateExtendedProperty.split("=")
                items = [e for e in items if (e.get("extendedProperties") or {}).get("private", {}).get(k) == v]
            if not showDeleted:
                items = [e for e in items if e["status"] != "cancelled"]
            if updatedMin:
                items = [e for e in items if e["updated"] >= datetime.fromisoformat(updatedMin)]
            start = int(pageToken or 0)
            page = items[start:start + maxResults]
            out = {"items": [{**e, "updated": e["updated"].isoformat()} for e in page], "summary": "me@example.com",
                   "timeZone": "America/New_York"}
            if start + maxResults < len(items):
                out["nextPageToken"] = str(start + maxResults)
            return out
        return _Req(self.svc, run)


class FakeService:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.offline = False
        self.calls = 0

    def events(self):
        return FakeEvents(self)

    def put(self, ev):
        ev = {**ev, "updated": datetime.now(timezone.utc)}
        self.store[ev["id"]] = ev
        return dict(ev)

    # what the operator does in Google Calendar
    def operator_delete(self, eid):
        self.put({**self.store[eid], "status": "cancelled"})

    def operator_move(self, eid, start, end):
        self.put({**self.store[eid], "start": start, "end": end})

    def live(self):
        return [e for e in self.store.values() if e["status"] == "confirmed"]


@pytest.fixture
def google(monkeypatch, tmp_path):
    svc = FakeService()
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(gs, "TOKEN", token)
    monkeypatch.setattr(gs, "CREDENTIALS", tmp_path / "credentials.json")
    gs.set_api_factory(lambda: gs.CalendarAPI(svc))
    yield svc
    gs.set_api_factory(None)


def ev(svc, key):
    return svc.store.get(gs.event_id(key))


def agent_lead(db, **kw):
    lead = make_lead(db, sheriff_days=None, stale_listing=True, **kw)
    agent = add_contact(db, lead, contact_type=ContactType.LISTING_AGENT, name="PAT AGENT", phone="9735550100",
                        email="pat@brokerage.example", company="Sample Realty")
    return lead, agent


def offer_packet(lead, agent, kind="offer"):
    return ao.AgentPacket(lead_id=lead.id, property_id=lead.property_id, kind=kind, agent_contact_id=agent.id,
                          agent_name=agent.name, agent_first="Pat", brokerage=agent.company, agent_email=agent.email,
                          agent_phone=agent.phone, address="1 Test St, Newark", list_price=450000, dom=140,
                          anchor=300000, best=330000, list_to_mao_75=0.8, subject="1 Test St", email_body="hi",
                          sms_text="hi", mailto=None)


# -- timing ------------------------------------------------------------------------------------------
def test_callable_slot_keeps_exact_48h_inside_calling_hours():
    assert gs.callable_slot(datetime(2026, 11, 5, 14, 15)) == datetime(2026, 11, 5, 14, 15)   # Thursday afternoon
    assert gs.callable_slot(datetime(2026, 11, 4, 22, 30)) == datetime(2026, 11, 5, 10, 0)    # after 9pm
    assert gs.callable_slot(datetime(2026, 11, 5, 6, 0)) == datetime(2026, 11, 5, 10, 0)      # before 8am
    assert gs.callable_slot(datetime(2026, 11, 8, 15, 0)) == datetime(2026, 11, 9, 10, 0)     # Sunday -> Monday
    assert gs.callable_slot(datetime(2026, 11, 5, 20, 50)) == datetime(2026, 11, 6, 10, 0)    # wouldn't end by 9pm


def test_agent_offer_email_creates_follow_up_exactly_48_hours_later(db, google):
    lead, agent = agent_lead(db)
    sent = datetime(2026, 11, 3, 14, 15, tzinfo=ET)                        # Tuesday 2:15 PM
    ao.record_sent(db, lead, offer_packet(lead, agent), "email", now=sent)
    r = gs.sync(db, now=datetime(2026, 11, 3, 14, 16))
    assert r.error is None and r.pushed["created"] == 2                     # the follow-up + the daily goal
    row = db.scalar(select(CalendarEvent).where(CalendarEvent.kind == "followup"))
    e = google.store[row.google_event_id]
    street = lead.property.street_address.title()
    assert e["summary"] == f"🔥 Follow Up: {street} Agent (Pat Agent)"
    assert e["start"] == {"dateTime": "2026-11-05T14:15:00", "timeZone": "America/New_York"}   # exactly 48 h
    assert e["colorId"] == "6" and e["reminders"]["overrides"] == [{"method": "popup", "minutes": 10}]
    d = e["description"]
    assert "(973) 555-0100" in d and "pat@brokerage.example" in d and "Asking price: $450,000" in d
    assert "Your offer: $300,000" in d and f"http://127.0.0.1:8000/leads/{lead.id}" in d
    assert e["source"]["url"] == f"http://127.0.0.1:8000/leads/{lead.id}"
    assert e["extendedProperties"]["private"] == {"njw": "1", "njwKey": row.key, "njwKind": "followup",
                                                  "njwLead": str(lead.id)}
    # a 9:30 PM send: 48 h later is outside calling hours, so the reminder waits for 10:00 the next morning
    lead2, agent2 = agent_lead(db)
    ao.record_sent(db, lead2, offer_packet(lead2, agent2, kind="opener"), "email",
                   now=datetime(2026, 11, 3, 21, 30, tzinfo=ET))
    gs.sync(db, now=datetime(2026, 11, 3, 21, 31))
    row2 = db.scalar(select(CalendarEvent).where(CalendarEvent.lead_id == lead2.id))
    assert row2.starts_at == datetime(2026, 11, 6, 10, 0)
    assert "availability question" in google.store[row2.google_event_id]["description"]


def test_a_new_touch_replaces_the_old_follow_up(db, google):
    lead, agent = agent_lead(db)
    ao.record_sent(db, lead, offer_packet(lead, agent, kind="opener"), "email", now=datetime(2026, 11, 3, 11, 0, tzinfo=ET))
    gs.sync(db, now=datetime(2026, 11, 3, 11, 1))
    first = db.scalar(select(CalendarEvent).where(CalendarEvent.kind == "followup"))
    ao.record_sent(db, lead, offer_packet(lead, agent), "email", now=datetime(2026, 11, 4, 9, 0, tzinfo=ET))
    gs.sync(db, now=datetime(2026, 11, 4, 9, 1))
    db.refresh(first)
    assert first.status == "cancelled" and google.store[first.google_event_id]["status"] == "cancelled"
    live = [e for e in google.live() if "Follow Up" in e["summary"]]
    assert len(live) == 1 and live[0]["start"]["dateTime"] == "2026-11-06T09:00:00"


def test_owner_call_back_never_carries_the_owner_name_or_phone(db, google):
    lead = make_lead(db)
    owner = add_contact(db, lead, contact_type="OWNER", name="Quinn Sample", phone="9735551234")
    now = datetime(2026, 11, 2, 11, 0, tzinfo=ET)
    crm.log_call(db, lead, "NO_ANSWER", contact=owner, now=now)
    gs.sync(db, now=now.replace(tzinfo=None))
    row = db.scalar(select(CalendarEvent).where(CalendarEvent.kind == "callback"))
    e = google.store[row.google_event_id]
    assert e["summary"].startswith("📞 Call back: ") and e["colorId"] == "7"
    blob = json.dumps(list(google.store.values()), default=str)
    for secret in ("Quinn", "QUINN", "555-1234", "9735551234"):
        assert secret not in blob                       # the defendant / owner never leaves the laptop
    assert lead.next_followup_at == row.starts_at


# -- contracts ---------------------------------------------------------------------------------------
def test_contract_puts_attorney_review_at_5pm_on_the_3rd_business_day_and_disapproval_removes_it(db, google):
    lead = make_lead(db, sheriff_days=None)
    crm.record_contract(db, lead, 300000, signed=date(2026, 11, 2))          # Monday; Tue Nov 3 is Election Day
    gs.sync(db, now=datetime(2026, 11, 2, 12, 0))
    review = ev(google, f"review:{lead.id}:20261102")
    assert review["summary"] == f"⚠️ ATTORNEY REVIEW ENDS: {lead.property.street_address.title()}"
    assert review["start"]["dateTime"] == "2026-11-06T17:00:00"                # Wed, Thu, Fri -> Friday 5 PM
    assert [o["minutes"] for o in review["reminders"]["overrides"]] == [1440, 180] and review["colorId"] == "5"
    assert "disapprove" in review["description"] and "nothing is paid for" in review["description"]
    emd = ev(google, f"milestone:{lead.id}:EMD_DEPOSITED")
    assert emd["summary"].startswith("📋 Earnest money deposited") and emd["start"]["dateTime"].endswith("T09:00:00")
    assert ev(google, f"milestone:{lead.id}:CONTRACT_EXECUTED") is None    # day 0 is already done
    assert not [r for r in db.scalars(select(CalendarEvent)) if r.kind == "callback"]   # no duplicate call-backs
    record_disapproval(db, lead, "seller's attorney disapproved")
    gs.sync(db, {lead.id}, now=datetime(2026, 11, 3, 9, 0))
    assert review["status"] == "confirmed" and ev(google, f"review:{lead.id}:20261102")["status"] == "cancelled"
    assert ev(google, f"milestone:{lead.id}:EMD_DEPOSITED")["status"] == "cancelled"


# -- sheriff sales + daily goal ----------------------------------------------------------------------
def test_sheriff_sale_event_moves_with_an_adjournment_and_never_duplicates(db, google):
    lead, _ = agent_lead(db)
    make_lead(db, sheriff_days=8)                      # the listing above has no sale; this one is a plain sale
    sale_lead = db.scalar(select(DistressSignal).where(DistressSignal.signal_type == SignalType.SHERIFF_SALE))
    now = gs.now_local()
    gs.sync(db, now=now)
    key = next(r.key for r in db.scalars(select(CalendarEvent).where(CalendarEvent.kind == "sheriff_sale")))
    e = ev(google, key)
    first = date.today() + timedelta(days=8)
    assert e["start"] == {"date": first.isoformat()} and e["end"] == {"date": (first + timedelta(days=1)).isoformat()}
    assert e["summary"].startswith("🚨 SHERIFF SALE AUCTION: ") and e["summary"].endswith("(Essex County)")
    assert e["transparency"] == "transparent" and [o["minutes"] for o in e["reminders"]["overrides"]] == [2340, 900]
    assert "Upset (about the payoff): $150,000" in e["description"] and "QUINN" not in e["description"]
    sale_lead.scheduled_auction_date = first + timedelta(days=14)          # adjourned two weeks
    db.flush()
    gs.sync(db, {int(key.split(":")[1])}, now=now)
    assert ev(google, key)["start"] == {"date": (first + timedelta(days=14)).isoformat()}
    assert len([x for x in google.store.values() if "SHERIFF" in x["summary"]]) == 1


def test_daily_goal_is_one_recurring_10am_event(db, google):
    gs.sync(db, now=datetime(2026, 11, 2, 7, 0))
    e = ev(google, "daily_goal")
    assert e["summary"] == "🎯 Send 15 Daily Wholesaling Outreach Emails (Open Dashboard)"
    assert e["recurrence"] == ["RRULE:FREQ=DAILY"] and e["colorId"] == "10"
    assert e["start"] == {"dateTime": "2026-11-02T10:00:00", "timeZone": "America/New_York"}
    calls = google.calls
    r = gs.sync(db, now=datetime(2026, 11, 3, 7, 0))                    # the next day: nothing to change
    assert r.pushed == {"created": 0, "updated": 0, "deleted": 0, "dismissed": 0, "failed": 0}
    assert google.calls == calls + 1                                     # just the pull


# -- reliability -------------------------------------------------------------------------------------
def test_offline_keeps_everything_queued_and_the_next_sync_sends_it(db, google):
    lead, agent = agent_lead(db)
    ao.record_sent(db, lead, offer_packet(lead, agent), "email", now=datetime(2026, 11, 3, 14, 15, tzinfo=ET))
    google.offline = True
    r = gs.sync(db, now=datetime(2026, 11, 3, 14, 16))
    assert "can't reach Google" in r.error and r.queued == 2 and google.store == {}
    assert db.scalar(select(CalendarEvent).where(CalendarEvent.kind == "followup")).status == "pending"
    google.offline = False
    r = gs.sync(db, now=datetime(2026, 11, 3, 15, 0))
    assert r.error is None and r.queued == 0 and len(google.live()) == 2


def test_lost_rows_or_a_second_machine_never_duplicate_events(db, google):
    lead, agent = agent_lead(db)
    ao.record_sent(db, lead, offer_packet(lead, agent), "email", now=datetime(2026, 11, 3, 14, 15, tzinfo=ET))
    gs.sync(db, now=datetime(2026, 11, 3, 14, 16))
    before = {k: dict(v) for k, v in google.store.items()}
    for row in db.scalars(select(CalendarEvent)):
        db.delete(row)                                    # a fresh database copy on another laptop
    db.flush()
    r = gs.sync(db, now=datetime(2026, 11, 3, 14, 30))
    assert r.pushed["created"] == 0 and r.pushed["updated"] == 2 and set(google.store) == set(before)


def test_expired_sign_in_is_reported_and_nothing_is_lost(db, google, monkeypatch):
    def refused():
        raise gs.CalendarAuthError("Google sign-in expired or was revoked: run python manage.py setup-calendar")
    gs.set_api_factory(refused)
    r = gs.sync(db, now=datetime(2026, 11, 2, 7, 0))
    assert "setup-calendar" in r.error and r.queued == 1
    assert gs.status(db)["needs_reauth"] is True


# -- two-way -----------------------------------------------------------------------------------------
def test_two_way_rules(db, google):
    lead = make_lead(db, sheriff_days=None)
    owner = add_contact(db, lead, contact_type="OWNER", name="Quinn Sample", phone="9735551234")
    crm.log_call(db, lead, "LEFT_VM", contact=owner, now=datetime(2026, 11, 2, 11, 0, tzinfo=ET))
    contract_lead = make_lead(db, sheriff_days=None)
    crm.record_contract(db, contract_lead, 300000, signed=date(2026, 11, 2))
    gs.sync(db, now=datetime(2026, 11, 2, 11, 1))
    cb = db.scalar(select(CalendarEvent).where(CalendarEvent.kind == "callback"))
    review_key = f"review:{contract_lead.id}:20261102"

    # the operator drags the call-back to Friday 3 PM: the CRM follow-up moves with it, nothing is pushed back
    google.operator_move(cb.google_event_id, {"dateTime": "2026-11-06T15:00:00-05:00"},
                         {"dateTime": "2026-11-06T15:15:00-05:00"})
    # ...drags the attorney review by mistake: it is put back at 5 PM on the legal day
    google.operator_move(gs.event_id(review_key), {"dateTime": "2026-11-09T09:00:00-05:00"},
                         {"dateTime": "2026-11-09T09:30:00-05:00"})
    # ...moves the daily reminder to 9:00: it stays at 9:00
    google.operator_move(gs.event_id("daily_goal"), {"dateTime": "2026-11-02T09:00:00-05:00"},
                         {"dateTime": "2026-11-02T09:30:00-05:00"})
    r = gs.sync(db, now=datetime(2026, 11, 2, 12, 0))
    assert r.pulled["moved"] == 2 and r.pulled["restored"] == 1
    db.refresh(lead)
    assert lead.next_followup_at == datetime(2026, 11, 6, 15, 0)
    assert ev(google, review_key)["start"]["dateTime"] == "2026-11-06T17:00:00"
    assert ev(google, "daily_goal")["start"]["dateTime"] == "2026-11-02T09:00:00-05:00"
    again = gs.sync(db, now=datetime(2026, 11, 2, 12, 5))
    assert again.pushed["updated"] == 0 and ev(google, "daily_goal")["start"]["dateTime"].startswith("2026-11-02T09:00")

    # the operator deletes the call-back reminder: it is never re-created, even when the follow-up changes
    google.operator_delete(cb.google_event_id)
    gs.sync(db, now=datetime(2026, 11, 2, 12, 10))
    db.refresh(cb)
    assert cb.status == "dismissed"
    lead.next_followup_at = datetime(2026, 11, 7, 10, 0)
    db.flush()
    gs.sync(db, now=datetime(2026, 11, 2, 12, 20))
    assert google.store[cb.google_event_id]["status"] == "cancelled"


# -- privacy -----------------------------------------------------------------------------------------
def test_stop_lists_hide_agent_phone_and_daniels_law_hides_the_address(db, google):
    lead, agent = agent_lead(db)
    ao.record_sent(db, lead, offer_packet(lead, agent), "email", now=datetime(2026, 11, 3, 14, 15, tzinfo=ET))
    compliance.add_internal(db, agent.phone, notes="agent asked to stop")
    p = lead.property
    compliance.suppress_address(db, f"{p.street_address}, {p.city}", source="daniels_law")
    db.flush()
    gs.sync(db, now=datetime(2026, 11, 3, 14, 16))
    e = google.store[db.scalar(select(CalendarEvent).where(CalendarEvent.kind == "followup")).google_event_id]
    assert e["summary"] == f"🔥 Follow Up: lead #{lead.id} Agent (Pat Agent)"
    assert "555-0100" not in e["description"] and "Phone: not shown" in e["description"]
    assert p.street_address.title() not in json.dumps(e, default=str) and p.street_address not in json.dumps(e, default=str)


# -- automatic sync ----------------------------------------------------------------------------------
def test_commits_kick_the_background_sync_only_when_connected(db, google, monkeypatch):
    import db.database as database
    gs.install_hooks()
    kicked = []
    monkeypatch.setattr(gs.worker, "kick", lambda leads=(), props=(), full=False: kicked.append(set(leads)))
    lead = make_lead(db, sheriff_days=None)
    db.commit()
    kicked.clear()
    with database.session_scope() as s:
        crm.log_call(s, s.get(type(lead), lead.id), "NO_ANSWER", now=datetime(2026, 11, 2, 11, 0, tzinfo=ET))
    assert kicked and lead.id in kicked[-1]
    kicked.clear()
    monkeypatch.setattr(gs, "TOKEN", gs.TOKEN.with_name("missing.json"))   # not connected: hooks stay silent
    with database.session_scope() as s:
        crm.log_call(s, s.get(type(lead), lead.id), "NO_ANSWER", now=datetime(2026, 11, 2, 12, 0, tzinfo=ET))
    assert kicked == []


def test_worker_run_once_syncs_the_kicked_lead(db, google):
    lead, agent = agent_lead(db)
    ao.record_sent(db, lead, offer_packet(lead, agent), "email",
                   now=datetime.now(ET).replace(hour=11, minute=0) + timedelta(days=1))
    db.commit()
    gs.worker.run_once({lead.id}, set(), False)
    assert gs.worker.last.get("error") is None
    assert any("Follow Up" in e["summary"] for e in google.live())


def test_calendar_agent_skips_cleanly_when_not_connected(db):
    run = gs.CalendarSyncAgent().execute()
    assert run.status == "success" and "not connected" in (run.self_improvement_notes or "")
    assert db.scalar(select(CalendarEvent)) is None                         # nothing queued, nothing sent


# -- one-time setup ----------------------------------------------------------------------------------
class FakeCreds:
    def to_json(self):
        return json.dumps({"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s"})


class FakeFlow:
    def __init__(self, path, scopes):
        self.path, self.scopes = path, scopes

    def run_local_server(self, **kw):
        assert kw["open_browser"] is True and kw["timeout_seconds"] == 300
        return FakeCreds()


def test_setup_needs_a_desktop_client_and_saves_a_private_token(monkeypatch, tmp_path):
    svc = FakeService()
    monkeypatch.setattr(gs, "CREDENTIALS", tmp_path / "credentials.json")
    monkeypatch.setattr(gs, "TOKEN", tmp_path / "token.json")
    gs.set_api_factory(lambda: gs.CalendarAPI(svc))
    try:
        with pytest.raises(gs.CalendarSetupError, match="Desktop app"):
            gs.setup(flow_factory=FakeFlow)                       # no file yet: the step-by-step instructions
        gs.CREDENTIALS.write_text(json.dumps({"web": {"client_id": "x"}}))
        with pytest.raises(gs.CalendarSetupError, match="'Desktop app'"):
            gs.setup(flow_factory=FakeFlow)
        gs.CREDENTIALS.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
        info = gs.setup(flow_factory=FakeFlow)
        assert info == {"calendar": "me@example.com", "time_zone": "America/New_York"}
        assert json.loads(gs.TOKEN.read_text())["refresh_token"] == "r"
        assert (os.stat(gs.TOKEN).st_mode & 0o777) == 0o600
        assert gs.SCOPES == ["https://www.googleapis.com/auth/calendar.events"]
    finally:
        gs.set_api_factory(None)


# -- cockpit -----------------------------------------------------------------------------------------
def test_cockpit_routes(db):
    from fastapi.testclient import TestClient

    import web.app as webapp
    client = TestClient(webapp.app)
    r = client.get("/leads/42", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/#/lead/42"
    st = client.get("/api/calendar/status").json()
    assert st["configured"] is False and "Desktop app" in st["setup_steps"]
    r = client.post("/api/calendar/sync").json()
    assert r["reconciled"]["created"] >= 1 and "setup-calendar" in r["error"] and r["queued"] >= 1
    assert client.post("/api/calendar/connect").status_code == 400          # no credentials.json yet


def test_requests_match_googles_calendar_v3_discovery_document():
    """The real client + discovery document (no fake): unknown parameter names would raise TypeError here."""
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence
    ok = {"status": "200"}
    http = HttpMockSequence([
        (ok, json.dumps({"id": "nj1", "status": "confirmed"})),
        (ok, json.dumps({"id": "nj1", "status": "confirmed"})),
        (ok, json.dumps({"id": "nj1", "status": "confirmed"})),
        ({"status": "204"}, ""),
        (ok, json.dumps({"items": [{"id": "nj1"}], "nextPageToken": "p2"})),
        (ok, json.dumps({"items": [{"id": "nj2"}]})),
        (ok, json.dumps({"summary": "me@example.com", "timeZone": "America/New_York", "items": []})),
        ({"status": "409"}, json.dumps({"error": {"code": 409, "message": "The requested identifier already exists."}})),
        ({"status": "503"}, json.dumps({"error": {"code": 503, "message": "Backend Error"}})),
        ({"status": "503"}, json.dumps({"error": {"code": 503, "message": "Backend Error"}})),
    ])
    service = build("calendar", "v3", http=http, cache_discovery=False, static_discovery=True)
    cal = gs.CalendarAPI(service, "primary")
    body = {"id": "nj1", "summary": "x", "start": {"date": "2026-11-06"}, "end": {"date": "2026-11-07"}}
    cal.insert(body)
    cal.update("nj1", body)
    cal.get("nj1")
    cal.delete("nj1")
    assert [i["id"] for i in cal.changed("2026-11-02T12:00:00+00:00")] == ["nj1", "nj2"]
    assert cal.probe() == {"calendar": "me@example.com", "time_zone": "America/New_York"}
    with pytest.raises(gs.Conflict):
        cal.insert(body)
    with pytest.raises(gs.CalendarOffline):
        cal.insert(body)                                  # 503 twice (one retry): queued, not lost


def test_end_to_end_commit_reaches_google_through_the_background_thread(db, google):
    """No mocks between the CRM and Google: a committed offer e-mail -> after_commit hook -> worker thread -> push."""
    import time

    import db.database as database
    gs.install_hooks()
    lead, agent = agent_lead(db)
    db.commit()
    sent = datetime.now(ET).replace(hour=11, minute=0, second=0, microsecond=0) + timedelta(days=1)
    with database.session_scope() as s:
        lead2 = s.get(type(lead), lead.id)
        ao.record_sent(s, lead2, offer_packet(lead2, s.get(type(agent), agent.id)), "email", now=sent)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not any("Follow Up" in e["summary"] for e in google.live()):
        time.sleep(0.2)
    gs.worker.drain()
    e = next(e for e in google.live() if "Follow Up" in e["summary"])
    assert e["start"]["dateTime"] == (sent + timedelta(hours=48)).replace(tzinfo=None).isoformat(timespec="seconds") \
        or (sent + timedelta(hours=48)).weekday() == 6
