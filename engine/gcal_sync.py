"""Google Calendar bridge: every time-sensitive step of a deal lands on the operator's own Google Calendar, with a
link back to the lead card in the local dashboard.

    python manage.py setup-calendar      # once: config/credentials.json (OAuth "Desktop app" client) -> browser
                                         # consent -> config/token.json (git-ignored, chmod 600) -> first sync
    python manage.py sync-calendar       # full two-way sync (--dry-run shows what would change, --status the queue)

Events (one natural key each; the key also fixes the Google event id, so nothing is ever created twice):
  followup:<lead>:i<interaction>  🔥 Follow Up: <street> Agent (<agent>)   48 h after an e-mail to the listing agent,
                                                                            moved into calling hours (8am-9pm, Mon-Sat)
                                  📞 Call back: <street>                    any other scheduled follow-up call
  review:<lead>:<contract date>   ⚠️ ATTORNEY REVIEW ENDS: <street>        5:00 PM on the 3rd NJ business day
  frfpa:<lead>:<contract date>    ⚠️ FRFPA CANCELLATION ENDS: <street>     covered foreclosures: the 10th business day
  milestone:<lead>:<step>         📋 <step>: <street>                      the other contract-to-close deadlines
  sale:<lead>                     🚨 SHERIFF SALE AUCTION: <street> (<county> County)   priority sales; the date moves
                                                                            with every adjournment the swarm sees
  daily_goal                      🎯 Send 15 Daily Wholesaling Outreach Emails (Open Dashboard)   10:00 every day

How it stays in sync: calendar_events is an outbox. Every commit that changes a follow-up, a contract, a closing
milestone, a sale date or an agent's do-not-call status marks the lead (SQLAlchemy session hooks, so the cockpit, the
CLI and the swarm are all covered); after the commit a background thread pulls Google's changes, rebuilds those
leads' events and pushes them. Offline or signed out, the rows stay queued and the next sync sends them (the cockpit
re-syncs every `calendar.background_minutes`, the swarm daemon every 15 minutes, or run `sync-calendar`).
Two-way: an event the operator deletes in Google is never re-created; a follow-up or the daily reminder dragged to
another time moves the CRM follow-up (or the reminder) with it; a legal date (attorney review, a sale, a milestone)
dragged by mistake is put back, because the date is a fact, not a preference.

What goes to Google (the operator's own account, like their Gmail): street address, county, asking price, upset, our
offer, the listing agent's business name / phone / e-mail, and the dashboard link. Never a homeowner's or defendant's
name or phone (the lead card shows those behind the do-not-call and calling-hours checks), never an agent phone on
an internal / Daniel's Law stop list, and for an address under a Daniel's Law or stop request only "lead #<id>".
"""
from __future__ import annotations

import atexit
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import socket
import threading
import time as clock
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import event, inspect, or_, select
from sqlalchemy.orm import Session

from agents.base import AgentContext, BaseAgent
from core.config import ROOT, settings
from core.dates import nj_business_days_after
from core.text import format_phone
from db.models import (CalendarEvent, ClosingMilestone, Contact, ContactType, DistressSignal, InboundReply, Interaction,
                       Lead, LeadStage, SignalType, SwarmState, utcnow)

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]      # events on your calendars, nothing else
# Asked for at sign-in only to learn WHICH account signed in (its e-mail address), so the reminders can't land on
# the wrong calendar; they open nothing else.
IDENTITY_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
CREDENTIALS = Path(os.environ.get("WHOLESALING_GCAL_CREDENTIALS") or ROOT / "config" / "credentials.json")
TOKEN = Path(os.environ.get("WHOLESALING_GCAL_TOKEN") or ROOT / "config" / "token.json")
# On a Mac that handed the desk to the cloud (engine/cloud_desk.py): its database is a stale copy from that moment.
HANDOFF_FLAG = Path(os.environ.get("WHOLESALING_HANDOFF_FLAG") or ROOT / "data" / "runtime_cloud.json")
STATE_KEY = "gcal"
WORKER_FLAG = "gcal_worker"            # session.info flag: this session belongs to the sync itself (no re-trigger)
TERMINAL = {LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED}
CONTRACT_STAGES = {LeadStage.UNDER_CONTRACT, LeadStage.ASSIGNED}
USER_EDITABLE = {"followup", "callback", "daily_goal"}   # the operator's time wins; legal dates are put back
AGENT_TOUCH_WORDS = {"OFFER_SENT_TO_AGENT": "offer", "AGENT_OPENER_SENT": "availability question",
                     "AGENT_INQUIRY_SENT": "inquiry", "AGENT_FOLLOWUP_SENT": "follow-up note"}
REPLY_OUTCOME = "AGENT_REPLIED"
SKIP_MILESTONES = {"CONTRACT_EXECUTED", "ATTORNEY_REVIEW_WINDOW"}     # day 0 is done; review has its own event
STOP_SOURCES = {"internal", "daniels_law"}
SETUP_STEPS = """Google Calendar isn't connected yet. One-time setup (about 5 minutes, free):
  1. https://console.cloud.google.com/ -> create a project (any name, e.g. "NJ Wholesale Calendar").
  2. APIs & Services -> Library -> "Google Calendar API" -> Enable.
  3. Google Auth platform -> Branding: app name + your e-mail. Audience: External; add your own Gmail under
     Test users (or press "Publish app" so the sign-in doesn't expire every 7 days).
  4. Google Auth platform -> Clients -> Create client -> Application type "Desktop app" -> Create ->
     Download JSON, and save it as config/credentials.json in this project.
  5. Run: python manage.py setup-calendar --account you@example.com   (the Google account whose calendar you
     check; add it under Test users too). A browser opens once on that account: allow calendar events."""


class CalendarError(RuntimeError):
    pass


class CalendarOffline(CalendarError):
    """No network, Google 5xx / rate limit: keep everything queued and try again later."""


class CalendarAuthError(CalendarError):
    """Not set up, consent revoked or expired: run `python manage.py setup-calendar` again."""


class CalendarSetupError(CalendarError):
    pass


class NotFound(CalendarError):
    pass


class Conflict(CalendarError):
    pass


# -- settings, time, ids -----------------------------------------------------------------------------
def cfg() -> dict[str, Any]:
    return dict(settings().get("calendar", {}) or {})


def tz_name() -> str:
    return str(cfg().get("timezone") or "America/New_York")


def dashboard_url() -> str:
    return str(cfg().get("dashboard_url") or "http://127.0.0.1:8000").rstrip("/")


def lead_link(lead_id: int) -> str:
    return f"{dashboard_url()}/leads/{lead_id}"


def card_link(lead_id: int) -> str | None:
    """The lead card link, only where a dashboard can exist (not from the cloud desk)."""
    return None if in_cloud() else lead_link(lead_id)


def _clock(key: str, default: str) -> time:
    hh, _, mm = str(cfg().get(key) or default).partition(":")
    return time(int(hh), int(mm or 0))


def now_local() -> datetime:
    """Naive America/New_York time, the convention leads and interactions are stored in."""
    return datetime.now(ZoneInfo(tz_name())).replace(tzinfo=None)


def handed_off() -> bool:
    """This computer handed the desk to the cloud: it never writes to the calendar again (2026-11-03: the idle Mac's
    background sync overwrote the cloud's fresh 📊 desk report with its stale copy of the database)."""
    return HANDOFF_FLAG.exists()


def in_cloud() -> bool:
    """The desk runs in GitHub Actions (.github/workflows/desk.yml): there is no dashboard to link to."""
    return os.environ.get("GITHUB_ACTIONS") == "true"


def configured() -> bool:
    return bool(cfg().get("enabled", True)) and TOKEN.is_file() and not handed_off()


def event_id(key: str) -> str:
    """Google event ids are base32hex (0-9, a-v): 'nj' + 40 hex characters, the same on every machine."""
    return "nj" + hashlib.sha256(key.encode()).hexdigest()[:40]


def body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def callable_slot(when: datetime, minutes: int | None = None) -> datetime:
    """`when` itself if a call then is inside calling hours (8am-9pm Eastern, Monday-Saturday), else the next 10:00."""
    from engine.compliance import CALL_END, CALL_START
    minutes = int(minutes or cfg().get("followup_minutes", 15))
    when = when.replace(second=0, microsecond=0)
    latest = datetime.combine(when.date(), time(CALL_END)) - timedelta(minutes=minutes)
    if when.weekday() != 6 and time(CALL_START) <= when.time() and when <= latest:
        return when
    day = when.date() if when.time() < time(CALL_START) else when.date() + timedelta(days=1)
    while day.weekday() == 6:
        day += timedelta(days=1)
    return datetime.combine(day, time(10))


def _timed(start: datetime, minutes: int) -> tuple[dict[str, str], dict[str, str]]:
    end = start + timedelta(minutes=minutes)
    return ({"dateTime": start.isoformat(timespec="seconds"), "timeZone": tz_name()},
            {"dateTime": end.isoformat(timespec="seconds"), "timeZone": tz_name()})


def _all_day(d: date) -> tuple[dict[str, str], dict[str, str]]:
    return {"date": d.isoformat()}, {"date": (d + timedelta(days=1)).isoformat()}


def parse_start(value: dict[str, Any] | None) -> datetime | None:
    """A Google start/end object -> naive local datetime (all-day -> midnight)."""
    if not value:
        return None
    if value.get("dateTime"):
        dt = datetime.fromisoformat(str(value["dateTime"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(value.get("timeZone") or tz_name()))
        return dt.astimezone(ZoneInfo(tz_name())).replace(tzinfo=None)
    if value.get("date"):
        return datetime.combine(date.fromisoformat(value["date"]), time(0))
    return None


def _normalized(value: dict[str, Any] | None) -> dict[str, str] | None:
    if not value:
        return None
    if value.get("date"):
        return {"date": value["date"]}
    dt = parse_start(value)
    return {"dateTime": dt.isoformat(timespec="seconds"), "timeZone": tz_name()} if dt else None


def _when(dt: datetime) -> str:
    return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def _long_day(d: date) -> str:
    return f"{d:%A, %B} {d.day}, {d.year}"


def _money(v: float | None) -> str:
    return f"${v:,.0f}" if v else "?"


def _name(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text.title() if text.isupper() else text


# -- what the calendar should hold -------------------------------------------------------------------
@dataclass
class Desired:
    key: str
    kind: str
    lead_id: int | None
    body: dict[str, Any]
    starts_at: datetime


def _body(kind: str, summary: str, lines: Iterable[str | None], start: dict[str, str], end: dict[str, str], *,
          reminders: Iterable[int], link: str | None = None, recurrence: list[str] | None = None,
          transparent: bool = False) -> dict[str, Any]:
    body: dict[str, Any] = {"summary": summary, "description": "\n".join(x for x in lines if x),
                            "start": start, "end": end,
                            "reminders": {"useDefault": False,
                                          "overrides": [{"method": "popup", "minutes": int(m)} for m in reminders]}}
    color = str((cfg().get("colors") or {}).get(kind) or "")
    if color:
        body["colorId"] = color
    if recurrence:
        body["recurrence"] = recurrence
    if link:
        body["source"] = {"title": "NJ Wholesale Cockpit", "url": link}
    if transparent:
        body["transparency"] = "transparent"
    return body


@dataclass
class _Place:
    street: str
    county: str | None
    ask: float | None
    redacted: bool


def _place(session: Session, lead: Lead) -> _Place:
    from engine import compliance
    from engine.human_copy import street
    from engine.underwriter import asking_price
    p = lead.property
    full = f"{p.street_address or ''}, {p.city or p.municipality or ''}"
    owner_mail = " ".join(x for x in (p.owner_mailing_address, p.owner_city_state_zip) if x)
    redacted = bool(compliance.address_suppressed(session, full)) or bool(
        owner_mail and compliance.address_suppressed(session, owner_mail))
    return _Place(street=f"lead #{lead.id}" if redacted else street(p.street_address) or f"lead #{lead.id}",
                  county=p.county, ask=asking_price(p), redacted=redacted)


def listing_agent(lead: Lead) -> Contact | None:
    return next((c for c in lead.contacts if c.contact_type == ContactType.LISTING_AGENT), None)


def agent_phone(session: Session, agent: Contact | None) -> str | None:
    """An agent's advertised number is B2B, but a stop request (internal / Daniel's Law) always wins."""
    from engine import compliance
    if not agent or not agent.phone or agent.dnc_status == "dnc":
        return None
    if set(compliance.dnc_sources(session, agent.phone)) & STOP_SOURCES:
        return None
    return format_phone(agent.phone)


def _agent_lines(session: Session, agent: Contact | None) -> list[str]:
    if agent is None:
        return []
    phone = agent_phone(session, agent)
    return [f"Agent: {_name(agent.name) or 'listing agent'}" + (f", {agent.company}" if agent.company else ""),
            f"Phone: {phone}" if phone else "Phone: not shown (none on record, or they asked you to stop)",
            f"E-mail: {agent.email}" if agent.email else None]


def _last_scheduling_touch(session: Session, lead: Lead) -> Interaction | None:
    return session.scalar(select(Interaction).where(Interaction.lead_id == lead.id,
                                                    Interaction.next_followup_at.is_not(None))
                          .order_by(Interaction.occurred_at.desc(), Interaction.id.desc()).limit(1))


def _listing_for(session: Session, lead: Lead) -> Any:
    """The MLS listing behind a lead: the one its STALE_LISTING signal names (a wrong-parcel link included), else the
    property's newest listing."""
    from db.models import Listing
    p = lead.property
    refs = [sg.source_ref for sg in p.signals if sg.signal_type == "STALE_LISTING" and sg.source_ref]
    if refs:
        lst = session.scalar(select(Listing).where(Listing.source_ref.in_(refs)).limit(1))
        if lst is not None:
            return lst
    return max(p.listings, key=lambda x: x.last_seen_at or datetime.min, default=None)


_QUESTIONS = {
    "basics": ["Does it need any work: roof, heating, electric, kitchen, bathrooms?",
               "Do you know what the seller needs to walk away with after everything's paid?",
               "Any offers so far? Why do you think it hasn't sold?",
               "Is it empty, or is someone living there?",
               "Would the seller take a cash offer, as is, with a quick closing?"],
    "units": ["How many apartments are there? Are they rented, what does each one pay a month, and when do the "
              "leases end?",
              "How's the condition: roof, heating, kitchens, bathrooms?",
              "Do you know what the seller needs to walk away with after everything's paid?",
              "Any offers so far? Why do you think it hasn't sold?"],
    "condo": ["What have same-size units in the building sold for lately?",
              "What's the monthly HOA fee, and is any special assessment coming?",
              "How's the condition of the unit itself?",
              "Do you know what the seller needs to walk away with after everything's paid?",
              "Any offers so far?"],
}


def call_brief(session: Session, lead: Lead, agent: Contact | None, place: _Place, *,
               ask_shown: bool = False, bounced: bool = False) -> list[str]:
    """A word-for-word call script on every call event (2026-11-03: the operator handles calls only, is new to real
    estate and asked for full scripts): the house, our numbers or why there's no price yet, the exact opener, the
    questions, the answers to what agents ask (offer, wholesaler / assign, proof of funds, a showing), the close and
    what to tell Claude after. Business facts only: the street (or "lead #N" when suppressed), the ask, our numbers,
    the agent's first name."""
    from engine import preflight
    from engine.human_copy import operator
    from engine.jv_partner_vault import pof_answer
    from engine.reply_negotiator import fact_check_issue
    try:
        pf = preflight.check(lead)
        fact = fact_check_issue(pf)
    except Exception:                               # noqa: BLE001 - a script never breaks the calendar
        log.exception("call script: pre-flight failed for lead %s", lead.id)
        fact = ("basics", "pre-flight didn't run")
    lst = _listing_for(session, lead)
    house = []
    if lst is not None:
        if (lst.property_type or "").startswith("multi"):
            house.append("multi-family (several apartments)")
        if lst.beds:
            house.append(f"{lst.beds:g} bed")
        if lst.baths:
            house.append(f"{lst.baths:g} bath")
        if lst.sqft:
            house.append(f"{lst.sqft:,} sq ft")
        if lst.year_built:
            house.append(f"built {lst.year_built}")
        if lst.days_on_market:
            listed = lst.list_date.date() if isinstance(lst.list_date, datetime) else lst.list_date
            dom = (date.today() - listed).days if isinstance(listed, date) else lst.days_on_market
            house.append(f"{max(dom, lst.days_on_market)} days on the market")
    if place.ask and not ask_shown:
        house.append(f"asking {_money(place.ask)}")
    written = f"your written offer ({_money(lead.offer_amount)})" if lead.offer_amount else None
    # The same one number every other surface states (engine/offer_math.py), locked to what already went out.
    from engine.offer_math import firm_offer, locked
    try:
        firm = locked(lead, firm_offer(lead, session))
    except Exception:                               # noqa: BLE001 - a script never breaks the calendar
        log.exception("call script: firm offer failed for lead %s", lead.id)
        firm = None
    priced = bool(not fact and firm is not None and (firm.priced or (written and firm.final)))
    ask_theirs = bool(not fact and not priced and firm is not None and firm.tier == "ask_first" and firm.final)
    # Same test as the call sheet (call_sheet._say): our own number needs a check (no living area, old comps, value
    # far above the ask), so no yes on the call, only the number written down (2026-11-05: the reply event for 139
    # Aycrigg said "at or under $682,100 it can work" while its call-sheet row said "agree to nothing").
    doubts = [r for r in (firm.reasons if firm is not None else [])
              if "of the ask: ask what the seller needs first" not in r] if ask_theirs else []
    extra_q = ["About how many square feet is it, and how many bedrooms and bathrooms?"] \
        if doubts and "living area" in doubts[0] else []
    if ask_theirs and doubts:
        kind, issue = "basics", ""
        numbers = (f"Don't name a price, and say yes to no number on this call ({doubts[0]}). Ask what the seller "
                   "needs and write it down; the desk checks the numbers after the call.")
    elif ask_theirs:
        kind, issue = "basics", ""
        numbers = (f"Don't name a price first ({firm.reasons[0] if firm.reasons else 'ask what the seller needs'}). "
                   f"Ask what the seller needs. At or under {_money(firm.final)} it can work; above it, you're too far "
                   "apart: be polite and ask to check back in a few weeks.")
    elif not priced:
        kind, issue = fact or ("basics", (firm.reasons[0] if firm is not None and firm.reasons else
                                          "no numbers for this house yet"))
        numbers = (f"Don't go above {written} on this call: {issue}." if written else
                   f"Don't name a price on this call: {issue}.") + " Get the facts first; the desk prices it after."
    else:
        kind, issue = "basics", ""
        opening = float(lead.offer_amount or firm.offer or firm.final)
        numbers = (f"Your one number: {_money(opening)}. Best and final: {_money(firm.final)}; never above it."
                   + (f" Already sent: {written}." if written and not ask_shown else ""))
    if lst is not None and (lst.property_type or "").startswith("multi") and kind == "basics":
        kind = "units"
    op = operator()
    first = (_name(agent.name).split() or ["there"])[0] if agent is not None and agent.name else "there"
    full = op["name"] or op["first"] or "your name"
    phone = op["phone"] or "your phone number"
    if priced:
        open_at = _money(lead.offer_amount) if lead.offer_amount else _money(opening)
        walk = _money(firm.final)
        offer = [(f'4) Your written offer ({open_at}) already went out by e-mail. Ask: "Did you get my offer at '
                  f'{open_at}? What did the seller think?" If they ask again: "It\'s {open_at}, cash, as is, with '
                  'attorney review."') if lead.offer_amount else
                 (f'4) "What\'s your offer?" (only after your questions): "I\'d be around {open_at}, cash, as is, with '
                  'attorney review. I know that\'s under the asking price; I\'m buying it as an investor. I\'ll send it '
                  'in writing today."'),
                 '   Too low? "I understand. What number do you think the seller would actually take?" Write it down, '
                 'agree to nothing, and say "Let me run that and get back to you."',
                 f'   Their number at or under {walk}: "That might work. Let me confirm my numbers and send it in '
                 f'writing." Above {walk}: "That\'s more than I can do. If anything changes, I\'d love to hear from '
                 'you."']
    elif ask_theirs and doubts:
        offer = ['4) Don\'t say a price first. Ask: "What number would the seller sign at for cash, as is, with a quick '
                 'close?" Write it down.',
                 '   Whatever number they say: "Thanks. Let me run it and I\'ll get back to you today." Don\'t say yes '
                 'or no to it on this call.',
                 '   "What\'s your offer?" first: "I\'d rather not guess at the seller\'s number. What would they need to '
                 'walk away with?"']
    elif ask_theirs:
        fin = _money(firm.final)
        offer = ['4) Don\'t say a price first. Ask: "What number would the seller sign at for cash, as is, with a quick '
                 'close?" Write it down.',
                 f'   Their number at or under {fin}: "That can work. Let me confirm and I\'ll send it in writing today." '
                 f'Above {fin}: "That\'s more than I can pay as is. Mind if I check back in a few weeks?"',
                 '   "What\'s your offer?" first: "I\'d rather not guess at the seller\'s number. What would they need to '
                 'walk away with?"']
    else:
        offer = ['4) "What\'s your offer?": "I want to give you a real number, not a guess. Let me run what you just '
                 'told me and I\'ll e-mail it to you in writing." Say no number on this call.']
    return [
        "--- Call script (word for word) ---",
        "The house: " + ", ".join(house) + "." if house else None,
        numbers,
        (f'1) Open: "Hi, is this {first}? Hi {first}, this is {full}, {phone}. I\'m calling about your listing at '
         f'{place.street}; my e-mail to you bounced. Do you have two minutes?"' if bounced else
         f'1) Open: "Hi, is this {first}? Hi {first}, this is {full}, {phone}. I\'m following up on my e-mail about '
         f'{place.street}. Do you have two minutes?"'),
        '   Bad time? "No problem. When\'s a better time to call you back?" Write it down, say thanks, hang up.',
        '2) Who you are: "I\'m a real estate investor. I buy for myself or with a partner investor. I\'m not a realtor '
        'and I don\'t have an agent."',
        "3) Ask one at a time and write every answer down:",
        *[f'   - "{q}"' for q in extra_q + _QUESTIONS.get(kind, _QUESTIONS["basics"])],
        *offer,
        '5) "Are you a wholesaler?" / "Will you assign it?": "I\'m an investor. I may buy it myself or assign the '
        'contract to a partner investor. That\'s in writing, and the seller\'s attorney reviews everything."',
        '   "We don\'t work with wholesalers" / "the seller won\'t accept an assignment": "Understood, thanks for '
        'telling me. I\'d rather be upfront about how I buy. If anything changes, I\'d still be glad to make an '
        'offer." Then end the call politely; don\'t argue and don\'t promise to close in your own name.',
        f'6) "Do you have proof of funds?": "{pof_answer(session)}"',
        '7) "Do you want to see it?": "Photos or a short video first, please. My partner can walk it during the '
        'inspection period."',
        '8) Anything else: "Good question. Let me check and get back to you."',
        f'9) End: "Thanks, {first}. I\'ll send it over in writing. Have a good one."',
        f'No answer? Leave this voicemail: "Hi {first}, this is {full}, {phone}, about {place.street}. Please give me a '
        f'call back at {phone}. Thanks." Then tell Claude "no answer, left a voicemail".',
        "Never: go above the walk-away, offer the agent money, promise to stop a foreclosure, or say you're a realtor.",
        f'After the call, tell Claude in the chat what they said (for example "lead {lead.id}: seller needs 450, '
        f'needs a roof, send the offer"). The desk sends the written offer in their Gmail thread.',
    ]

def followup_events(session: Session, lead: Lead, now: datetime) -> list[Desired]:
    due = lead.next_followup_at
    if due is None or lead.status_stage in TERMINAL | CONTRACT_STAGES:
        return []
    inter = _last_scheduling_touch(session, lead)
    touch = AGENT_TOUCH_WORDS.get(inter.outcome) if inter else None
    contact = session.get(Contact, inter.contact_id) if inter and inter.contact_id else None
    agent = contact if contact is not None and contact.contact_type == ContactType.LISTING_AGENT else (
        listing_agent(lead) if touch or (inter and inter.outcome == REPLY_OUTCOME) else None)
    hours = int(cfg().get("followup_hours", 48))
    minutes = int(cfg().get("followup_minutes", 15))
    sent = inter.occurred_at if inter else None
    unmoved = inter is not None and inter.next_followup_at == due
    # "Please call me": the answer e-mail set the follow-up to the next call slot, hours (not 48 h) after it went out.
    # That call is the event, at that time (2026-11-03: two "call me" agents landed on the calendar two days late).
    # (record_sent's own follow-up is two business days out, never under a day: only a call slot is this close)
    asked_call = bool(touch and sent and sent < due < sent + timedelta(hours=20))
    # A bounce sets the same near call slot (gmail_autopilot: a dead e-mail with a phone on the card): that call is
    # not "they asked you to call" (2026-11-06, 344 Daniele Dr: the agent never got our e-mail).
    # The bounce's Date header can be a second before our logged send time (a 550 comes back at once): 10 minutes slack.
    bounced = bool(asked_call and session.scalar(
        select(InboundReply.id).where(InboundReply.lead_id == lead.id, InboundReply.status == "bounce",
                                      InboundReply.received_at >= sent - timedelta(minutes=10)).limit(1)))
    start = callable_slot(sent + timedelta(hours=hours), minutes) if touch and sent and unmoved and not asked_call \
        else due
    if start < now:
        return []           # an overdue follow-up is on today's call list; a reminder in the past helps no one
    if inter is not None and inter.outcome == REPLY_OUTCOME and session.scalar(
            select(InboundReply.id).where(InboundReply.lead_id == lead.id,
                                          InboundReply.status == "needs_approval").limit(1)):
        return []           # the waiting reply's own 📞 event is the call (2026-11-03: the same reply showed twice)
    key = f"followup:{lead.id}:i{inter.id}" if inter else f"followup:{lead.id}:{due:%Y%m%d%H%M}"
    place = _place(session, lead)
    s, e = _timed(start, minutes)
    if agent is not None:
        who = _name(agent.name) or "the listing agent"
        # A call the operator logged from the chat (cloud_desk.apply_note): the next step is that call again.
        called = inter is not None and inter.channel == "call" and not touch
        missed = called and inter.outcome in ("NO_ANSWER", "LEFT_VM", "WRONG_NUMBER")
        # Only notes written through the chat channel reach the calendar: Claude keeps them to business facts (no
        # homeowner names or phones); notes typed elsewhere might name a seller.
        chat_note = called and (inter.debrief_json or {}).get("source") == "chat_note"
        if bounced:
            summary = f"📞 Call {who} about {place.street} (your e-mail bounced)"
        elif asked_call:
            summary = f"📞 Call {who} about {place.street} (they asked you to call)"
        elif called:
            summary = (f"📞 Try {who} again about {place.street} (no answer last time)" if missed else
                       f"📞 Call {who} back about {place.street} (check in)")
        else:
            summary = f"🔥 Follow Up: {place.street} Agent ({who})"
        lead_line = (f"Your e-mail of {_when(sent)} bounced (the address is dead and off the card): call instead."
                     if bounced else
                     f"They asked you to call. Your e-mail answer went out {_when(sent)}; this is the call."
                     if asked_call else
                     f"You called {_when(sent)} and they didn't pick up. Try once more; if it goes to voicemail, "
                     "leave the voicemail below." if missed else
                     f"You spoke {_when(sent)}" + (f": {inter.notes}" if inter.notes and chat_note else "") + "."
                     if called else
                     f"Follow-up call to the listing agent, {hours} hours after your {touch} e-mail ({_when(sent)})."
                     if touch and unmoved and sent else "Follow-up with the listing agent.")
        nudge = None if asked_call or called else ("Optional call. If you don't call, the desk e-mails them a short "
                                                   "nudge by itself in its next morning batch.")
        offer = f"Your offer: {_money(lead.offer_amount)}" if touch == "offer" and lead.offer_amount else None
        card = card_link(lead.id)
        lines = [lead_line, nudge, *_agent_lines(session, agent),
                 f"Asking price: {_money(place.ask)}" if place.ask else None, offer,
                 f"Open the lead card: {card}" if card else None,
                 "Call 8am-9pm Eastern. If they asked you to stop, don't call: the card checks the do-not-call lists.",
                 *call_brief(session, lead, agent, place, ask_shown=True, bounced=bounced)]
        kind = "followup"
    else:
        summary = f"📞 Call back: {place.street}"
        what = (inter.outcome or "").replace("_", " ").lower() if inter else "a scheduled follow-up"
        lines = [f"Scheduled call-back ({what}" + (f", {_when(sent)}" if sent else "") + ").",
                 "The number is on the lead card, behind the do-not-call and calling-hours checks.",
                 (f"Open the lead card: {lead_link(lead.id)}" if card_link(lead.id) else None)]
        kind = "callback"
    return [Desired(key, kind, lead.id, _body(kind, summary, lines, s, e, reminders=[10], link=card_link(lead.id)),
                    start)]


def reply_events(session: Session, lead: Lead, now: datetime) -> list[Desired]:
    """🔥 An agent answered and the deal waits on the operator's click (engine/gmail_autopilot.py Action Needed).

    The alert lands ~10 minutes after the reply arrived (inside calling hours) and disappears once it's approved or
    dismissed. It carries the street, the light and the numbers, never the agent's message (it may name the seller)."""
    out: list[Desired] = []
    rows = session.scalars(select(InboundReply).where(InboundReply.lead_id == lead.id,
                                                      InboundReply.status == "needs_approval"))
    place = _place(session, lead)
    for row in rows:
        # Timed from when the Mac READ the reply, not when it was sent: a Mac that was asleep or a daemon that wasn't
        # running reads it hours later, and an alert timed off the agent's send time would land in the past and never
        # be created (2026-11-03: two morning replies read at noon got no alert).
        seen = row.received_at                          # naive Eastern
        if row.created_at is not None:                  # naive UTC (utcnow): convert before comparing
            read = row.created_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name())).replace(tzinfo=None)
            seen = max(seen, read) if seen else read
        start = callable_slot((seen or now) + timedelta(minutes=10), 15)
        if start < now:
            continue
        agent = session.get(Contact, row.contact_id) if row.contact_id else listing_agent(lead)
        n = (row.analysis_json or {}).get("numbers") or {}
        s, e = _timed(start, 15)
        # The operator handles calls only (2026-11-03): a reply the desk can't answer by itself is a call.
        phone = agent_phone(session, agent)
        who = _name(agent.name) if agent is not None and agent.name else "the agent"
        how = {"voice_text": "texted you", "voice_mail": "left you a voicemail"}.get(row.match_method or "", "replied")
        if phone:
            summary = (f"📞 COUNTER-OFFER RECEIVED: call {who} about {place.street}" if row.counter_amount else
                       f"📞 Call {who} about {place.street}: they {how}")
        else:
            summary = (f"🔥 COUNTER-OFFER RECEIVED: {place.street} (no phone on file)" if row.counter_amount else
                       f"🔥 Agent replied: {place.street} (no phone on file)")
        lines = [f"The listing agent answered: {row.headline or row.verdict or 'see the dashboard'}.",
                 f"Their number: {_money(row.counter_amount)}" if row.counter_amount else None,
                 f"Your walk-away: {_money(n.get('mao_walkaway'))}" if n.get("mao_walkaway") else None,
                 f"Why it waits for you: {row.note}" if row.note else None, *_agent_lines(session, agent),
                 (f"Or text back from your phone: {(row.analysis_json or {}).get('sms_text')}"
                  if (row.match_method or "").startswith("voice") and (row.analysis_json or {}).get("sms_text")
                  else None),
                 f'Or skip the call: say "send #{row.id}" to Claude in the chat and the drafted answer goes out.'
                 if (row.analysis_json or {}).get("reply_text") and not (row.match_method or "").startswith("voice")
                 else None,
                 *(call_brief(session, lead, agent, place) if phone else [])]
        out.append(Desired(f"reply:{lead.id}:r{row.id}", "reply", lead.id,
                           _body("reply", summary, lines, s, e, reminders=[0], link=card_link(lead.id)), start))
    return out


def contract_events(session: Session, lead: Lead, now: datetime) -> list[Desired]:
    from engine.closing_coordinator import LABELS, milestones
    if not lead.contract_date or lead.status_stage in {LeadStage.DEAD, LeadStage.DO_NOT_CONTACT}:
        return []
    place = _place(session, lead)
    link = card_link(lead.id)
    review_days = int(settings().get("compliance", {}).get("attorney_review_business_days", 3))
    day3 = nj_business_days_after(lead.contract_date, review_days)
    steps = {m.milestone: m for m in milestones(session, lead)}
    out: list[Desired] = []
    at = _clock("attorney_review_time", "17:00")
    ar = steps.get("ATTORNEY_REVIEW_WINDOW")
    start = datetime.combine(day3, at)
    if (ar is None or ar.status != "cancelled") and start >= now:
        s, e = _timed(start, 30)
        lines = [f"Attorney review ends today: {review_days} NJ business days after the fully signed contract reached "
                 f"both sides on {_long_day(lead.contract_date)}.",
                 "Until it ends, either side's attorney may disapprove for any reason. Nothing is marketed to buyers "
                 "and nothing is paid for (title, inspection) before it ends with no disapproval.",
                 "Confirm with your attorney that no disapproval was sent, then mark it done in Closing & Money.",
                 f"Contract price: {_money(lead.offer_amount)}" if lead.offer_amount else None,
                 (f"Open the lead card: {link}" if link else None)]
        out.append(Desired(f"review:{lead.id}:{lead.contract_date:%Y%m%d}", "attorney_review", lead.id,
                           _body("attorney_review", f"⚠️ ATTORNEY REVIEW ENDS: {place.street}", lines, s, e,
                                 reminders=[1440, 180], link=link), start))
    if lead.attorney_review_ends and lead.attorney_review_ends > day3:     # FRFPA: the 10-business-day cancellation
        start = datetime.combine(lead.attorney_review_ends, at)
        if start >= now:
            s, e = _timed(start, 30)
            lines = ["The owner's 10-business-day cancellation right under the Foreclosure Rescue Fraud Prevention Act "
                     "runs through today. No deed is signed and nothing is marketed before it ends.",
                     (f"Open the lead card: {link}" if link else None)]
            out.append(Desired(f"frfpa:{lead.id}:{lead.contract_date:%Y%m%d}", "attorney_review", lead.id,
                               _body("attorney_review", f"⚠️ FRFPA CANCELLATION ENDS: {place.street}", lines, s, e,
                                     reminders=[1440, 180], link=link), start))
    mt = _clock("milestone_time", "09:00")
    for key, m in steps.items():
        if key in SKIP_MILESTONES or m.status != "pending" or not m.due_date:
            continue
        start = datetime.combine(m.due_date, mt)
        if start < now:
            continue                      # overdue steps are flagged in the cockpit and the call queue
        s, e = _timed(start, 15)
        lines = [f"Contract-to-close deadline for {place.street}: {LABELS.get(key, key)}.",
                 "Mark it done in Closing & Money when it's finished.", (f"Open the lead card: {link}" if link else None)]
        out.append(Desired(f"milestone:{lead.id}:{key}", "milestone", lead.id,
                           _body("milestone", f"📋 {LABELS.get(key, key)}: {place.street}", lines, s, e,
                                 reminders=[1440, 15], link=link), start))
    return out


def _sheriff_signal(lead: Lead) -> DistressSignal | None:
    return next((s for s in lead.property.active_signals
                 if s.signal_type == SignalType.SHERIFF_SALE and s.scheduled_auction_date), None)


def sale_event(session: Session, lead: Lead, today: date) -> Desired | None:
    sig = _sheriff_signal(lead)
    if sig is None or sig.scheduled_auction_date < today or lead.status_stage in TERMINAL:
        return None
    md = sig.raw_metadata_json or {}
    place = _place(session, lead)
    county = place.county or md.get("county") or "?"
    d = sig.scheduled_auction_date
    upset = sig.amount_owed or md.get("upset_amount")
    adj = md.get("adjournments_total")
    q = ((lead.property.current_valuation.assumptions_json or {}).get("quant") or {}) \
        if lead.property.current_valuation else {}
    agent = listing_agent(lead) if place.ask else None
    lines = [f"{county} County sheriff's sale scheduled for {_long_day(d)}"
             + (f" (sheriff # {md['sheriff_number']})." if md.get("sheriff_number") else "."),
             f"Upset (about the payoff): {_money(upset)}" if upset else None,
             f"Status: {sig.status or 'scheduled'}" + (f"; adjourned {adj} times so far" if adj else "")
             + ". Sales are often adjourned: this event moves when the swarm sees a new date.",
             f"Listed for {_money(place.ask)}." if place.ask else None,
             *_agent_lines(session, agent),
             f"Your numbers: walk-away {_money(q.get('mao_walkaway'))}, target {_money(q.get('mao_target'))}."
             if q.get("mao_walkaway") else None,
             "We can't stop or postpone a sale or negotiate with the lender; any offer goes through the listing agent "
             "and the seller's attorney.",
             f"CivilView: {md.get('detail_url_at_scrape') or sig.source_url}"
             if (md.get("detail_url_at_scrape") or sig.source_url) and not place.redacted else None,
             (f"Open the lead card: {lead_link(lead.id)}" if card_link(lead.id) else None)]
    s, e = _all_day(d)
    return Desired(f"sale:{lead.id}", "sheriff_sale", lead.id,
                   _body("sheriff_sale", f"🚨 SHERIFF SALE AUCTION: {place.street} ({county} County)", lines, s, e,
                         reminders=[2340, 900], link=card_link(lead.id), transparent=True),
                   datetime.combine(d, time(0)))


def priority_sale_leads(session: Session, today: date, limit: int | None = None) -> list[Lead]:
    """Listed foreclosures with an offer path first, then ready-to-call sheriff leads, then the highest priority."""
    from engine import fast_track as ft
    limit = int(limit if limit is not None else cfg().get("sheriff_sale_limit", 25))
    out: list[Lead] = []
    seen: set[int] = set()

    def add(lead: Lead | None) -> None:
        if lead is not None and lead.id not in seen and len(out) < limit and lead.status_stage not in TERMINAL:
            sig = _sheriff_signal(lead)
            if sig is not None and sig.scheduled_auction_date >= today:
                seen.add(lead.id)
                out.append(lead)

    for lf in ft.listed_foreclosures(session, today=today):
        if lf.track not in ("NO_OFFER",):
            add(session.get(Lead, lf.lead_id))
    sheriff = (select(Lead).join(DistressSignal, (DistressSignal.property_id == Lead.property_id)
                                 & (DistressSignal.signal_type == SignalType.SHERIFF_SALE)
                                 & DistressSignal.active.is_(True))
               .where(DistressSignal.scheduled_auction_date >= today,
                      Lead.status_stage.notin_([s.value for s in TERMINAL])))
    for lead in session.scalars(sheriff.where(Lead.ready_to_call.is_(True))
                                .order_by(Lead.priority_rank_score.desc()).limit(limit)).unique():
        add(lead)
    for lead in session.scalars(sheriff.order_by(Lead.priority_rank_score.desc()).limit(limit * 2)).unique():
        add(lead)
    return out


def reconnect_event(session: Session, now: datetime) -> Desired | None:
    """While the OAuth app is in "Testing", Google ends its sign-in 7 days after consent and the sync stops without a
    sound. One event the day before says so, with the command (calendar.reconnect_reminder_days; 0 = off)."""
    days = int(cfg().get("reconnect_reminder_days", 6) or 0)
    st = (session.get(SwarmState, STATE_KEY).value_json or {}) if session.get(SwarmState, STATE_KEY) else {}
    if not days or not st.get("connected_at"):
        return None
    connected = datetime.fromisoformat(st["connected_at"])
    start = datetime.combine((connected + timedelta(days=days)).date(), time(10, 15))
    if start < now:
        return None
    account = st.get("account") or "you@example.com"
    extra = "" if target_calendar() == "primary" else f" --calendar {target_calendar()}"
    s, e = _timed(start, 15)
    lines = ["Google ends this app's calendar sign-in 7 days after you connect it while the app is in 'Testing'. "
             "After that, new calls and deadlines stop reaching this calendar until you sign in again.",
             "1) So it never ends again (2 minutes, any browser): console.cloud.google.com, signed in as the account "
             "that made the app → Google Auth platform → Audience → Publish app → Confirm (unverified is fine for "
             "your own account).",
             "2) Sign in once more (1 minute, on your Mac): open Terminal, paste this line and press Return:",
             f"cd ~/realestateauto && .venv/bin/python manage.py setup-calendar --account {account}{extra}",
             "Google opens: pick that account and click Allow. If it then asks for a Gmail app password (the old one "
             "was deleted), make one named \"mac\" at myaccount.google.com/apppasswords and paste it at the hidden "
             "prompt. It hands the sign-in to the cloud desk right away.",
             "Once the app is published, this reminder can go: tell Claude \"the calendar app is published\"."]
    return Desired(f"reconnect:{connected:%Y%m%d%H%M}", "reconnect", None,
                   _body("reconnect", "🔑 Re-connect Google Calendar sync (sign-in ends tomorrow)", lines, s, e,
                         reminders=[0, 120], link=None if in_cloud() else f"{dashboard_url()}/"), start)


def note_reconnected(session: Session, when: datetime) -> None:
    """The cloud desk got a new sign-in from the Mac (a config bundle): the next 🔑 reminder counts from it."""
    row = session.get(SwarmState, STATE_KEY)
    if row is None:
        row = SwarmState(key=STATE_KEY, value_json={})
        session.add(row)
    row.value_json = {**(row.value_json or {}), "connected_at": when.isoformat(timespec="seconds")}
    session.flush()


def openers_paused() -> bool:
    """2026-11-09: cold openers paused on the cloud desk (DAILY_OPENERS "0"): no "daily outreach" goal on the calendar."""
    return os.environ.get("DAILY_OPENERS", "").strip() == "0" or not cfg().get("daily_goal_event", True)


def daily_goal_event(session: Session, today: date) -> Desired | None:
    from engine.autopilot import daily_goal
    if openers_paused():
        return None
    goal = daily_goal()
    row = session.scalar(select(CalendarEvent).where(CalendarEvent.key == "daily_goal"))
    first = parse_start((row.body_json or {}).get("start")) if row and row.body_json else None
    start = datetime.combine(first.date() if first else today, _clock("daily_goal_time", "10:00"))
    s, e = _timed(start, 30)
    lines = [f"Today's outreach: {goal} new listing agents, e-mailed from your Gmail 20-45 seconds apart (urgent "
             "listed foreclosures, then due follow-ups, then stale MLS fixers).",
             "The desk sends them by itself at 10:30 and 2:00 PM on weekdays: there is nothing to do here.",
             "Your calls are in the four 📞 Call block events (25 calls each, one script); replies that need you "
             "get their own event."]
    return Desired("daily_goal", "daily_goal", None,
                   _body("daily_goal", f"🎯 Daily outreach: {goal} agent e-mails (autopilot)", lines,
                         s, e, reminders=[0], recurrence=[str(cfg().get("daily_goal_rrule") or "RRULE:FREQ=DAILY")]),
                   start)


def note_busy(session: Session, cal: Any, now: datetime) -> None:
    """Remember today's busy times (classes, meetings) so the call blocks fit around them. Never fails a sync."""
    day0 = datetime.combine(now.date(), time(0, 0))
    try:
        spans = cal.busy(day0, day0 + timedelta(days=1))
    except Exception:                                    # noqa: BLE001 - an older client / offline: keep the plan
        log.debug("calendar: busy times not read", exc_info=True)
        return
    _set_state(session, busy={"date": now.date().isoformat(),
                              "spans": [[a.strftime("%H:%M"), b.strftime("%H:%M")] for a, b in spans]})


def place_blocks(day: date, planned: list[tuple[str, str, int]], busy: list[tuple[datetime, datetime]],
                 *, min_minutes: int = 40, last_end: time = time(20, 30)) -> dict[str, tuple[datetime, int]]:
    """Fit each call block (letter, HH:MM, minutes) at or after its planned time into the first free gap of at least
    `min_minutes` (a block shrinks to the gap; most calls are a 1-minute voicemail), never over a class or meeting,
    never past `last_end` (calling hours end at 9 PM). Blocks keep their order and never overlap each other."""
    out: dict[str, tuple[datetime, int]] = {}
    taken = sorted(busy)
    cursor = datetime.combine(day, time(0, 0))
    limit = datetime.combine(day, last_end)
    for letter, hhmm, mins in planned:
        hh, mm = (int(x) for x in hhmm.split(":", 1))
        start = max(datetime.combine(day, time(hh, mm)), cursor)
        while start + timedelta(minutes=min_minutes) <= limit:
            clash = next(((a, b) for a, b in taken if a < start + timedelta(minutes=min_minutes) and b > start), None)
            if clash is not None:
                start = clash[1]
                continue
            nxt = min((a for a, _ in taken if a >= start), default=limit)
            length = int(min(mins, (min(nxt, limit) - start).total_seconds() // 60))
            out[letter] = (start, length)
            cursor = start + timedelta(minutes=length)
            taken.append((start, cursor))
            taken.sort()
            break
    return out


def _exists(session: Session, key: str) -> bool:
    return bool(session.scalar(select(CalendarEvent.id).where(CalendarEvent.key == key,
                                                              CalendarEvent.status.in_(("pending", "synced")))))


def _catchup_slot(session: Session, now: datetime, placed: dict[str, tuple[datetime, int]],
                  busy: list[tuple[datetime, datetime]]) -> dict[str, tuple[datetime, int]]:
    """Where today's 📞 Catch-up calls go: the first free 30+ minutes after the last block; when there is none (an
    evening class, 2026-11-04) or that time has passed with calls still left, the first free 30+ minutes from now.
    Once placed it stays put (saved in the calendar state) until its time passes."""
    day = now.date()
    st = _state(session).value_json or {}
    saved = st.get("catchup") or {}
    if saved.get("date") == day.isoformat():
        start = datetime.combine(day, time(*(int(x) for x in str(saved["start"]).split(":"))))
        if start + timedelta(minutes=int(saved["mins"])) >= now:
            return {"X": (start, int(saved["mins"]))}
    spans = busy + [(s, s + timedelta(minutes=m)) for s, m in placed.values()]
    ends = [s + timedelta(minutes=m) for s, m in placed.values()]
    soon = (now + timedelta(minutes=15 - now.minute % 15)).replace(second=0, microsecond=0)
    for begin in ([max(ends)] if ends else []) + [soon]:
        if begin.date() != day:
            continue
        got = place_blocks(day, [("X", f"{begin:%H:%M}", 60)], spans, min_minutes=30).get("X")
        if got and got[0] >= now - timedelta(minutes=5):           # a new slot never starts in the past
            _set_state(session, catchup={"date": day.isoformat(), "start": f"{got[0]:%H:%M}", "mins": got[1]})
            return {"X": got}
    return {}


def call_block_events(session: Session, now: datetime) -> list[Desired]:
    """📞 The day's call sheet (engine/call_sheet.py) as four events of 25 calls: one word-for-word script, then one
    line per call with the single number to use. Built from the frozen sheet, so "A07" means the same call all day.
    Streets (or "lead #<id>" when suppressed), the agent's first name, brokerage and advertised number only.

    What is left stays obvious (2026-11-04: classes make the operator skip calls and come back later): a reported call
    is ✅ and drops to the "Done" part, the title counts what is left, a block with nothing left turns graphite
    ("✅ … all done"), and the calls a passed block didn't reach move to one "📞 Catch-up calls" event in free time
    (same codes, so "B07 vm" still works). With no free 30 minutes left today (classes, evening events), they are
    added to the next block that hasn't ended instead; after the last block they go on tomorrow's sheet by themselves.
    A passed block keeps updating, never re-created."""
    from engine import call_sheet as cs
    if not cfg().get("call_blocks", True):
        return []
    rows = cs.stored(session, now.date())
    out: list[Desired] = []
    st = _state(session).value_json or {}
    b = st.get("busy") or {}
    busy = []
    if b.get("date") == now.date().isoformat():
        for a0, b0 in b.get("spans") or []:
            busy.append(tuple(datetime.combine(now.date(), time(*(int(x) for x in v.split(":")))) for v in (a0, b0)))
    placed = place_blocks(now.date(), cs.blocks(), busy)
    done_color = str((cfg().get("colors") or {}).get("done") or "8")
    catch_key = f"callblock:{now:%Y%m%d}:catchup"
    catch = _catchup_slot(session, now, {k: v for k, v in placed.items() if cs.block_rows(rows, k)}, busy)
    catch_at = _when(catch["X"][0]).split(", ")[-1] if catch.get("X") else None
    # pass 1: what each block holds now
    info: list[dict[str, Any]] = []
    for letter, _hhmm, _mins in cs.blocks():
        mine = cs.block_rows(rows, letter)
        if not mine or letter not in placed:
            continue
        start, mins = placed[letter]
        key = f"callblock:{now:%Y%m%d}:{letter}"
        passed = start + timedelta(minutes=mins) < now
        mine = [cs.refreshed(session, r, now.date()) for r in mine]
        info.append({"letter": letter, "start": start, "mins": mins, "key": key, "passed": passed,
                     "show": not passed or _exists(session, key),
                     "open": [r for r in mine if cs.row_state(r) == "open"],
                     "done": [r for r in mine if cs.row_state(r) == "done"],
                     "skip": [r for r in mine if cs.row_state(r) == "skip"]})
    missed = [r for i in info if i["passed"] for r in i["open"]]
    host = None if catch.get("X") else next((i for i in info if not i["passed"] and i["show"]), None)
    where = (f"📞 Catch-up calls{' at ' + catch_at if catch_at else ''}" if catch.get("X") else
             f"block {host['letter']} (end of its list)" if host else "tomorrow's sheet (by itself)")
    # pass 2: the events
    for i in info:
        if not i["show"]:
            continue                                  # never create one in the past (an existing one keeps updating)
        letter, open_, done, skip = i["letter"], i["open"], i["done"], i["skip"]
        extra = missed if host is i else []
        offers = sum(1 for r in open_ if r.say.startswith("OFFER"))
        asks = sum(1 for r in open_ if r.say.startswith("ASK"))
        tail = (f" · ✅ {len(done)} done" if done else "")
        if not open_ and not extra:
            summary = f"✅ Call block {letter}: all done ({len(done)} called" + (
                f", {len(skip)} no call needed)" if skip else ")")
        elif i["passed"]:
            summary = f"⏩ Call block {letter}: {len(open_)} moved to " + (
                "Catch-up calls" if catch.get("X") else f"block {host['letter']}" if host else "tomorrow") + tail
        else:
            summary = (f"📞 Call block {letter}: {len(open_)} to call" + (f" + {len(extra)} from earlier" if extra else "")
                       + tail + (f" ({offers} offers, {asks} ask their number)" if offers or asks else ""))
        head = "--- The calls (code | number agent | house | facts | what to say) ---"
        if i["passed"] and open_:
            head = f"--- Not reached: moved to {where} (same codes) ---"
        lines = [*cs.block_script(letter, session), "", head, *[r.line() for r in open_]]
        if extra:
            lines += ["", f"--- Also from earlier blocks, if you have time ({len(extra)}; same codes) ---",
                      *[r.line() for r in extra]]
        if done:
            lines += ["", f"--- ✅ Done ({len(done)}) ---", *["✅ " + r.line() for r in done]]
        if skip:
            lines += ["", f"--- ⏭ No call needed today ({len(skip)}) ---", *["⏭ " + r.line() for r in skip]]
        s0, e0 = _timed(i["start"], i["mins"])
        quiet = i["passed"] or not (open_ or extra)
        body = _body("callblock", summary, lines, s0, e0, reminders=[] if quiet else [10])
        if quiet:
            body["colorId"] = done_color              # nothing more to do in this block
        out.append(Desired(i["key"], "callblock", None, body, i["start"]))
    if catch.get("X") and (missed or _exists(session, catch_key)):
        start, mins = catch["X"]
        if start + timedelta(minutes=mins) >= now or _exists(session, catch_key):
            mins = max(30, min(mins, 10 + 3 * len(missed)))
            if missed:
                summary = f"📞 Catch-up calls: {len(missed)} you didn't reach earlier"
                lines = ["Calls from today's blocks that time ran out on (class, no time). Same codes as their block, "
                         'so report them the same way ("B07 vm, C03 340").',
                         "Call them any time before 8:30 PM; anything left goes on tomorrow's sheet by itself.",
                         *cs.block_script(missed[0].code[0], session), "", "--- Still to call ---",
                         *[r.line() for r in missed]]
                body = _body("catchup", summary, lines, *_timed(start, mins), reminders=[10])
            else:
                body = _body("catchup", "✅ Catch-up calls: all done", ["Every call from today's blocks is done or "
                                                                        "needs no call."], *_timed(start, mins),
                             reminders=[])
                body["colorId"] = done_color
            out.append(Desired(catch_key, "catchup", None, body, start))
    return out


def desk_status_event(session: Session, now: datetime) -> Desired | None:
    """📊 One all-day event per day that your Mac keeps current: what went out, what came back and exactly what waits
    on you (with the command). It is how the deal desk reports when you only read the calendar (and how a Claude
    chat in the cloud, which can't reach the Mac, reads the day). Streets and numbers only: no agent message text,
    no homeowner names; a Daniel's Law / stop address shows as "lead #<id>"."""
    if not cfg().get("desk_status_event", True):
        return None
    from engine import agent_outreach as ao
    from engine import gmail_autopilot as gm
    day0 = datetime.combine(now.date(), time(0, 0))
    rows = gm.inbox_rows(session, now, days=1)

    def street(lead_id: int | None, fallback: str) -> str:
        lead = session.get(Lead, lead_id) if lead_id else None
        return _place(session, lead).street if lead is not None else (fallback or "agent reply")

    waiting = [r for r in rows if r["status"] == "needs_approval"]
    queued = [r for r in rows if r["status"] in ("auto_queued", "sending")]
    sent_rows = list(session.scalars(select(Interaction).where(
        Interaction.occurred_at >= day0, Interaction.channel.in_(("email_smtp", "email_gmail")),
        Interaction.outcome.in_(tuple(AGENT_TOUCH_WORDS)))))
    def once(items: list[Any], lead: Callable[[Any], Any]) -> list[Any]:
        """One line per house: a second reply or a re-sent offer on the same lead isn't a second house."""
        seen: set[Any] = set()
        return [x for x in items if not (lead(x) in seen or seen.add(lead(x)))]

    offers = once(sorted((i for i in sent_rows if i.outcome == "OFFER_SENT_TO_AGENT"), key=lambda i: i.occurred_at,
                         reverse=True), lambda i: i.lead_id)
    openers = sum(1 for i in sent_rows if i.outcome == "AGENT_OPENER_SENT")
    replies_today = sum(1 for r in rows if r["received_at"] and str(r["received_at"])[:10] == now.date().isoformat())
    summary = (f"📊 Deal desk: {len(sent_rows)} sent · {replies_today} repl{'y' if replies_today == 1 else 'ies'} · "
               f"{len(waiting)} waiting on you")
    lines = [f"Updated {_when(now)} by {'the cloud desk' if in_cloud() else 'your Mac'}.", ""]
    if waiting:
        lines.append(f"WAITING ON YOU ({len(waiting)}):")
        for r in waiting:
            st = street(r["lead_id"], r["address"])
            lines.append(f"- #{r['id']} {st}: {r['light']} -> call them (the number is on the 📞 event), or say "
                         f"\"send #{r['id']}\" to Claude")
        lines.append("")
    if queued:
        lines.append("GOING OUT ON THE NEXT CHECK: " + ", ".join(street(r["lead_id"], r["address"]) for r in queued))
    stuck = [r for r in rows if str(r.get("note") or "").startswith("not sent yet")]
    if stuck:
        lines.append("GMAIL DIDN'T TAKE IT (retries by itself): " + ", ".join(street(r["lead_id"], r["address"])
                                                                            for r in stuck))
    if offers:
        lines.append("OFFERS SENT TODAY: " + "; ".join(
            f"{street(i.lead_id, '')} {_money(i.lead.offer_amount) if i.lead and i.lead.offer_amount else ''}".strip()
            for i in offers[:15]))
    answered = once([r for r in rows if r["status"] in ("auto_sent", "approved") and not r["offer_sent"]],
                    lambda r: r["lead_id"])
    if answered:
        lines.append("ANSWERED WITHOUT A PRICE: " + "; ".join(f"{street(r['lead_id'], r['address'])} ({r['you_do']})"
                                                            for r in answered[:10]))
    calls = once([r for r in rows if "call them" in r["you_do"]], lambda r: r["lead_id"])
    if calls:
        lines.append("CALL: " + "; ".join(f"{street(r['lead_id'], r['address'])} {r['phone'] or ''}".strip()
                                          for r in calls))
    lines += ["", f"Openers today: {openers}. Cold e-mails in the last 24 hours: {ao.sent_last_24h(session, now, cold_only=True)}"
              f" of {int((settings().get('outreach', {}) or {}).get('daily_email_limit', 25))}."]
    s, e = _all_day(now.date())
    return Desired(f"desk:{now:%Y%m%d}", "desk", None,
                   _body("desk", summary, lines, s, e, reminders=[], transparent=True), day0)


def actions_event(session: Session, now: datetime) -> Desired | None:
    """✅ Today's ten (2026-11-09 pivot, `deal_desk.daily_actions`): one block of at most ten high-leverage actions in
    the first free 45 minutes from `actions_time` (11:00), fitted around classes like the old call blocks. Weekdays
    only; nothing to do = no event."""
    from engine import deal_desk
    if not cfg().get("actions_event", True) or now.weekday() >= 5:
        return None
    items = deal_desk.daily_actions(session, now)
    if not items:
        return None
    st = _state(session).value_json or {}
    b = st.get("busy") or {}
    busy = []
    if b.get("date") == now.date().isoformat():
        for a0, b0 in b.get("spans") or []:
            busy.append(tuple(datetime.combine(now.date(), time(*(int(x) for x in v.split(":")))) for v in (a0, b0)))
    placed = place_blocks(now.date(), [("T", str(cfg().get("actions_time") or "11:00"), 45)], busy,
                          min_minutes=30).get("T")
    start, mins = placed if placed else (datetime.combine(now.date(), time(11, 0)), 45)
    s, e = _timed(start, mins)
    lines = [f"{i}. {x}" for i, x in enumerate(items, 1)]
    lines += ["", "Tell Claude in the chat what you did (\"1 done, 3 no answer, 5 sent\") and it updates the desk.",
              f"Updated {_when(now)} by {'the cloud desk' if in_cloud() else 'your Mac'}."]
    return Desired(f"actions:{now:%Y%m%d}", "actions", None,
                   _body("actions", f"✅ Today's {len(items)}: replies, call-backs, flipper pitches", lines, s, e,
                         reminders=[10]), start)


def desired_events(session: Session, lead_ids: set[int] | None = None, now: datetime | None = None
                   ) -> dict[str, Desired]:
    """Everything the calendar should hold: all of it, or (lead_ids) only those leads' events."""
    now = now or now_local()
    today = now.date()
    out: dict[str, Desired] = {}

    def put(items: Iterable[Desired | None]) -> None:
        for d in items:
            if d is not None:
                out[d.key] = d

    if lead_ids is None:
        # An agent follow-up fires up to a day after the CRM due time (exactly 48 h vs the 10:00 call slot), and a
        # lead with a future event on the calendar must be looked at even if nothing else points to it.
        on_calendar = select(CalendarEvent.lead_id).where(CalendarEvent.status.in_(("pending", "synced")),
                                                          CalendarEvent.starts_at >= now,
                                                          CalendarEvent.lead_id.is_not(None))
        waiting = select(InboundReply.lead_id).where(InboundReply.status == "needs_approval",
                                                     InboundReply.lead_id.is_not(None))
        leads = list(session.scalars(select(Lead).where(or_(Lead.next_followup_at >= now - timedelta(days=2),
                                                            Lead.contract_date.is_not(None),
                                                            Lead.id.in_(on_calendar), Lead.id.in_(waiting)))))
        sale_ids = {lead.id for lead in priority_sale_leads(session, today)}
        put([daily_goal_event(session, today), reconnect_event(session, now)])
        try:
            put([desk_status_event(session, now)])
        except Exception:                                # noqa: BLE001 - a status line never blocks the real events
            log.exception("calendar: desk status event skipped")
        try:
            put([actions_event(session, now)])
        except Exception:                                # noqa: BLE001
            log.exception("calendar: daily actions skipped")
        try:
            put(call_block_events(session, now))
        except Exception:                                # noqa: BLE001
            log.exception("calendar: call blocks skipped")
    else:
        leads = [lead for lead in (session.get(Lead, i) for i in sorted(lead_ids)) if lead is not None]
        # A lead's sale event is refreshed here only when it is already on the calendar; which sales make the
        # calendar at all is decided by the full sync (listed foreclosures and the top of the list).
        sale_ids = set(session.scalars(select(CalendarEvent.lead_id).where(
            CalendarEvent.kind == "sheriff_sale", CalendarEvent.lead_id.in_(lead_ids),
            CalendarEvent.status.in_(("pending", "synced")))))
    from engine import call_sheet as cs
    on_sheet = cs.lead_ids_on(session, today)
    for lead in leads:
        # A lead on today's call sheet is called from its block: no second event for the same call today.
        put(d for d in followup_events(session, lead, now) if not (lead.id in on_sheet and d.starts_at.date() == today))
        put(contract_events(session, lead, now))
        put(reply_events(session, lead, now))
    for lid in sale_ids:
        lead = session.get(Lead, lid)
        if lead is not None:
            put([sale_event(session, lead, today)])
    return out


# -- done marks ---------------------------------------------------------------------------------------
DONE_KINDS = {"followup", "callback", "reply"}
_OUTCOME_WORDS = {"NO_ANSWER": "called, no answer", "LEFT_VM": "called, left a voicemail",
                  "WRONG_NUMBER": "called, wrong number", "SPOKE_WARM_FOLLOWUP": "talked, follow-up set",
                  "SPOKE_NEEDS_OFFER": "talked, they want the offer", "SPOKE_NOT_INTERESTED": "not interested",
                  "DEAD": "closed out", "DNC": "asked not to be contacted", "OFFER_SENT_TO_AGENT": "offer e-mailed",
                  "AGENT_FOLLOWUP_SENT": "follow-up e-mailed", "AGENT_REPLIED": "they replied",
                  "OFFER_ACCEPTED": "offer accepted"}


def handled(session: Session, r: CalendarEvent) -> tuple[str, bool] | None:
    """(what settled this call event, with the time; was it the operator's own call / answer?), or None while it
    still waits."""
    if r.lead_id is None:
        return None
    if r.kind == "reply":
        m = re.match(r"reply:\d+:r(\d+)$", r.key or "")
        row = session.get(InboundReply, int(m.group(1))) if m else None
        if row is None or row.status in ("needs_approval", "auto_queued", "sending"):
            return None
        when = _when(row.resolved_at) if row.resolved_at else "today"
        return f"{when}: " + ("answered" if row.status == "approved" else "handled, no answer needed"), True
    m = re.match(r"followup:\d+:i(\d+)$", r.key or "")
    q = select(Interaction).where(Interaction.lead_id == r.lead_id)
    if m:
        q = q.where(Interaction.id > int(m.group(1)))
    else:
        q = q.where(Interaction.occurred_at >= (r.starts_at or datetime.min) - timedelta(hours=12))
    inter = session.scalars(q.order_by(Interaction.occurred_at.desc()).limit(1)).first()
    if inter is None:
        return None
    words = _OUTCOME_WORDS.get(inter.outcome or "", (inter.outcome or "done").lower().replace("_", " "))
    mine = inter.channel == "call" or (inter.debrief_json or {}).get("source") == "chat_note"
    return f"{_when(inter.occurred_at)}: {words}", mine


def moved_to(want: dict[str, Desired], r: CalendarEvent) -> Desired | None:
    """The lead's next call event, when this one was replaced by a later one (same lead, a call kind)."""
    later = [d for d in want.values() if d.lead_id == r.lead_id and d.kind in DONE_KINDS and d.key != r.key
             and d.starts_at >= (r.starts_at or datetime.min)]
    return min(later, key=lambda d: d.starts_at) if later else None


def moved_body(body: dict[str, Any], when: datetime) -> dict[str, Any]:
    b = dict(body)
    summary = re.sub(r"^[^\w$#(]+", "", str(b.get("summary") or "")).strip()
    at = _when(when).split(", ", 1)[-1] if when.date() == (parse_start(body.get("start")) or when).date() else _when(when)
    b["summary"] = f"⏩ {summary} (moved to {at})"
    b["colorId"] = str((cfg().get("colors") or {}).get("done") or "8")
    b["description"] = f"⏩ MOVED to {_when(when)}: that event is the one to use.\n\n" + str(b.get("description") or "")
    b["reminders"] = {"useDefault": False, "overrides": []}
    return b


def done_body(body: dict[str, Any], label: str) -> dict[str, Any]:
    b = dict(body)
    summary = re.sub(r"^[^\w$#(]+", "", str(b.get("summary") or "")).strip()
    b["summary"] = "✅ " + summary
    b["colorId"] = str((cfg().get("colors") or {}).get("done") or "8")
    b["description"] = f"✅ DONE {label}. Nothing more to do here.\n\n" + str(b.get("description") or "")
    b["reminders"] = {"useDefault": False, "overrides": []}
    return b


# -- the outbox --------------------------------------------------------------------------------------
def reconcile(session: Session, lead_ids: set[int] | None = None, now: datetime | None = None) -> dict[str, int]:
    """Bring calendar_events in line with the pipeline: new rows, changed bodies, future events no longer needed."""
    now = now or now_local()
    want = desired_events(session, lead_ids, now)
    stmt = select(CalendarEvent)
    if lead_ids is not None:
        stmt = stmt.where(or_(CalendarEvent.lead_id.in_(lead_ids), CalendarEvent.key.in_(list(want))))
    rows = {r.key: r for r in session.scalars(stmt)}
    stats = {"created": 0, "changed": 0, "cancelled": 0}
    for key, d in want.items():
        r = rows.get(key)
        body = d.body
        if r is not None and r.operator_edit_json and d.kind in USER_EDITABLE:
            body = {**body, **r.operator_edit_json}
        h = body_hash(body)
        if r is None:
            session.add(CalendarEvent(key=key, kind=d.kind, lead_id=d.lead_id, status="pending", body_json=body,
                                      body_hash=h, starts_at=parse_start(body.get("start")) or d.starts_at))
            stats["created"] += 1
        elif r.status == "dismissed":
            continue
        elif r.body_hash != h or r.status in ("cancel", "cancelled"):
            r.body_json, r.body_hash, r.kind = body, h, d.kind
            r.starts_at = parse_start(body.get("start")) or d.starts_at
            if r.status in ("cancel", "cancelled") or r.synced_hash != h:
                r.status = "pending"
            stats["changed"] += 1
    for key, r in rows.items():
        if key in want or r.status not in ("pending", "synced") or (r.kind == "daily_goal" and not openers_paused()):
            continue
        if r.kind == "daily_goal":
            r.status = "cancel" if r.google_event_id else "cancelled"     # the recurring goal goes with the openers
            stats["cancelled"] += 1
            continue
        if str((r.body_json or {}).get("summary") or "").startswith(("✅", "⏩")):
            continue                                     # already marked done / moved: it stays as the record
        if r.kind in DONE_KINDS and r.starts_at is not None and r.starts_at.date() <= now.date() \
                and r.starts_at >= now - timedelta(days=3):
            got = handled(session, r)
            nxt = moved_to(want, r)
            body = None
            if got and (got[1] or nxt is None):
                # Done: the event stays where it was, ✅ and graphite, with what settled it (2026-11-04: the operator
                # couldn't tell finished calls from the ones still waiting after a class).
                body = done_body(r.body_json or {}, got[0])
            elif nxt is not None:
                # Not done by the operator, just rescheduled (a new call slot, a moved call-back): it says where it went,
                # never "done" (2026-11-04: Anna's 1:45 call showed ✅ after the desk e-mailed her and set 5:15 PM).
                body = moved_body(r.body_json or {}, nxt.starts_at)
            if body is not None:
                if r.google_event_id:
                    r.body_json = body
                    r.body_hash, r.status = body_hash(body), "pending"
                else:
                    r.status = "cancelled"               # never reached the calendar: nothing to mark
                stats["done"] = stats.get("done", 0) + 1
                continue
        if r.starts_at is not None and r.starts_at < now:
            continue                                     # it already happened: leave it on the calendar as history
        r.status = "cancel" if r.google_event_id else "cancelled"
        stats["cancelled"] += 1
    session.flush()
    return stats


def wire_body(row: CalendarEvent) -> dict[str, Any]:
    return {**(row.body_json or {}), "id": event_id(row.key), "status": "confirmed",
            "extendedProperties": {"private": {"njw": "1", "njwKey": row.key, "njwKind": row.kind,
                                               "njwLead": str(row.lead_id or "")}}}


# -- Google API --------------------------------------------------------------------------------------
class CalendarAPI:
    """The five calls we make, with Google's errors turned into offline / auth / not-found / conflict."""

    def __init__(self, service: Any, calendar_id: str = "primary") -> None:
        self.service = service
        self.calendar_id = calendar_id

    def _run(self, request: Any) -> Any:
        import httplib2
        from google.auth.exceptions import RefreshError, TransportError
        from googleapiclient.errors import HttpError
        try:
            return request.execute(num_retries=1)
        except HttpError as exc:
            status = int(getattr(exc.resp, "status", 0) or 0)
            text = str(exc)
            if status in (404, 410):
                raise NotFound(text) from exc
            if status == 409:
                raise Conflict(text) from exc
            if status == 429 or status >= 500 or (status == 403 and "ateLimitExceeded" in text):
                raise CalendarOffline(f"Google is busy ({status}); queued for the next sync") from exc
            if status in (401, 403):
                raise CalendarAuthError(f"Google refused the request ({status}): {text[:200]}. Check that the "
                                        "Google Calendar API is enabled, then run python manage.py setup-calendar")
            raise CalendarError(f"Google said {status}: {text[:300]}") from exc
        except RefreshError as exc:
            raise CalendarAuthError("Google sign-in expired or was revoked: run python manage.py setup-calendar") \
                from exc
        except (TransportError, httplib2.HttpLib2Error, socket.timeout, OSError) as exc:
            raise CalendarOffline(f"can't reach Google ({exc.__class__.__name__}); queued for the next sync") from exc

    @property
    def _events(self) -> Any:
        return self.service.events()

    def insert(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(self._events.insert(calendarId=self.calendar_id, body=body, sendUpdates="none"))

    def update(self, eid: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._run(self._events.update(calendarId=self.calendar_id, eventId=eid, body=body, sendUpdates="none"))

    def get(self, eid: str) -> dict[str, Any]:
        return self._run(self._events.get(calendarId=self.calendar_id, eventId=eid))

    def delete(self, eid: str) -> None:
        self._run(self._events.delete(calendarId=self.calendar_id, eventId=eid, sendUpdates="none"))

    def busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """The operator's own timed events (classes, meetings) between start and end, as naive local times: ours
        (njw=1), all-day and "free" events are skipped. Used only to fit the call blocks around them."""
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name())
        page = self._run(self._events.list(calendarId=self.calendar_id, timeMin=start.replace(tzinfo=tz).isoformat(),
                                           timeMax=end.replace(tzinfo=tz).isoformat(), singleEvents=True,
                                           orderBy="startTime", maxResults=250))
        out: list[tuple[datetime, datetime]] = []
        for it in page.get("items", []):
            if ((it.get("extendedProperties") or {}).get("private") or {}).get("njw") or \
                    it.get("transparency") == "transparent" or it.get("status") == "cancelled":
                continue
            a, b = (it.get("start") or {}).get("dateTime"), (it.get("end") or {}).get("dateTime")
            if not a or not b:
                continue
            out.append(tuple(datetime.fromisoformat(x.replace("Z", "+00:00")).astimezone(tz).replace(tzinfo=None)
                             for x in (a, b)))
        return out

    def changed(self, since: str | None) -> list[dict[str, Any]]:
        """Our events (private property njw=1) changed since `since`, deleted ones included."""
        items: list[dict[str, Any]] = []
        token = None
        while True:
            params: dict[str, Any] = {"calendarId": self.calendar_id, "privateExtendedProperty": "njw=1",
                                      "showDeleted": True, "maxResults": 250}
            if since:
                params["updatedMin"] = since
            if token:
                params["pageToken"] = token
            page = self._run(self._events.list(**params))
            items += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return items

    def probe(self) -> dict[str, Any]:
        page = self._run(self._events.list(calendarId=self.calendar_id, maxResults=1))
        return {"calendar": page.get("summary"), "time_zone": page.get("timeZone")}


_api_factory: Callable[[Any, str], CalendarAPI] | None = None


def set_api_factory(factory: Callable[[Any, str], CalendarAPI] | None) -> None:
    """Tests (and nothing else) plug a fake Google here."""
    global _api_factory
    _api_factory = factory


def save_token(creds: Any) -> None:
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(creds.to_json())
    try:
        TOKEN.chmod(0o600)
    except OSError:
        pass


def load_credentials() -> Any:
    from google.auth.exceptions import RefreshError, TransportError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    if not TOKEN.is_file():
        raise CalendarAuthError("Google Calendar isn't connected: run python manage.py setup-calendar")
    creds = Credentials.from_authorized_user_file(str(TOKEN))      # the scopes it was granted with
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise CalendarAuthError("the saved Google sign-in can't be renewed: run python manage.py setup-calendar")
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            raise CalendarAuthError("Google sign-in expired or was revoked (apps left in 'Testing' expire after 7 "
                                    "days): run python manage.py setup-calendar") from exc
        except TransportError as exc:
            raise CalendarOffline("can't reach Google to renew the sign-in; queued for the next sync") from exc
        save_token(creds)
    return creds


def target_calendar() -> str:
    """The calendar the events go to: `primary` = the signed-in account's own calendar, or the e-mail address of a
    calendar that account may edit (calendar.calendar_id in settings.local.yaml, set by setup-calendar)."""
    return str(cfg().get("calendar_id") or "primary").strip() or "primary"


def api(creds: Any = None, calendar_id: str | None = None) -> CalendarAPI:
    calendar_id = calendar_id or target_calendar()
    if _api_factory is not None:
        return _api_factory(creds, calendar_id)
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build
    http = AuthorizedHttp(creds or load_credentials(), http=httplib2.Http(timeout=20))
    service = build("calendar", "v3", http=http, cache_discovery=False, static_discovery=True)
    return CalendarAPI(service, calendar_id)


def signed_in_email(creds: Any) -> str | None:
    """The account that just signed in, from the ID token Google returns with the `openid email` scopes."""
    token = getattr(creds, "id_token", None)
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    part = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, binascii.Error):
        return None
    email = str(claims.get("email") or "").strip().lower()
    return email or None


# -- state -------------------------------------------------------------------------------------------
def _state(session: Session) -> SwarmState:
    row = session.get(SwarmState, STATE_KEY)
    if row is None:
        row = SwarmState(key=STATE_KEY, value_json={})
        session.add(row)
    return row


def _set_state(session: Session, **values: Any) -> None:
    row = _state(session)
    row.value_json = {**(row.value_json or {}), **values}


# -- sync --------------------------------------------------------------------------------------------
def pull(session: Session, cal: CalendarAPI, now: datetime | None = None) -> dict[str, int]:
    """What the operator changed in Google: deleted -> dismissed; a moved follow-up moves the CRM follow-up;
    a moved legal date is queued to be put back."""
    now = now or now_local()
    state = _state(session).value_json or {}
    started = datetime.now(timezone.utc) - timedelta(seconds=60)
    stats = {"seen": 0, "dismissed": 0, "moved": 0, "restored": 0}
    by_key: dict[str, CalendarEvent] = {}
    try:
        items = cal.changed(state.get("last_pull"))
    except NotFound:                                     # 410: "updatedMin too old" -> read everything once
        items = cal.changed(None)
    for item in items:
        if item.get("recurringEventId"):
            continue                                     # one changed instance of the daily reminder: theirs to keep
        key = ((item.get("extendedProperties") or {}).get("private") or {}).get("njwKey")
        row = by_key.get(key) if key else None
        row = row or (session.scalar(select(CalendarEvent).where(CalendarEvent.key == key)) if key else None)
        if row is None:
            continue
        by_key[row.key] = row
        stats["seen"] += 1
        if item.get("status") == "cancelled":
            if row.status in ("synced", "pending"):
                row.status = "dismissed"
                stats["dismissed"] += 1
            elif row.status == "cancel":
                row.status = "cancelled"
            continue
        if row.status != "synced":
            continue                                     # a local change is waiting to go up: it wins
        g_start, g_end = _normalized(item.get("start")), _normalized(item.get("end"))
        if g_start is None or parse_start(g_start) == parse_start((row.body_json or {}).get("start")):
            continue
        if row.kind in USER_EDITABLE:
            moved = parse_start(g_start)
            row.body_json = {**(row.body_json or {}), "start": g_start, "end": g_end}
            row.body_hash = row.synced_hash = body_hash(row.body_json)
            row.starts_at = moved
            if row.kind == "daily_goal":
                row.operator_edit_json = {"start": g_start, "end": g_end}
            elif row.lead_id:
                lead = session.get(Lead, row.lead_id)
                if lead is not None and lead.status_stage not in TERMINAL:
                    lead.next_followup_at = moved
            stats["moved"] += 1
        else:
            row.status, row.synced_hash = "pending", None     # our date is the legal one: put it back
            stats["restored"] += 1
    _set_state(session, last_pull=started.isoformat(timespec="seconds"))
    session.flush()
    return stats


def push(session: Session, cal: CalendarAPI) -> dict[str, int]:
    """Send queued rows. Stops at the first network / auth problem; everything left stays queued."""
    stats = {"created": 0, "updated": 0, "deleted": 0, "dismissed": 0, "failed": 0}
    rows = session.scalars(select(CalendarEvent).where(CalendarEvent.status.in_(("pending", "cancel")))
                           .order_by(CalendarEvent.starts_at.is_(None), CalendarEvent.starts_at)).all()
    for r in rows:
        eid = r.google_event_id or event_id(r.key)
        try:
            if r.status == "cancel":
                try:
                    cal.delete(eid)
                except NotFound:
                    pass
                r.status = "cancelled"
                stats["deleted"] += 1
            else:
                body = wire_body(r)
                if r.google_event_id:
                    cal.update(eid, body)                # also brings back an event we cancelled earlier
                    stats["updated"] += 1
                else:
                    try:
                        cal.insert(body)
                        stats["created"] += 1
                    except Conflict:
                        existing = cal.get(eid)          # another machine (or a lost row) created it already
                        if existing.get("status") == "cancelled":
                            r.status, r.google_event_id = "dismissed", eid
                            stats["dismissed"] += 1
                            continue
                        cal.update(eid, body)
                        stats["updated"] += 1
                r.google_event_id = eid
                r.status, r.synced_hash = "synced", r.body_hash
            r.synced_at, r.attempts, r.last_error = utcnow(), 0, None
        except (CalendarOffline, CalendarAuthError) as exc:
            r.attempts, r.last_error = (r.attempts or 0) + 1, str(exc)[:500]
            stats["failed"] += 1
            session.flush()
            raise
        except CalendarError as exc:                     # this one event is bad: note it, carry on with the rest
            r.attempts, r.last_error = (r.attempts or 0) + 1, str(exc)[:500]
            stats["failed"] += 1
    session.flush()
    return stats


@dataclass
class SyncResult:
    pulled: dict[str, int] = field(default_factory=dict)
    reconciled: dict[str, int] = field(default_factory=dict)
    pushed: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    queued: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"pulled": self.pulled, "reconciled": self.reconciled, "pushed": self.pushed, "error": self.error,
                "queued": self.queued}


_sync_lock = threading.RLock()


def sync(session: Session, lead_ids: set[int] | None = None, *, cal: CalendarAPI | None = None,
         now: datetime | None = None) -> SyncResult:
    """Pull Google's changes -> rebuild the wanted events -> push the queue. Never raises for offline / signed-out:
    the error is reported and the rows stay queued."""
    session.info[WORKER_FLAG] = True
    out = SyncResult()
    if cal is None and handed_off():
        out.error = "the cloud desk keeps the calendar now (this computer's copy of the database is out of date)"
        return out
    with _sync_lock:
        connected = cal is not None or configured()
        try:
            cal = cal or (api() if connected else None)
        except CalendarError as exc:
            out.error, cal = str(exc), None
        if cal is not None:
            try:
                out.pulled = pull(session, cal, now)
            except CalendarError as exc:
                out.error = str(exc)
            if lead_ids is None and not isinstance(out.error, str):
                note_busy(session, cal, now or now_local())
        out.reconciled = reconcile(session, lead_ids, now)
        if cal is not None and not isinstance(out.error, str):
            # Release SQLite's write lock before talking to Google: a slow or offline Google must never stall the
            # cockpit's own writes (push only flushes its results at the end). The leads this sync just covered are
            # dropped from the session's queue first, so the commit doesn't start a second sync for them.
            if lead_ids is None:
                session.info.pop("gcal_leads", None)
                session.info.pop("gcal_props", None)
            elif session.info.get("gcal_leads"):
                session.info["gcal_leads"].difference_update(lead_ids)
            session.commit()
            try:
                out.pushed = push(session, cal)
                _set_state(session, last_sync=utcnow().isoformat(timespec="seconds"), last_error=None,
                           needs_reauth=False)
            except CalendarAuthError as exc:
                out.error = str(exc)
                _set_state(session, last_error=out.error, needs_reauth=True)
            except CalendarError as exc:
                out.error = str(exc)
                _set_state(session, last_error=out.error)
        elif out.error:
            _set_state(session, last_error=out.error,
                       needs_reauth=connected and "setup-calendar" in out.error)
        elif not connected:
            out.error = "not connected: run python manage.py setup-calendar"
        out.queued = len(session.scalars(select(CalendarEvent.id).where(
            CalendarEvent.status.in_(("pending", "cancel")))).all())
        session.flush()
    return out


def status(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or now_local()
    counts: dict[str, int] = {}
    for s in session.scalars(select(CalendarEvent.status)):
        counts[s] = counts.get(s, 0) + 1
    upcoming = session.scalars(select(CalendarEvent).where(CalendarEvent.status.in_(("pending", "synced")),
                                                           CalendarEvent.starts_at >= now - timedelta(hours=1))
                               .order_by(CalendarEvent.starts_at).limit(8)).all()
    st = (session.get(SwarmState, STATE_KEY).value_json or {}) if session.get(SwarmState, STATE_KEY) else {}
    return {"configured": configured(), "credentials_file": CREDENTIALS.is_file(), "token_file": TOKEN.is_file(),
            "enabled": bool(cfg().get("enabled", True)), "counts": counts,
            "queued": counts.get("pending", 0) + counts.get("cancel", 0), "calendar": st.get("calendar"),
            "account": st.get("account") or cfg().get("account"), "calendar_id": target_calendar(),
            "connected_at": st.get("connected_at"),
            "last_sync": st.get("last_sync"), "last_error": st.get("last_error"),
            "needs_reauth": bool(st.get("needs_reauth")),
            "upcoming": [{"summary": (r.body_json or {}).get("summary"), "start": r.starts_at.isoformat()
                          if r.starts_at else None, "all_day": "date" in ((r.body_json or {}).get("start") or {}),
                          "status": r.status, "lead_id": r.lead_id} for r in upcoming],
            "setup_steps": None if configured() else SETUP_STEPS}


# -- one-time setup ----------------------------------------------------------------------------------
def _consent(want: str | None, *, open_browser: bool, port: int, timeout: int, flow_factory: Any) -> Any:
    if not CREDENTIALS.is_file():
        raise CalendarSetupError(SETUP_STEPS)
    try:
        data = json.loads(CREDENTIALS.read_text())
    except ValueError as exc:
        raise CalendarSetupError(f"{CREDENTIALS.name} isn't the JSON file Google gave you: download it again from "
                                 "Google Auth platform -> Clients") from exc
    if "installed" not in data:
        raise CalendarSetupError(f"{CREDENTIALS.name} is for a '{next(iter(data), '?')}' client. Create a client of "
                                 "type 'Desktop app' (Google Auth platform -> Clients -> Create client) and download "
                                 "that JSON instead")
    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow_factory = InstalledAppFlow.from_client_secrets_file
    # Google answers the e-mail scope in its own spelling; that is not a change in what was granted.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = flow_factory(str(CREDENTIALS), SCOPES + IDENTITY_SCOPES)
    extra = {"login_hint": want} if want else {}          # the Google window opens on that account
    try:
        creds = flow.run_local_server(
            port=port, open_browser=open_browser, timeout_seconds=timeout,
            authorization_prompt_message="Opening Google sign-in in your browser. If nothing opens, visit:\n{url}",
            success_message="The NJ Wholesale Cockpit is connected to your Google Calendar. You can close this tab.",
            **extra)
    except Exception as exc:  # noqa: BLE001  (timeout, closed tab, denied consent, blocked by an organization)
        raise CalendarSetupError(f"Google sign-in didn't finish ({exc.__class__.__name__}: {exc}). Run "
                                 "python manage.py setup-calendar again") from exc
    if creds is None:
        raise CalendarSetupError("Google sign-in didn't finish: run python manage.py setup-calendar again")
    return creds


def _remove_from(cal: CalendarAPI) -> int:
    """Delete every event this app put on a calendar we are leaving (other events are never touched)."""
    removed = 0
    for item in cal.changed(None):
        if item.get("status") == "cancelled" or item.get("recurringEventId"):
            continue
        try:
            cal.delete(item["id"])
            removed += 1
        except NotFound:
            pass
    return removed


def connect(session: Session, *, account: str | None = None, calendar_id: str | None = None,
            open_browser: bool = True, port: int = 0, timeout: int = 300, flow_factory: Any = None
            ) -> dict[str, Any]:
    """Browser consent (once) -> check WHO signed in and that the target calendar is writable -> move our events off
    the calendar used before, if any -> save token.json + the account choice (settings.local.yaml, git-ignored)."""
    from engine.operator_setup import save_preference
    session.info[WORKER_FLAG] = True
    want = (account or cfg().get("account") or "").strip().lower() or None
    target = (calendar_id or target_calendar()).strip() or "primary"
    creds = _consent(want, open_browser=open_browser, port=port, timeout=timeout, flow_factory=flow_factory)
    new = api(creds, target)
    try:
        info = new.probe()
    except (NotFound, CalendarAuthError) as exc:
        if target == "primary":
            raise
        raise CalendarSetupError(f"The account you signed in with can't add events to the calendar {target}. In "
                                 f"that calendar's settings, share it with the account you sign in with and choose "
                                 f"'Make changes to events', then run setup-calendar again ({exc})") from exc
    who = signed_in_email(creds)
    if who is None and target == "primary" and "@" in str(info.get("calendar") or ""):
        who = str(info["calendar"]).lower()            # a primary calendar is named after its account
    if want and who and who != want:
        raise CalendarSetupError(f"You signed in as {who}, but the reminders are set to go to {want}. Run "
                                 f"setup-calendar again and pick {want} in the Google window ('Use another account' "
                                 "if it isn't listed). Nothing was changed.")
    st = _state(session).value_json or {}
    before = (str(st.get("account") or st.get("calendar") or "").lower() or None,
              str(st.get("calendar_id") or "primary"))
    after = (who, target)
    had_events = session.scalar(select(CalendarEvent.id).where(CalendarEvent.google_event_id.is_not(None))
                                .limit(1)) is not None
    switched = had_events and before != after
    removed, cleanup_error = 0, None
    if switched and TOKEN.is_file():
        try:
            removed = _remove_from(api(load_credentials(), before[1]))
        except CalendarError as exc:
            cleanup_error = (f"couldn't remove the old events from {before[0] or 'the previous calendar'} ({exc}); "
                             "delete them there by hand if you want them gone")
    save_token(creds)
    save_preference("calendar", "account", who or want)
    save_preference("calendar", "calendar_id", target)
    if switched:
        for row in session.scalars(select(CalendarEvent)):
            row.google_event_id, row.synced_hash, row.attempts, row.last_error = None, None, 0, None
            row.status = "cancelled" if row.status in ("cancel", "cancelled") else "pending"
    _set_state(session, account=who or want, calendar_id=target, calendar=info.get("calendar"), last_pull=None,
               connected_at=now_local().isoformat(timespec="minutes"),
               needs_reauth=False, last_error=None)
    session.flush()
    return {**info, "account": who or want, "calendar_id": target, "switched": switched, "removed_old": removed,
            "previous": before[0] if switched else None, "cleanup_error": cleanup_error}


def setup(*, open_browser: bool = True, port: int = 0, timeout: int = 300, flow_factory: Any = None,
          account: str | None = None, calendar_id: str | None = None) -> dict[str, Any]:
    """`connect` in its own database session (the CLI and the cockpit button)."""
    from db.database import session_scope
    with session_scope() as s:
        return connect(s, account=account, calendar_id=calendar_id, open_browser=open_browser, port=port,
                       timeout=timeout, flow_factory=flow_factory)


_connect_thread: threading.Thread | None = None
_connect_result: dict[str, Any] = {}


def connect_in_background(timeout: int = 300, account: str | None = None) -> dict[str, Any]:
    """The cockpit's Connect button: the consent page opens in the laptop's browser while the request returns."""
    global _connect_thread
    if not CREDENTIALS.is_file():
        raise CalendarSetupError(SETUP_STEPS)
    if _connect_thread is not None and _connect_thread.is_alive():
        return {"started": False, "message": "a Google sign-in window is already open"}

    def run() -> None:
        _connect_result.clear()
        try:
            info = setup(timeout=timeout, account=account)
            from db.database import session_scope
            with session_scope() as s:
                r = sync(s)
            _connect_result.update({"ok": True, **info, "sync": r.to_json()})
        except Exception as exc:  # noqa: BLE001
            _connect_result.update({"ok": False, "error": str(exc)})

    _connect_thread = threading.Thread(target=run, name="gcal-connect", daemon=True)
    _connect_thread.start()
    return {"started": True, "message": "A Google sign-in page is opening in your browser."}


def connect_result() -> dict[str, Any]:
    running = _connect_thread is not None and _connect_thread.is_alive()
    return {"running": running, **({} if running else _connect_result)}


# -- automatic sync: session hooks + a background worker ---------------------------------------------
WATCH_LEAD = ("next_followup_at", "status_stage", "contract_date", "attorney_review_ends")


def _changed(obj: Any, *attrs: str) -> bool:
    state = inspect(obj)
    return any(state.attrs[a].history.has_changes() for a in attrs)


def _after_flush(session: Session, _ctx: Any) -> None:
    """Note which leads a commit touches. It must never break the commit (or the click) it rides on."""
    try:
        _collect(session)
    except Exception:                      # noqa: BLE001 - a calendar problem is logged, the save goes through
        log.exception("calendar hook skipped")


def _collect(session: Session) -> None:
    if session.info.get(WORKER_FLAG) or not configured():
        return
    leads: set[int] = session.info.setdefault("gcal_leads", set())
    props: set[int] = session.info.setdefault("gcal_props", set())
    for obj in (*session.new, *session.dirty):
        if isinstance(obj, Lead):
            if obj in session.new or _changed(obj, *WATCH_LEAD):
                leads.add(obj.id)
        elif isinstance(obj, (Interaction, ClosingMilestone, InboundReply)):
            if obj.lead_id:
                leads.add(obj.lead_id)
        elif isinstance(obj, DistressSignal):
            if obj.signal_type == SignalType.SHERIFF_SALE and (
                    obj in session.new or _changed(obj, "scheduled_auction_date", "active", "status")):
                props.add(obj.property_id)
        elif isinstance(obj, Contact):
            if obj.lead_id and (obj in session.new or _changed(obj, "dnc_status", "phone")):
                leads.add(obj.lead_id)


def _after_commit(session: Session) -> None:
    leads, props = session.info.pop("gcal_leads", None), session.info.pop("gcal_props", None)
    if leads or props:
        try:
            worker.kick(leads or (), props or ())
        except Exception:                  # noqa: BLE001
            log.exception("calendar sync could not be queued")


def _after_rollback(session: Session) -> None:
    session.info.pop("gcal_leads", None)
    session.info.pop("gcal_props", None)


_hooks_installed = False


def install_hooks() -> None:
    """Idempotent. Called by the cockpit, manage.py and the swarm at start-up."""
    global _hooks_installed
    if _hooks_installed:
        return
    event.listen(Session, "after_flush", _after_flush)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_rollback", _after_rollback)
    _hooks_installed = True


class _Worker:
    """One background thread per process. In the cockpit it also runs a full sync every `background_minutes`;
    in a CLI command it handles the kicks and finishes before the process exits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leads: set[int] = set()
        self._props: set[int] = set()
        self._full = False
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._periodic = False
        self._atexit = False
        self.last: dict[str, Any] = {}

    def kick(self, leads: Iterable[int] = (), props: Iterable[int] = (), full: bool = False) -> None:
        if not configured():
            return
        with self._lock:
            self._leads |= set(leads)
            self._props |= set(props)
            self._full = self._full or full
        self._ensure()
        self._wake.set()

    def start_periodic(self) -> None:
        self._periodic = True
        self._full = True
        self._ensure()
        self._wake.set()

    def _ensure(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="gcal-sync", daemon=True)
            self._thread.start()
            if not self._periodic and not self._atexit:
                atexit.register(self.drain)
                self._atexit = True

    def _take(self) -> tuple[set[int], set[int], bool]:
        with self._lock:
            out = (self._leads, self._props, self._full)
            self._leads, self._props, self._full = set(), set(), False
            return out

    def _loop(self) -> None:
        interval = max(60, int(cfg().get("background_minutes", 10)) * 60)
        while True:
            woke = self._wake.wait(timeout=interval if self._periodic else 2.0)
            self._wake.clear()
            if woke:
                clock.sleep(0.8)                     # one sync for a burst of commits
            leads, props, full = self._take()
            if not woke and not self._periodic:
                return
            if woke and not (leads or props or full):
                continue
            self.run_once(leads, props, full or not woke)

    def run_once(self, leads: set[int], props: set[int], full: bool) -> None:
        if not configured():
            return
        from db.database import session_scope
        try:
            with session_scope() as s:
                s.info[WORKER_FLAG] = True
                ids = set(leads)
                if props:
                    ids |= set(s.scalars(select(Lead.id).where(Lead.property_id.in_(props))))
                r = sync(s, None if full else ids)
                self.last = {"at": now_local().isoformat(timespec="seconds"), **r.to_json()}
        except Exception:  # noqa: BLE001  (a calendar problem never breaks the cockpit or a command)
            log.exception("calendar sync failed")

    def drain(self, timeout: float = 25.0) -> None:
        t = self._thread
        if t is not None and t.is_alive() and not self._periodic:
            t.join(timeout)


worker = _Worker()


def kick(lead_ids: Iterable[int] = (), *, full: bool = False) -> None:
    worker.kick(lead_ids, full=full)


def start_background() -> None:
    """The cockpit: hooks + a periodic full sync. The loop runs even before setup and starts syncing as soon as
    config/token.json appears (the Connect button or `setup-calendar` in another terminal), no restart needed."""
    install_hooks()
    worker.start_periodic()


# -- swarm -------------------------------------------------------------------------------------------
class CalendarSyncAgent(BaseAgent):
    name = "calendar_sync"
    description = "Google Calendar: follow-ups, attorney review, milestones, priority sheriff sales, daily goal."

    def run(self, ctx: AgentContext) -> None:
        if not configured():
            ctx.note("Google Calendar not connected (python manage.py setup-calendar): nothing sent")
            return
        r = sync(ctx.session)
        for part in ("pulled", "reconciled", "pushed"):
            for k, n in getattr(r, part).items():
                if n:
                    ctx.bump(f"{part}_{k}", n)
        ctx.records_processed += sum(r.pushed.values())
        if r.error:
            ctx.note(f"calendar: {r.error} ({r.queued} queued)")
