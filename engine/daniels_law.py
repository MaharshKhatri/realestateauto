"""Daniel's Law takedown inbox (N.J.S.A. 56:8-166.1; learned 2026-11-10).

The research firm's reports print street addresses (surplus, contractor, tax-appeal, pre-foreclosure, digests). A covered
person, or an assignee such as a data-privacy company, may send a nondisclosure request to any address we mail from. We
must stop disclosing that home address / unpublished phone within 10 business days. Liability is strict: $1,000+ per
violation (N.J. Supreme Court, Aug. 2026). The lead reader skips mail from unknown senders, so this module looks at
every new INBOX header:

1. ``candidate()``: a subject or sender that may be a request (Daniel's Law, 56:8-166.1, nondisclosure, removal / privacy
   request, a known privacy-assignee domain). Only those bodies are downloaded.
2. ``confirmed()``: the subject + body name the law or a nondisclosure / do-not-disclose demand. Newsletters mentioning
   the law are still read (over-suppression only costs a lead), but the alert says to look.
3. ``handle()``: every NJ street address in the body or a text / CSV / PDF attachment goes on the mail-suppression list
   with source ``daniels_law``. Our own parcels at that street (property or owner mailing address, same town or ZIP) are
   suppressed in their stored form too. Every phone number goes on the internal DNC list with the same source.
   ``address_ok`` / ``address_suppressed`` (every stream, digest, letter and calendar line) then leave them out.
4. The request is recorded in swarm_state ``daniels_law_requests`` with counts, the sender's domain and the deadline
   (10 NJ business days after receipt). No name, address, subject or body text is stored; the e-mail stays in Gmail.
   Nothing is answered automatically. The calendar gets a ⚖️ event, and the 📊 status a line, until the operator
   confirms (sealed note ``{"daniels_law": "<id>", "done": true}``) or the deadline passes.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import date, datetime
from email import message_from_bytes, policy
from email.message import Message
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.dates import nj_business_days_after
from core.text import normalize_street
from db.models import Property, SwarmState

log = logging.getLogger(__name__)

STATE_KEY = "daniels_law_requests"
BUSINESS_DAYS = 10
KEEP = 200

STRONG = re.compile(
    r"daniel\W{0,2}s\s+law|56\s*:\s*8\s*-\s*166|47\s*:\s*1b|non-?\s?disclosure\s+(?:request|notice|demand)|"
    r"request\s+(?:for|of)\s+non-?\s?disclosure|nondisclosure|non-disclosure|do\s+not\s+disclose|cease\s+disclos|"
    r"stop\s+disclos|covered\s+person|atlas\s+data\s+privacy|judicial\s+(?:officer|privacy)|redact\s+(?:my|the|our)",
    re.I)
WEAK = re.compile(
    r"privacy\s+request|data\s+(?:subject|privacy|removal|deletion)\s+request|remov(?:e|al)\s+(?:of\s+)?(?:my|our|"
    r"personal|home)\s+(?:home\s+)?(?:address|information|info|data|record)|delete\s+my\s+(?:data|information|info)|"
    r"redaction|take\s*down\s+request|opt[- ]out\s+request|personal\s+information\s+request", re.I)
ASSIGNEE_DOMAINS = re.compile(r"atlas(?:data|dataprivacy|mail|privacy)|daniels-?law|privacy-?request", re.I)

_SUFFIX = (r"(?:st|street|ave|avenue|av|rd|road|dr|drive|ln|lane|ct|court|pl|place|blvd|boulevard|ter|terrace|terr|"
           r"way|pkwy|parkway|cir|circle|hwy|highway|sq|square|trl|trail|row|plz|plaza|aly|alley|pike|tpke|turnpike|"
           r"cres|crescent|xing|crossing|run|path|walk|pass|loop|close|commons|grn|green|hts|heights|ext|broadway|"
           r"mews|point|pt|ridge|rdg|knoll|hill|hollow|glen|gardens|gdns|park|terrace)")
_STREET = (rf"\d{{1,6}}[a-z]?(?:[ \t]?-[ \t]?\d{{1,5}}[a-z]?)?[ \t]+(?:[nsew]\.?[ \t]+)?(?:[a-z0-9.'’-]+[ \t]+){{0,5}}?"
           rf"{_SUFFIX}\b\.?(?:[ \t]*,?[ \t]*(?:#|unit|apt\.?|apartment|ste\.?|suite|fl\.?|floor)[ \t]*[a-z0-9-]+)?")
_FULL = re.compile(rf"(?<![\w#])({_STREET})\s*(?:,|\n|\s)\s*([a-z][a-z .'’-]{{1,40}}?)[ \t]*,?[ \t]*"
                   r"(?:nj|n\.\s?j\.|new\s+jersey)\b\.?,?\s*(\d{5})?", re.I)
_BARE = re.compile(rf"(?<![\w#])({_STREET})", re.I)
_PHONE = re.compile(r"(?<![\d-])(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?([2-9]\d{2})[\s.-]?(\d{4})(?!\d)")
_CITY_TRIM = re.compile(r"\b(?:twp|township|borough|boro)\b\.?", re.I)


# -- detection ------------------------------------------------------------------------------------------------------
def candidate(subject: str, from_email: str) -> bool:
    """Worth downloading: the subject or the sender may be a nondisclosure request."""
    domain = (from_email or "").rpartition("@")[2]
    return bool(STRONG.search(subject or "") or WEAK.search(subject or "") or ASSIGNEE_DOMAINS.search(domain))


def confirmed(subject: str, text: str) -> bool:
    """A request, not a receipt: the law or a do-not-disclose demand is named somewhere in it."""
    return bool(STRONG.search(f"{subject or ''}\n{text or ''}"))


# -- text -----------------------------------------------------------------------------------------------------------
def full_text(raw: bytes) -> str:
    """Every readable part: plain / HTML body and text, CSV or PDF attachments (no quoted-thread stripping, no cap:
    a list of addresses must be read to the end)."""
    msg = message_from_bytes(raw, policy=policy.default)
    parts: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        name = (part.get_filename() or "").lower()
        try:
            if ctype in ("text/plain", "text/csv") or name.endswith((".txt", ".csv")):
                parts.append(_decode(part))
            elif ctype == "text/html":
                parts.append(_html_text(_decode(part)))
            elif ctype == "application/pdf" or name.endswith(".pdf"):
                parts.append(_pdf_text(part.get_payload(decode=True) or b""))
        except Exception:                                 # noqa: BLE001 - an odd part must not lose the rest
            log.warning("daniels_law: unreadable part %s", ctype)
            parts.append("[unreadable attachment]")
    return "\n".join(p for p in parts if p)


def _decode(part: Message) -> str:
    try:
        return str(part.get_content())
    except (LookupError, ValueError, KeyError):
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def _html_text(markup: str) -> str:
    import html as _html
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</td>", "\n", markup)
    return _html.unescape(re.sub(r"<[^>]+>", " ", markup))


def _pdf_text(data: bytes) -> str:
    if not data:
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:                                   # pragma: no cover - pypdf ships with the desk
        return "[unreadable attachment]"
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages[:40])


# -- parsing --------------------------------------------------------------------------------------------------------
def _clean_city(city: str) -> str:
    city = _CITY_TRIM.sub("", city or "")
    return re.sub(r"\s+", " ", city).strip(" .,'’-")


def addresses(text: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """(street, city, zip) for each 'STREET, CITY, NJ [ZIP]' and the bare streets with no town next to them."""
    full: list[tuple[str, str, str]] = []
    spans: list[tuple[int, int]] = []
    for m in _FULL.finditer(text or ""):
        street = re.sub(r"\s+", " ", m.group(1)).strip(" ,")
        full.append((street, _clean_city(m.group(2)), m.group(3) or ""))
        spans.append(m.span(0))
    bare = []
    for m in _BARE.finditer(text or ""):
        if any(m.start(1) < b and a < m.end(1) for a, b in spans):
            continue
        bare.append(re.sub(r"\s+", " ", m.group(1)).strip(" ,"))
    return list(dict.fromkeys(full)), list(dict.fromkeys(bare))


def phones(text: str, exclude: Iterable[str] = ()) -> list[str]:
    skip = {re.sub(r"\D", "", x)[-10:] for x in exclude if x}
    out = []
    for m in _PHONE.finditer(text or ""):
        digits = "".join(m.groups())
        if digits not in skip:
            out.append(digits)
    return list(dict.fromkeys(out))


def _same_place(prop_city: str | None, prop_zip: str | None, city: str, zip_code: str, text: str) -> bool:
    pc, pz = _clean_city(prop_city or "").upper(), (prop_zip or "")[:5]
    if zip_code and pz:
        return pz == zip_code
    if city and pc:
        return pc == city.upper() or pc in city.upper() or city.upper() in pc
    # A bare street: our parcel counts when its town or ZIP is named anywhere in the request.
    up = (text or "").upper()
    return bool((pc and re.search(rf"\b{re.escape(pc)}\b", up)) or (pz and pz in up))


def _matching_properties(session: Session, street: str, city: str, zip_code: str, text: str) -> list[tuple[str, str]]:
    """Our stored address forms at that street in that town: (property address, or owner mailing address)."""
    norm = normalize_street(street)
    num = norm.split(" ", 1)[0] if norm else ""
    if not num or not num[0].isdigit():
        return []
    out: list[tuple[str, str]] = []
    rows = session.scalars(select(Property).where(
        (Property.street_address.ilike(f"{num} %")) | (Property.owner_mailing_address.ilike(f"{num} %"))))
    broad: list[tuple[str, str]] = []
    for p in rows:
        if p.street_address and normalize_street(p.street_address) == norm:
            form = (f"{p.street_address}, {p.city or ''} NJ {p.zip_code or ''}", "property")
            if _same_place(p.city, p.zip_code, city, zip_code, text):
                out.append(form)
            else:
                broad.append(form)
        mail = p.owner_mailing_address
        if mail and normalize_street(mail) == norm:
            csz = p.owner_city_state_zip or ""
            zm = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", csz)
            form = (f"{mail}, {csz}", "owner mailing")
            if _same_place(re.sub(r"[\s,]+(?:N\.?\s?J\.?|[A-Z]{2})?\.?[\s,]*(\d{5}(?:-\d{4})?)?\s*$", "", csz),
                           zm.group(1) if zm else "", city, zip_code, text):
                out.append(form)
            else:
                broad.append(form)
    # A street with no town anywhere in the request: every parcel at that street (over-suppression costs a lead;
    # a missed request costs $1,000+ per disclosure).
    if not out and not city and not zip_code:
        out = broad
    return list(dict.fromkeys(out))


# -- handling -------------------------------------------------------------------------------------------------------
def request_id(message_id: str) -> str:
    return hashlib.sha1((message_id or "").encode()).hexdigest()[:8]


def _state(session: Session) -> list[dict[str, Any]]:
    row = session.get(SwarmState, STATE_KEY)
    return list(((row.value_json or {}).get("items")) or []) if row else []


def _save(session: Session, items: list[dict[str, Any]]) -> None:
    row = session.get(SwarmState, STATE_KEY)
    if row is None:
        row = SwarmState(key=STATE_KEY, value_json={})
        session.add(row)
    row.value_json = {"items": items[-KEEP:]}
    session.flush()


def handle(session: Session, *, message_id: str, from_email: str, subject: str, text: str,
           received: datetime | None, operator_phones: Iterable[str] = ()) -> dict[str, Any] | None:
    """Suppress every address and phone in a nondisclosure request; record it once. None for a repeat."""
    from engine import compliance
    rid = request_id(message_id)
    items = _state(session)
    if any(i.get("id") == rid for i in items):
        return None
    got = received.date() if received else date.today()
    full, bare = addresses(text)
    note = f"Daniel's Law request {rid} received {got.isoformat()}"
    suppressed: set[str] = set()
    props = 0
    for street, city, zip_code in full:
        form = f"{street}, {city} NJ {zip_code}".strip()
        if compliance.suppress_address(session, form, source="daniels_law", notes=note) is not None:
            suppressed.add(compliance.address_key(form) or form)
        for stored, _kind in _matching_properties(session, street, city, zip_code, text):
            if compliance.suppress_address(session, stored, source="daniels_law", notes=note) is not None:
                suppressed.add(compliance.address_key(stored) or stored)
                props += 1
    for street in bare:
        for stored, _kind in _matching_properties(session, street, "", "", text):
            if compliance.suppress_address(session, stored, source="daniels_law", notes=note) is not None:
                suppressed.add(compliance.address_key(stored) or stored)
                props += 1
    nums = phones(text, exclude=operator_phones)
    for digits in nums:
        compliance.add_internal(session, digits, source="daniels_law", notes=note)
    unreadable = "[unreadable attachment]" in (text or "")
    rec = {"id": rid, "message_id": message_id, "received": got.isoformat(),
           "deadline": nj_business_days_after(got, BUSINESS_DAYS).isoformat(),
           "from_domain": (from_email or "").rpartition("@")[2].lower()[:80],
           "addresses": len(full) + len(bare), "suppressed": len(suppressed), "our_parcels": props,
           "phones": len(nums), "needs_review": bool(unreadable or not (full or bare or nums)),
           "confirmed": False}
    items.append(rec)
    _save(session, items)
    log.warning("daniels_law: request %s: %d address(es), %d suppression(s), %d phone(s)", rid,
                rec["addresses"], rec["suppressed"], rec["phones"])
    return rec


def open_requests(session: Session, today: date | None = None) -> list[dict[str, Any]]:
    """Requests the operator hasn't confirmed, newest first (kept on the calendar a week past the deadline)."""
    today = today or date.today()
    out = []
    for i in _state(session):
        if i.get("confirmed"):
            continue
        try:
            dl = date.fromisoformat(str(i.get("deadline")))
        except ValueError:
            continue
        if (today - dl).days <= 7:
            out.append(i)
    return sorted(out, key=lambda i: i.get("received") or "", reverse=True)


def confirm(session: Session, rid: str) -> bool:
    items = _state(session)
    hit = False
    for i in items:
        if i.get("id") == str(rid).strip():
            i["confirmed"], hit = True, True
    if hit:
        _save(session, items)
    return hit


def summary_line(rec: dict[str, Any]) -> str:
    """Counts only: no name, address or message text."""
    what = (f"{rec['addresses']} address(es) found, {rec['suppressed']} suppression(s) incl. {rec['our_parcels']} of our "
            f"parcel record(s), {rec['phones']} phone(s) on the stop list")
    look = (" READ IT IN GMAIL: nothing (or an unreadable attachment) could be parsed, so tell Claude the address."
            if rec.get("needs_review") else "")
    return (f"⚖️ Daniel's Law request {rec['id']} (from @{rec['from_domain']}, received {rec['received']}): {what}; "
            f"must be honored by {rec['deadline']}.{look}")
