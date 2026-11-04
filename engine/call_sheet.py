"""The daily call sheet: up to 100 listing-agent calls in four blocks of 25 (learned 2026-11-03).

The operator asked for 100 calls a day ("I can make as many calls as you want") with every number concrete and
final. Each weekday morning the desk builds one sheet and freezes it for the day, so a code like "A07" means the
same call all day. The sheet goes on the calendar as four events (`callblock:<YYYYMMDD>:<A-D>`), each with one
word-for-word script and 25 rows. The operator reports one line per block in the chat ("A01 vm, A02 na, A03 send,
A04 340, A05 stop"), and Claude seals those codes into one note for the cloud desk (`apply_codes`).

Order of rows (the money first):
  T0  an agent replied and waits on us (Action Needed)
  T1  listed foreclosures with a sheriff sale inside 21 days
  T2  due follow-ups and retries (at most `max_attempts` calls with no conversation)
  T3  firm offers (engine/offer_math.py tier "firm"): we state the one number
  T4  ask-first listings: we ask what the seller needs and accept anything at or under our final

Left off: closed leads, a number on any stop list (internal / Daniel's Law always win), fax lines, anything called
in the last 2 days (T0-T2 excepted), and skip-tier leads. One row per phone number (up to 3 streets per agent).
Every row states one number: the same one the e-mail and the LOI carry ("OFFER $X · best and final $Y") or "ASK
THEIR NUMBER (final $Y)". Streets and business contacts only; a suppressed address shows as "lead #<id>".
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import settings
from db.models import Contact, ContactType, DistressSignal, InboundReply, Lead, SignalType, SwarmState

log = logging.getLogger(__name__)

STATE_KEY = "call_sheet"
LISTING_SIGNALS = (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)
CLOSED = {"DEAD", "CLOSED", "UNDER_CONTRACT", "ASSIGNED", "DO_NOT_CONTACT"}
TIER_LABEL = {"T0": "replied", "T1": "sale soon", "T2": "follow-up", "T3": "firm offer", "T4": "ask their number"}
PHONE_RANK = {"wireless": 0, "mobile": 0, "unknown": 1, None: 1, "landline": 2, "office": 3}
# What the operator types after a call -> the CRM outcome (engine/crm.py::log_call).
CODES = {
    "na": "NO_ANSWER", "noanswer": "NO_ANSWER",
    "vm": "LEFT_VM",
    "wn": "WRONG_NUMBER", "wrong": "WRONG_NUMBER",
    "stop": "DNC", "dnc": "DNC",
    "sold": "DEAD", "gone": "DEAD",
    "uc": "SPOKE_NOT_INTERESTED",           # under contract with someone else: check back when it falls through
    "ni": "SPOKE_NOT_INTERESTED", "no": "SPOKE_NOT_INTERESTED",
    "cb": "SPOKE_WARM_FOLLOWUP", "later": "SPOKE_WARM_FOLLOWUP",
    "send": "SPOKE_NEEDS_OFFER", "yes": "SPOKE_NEEDS_OFFER",
    "number": "SPOKE_NEEDS_OFFER",
}


def _cfg() -> dict[str, Any]:
    return dict(settings().get("call_sheet") or {})


def _money(x: float | None) -> str:
    return f"${x:,.0f}" if x else "-"


def blocks() -> list[tuple[str, str, int]]:
    """(letter, start HH:MM, minutes) for the day's call blocks."""
    raw = _cfg().get("blocks") or [["A", "09:30"], ["B", "11:00"], ["C", "13:30"], ["D", "16:00"]]
    mins = int(_cfg().get("block_minutes", 75))
    return [(str(b[0]), str(b[1]), mins) for b in raw]


def per_block() -> int:
    return int(_cfg().get("per_block", 25))


def capacity(day: date) -> int:
    """Weekdays: every block; Saturday: the first block only (agents work Saturdays); Sunday: none."""
    if day.weekday() == 6:
        return 0
    n = 1 if day.weekday() == 5 else len(blocks())
    return min(n * per_block(), int((settings().get("crm") or {}).get("call_queue_size", 100)))


@dataclass
class Row:
    code: str
    tier: str
    lead_id: int
    phone: str
    who: str
    streets: list[str]
    facts: str
    say: str
    lead_ids: list[int] = field(default_factory=list)
    offer: float | None = None
    final: float | None = None

    def line(self) -> str:
        more = f" (+{', '.join(self.streets[1:])})" if len(self.streets) > 1 else ""
        return (f"{self.code} | {self.phone} {self.who} | {self.streets[0]}{more} | {self.facts} | {self.say} | "
                f"lead {self.lead_id}")


def _fmt_phone(p: str) -> str:
    d = "".join(ch for ch in p if ch.isdigit())[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else p


def agent_line(session: Session, lead: Lead) -> tuple[str, str] | None:
    """(phone, "First (Brokerage)") for the best callable listing-agent number on a lead, or None.
    Wireless first, then unknown, then office; never a fax or a number on any stop list."""
    from engine import compliance
    from engine.gcal_sync import STOP_SOURCES, _name
    best: tuple[int, Contact] | None = None
    for c in lead.contacts:
        if c.contact_type != ContactType.LISTING_AGENT or not c.phone or c.dnc_status == "dnc":
            continue
        if (c.phone_type or "").lower() == "fax" or (c.confidence_score is not None and c.confidence_score <= 0):
            continue
        if set(compliance.dnc_sources(session, c.phone)) & STOP_SOURCES:
            continue
        if compliance.classify_number(session, c.phone, b2b=True) == "dnc":
            continue
        rank = PHONE_RANK.get((c.phone_type or "unknown").lower(), 1)
        if best is None or rank < best[0]:
            best = (rank, c)
    if best is None:
        return None
    c = best[1]
    first = (_name(c.name) or "").split()[:1] or ["agent"]
    return _fmt_phone(c.phone), first[0] + (f" ({c.company})" if c.company else "")


def _facts(session: Session, lead: Lead, ask: float | None) -> str:
    from engine.offer_math import listing_for
    lst = listing_for(session, lead)
    bits: list[str] = []
    if lst is not None:
        if (lst.property_type or "").startswith("multi"):
            bits.append("multi-family")
        bb = "/".join(x for x in (f"{lst.beds:g}bd" if lst.beds else "", f"{lst.baths:g}ba" if lst.baths else "") if x)
        if bb:
            bits.append(bb)
        if lst.sqft:
            bits.append(f"{lst.sqft:,}sf")
        if lst.year_built:
            bits.append(str(lst.year_built))
    ask_s = f"ask {_money(ask)}" if ask else ""
    dom = getattr(lst, "days_on_market", None) if lst is not None else None
    return " ".join(bits) + (" | " if bits else "") + ask_s + (f", {dom} DOM" if dom else "")


def _short(reason: str) -> str:
    return reason.split(":")[0].replace("the listing describes a renovated, move-in-ready house (retail buyers pay more "
                                        "than an investor can)", "listing says move-in ready")[:70]


def _say(firm: Any, tier: str) -> str:
    if tier == "T0":
        return "THEY REPLIED: answer their reply (no new price on the call)"
    if firm.priced:
        return f"OFFER {_money(firm.offer)} · best and final {_money(firm.final)}"
    doubts = [r for r in firm.reasons if "of the ask: ask what the seller needs first" not in r]
    if firm.final and not doubts:
        return f"ASK THEIR NUMBER · yes at or under {_money(firm.final)}"
    if firm.final:
        # our own number needs a check (old comps, retail condition, value far above the ask): agree to nothing
        return "ASK THEIR NUMBER · write it down, agree to nothing (" + _short(doubts[0]) + ")"
    return "NO PRICE: get the facts (" + (firm.reasons[0] if firm.reasons else "no numbers yet") + ")"


def _pool(session: Session) -> list[Lead]:
    listed = select(DistressSignal.property_id).where(DistressSignal.active.is_(True),
                                                      DistressSignal.signal_type.in_(LISTING_SIGNALS))
    return list(session.scalars(select(Lead).where(Lead.property_id.in_(listed),
                                                   Lead.status_stage.notin_(tuple(CLOSED)))))


def build(session: Session, now: datetime | None = None, limit: int | None = None) -> list[Row]:
    """Rank today's calls. Pure read: nothing is logged or sent."""
    from engine import crm
    from engine import fast_track as ft
    from engine.gcal_sync import _place
    from engine.offer_math import firm_offer, locked
    now = now or datetime.now()
    limit = capacity(now.date()) if limit is None else limit
    max_attempts = int(_cfg().get("max_attempts", 4))
    recent = now - timedelta(days=int(_cfg().get("recall_gap_days", 2)))
    rows: list[Row] = []
    by_phone: dict[str, Row] = {}
    seen: set[int] = set()

    def add(lead: Lead | None, tier: str) -> None:
        if lead is None or lead.id in seen or lead.status_stage in CLOSED:
            return
        seen.add(lead.id)
        if tier in ("T3", "T4") and lead.last_contact_at and lead.last_contact_at > recent:
            return
        line = agent_line(session, lead)
        if line is None:
            return
        phone, who = line
        try:
            firm = locked(lead, firm_offer(lead, session))
        except Exception:                          # noqa: BLE001 - one broken valuation never stops the sheet
            log.exception("call sheet: firm offer failed for lead %s", lead.id)
            return
        if tier in ("T1", "T3", "T4") and firm.tier == "skip":
            return      # a lender short sale / refer-out / too-far listing: no offer we could make, so no call
        place = _place(session, lead)
        row = by_phone.get(phone)
        if row is not None:
            if len(row.streets) < 3:
                row.streets.append(place.street)
            row.lead_ids.append(lead.id)
            return
        if len(rows) >= limit:
            return
        row = Row(code="", tier=tier, lead_id=lead.id, phone=phone, who=who, streets=[place.street],
                  facts=_facts(session, lead, place.ask), say=_say(firm, tier), lead_ids=[lead.id],
                  offer=firm.offer if firm.priced else None, final=firm.final)
        by_phone[phone] = row
        rows.append(row)

    waiting = session.scalars(select(InboundReply.lead_id).where(InboundReply.status == "needs_approval",
                                                                 InboundReply.lead_id.is_not(None)))
    for lid in list(waiting):
        add(session.get(Lead, lid), "T0")
    try:
        for lf in ft.listed_foreclosures(session, today=now.date()):
            if lf.days_to_sale is not None and lf.days_to_sale <= 21 and lf.track not in ("NO_OFFER",):
                add(session.get(Lead, lf.lead_id), "T1")
    except Exception:                              # noqa: BLE001
        log.exception("call sheet: listed foreclosures failed")
    for lead in crm.due_followups(session, now):
        if lead.contact_attempts <= max_attempts:
            add(lead, "T2")
    firm_rows: list[tuple[float, Lead]] = []
    ask_rows: list[tuple[float, Lead]] = []
    for lead in _pool(session):
        if lead.id in seen:
            continue
        try:
            f = firm_offer(lead, session)
        except Exception:                          # noqa: BLE001
            continue
        if f.tier == "firm":
            firm_rows.append((f.reach or 0, lead))
        elif f.tier == "ask_first":
            ask_rows.append((f.reach or 0, lead))
    for _, lead in sorted(firm_rows, key=lambda x: (-x[0], -(x[1].priority_rank_score or 0))):
        add(lead, "T3")
    for _, lead in sorted(ask_rows, key=lambda x: (-x[0], -(x[1].priority_rank_score or 0))):
        add(lead, "T4")
    letters = [b[0] for b in blocks()]
    for i, row in enumerate(rows):
        row.code = f"{letters[min(i // per_block(), len(letters) - 1)]}{i % per_block() + 1:02d}"
    return rows


def today(session: Session, now: datetime | None = None, *, rebuild: bool = False) -> list[Row]:
    """The day's sheet, built once and frozen (so "A07" means the same call all day)."""
    now = now or datetime.now()
    day = now.date().isoformat()
    row = session.get(SwarmState, STATE_KEY)
    state = dict(row.value_json or {}) if row is not None else {}
    if state.get("date") == day and not rebuild:
        return [Row(**r) for r in state.get("rows") or []]
    rows = build(session, now)
    if row is None:
        row = SwarmState(key=STATE_KEY, value_json={})
        session.add(row)
    row.value_json = {"date": day, "built_at": now.isoformat(timespec="minutes"), "rows": [asdict(r) for r in rows]}
    session.flush()
    return rows


def stored(session: Session, day: date) -> list[Row]:
    """The frozen sheet for `day` ([] when none was built): never builds."""
    row = session.get(SwarmState, STATE_KEY)
    state = dict(row.value_json or {}) if row is not None else {}
    return [Row(**r) for r in state.get("rows") or []] if state.get("date") == day.isoformat() else []


def refreshed(session: Session, row: Row) -> Row:
    """The row with today's words: the code and the phone stay frozen, but what to say follows the current firm
    numbers (a re-price or a stricter certainty check during the day never leaves a stale price on the calendar)."""
    from engine.offer_math import firm_offer, locked
    if row.tier == "T0":
        return row
    lead = session.get(Lead, row.lead_id)
    if lead is None:
        return row
    try:
        firm = locked(lead, firm_offer(lead, session))
    except Exception:                              # noqa: BLE001 - keep the morning's words
        return row
    say = _say(firm, row.tier)
    if firm.tier == "skip":
        say = "SKIP TODAY: " + (firm.reasons[0] if firm.reasons else "no offer we could make")
    return Row(**{**asdict(row), "say": say, "offer": firm.offer if firm.priced else None, "final": firm.final})


def lead_ids_on(session: Session, day: date) -> set[int]:
    return {i for r in stored(session, day) for i in (r.lead_ids or [r.lead_id])}


# -- the block script ---------------------------------------------------------------------------------
def block_script(letter: str) -> list[str]:
    """One word-for-word script for every row in a block (the operator is new to real estate: spoken lines, not
    hints). The row says which number to use."""
    from engine.human_copy import operator
    op = operator()
    full = op["name"] or op["first"] or "your name"
    phone = op["phone"] or "your phone number"
    return [
        "--- Script for every call in this block (word for word) ---",
        f'1) Open: "Hi, is this [first name]? Hi, this is {full}, {phone}. I\'m calling about your listing at '
        '[street]. Do you have a minute?"',
        '   Bad time? "No problem. When\'s a better time?" Write it down, say thanks, hang up (report: cb + the time).',
        '2) "I\'m a real estate investor. I buy for myself or with a partner investor. I\'m not a realtor and I don\'t '
        'have an agent. Is it still available?"',
        '3) Ask, one at a time: "Does it need work: roof, heating, kitchen, baths?" "Is anyone living there?" '
        '"Any offers so far?" "What does the seller need to walk away with?"',
        '4a) Row says OFFER $X: "I can do $X cash, as is, no financing or appraisal contingency, with attorney review. '
        'I\'ll send it in writing right after this call." If they push: "My best and final is $Y, and that\'s my '
        'ceiling." Never above $Y.',
        '4b) Row says ASK THEIR NUMBER: don\'t say a price first. "What number would the seller sign at for cash, as '
        'is, with a quick close?" At or under the row\'s number: "That can work. I\'ll confirm and send it in writing '
        'today." Above it: "That\'s more than I can pay as is. Mind if I check back in a few weeks?" When the row says '
        '"agree to nothing": write their number down and say "Let me run it and I\'ll get back to you today."',
        '4c) Row says NO PRICE: ask the questions in 3, then "Let me run it and I\'ll e-mail you a number."',
        '5) "Are you a wholesaler?" / "Will you assign?": "I\'m an investor. I may buy it myself or assign the contract '
        'to a partner investor. That\'s in writing, and the seller\'s attorney reviews everything."',
        '6) "Proof of funds?": "Yes, it comes with the written offer."  7) "Want to see it?": "Photos or a short video '
        'first, please. My partner walks it during the inspection period."',
        '8) Anything else: "Good question. Let me check and get back to you."',
        f'Voicemail: "Hi [first name], this is {full}, {phone}, about [street]. Please call me back at {phone}. '
        'Thanks."',
        '"Please stop calling" / "take me off your list": "Understood, I won\'t call again." (report: stop)',
        "Never: go above the best and final, offer the agent money, promise to stop a foreclosure, say you're a "
        "realtor.",
        f"--- Report this block to Claude in one line: {letter}01 vm, {letter}02 na, {letter}03 send, {letter}04 340, "
        f"{letter}05 cb thu 2pm, {letter}06 stop ---",
        "Codes: na no answer · vm voicemail · wn wrong number · stop asked not to be called · sold · uc under contract "
        "· ni not interested · cb call back (+ when) · send wants it in writing · a number = what the seller needs "
        "(340 = $340,000) · add any facts after the code (roof, tenants, oil tank).",
    ]


def block_rows(rows: list[Row], letter: str) -> list[Row]:
    return [r for r in rows if r.code.startswith(letter)]


# -- outcomes reported in the chat ---------------------------------------------------------------------
def parse_amount(token: str) -> float | None:
    t = token.lower().replace("$", "").replace(",", "").strip()
    mult = 1.0
    if t.endswith("k"):
        t, mult = t[:-1], 1000.0
    elif t.endswith("m"):
        t, mult = t[:-1], 1_000_000.0
    try:
        v = float(t) * mult
    except ValueError:
        return None
    if v < 5000:                                   # "340" / "469.9" = thousands
        v *= 1000
    return round(v, -2) if 20_000 <= v <= 10_000_000 else None


def apply_codes(session: Session, items: list[dict[str, Any]], *, now: datetime | None = None,
                smtp_factory: Any = None) -> dict[str, int]:
    """Each item: {"code": "A07", "result": "vm" | "send" | "340" | ..., "notes": "...", "followup_at": ISO,
    "date": "YYYY-MM-DD"}. The code is resolved on that day's frozen sheet; every listing on the row's phone gets the
    outcome. "send" e-mails the lead's next offer (the desk's own firm number); a number at or under our best and
    final gets the written offer at that number only when the valuation passes every certainty check, otherwise the
    call is logged and the call-back says what to confirm; a number above it gets the negotiator's answer."""
    from engine import agent_outreach as ao
    from engine import autopilot, crm
    from engine import gmail_autopilot as gm
    from engine import reply_negotiator as rn
    from engine.offer_math import certainty_issues, firm_offer, locked
    counts = {"logged": 0, "offers": 0, "answers": 0, "held": 0, "unknown": 0}
    sheets: dict[str, list[Row]] = {}
    for item in items:
        day = str(item.get("date") or (now or datetime.now()).date().isoformat())
        if day not in sheets:
            sheets[day] = stored(session, date.fromisoformat(day))
        row = next((r for r in sheets[day] if r.code.upper() == str(item.get("code", "")).upper()), None)
        result = str(item.get("result") or "").strip()
        key = result.split()[0].lower() if result else ""
        amount = parse_amount(key) if key not in CODES else None
        outcome = CODES.get(key) or ("SPOKE_NEEDS_OFFER" if amount else None)
        if row is None or outcome is None:
            counts["unknown"] += 1
            continue
        notes = " ".join(x for x in (result, str(item.get("notes") or "")) if x).strip()[:600]
        when = datetime.fromisoformat(item["followup_at"]) if item.get("followup_at") else None
        if key == "uc" and when is None:            # under contract elsewhere: check back when deals fall out
            base = (now or datetime.now(crm.TZ)).astimezone(crm.TZ).replace(tzinfo=None)
            when = base.replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(
                days=int((rn._ncfg() or {}).get("under_contract_days", 45)))
        for i, lid in enumerate(row.lead_ids or [row.lead_id]):
            lead = session.get(Lead, lid)
            if lead is None:
                continue
            main = i == 0
            contact = next((c for c in lead.contacts if c.contact_type == ContactType.LISTING_AGENT
                            and c.phone and c.phone[-10:] == "".join(ch for ch in row.phone if ch.isdigit())[-10:]),
                           None) or ao.agent_contact(lead)
            if not main and outcome not in ("DNC", "WRONG_NUMBER", "NO_ANSWER", "LEFT_VM"):
                continue                           # a conversation was about the row's first street only
            crm.log_call(session, lead, outcome, notes=notes, contact=contact, followup_at=when if main else None,
                         debrief={"source": "chat_note", "code": row.code, **({"asking_price": amount} if amount else {})},
                         now=now)
            counts["logged"] += 1
            if not main:
                continue
            try:
                if key in ("send", "yes"):
                    autopilot.send(session, lead.id, "smtp", now=now, smtp_factory=smtp_factory)
                    counts["offers"] += 1
                elif amount:
                    firm = locked(lead, firm_offer(lead, session, check_certainty=False))
                    if firm.final and amount <= firm.final and certainty_issues(lead, session):
                        # Their number fits our final, but the final rests on old or thin data: re-value live with
                        # recent sold comps before anything goes in writing (a written number is final).
                        from engine.offer_math import firm_up
                        fresh = locked(lead, firm_up(session, lead))
                        if not (fresh.final and amount <= fresh.final and not certainty_issues(lead, session)):
                            counts["held"] += 1    # the call-back event says what to confirm before writing it
                            continue
                    an = rn.analyze(session, lead, f"The seller would take {amount:,.0f}. {notes}", now=now)
                    price = an.numbers.get("counter") if an.verdict == "GREEN" else an.numbers.get("our_counter")
                    # In writing only when it moves the deal: their number works (GREEN) or our one best-and-final
                    # is within reach (YELLOW). "Too far apart" was already said on the phone.
                    if an.reply_text and an.verdict in ("GREEN", "YELLOW") and price:
                        gm.send_custom_reply(session, lead.id, an.reply_text, price=price or None, now=now,
                                             smtp_factory=smtp_factory)
                        counts["offers" if an.verdict == "GREEN" else "answers"] += 1
            except Exception as exc:               # noqa: BLE001 - the call is logged either way
                log.warning("call sheet: follow-up for %s failed: %s", row.code, type(exc).__name__)
                counts["held"] += 1
    session.flush()
    return counts
