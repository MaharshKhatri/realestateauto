"""Call compliance guard: Do-Not-Call registry, internal DNC, Daniel's Law, calling hours.

- National DNC (47 CFR 64.1200(c)): telemarketers must scrub against registry data no older than
  31 days. The FTC lets you download up to 5 area codes free (each additional one is $85/yr in FY2027).
  NJ target-county area codes: 201 551 (Bergen/Hudson), 973 862 (Essex/Passaic), 908 (Union),
  732 848 (Middlesex), 856 (Camden), 609 (Atlantic/Mercer). The free five that cover the most leads
  are chosen by `recommended_free_area_codes()` from our own lead data.
- Internal DNC: honor every stop request within 10 business days and keep it 5 years.
- Daniel's Law (N.J.S.A. 56:8-166.1): a nondisclosure request suppresses that person's number and
  address everywhere (calling, mailing, vendor sharing) within 10 business days.
- Calling window: 8am–9pm in the recipient's time zone (NJ numbers → America/New_York).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.text import normalize_phone
from db.models import Contact, DncEntry

NJ_AREA_CODES = ["201", "551", "973", "862", "908", "732", "848", "856", "609", "640"]
FTC_MAX_AGE_DAYS = 31
CALL_START, CALL_END = 8, 21
LINE_RE = re.compile(r"(\d{3})\D{0,3}(\d{3})\D?(\d{4})")


def parse_dnc_lines(lines: Iterable[str]) -> Iterable[str]:
    """FTC downloads are one number per line ('2015551234' or '201,5551234' / '201-555-1234')."""
    for line in lines:
        m = LINE_RE.search(line)
        if m:
            yield "".join(m.groups())


def load_ftc_file(session: Session, path: Path, list_date: date | None = None) -> int:
    """Load an FTC registry download. Re-loading replaces older FTC rows for the same numbers."""
    list_date = list_date or date.fromtimestamp(path.stat().st_mtime)
    existing = {e.phone: e for e in session.scalars(select(DncEntry).where(DncEntry.source == "ftc"))}
    n = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for phone in parse_dnc_lines(fh):
            row = existing.get(phone)
            if row:
                row.list_date = list_date
            else:
                row = DncEntry(phone=phone, source="ftc", list_date=list_date)
                session.add(row)
                existing[phone] = row
            n += 1
            if n % 50000 == 0:
                session.flush()
    session.flush()
    refresh_contact_dnc(session)
    return n


def add_internal(session: Session, phone: str, source: str = "internal", notes: str | None = None) -> DncEntry | None:
    digits = normalize_phone(phone)
    if not digits:
        return None
    row = session.get(DncEntry, (digits, source))
    if row is None:
        row = DncEntry(phone=digits, source=source, list_date=date.today(), notes=notes)
        session.add(row)
    session.flush()
    for c in session.scalars(select(Contact).where(Contact.phone == digits)):
        c.dnc_status = "dnc"
    session.flush()
    return row


def ftc_data_status(session: Session) -> dict:
    rows = session.execute(select(DncEntry.list_date).where(DncEntry.source == "ftc").distinct()).all()
    dates = [r[0] for r in rows if r[0]]
    if not dates:
        return {"loaded": False, "fresh": False, "list_date": None}
    newest = max(dates)
    return {"loaded": True, "list_date": newest.isoformat(),
            "fresh": (date.today() - newest).days <= FTC_MAX_AGE_DAYS}


def dnc_sources(session: Session, phone: str | None) -> list[str]:
    digits = normalize_phone(phone)
    if not digits:
        return []
    return [r.source for r in session.scalars(select(DncEntry).where(DncEntry.phone == digits))]


def classify_number(session: Session, phone: str | None, *, b2b: bool = False) -> str:
    """dnc | clear | not_checked | b2b. 'not_checked' means no fresh FTC data covers its area code."""
    digits = normalize_phone(phone)
    if not digits:
        return "not_checked"
    if dnc_sources(session, digits):
        return "dnc"
    if b2b:
        return "b2b"
    status = ftc_data_status(session)
    if not status["fresh"]:
        return "not_checked"
    covered = session.scalar(select(DncEntry.phone).where(DncEntry.source == "ftc",
                                                          DncEntry.phone.like(f"{digits[:3]}%")).limit(1))
    return "clear" if covered else "not_checked"


def refresh_contact_dnc(session: Session) -> int:
    n = 0
    for c in session.scalars(select(Contact).where(Contact.phone.is_not(None))):
        new = classify_number(session, c.phone, b2b=c.contact_type == "LISTING_AGENT")
        if c.dnc_status != new:
            c.dnc_status = new
            n += 1
    return n


def within_calling_hours(now: datetime | None = None, tz: str = "America/New_York") -> bool:
    local = (now or datetime.now(ZoneInfo(tz))).astimezone(ZoneInfo(tz))
    return CALL_START <= local.hour < CALL_END


def can_call(session: Session, contact: Contact, now: datetime | None = None) -> tuple[bool, str]:
    """The cockpit asks this before showing a number as callable."""
    if not contact.phone:
        return False, "no phone"
    status = classify_number(session, contact.phone, b2b=contact.contact_type == "LISTING_AGENT")
    if status == "dnc":
        return False, "on a do-not-call list: send a letter instead"
    if not within_calling_hours(now):
        return False, "outside 8am–9pm Eastern"
    if status == "not_checked":
        return True, "not yet scrubbed against the National DNC registry — load fresh FTC data first"
    return True, "ok"


def recommended_free_area_codes(session: Session, limit: int = 5) -> list[tuple[str, int]]:
    """The 5 free FTC area codes that cover the most of our callable contacts (else NJ defaults)."""
    counts = Counter(c.phone[:3] for c in session.scalars(select(Contact).where(Contact.phone.is_not(None)))
                     if c.contact_type != "LISTING_AGENT")
    if not counts:
        return [(a, 0) for a in ["973", "201", "908", "732", "856"]][:limit]
    return counts.most_common(limit)


def suppressed_for_daniels_law(session: Session, phone: str | None) -> bool:
    return "daniels_law" in dnc_sources(session, phone)


def stale_after(list_date: date) -> date:
    return list_date + timedelta(days=FTC_MAX_AGE_DAYS)

