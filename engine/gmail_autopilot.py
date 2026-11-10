"""Phase 8: the Gmail autopilot. It reads agent replies over IMAP, sends Touch 2 by itself, and sends a paced daily batch.

The operator's Gmail address and app password from My Setup (SMTP_USER / SMTP_APP_PASSWORD in .env) work for both
smtp.gmail.com and imap.gmail.com. This module uses only the standard library (imaplib, email, smtplib via
engine.agent_outreach.deliver):

* **poll_gmail_replies()** (every `poll_minutes`, or on demand) opens INBOX **read-only** and fetches headers with
  BODY.PEEK, so nothing gets marked as read. It looks only at messages newer than the last UID it saw. A message is
  downloaded past its headers only when it belongs to one of our leads, matched by one of:
  - thread: In-Reply-To / References hold a Message-ID we sent or received;
  - agent_email: the sender is a listing agent we wrote to (their street in the text picks the listing when they
    have several; otherwise agent_latest, the one we wrote to last);
  - address: an unknown sender whose subject names a street we e-mailed about.
  Out-of-office replies are ignored. Bounces take the dead address off the agent's card. Everything else goes to
  engine.reply_negotiator (analyze + record: AGENT_REPLIED, counter, stage, opt-out suppression).
* **Touch 1 -> Touch 2 by itself** (`auto_send_touch2`) happens only when all of these hold:
  - the reply says the house is available;
  - the reply has no number, no other topic (tank, tenants, CO, POF, bidding war, estate, short sale, under
    contract, stop), and no question beyond "what's your offer?";
  - the match came from the thread or the agent's own address;
  - pre-flight shows nothing that blocks a price;
  - the priced packet builds with no warning and its LOI generates.
  The offer then goes out in the agent's Gmail thread (In-Reply-To / References, "Re:" subject) with the one-page
  LOI attached, after a human-paced delay and inside `send_hours`, and is logged with its 48-hour follow-up.
  Anything else waits in the Action Needed box for one click (approve()). Nothing negotiates by itself.
* **run_daily_outbound_autopilot()** sends today's top ready cards (urgent listed foreclosures, due follow-ups, GO /
  VERIFY MLS fixers that have an e-mail) over SMTP, 25-60 s apart, inside the rolling 25-per-24-hours limit. Every
  send goes through engine.autopilot.send (the same gates, logging and 48-hour follow-up as the Send button).

Privacy: unmatched mail is never downloaded or stored. A matched reply keeps only the agent's own words (quoted
history stripped, capped at `max_body_chars`). Nothing goes to any service but Gmail.
"""
from __future__ import annotations

import html as _html
import imaplib
import logging
import os
import random
import re
import smtplib
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from email import message_from_bytes, policy
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Callable

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.config import settings
from core.mail import GmailSendError, clean_app_password, explain, ssl_context
from core.text import format_phone
from agents.base import AgentContext, BaseAgent
from db.database import session_scope
from db.models import Contact, ContactType, InboundReply, Interaction, Lead, SwarmState
from engine import agent_outreach as ao
from engine import human_copy as hc

log = logging.getLogger(__name__)

STATE_KEY = "gmail_autopilot"
OPEN_STATUSES = ("needs_approval", "auto_queued", "sending")
HANDLED_STATUSES = ("auto_sent", "approved", "opted_out", "bounce", "dismissed")
HEADER_FIELDS = ("FROM", "TO", "SUBJECT", "DATE", "MESSAGE-ID", "IN-REPLY-TO", "REFERENCES", "AUTO-SUBMITTED",
                 "X-AUTOREPLY", "X-AUTORESPOND", "PRECEDENCE", "CONTENT-TYPE")
AUTO_OK_INTENTS = {"AVAILABLE", "QUESTION", "ASKS_ROLE", "ASKS_SHOWING", "CALL_ME", "BACKUP"}
# Pre-flight issues that must reach a person before a price does (the rest are "confirm on the call" notes).
PRICE_BLOCKERS = re.compile(r"FRFPA|payoff|condo / townhouse unit|unit count|sheriff sale in|outlier|not a 1-4|"
                            r"above every comp|confidence [0-4]\d/100", re.I)
OFFER_QUESTION = re.compile(r"what('?s| is| would be) (your|the) (offer|number|price)|how much|what (number|price)|"
                            r"(send|give) (me |us )?(your|an|the) (offer|number)|make (me |us )?an offer|"
                            r"submit (an|your) offer|any offers?|what can you (do|offer|pay)|what are you thinking", re.I)
AUTO_SUBJECT = re.compile(r"^\s*(automatic reply|auto(matic)?[- ]?(reply|response)|out of (the )?office|ooo\b|"
                          r"away from (the |my )?(office|desk)|on vacation|autoreply)", re.I)
BOUNCE_FROM = re.compile(r"^(mailer-daemon|postmaster)@", re.I)
MSGID = re.compile(r"<[^<>\s]+>")


class GmailError(RuntimeError):
    """Gmail could not be reached or refused the sign-in (the message says which)."""


class ApprovalError(ValueError):
    pass


class CampaignError(ValueError):
    pass


def _cfg() -> dict[str, Any]:
    return settings().get("gmail_autopilot", {}) or {}


def credentials() -> tuple[str, str] | None:
    from engine import operator_setup
    operator_setup.sync_from_disk()        # a password fixed in My Setup / .env counts without a restart
    user, pw = os.environ.get("SMTP_USER", "").strip(), clean_app_password(os.environ.get("SMTP_APP_PASSWORD", ""))
    return (user, pw) if user and pw else None


def configured() -> tuple[bool, str]:
    if not _cfg().get("enabled", True):
        return False, "gmail_autopilot.enabled is off in settings"
    if credentials() is None:
        return False, "add your Gmail address and app password in My Setup"
    return True, "ok"


def now_eastern(now: datetime | None = None) -> datetime:
    """Naive America/New_York, the convention interactions are stored in."""
    return ao._eastern(now)


def in_send_hours(now: datetime | None = None) -> bool:
    start, end = (_cfg().get("send_hours") or [8, 20])[:2]
    return int(start) <= now_eastern(now).hour < int(end)


def send_hours_text() -> str:
    start, end = (_cfg().get("send_hours") or [8, 20])[:2]
    fmt = lambda h: f"{(int(h) - 1) % 12 + 1} {'AM' if int(h) < 12 else 'PM'}"   # noqa: E731
    return f"{fmt(start)} to {fmt(end)} Eastern"


# -- parsing --------------------------------------------------------------------------------------------------
@dataclass
class Mail:
    uid: int
    message_id: str
    from_email: str
    from_name: str
    subject: str
    received_at: datetime | None
    in_reply_to: str | None
    references: list[str] = field(default_factory=list)
    text: str = ""
    auto_reply: bool = False
    bounce: bool = False
    bounced_ids: list[str] = field(default_factory=list)
    bounced_addresses: list[str] = field(default_factory=list)


def _ids(value: Any) -> list[str]:
    return MSGID.findall(str(value or ""))


def _received(msg: Message) -> datetime | None:
    try:
        dt = parsedate_to_datetime(str(msg.get("Date")))
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return now_eastern(dt) if dt.tzinfo else dt


def is_auto_reply(msg: Message) -> bool:
    submitted = str(msg.get("Auto-Submitted") or "").strip().lower()
    if submitted and submitted != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        return True
    if str(msg.get("Precedence") or "").strip().lower() in ("auto_reply", "bulk", "junk", "list"):
        return True
    return bool(AUTO_SUBJECT.search(str(msg.get("Subject") or "")))


def _is_bounce(msg: Message, from_email: str) -> bool:
    ctype = str(msg.get("Content-Type") or "").lower()
    return bool(BOUNCE_FROM.match(from_email)) or ("multipart/report" in ctype and "delivery-status" in ctype)


def parse_headers(raw: bytes, uid: int, uidvalidity: int = 0) -> Mail:
    msg = message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(str(msg.get("From") or ""))
    mid = (_ids(msg.get("Message-ID")) or [f"<uid-{uidvalidity}-{uid}@imap.local>"])[0]
    irt = (_ids(msg.get("In-Reply-To")) or [None])[0]
    return Mail(uid=uid, message_id=mid, from_email=addr.strip().lower(), from_name=name.strip(),
                subject=str(msg.get("Subject") or "").strip(), received_at=_received(msg), in_reply_to=irt,
                references=_ids(msg.get("References")), auto_reply=is_auto_reply(msg),
                bounce=_is_bounce(msg, addr.strip().lower()))


_QUOTE_START = [
    re.compile(r"^\s*On\b.{0,240}\bwrote:\s*$", re.I),                   # Gmail / Apple Mail (one line)
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.I),           # Outlook
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}", re.I),
    re.compile(r"^\s*_{8,}\s*$"),                                        # Outlook web separator
    re.compile(r"^\s*>"),                                                # quoted lines
    re.compile(r"(sent from my (iphone|ipad|android|samsung|galaxy|mobile|phone|cell)|get outlook for (ios|android))", re.I),
    re.compile(r"^--\s*$"),                                             # the standard signature delimiter
]
# A sign-off on its own line ends the message: everything under it is the signature (name, brokerage, phones,
# "Top 1%", "20 Million Dollar Sales Volume", ...), and none of it is part of the answer.
_SIGN_OFF = re.compile(r"^\s*(sincerely|best|best regards|regards|kind regards|warm regards|warmly|thanks|thank you|"
                       r"thanks again|many thanks|cheers|respectfully|all the best|talk soon)[,.!]?\s*$", re.I)
_OUTLOOK_FROM = re.compile(r"^\s*\*?From:\*?\s", re.I)
_OUTLOOK_NEXT = re.compile(r"^\s*\*?(Sent|Date|To|Subject):\*?\s", re.I)


def strip_quoted(text: str, limit: int | None = None) -> str:
    """The agent's own words: everything above the quoted thread, the phone signature or the Outlook header block."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if any(p.search(line) for p in _QUOTE_START):
            break
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if re.match(r"^\s*On\b", line, re.I) and re.search(r"\bwrote:\s*$", nxt, re.I) and len(line) < 200:
            break                                                        # "On ... <x@y.com>\nwrote:" (wrapped)
        if _OUTLOOK_FROM.match(line) and any(_OUTLOOK_NEXT.match(x) for x in lines[i + 1:i + 5]):
            break
        if _SIGN_OFF.match(line) and any(x.strip() for x in out):
            break
        out.append(line.rstrip())
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    limit = limit or int(_cfg().get("max_body_chars", 4000))
    return body[:limit]


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<div[^>]+class=\"?[^\">]*gmail_quote.*", "", markup)   # Gmail's quoted thread
    markup = re.sub(r"(?is)<blockquote.*?</blockquote>", "", markup)
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", markup)
    return _html.unescape(re.sub(r"<[^>]+>", "", markup))


def body_text(msg: Message) -> str:
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError):
        content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    return _html_to_text(content) if part.get_content_subtype() == "html" else str(content)


def parse_message(raw: bytes, uid: int, uidvalidity: int = 0) -> Mail:
    mail = parse_headers(raw, uid, uidvalidity)
    msg = message_from_bytes(raw, policy=policy.default)
    if mail.bounce:
        flat = raw.decode("utf-8", "replace")
        mail.bounced_ids = [m for m in dict.fromkeys(re.findall(r"(?im)^\s*Message-ID:\s*(<[^<>\s]+>)", flat))
                            if m != mail.message_id]
        mail.bounced_addresses = sorted({a.lower() for a in re.findall(
            r"(?im)^\s*(?:Final|Original)-Recipient:\s*rfc822;\s*<?([^\s<>;]+@[^\s<>;]+)>?", flat)})
        return mail
    mail.text = strip_quoted(body_text(msg))
    return mail


# -- IMAP -------------------------------------------------------------------------------------------------------
_imap_factory: Callable[[], Any] | None = None


def set_imap_factory(factory: Callable[[], Any] | None) -> None:
    """Tests (and a dry run) swap in a fake IMAP connection."""
    global _imap_factory
    _imap_factory = factory


def _default_imap() -> Any:
    # WHOLESALING_IMAP_HOST / _PORT: tests point these at a closed local port so no test can reach a real inbox.
    host = os.environ.get("WHOLESALING_IMAP_HOST") or _cfg().get("imap_host", "imap.gmail.com")
    port = int(os.environ.get("WHOLESALING_IMAP_PORT") or _cfg().get("imap_port", 993))
    # 60 s per socket operation: a busy Gmail answer took longer than 30 s on about 1 run in 5 (2026-11-09)
    return imaplib.IMAP4_SSL(host, port, ssl_context=ssl_context(), timeout=60)


def _quote_box(name: str) -> str:
    return name if name.upper() == "INBOX" or name.startswith('"') else f'"{name}"'


def _uid_of(meta: bytes) -> int | None:
    m = re.search(rb"UID (\d+)", meta)
    return int(m.group(1)) if m else None


class Mailbox:
    """INBOX, opened read-only: we never set \\Seen, move or delete anything in the operator's Gmail."""

    def __init__(self, user: str, password: str, mailbox: str | None = None) -> None:
        self.user, self.password = user, password
        self.mailbox = mailbox                 # None = INBOX; "\\Sent" = the account's Sent folder (any language)
        self.imap: Any = None
        self.uidvalidity = 0

    def __enter__(self) -> "Mailbox":
        try:
            self.imap = (_imap_factory or _default_imap)()
        except OSError as exc:              # includes ssl.SSLCertVerificationError (macOS without certificates)
            why = explain(exc, self.user, "imap.gmail.com")
            raise GmailError(f"can't reach Gmail: {why}" if "CERTIFICATE" not in str(exc).upper() else why) from exc
        try:
            self.imap.login(self.user, self.password)
        except imaplib.IMAP4.error as exc:
            raise GmailError("Gmail refused the sign-in: check the Gmail address and the 16-letter app password "
                             f"in My Setup ({exc})") from exc
        box = self.mailbox or _cfg().get("mailbox", "INBOX")
        if box == "\\Sent":
            box = self._special("\\Sent") or "[Gmail]/Sent Mail"
        typ, _ = self.imap.select(_quote_box(box), readonly=True)
        if typ != "OK":
            raise GmailError("Gmail would not open the inbox")
        _, val = self.imap.response("UIDVALIDITY")
        try:
            self.uidvalidity = int((val or [b"0"])[0] or 0)
        except (TypeError, ValueError):
            self.uidvalidity = 0
        return self

    def _special(self, flag: str) -> str | None:
        """The mailbox carrying a special-use flag (RFC 6154), e.g. Gmail's Sent folder in any interface language."""
        try:
            typ, data = self.imap.list()
        except Exception:                  # noqa: BLE001 - an old server without LIST attributes: use the default
            return None
        for line in data or []:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            if flag.lower() in text.lower():
                m = re.search(r'"([^"]+)"\s*$', text) or re.search(r"(\S+)\s*$", text)
                if m:
                    return m.group(1)
        return None

    def __exit__(self, *exc: Any) -> None:
        for step in ("close", "logout"):
            try:
                getattr(self.imap, step)()
            except Exception:          # noqa: BLE001 - closing a dead connection must not hide the real error
                pass

    def search(self, after_uid: int, since: date) -> list[int]:
        if after_uid:
            typ, data = self.imap.uid("SEARCH", None, "UID", f"{after_uid + 1}:*")
        else:
            typ, data = self.imap.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))
        if typ != "OK":
            raise GmailError("Gmail search failed")
        uids = [int(x) for x in (data[0] or b"").split() if x.isdigit()]
        return sorted(u for u in uids if u > after_uid)          # "N:*" always returns the newest message

    def _fetch(self, uids: list[int], what: str) -> dict[int, bytes]:
        out: dict[int, bytes] = {}
        for i in range(0, len(uids), 100):
            chunk = ",".join(str(u) for u in uids[i:i + 100])
            typ, data = self.imap.uid("FETCH", chunk, f"(UID {what})")
            if typ != "OK":
                raise GmailError("Gmail fetch failed")
            for item in data or []:
                if isinstance(item, tuple) and len(item) >= 2:
                    uid = _uid_of(item[0])
                    if uid is not None:
                        out[uid] = item[1]
        return out

    def headers(self, uids: list[int]) -> dict[int, bytes]:
        return self._fetch(uids, f"BODY.PEEK[HEADER.FIELDS ({' '.join(HEADER_FIELDS)})]")

    def message(self, uid: int) -> bytes:
        return self._fetch([uid], "BODY.PEEK[]").get(uid, b"")


# -- matching ---------------------------------------------------------------------------------------------------
@dataclass
class Known:
    own: str
    thread: dict[str, int]                         # Message-ID -> lead
    agents: dict[str, list[int]]                   # agent e-mail -> leads where they are the listing agent
    touched: dict[int, datetime]                   # lead -> our last e-mail / text to its agent
    streets: dict[int, re.Pattern[str]]            # lead -> "30 Rieder" pattern (touched leads only)
    seen: set[str]
    phones: dict[str, list[int]] = field(default_factory=dict)   # agent phone (10 digits) -> leads (Google Voice)


# Google Voice forwards texts and voicemails to the same Gmail (Voice settings: "Forward messages to email",
# "Get voicemail via email"). Voice has no API for software to send texts or place calls (and automated texts are
# off-limits here anyway), so the desk READS them: an agent's text or voicemail becomes their reply on the lead, a
# 📞 call on the calendar and a drafted text the operator can send from their phone.
_VOICE_TEXT = re.compile(r"@txt\.voice\.google\.com$", re.I)
_VOICE_MAIL = re.compile(r"^voice-noreply@google\.com$", re.I)
_PHONE10 = re.compile(r"\(?(\d{3})\)?[\s.-]*(\d{3})[\s.-]*(\d{4})")
_VOICE_FOOTER = re.compile(r"(?im)^\s*(to respond to this text message|you received this|this email was sent|"
                           r"your account|help center|help forum|google llc|play message|listen to (the )?message|"
                           r"view this (message|voicemail)|reply to this email)")


def voice_kind(mail: Mail) -> str | None:
    """"voice_text" / "voice_mail" for a Google Voice notification, else None."""
    if _VOICE_TEXT.search(mail.from_email or ""):
        return "voice_text"
    if _VOICE_MAIL.search(mail.from_email or "") and "voicemail" in (mail.subject or "").lower():
        return "voice_mail"
    return None


def voice_phone(mail: Mail) -> str | None:
    for text in (mail.subject or "", mail.from_name or ""):
        m = _PHONE10.search(text)
        if m:
            return "".join(m.groups())
    return None


def strip_voice(text: str) -> str:
    """The agent's own words from a Voice notification: no footer, links or "Transcript:" label."""
    m = _VOICE_FOOTER.search(text or "")
    body = (text or "")[:m.start()] if m else (text or "")
    body = re.sub(r"https?://\S+|<[^>]+>", " ", body)
    body = re.sub(r"(?im)^\s*transcript\s*:\s*", "", body)
    return re.sub(r"[ \t]+", " ", body).strip()


def street_pattern(address: str | None) -> re.Pattern[str] | None:
    st = hc.street(address or "")
    m = re.match(r"\s*(\d+[A-Za-z]?)(?:-\d+)?\s+([A-Za-z][A-Za-z'.-]+)", st)
    if not m:
        return None
    word = m.group(2).rstrip(".")
    if word.lower() in {"n", "s", "e", "w", "north", "south", "east", "west", "old", "new", "st", "mt"}:
        rest = st[m.end():].split()
        if rest:
            word = rf"{re.escape(word)}\.?\s+{re.escape(rest[0].rstrip('.'))}"
            return re.compile(rf"\b{re.escape(m.group(1))}\s+{word}\b", re.I)
    return re.compile(rf"\b{re.escape(m.group(1))}\s+{re.escape(word)}\b", re.I)


def load_known(session: Session) -> Known:
    from engine.scripts import lead_facts
    own = (credentials() or ("", ""))[0].lower()
    thread = {mid: lid for mid, lid in session.execute(
        select(Interaction.email_message_id, Interaction.lead_id).where(Interaction.email_message_id.is_not(None)))}
    agents: dict[str, list[int]] = {}
    for email, lid in session.execute(select(Contact.email, Contact.lead_id).where(
            Contact.contact_type == ContactType.LISTING_AGENT, Contact.email.is_not(None), Contact.lead_id.is_not(None))):
        agents.setdefault(email.strip().lower(), []).append(lid)
    touched = {lid: at for lid, at in session.execute(
        select(Interaction.lead_id, func.max(Interaction.occurred_at)).where(
            Interaction.outcome.in_(ao.TOUCH_OUTCOMES)).group_by(Interaction.lead_id))}
    streets: dict[int, re.Pattern[str]] = {}
    for lead in session.scalars(select(Lead).where(Lead.id.in_(list(touched)))) if touched else []:
        pat = street_pattern(lead_facts(lead)["address"])
        if pat is not None:
            streets[lead.id] = pat
    seen = set(session.scalars(select(InboundReply.message_id)))
    phones: dict[str, list[int]] = {}
    for phone, lid in session.execute(select(Contact.phone, Contact.lead_id).where(
            Contact.contact_type == ContactType.LISTING_AGENT, Contact.phone.is_not(None), Contact.lead_id.is_not(None))):
        digits = re.sub(r"\D", "", phone or "")[-10:]
        if len(digits) == 10:
            phones.setdefault(digits, []).append(lid)
    return Known(own=own, thread=thread, agents=agents, touched=touched, streets=streets, seen=seen, phones=phones)


def match(known: Known, mail: Mail) -> tuple[int | None, str | None]:
    """(lead_id, method) or (None, None). Body text is used only when the mail has already been downloaded."""
    for mid in [mail.in_reply_to, *reversed(mail.references)]:
        if mid and mid in known.thread:
            return known.thread[mid], "thread"
    kind = voice_kind(mail)
    if kind:
        leads = list(dict.fromkeys(known.phones.get(voice_phone(mail) or "", [])))
        touched = [lid for lid in leads if lid in known.touched]
        if touched:
            return max(touched, key=lambda lid: known.touched[lid]), kind
        return None, None                         # a text from someone who isn't an agent we contacted: never read
    hay = f"{mail.subject}\n{mail.text}"
    leads = known.agents.get(mail.from_email, [])
    if leads:
        touched = [lid for lid in dict.fromkeys(leads) if lid in known.touched]
        named = [lid for lid in (touched or list(dict.fromkeys(leads)))
                 if lid in known.streets and known.streets[lid].search(hay)]
        if len(named) == 1:
            return named[0], "agent_email"
        if len(touched) == 1:
            return touched[0], "agent_email"
        if touched:
            return max(touched, key=lambda lid: known.touched[lid]), "agent_latest"
        return None, None                         # an agent we never wrote to: not a reply to us
    hits = [lid for lid, pat in known.streets.items() if pat.search(mail.subject)]
    if len(hits) == 1:
        return hits[0], "address"
    return None, None


def _firm_target(known: Known, fthread: dict[str, int], femails: dict[str, int], mail: Mail) -> int | None:
    """A reply from a research-firm prospect: matched by our pitch's Message-ID, else by a pitched address that is
    not one of our listing agents' threads (a listing agent answering a lead thread stays a lead reply)."""
    from engine import firm_outbox as fo
    if mail.message_id in known.seen or (known.own and mail.from_email == known.own) or mail.bounce:
        return None
    lead_id, method = match(known, mail)
    if method == "thread":
        return None
    tid = fo.match(fthread, {}, mail)
    if tid is not None:
        return tid
    if lead_id is not None:
        return None
    return fo.match({}, femails, mail)


def firm_bounced(session: Session, mail: Mail) -> int:
    """A pitch that bounced: the address is remembered (never written to again) and the target closed."""
    from db.models import FirmTarget
    n = 0
    for addr in mail.bounced_addresses or []:
        t = session.scalar(select(FirmTarget).where(FirmTarget.email == addr.lower()))
        if t is not None:
            ao.record_bounce(session, addr)
            t.stage, t.notes = "LOST", ((t.notes or "") + f"\nbounced {now_eastern():%Y-%m-%d}").strip()
            n += 1
    return n


def wanted(known: Known, mail: Mail) -> bool:
    """Header-only check: is this message worth downloading?"""
    if mail.message_id in known.seen or (known.own and mail.from_email == known.own):
        return False
    if mail.bounce:
        return True
    return match(known, mail)[0] is not None


# -- processing -------------------------------------------------------------------------------------------------
def reply_subject(subject: str | None) -> str:
    s = (subject or "").strip()
    return s if re.match(r"(?i)^re\s*:", s) else f"Re: {s}" if s else "Re: your listing"


def _only_offer_questions(text: str) -> bool:
    """Every question in the reply is one Touch 2 answers: what's your offer, are you a buyer or an agent, do you want
    to see it, did you want to submit an offer."""
    from engine import reply_negotiator as rn
    answerable = [OFFER_QUESTION, *rn._COMPILED["ASKS_ROLE"], *rn._COMPILED["ASKS_SHOWING"], *rn._COMPILED["BACKUP"],
                  re.compile(r"(submit|send|make|put in) (an |your |a )?offer", re.I)]
    qs = [q for q in re.findall(r"[^.!?\n]*\?", text or "") if q.strip(" ?")]
    return all(any(p.search(q) for p in answerable) for q in qs)


def asked_to_check_back(session: Session, lead_id: int, exclude_id: int | None = None) -> bool:
    """Our latest answer on the lead asked "mind if I check back in about N days?" (too far apart / not now)."""
    q = select(InboundReply).where(InboundReply.lead_id == lead_id, InboundReply.status.in_(("auto_sent", "approved")))
    if exclude_id is not None:
        q = q.where(InboundReply.id != exclude_id)
    last = session.scalar(q.order_by(InboundReply.resolved_at.desc(), InboundReply.id.desc()).limit(1))
    if last is None:
        return False
    text = " ".join(str(x or "") for x in (last.headline, (last.analysis_json or {}).get("reply_text"))).lower()
    return "check back" in text


def asked_facts_before(session: Session, lead_id: int, exclude_id: int | None = None) -> bool:
    """We already sent this lead's fact question (beds / baths / sqft, the rent roll, unit sales + HOA). Asking it again
    after the agent answered it reads as a bot (2026-11-05: Olga answered "good condition, the website is correct" and
    got the same question back)."""
    q = select(InboundReply.analysis_json).where(InboundReply.lead_id == lead_id,
                                                 InboundReply.status.in_(("auto_sent", "approved")))
    if exclude_id is not None:
        q = q.where(InboundReply.id != exclude_id)
    return any((a or {}).get("kind") == "fact_check" for a in session.scalars(q))


def asked_net_before(session: Session, lead_id: int) -> bool:
    """We already sent this lead's ask-first note ("what would the seller need to net?")."""
    rows = session.scalars(select(Interaction.debrief_json).where(Interaction.lead_id == lead_id,
                                                                 Interaction.debrief_json.is_not(None)))
    return any(((d or {}).get("packet") or {}).get("kind") == "ask_first" for d in rows)


def earlier_counter(session: Session, lead_id: int, exclude_id: int | None = None) -> float | None:
    """The newest number an agent already gave us on this lead, if any."""
    q = select(InboundReply.counter_amount).where(InboundReply.lead_id == lead_id,
                                                  InboundReply.counter_amount.is_not(None))
    if exclude_id is not None:
        q = q.where(InboundReply.id != exclude_id)
    return session.scalar(q.order_by(InboundReply.id.desc()).limit(1))


def auto_touch2_block(session: Session, lead: Lead, analysis: Any, method: str | None, text: str) -> str | None:
    """None when Touch 2 may go out by itself; otherwise the plain reason it waits for the operator."""
    from engine import preflight
    from engine.fast_track import is_listed_foreclosure, listed_foreclosure
    if not _cfg().get("auto_send_touch2", True):
        return "auto-send of the offer is off in settings"
    if (method or "").startswith("voice"):
        return ("they texted you" if method == "voice_text" else "they left you a voicemail") + \
            ": call them back, or text back from your phone (the drafted text is on the lead)"
    if method not in ("thread", "agent_email"):
        return "matched by the street name only: confirm it's about this listing"
    intents = set(analysis.parsed.intents)
    if analysis.parsed.counter_amount:
        return "they named a number: your call"
    before = earlier_counter(session, lead.id)
    if before:
        # Asking "what would the seller need to net?" (or opening at our price) after they told us reads as a bot.
        return f"they already gave a number on this house ({hc.money(before)}): your call"
    sent_offer = float(lead.offer_amount or 0)
    if sent_offer:
        # Touch 2 is the FIRST written offer. 2026-11-09, 1708 Heck Ave: three offers had gone out and the agent's
        # concern about a viewing was answered by the desk re-sending its opening e-mail word for word.
        return f"an offer is already out on this house ({hc.money(sent_offer)}): their answer needs a look"
    fact = getattr(analysis, "kind", "") == "fact_check"
    if not ({"AVAILABLE", "BACKUP"} & intents) or (analysis.verdict != "GREEN" and not fact):
        return "not a plain 'still available'"
    extra = intents - AUTO_OK_INTENTS
    if extra:
        return "they also mentioned " + ", ".join(sorted(x.lower().replace("_", " ") for x in extra))
    if "?" in (text or "") and not _only_offer_questions(text):
        # (the negotiator drops QUESTION when another intent is present, so read the text itself)
        return "they asked a question that needs your answer"
    agent = ao.agent_contact(lead)
    if agent is None:
        return "no listing-agent contact on the lead"
    if fact:
        # A no-price question (the unit's size and HOA, the rent roll, beds / baths / sqft on a mismatched parcel):
        # it answers what they asked and asks for the one fact the price needs. No number, no LOI.
        if asked_facts_before(session, lead.id):
            return "we already asked for these facts: their answer needs a look"
        if not _cfg().get("auto_send_fact_check", True):
            return "pre-flight: " + analysis.headline.split(": ", 1)[-1]
        if agent.dnc_status == "dnc":
            return "this agent asked to stop"
        ok, why = ao.smtp_ready()
        return None if ok else why
    try:
        packet = ao.build_packet(lead, agent=agent, generate_loi=False, session=session)
    except ValueError as exc:
        return f"no offer path ({exc})"
    if packet.kind == "ask_first":
        # No price (engine/offer_math.py ask-first tier): "what would the seller need to net?" Nothing in it can be
        # wrong or a lowball, so it answers "still available" by itself like a fact check.
        if not _cfg().get("auto_send_ask_first", True):
            return "ask-first answers wait for you (settings)"
        if asked_net_before(session, lead.id):
            # 2026-11-05: Tracy answered "please submit an offer in writing" and got the same "what would the seller
            # need to net?" back. Asked once; the next answer is a number or the one fact the number needs.
            return "we already asked what the seller needs: they want a written number (your call)"
        if agent.dnc_status == "dnc":
            return "this agent asked to stop"
        if is_listed_foreclosure(lead.property):
            return "listed foreclosure: your call"
        ok, why = ao.smtp_ready()
        return None if ok else why
    if packet.kind != "offer" or not packet.anchor:
        return "no price yet: ask who lives there first (FRFPA)"
    if packet.warnings:
        return "; ".join(packet.warnings)
    pf = preflight.check(lead)
    if pf.status == "HOLD":
        return "pre-flight HOLD: " + "; ".join(pf.issues[:2])
    blockers = [i for i in pf.issues if PRICE_BLOCKERS.search(i)]
    if blockers:
        return "pre-flight: " + blockers[0]
    if is_listed_foreclosure(lead.property):
        track = listed_foreclosure(session, lead).track
        if track not in ("CASH_OFFER",):
            return f"listed foreclosure on the {str(track).replace('_', ' ').lower()} track"
    ok, why = ao.smtp_ready()
    if not ok:
        return why
    return None


def _analysis_json(an: Any) -> dict[str, Any]:
    d = an.to_json()
    d.pop("parsed", None)
    return {k: d.get(k) for k in ("verdict", "headline", "numbers", "reply_subject", "reply_text", "sms_text", "actions",
                                  "followup_at", "warnings", "style", "stage_to", "kind")}


def reply_price(an: Any) -> float | None:
    """The number our answer carries (and its LOI): their price when we accept, else our counter / hold / opening."""
    n = an.numbers if hasattr(an, "numbers") else (an or {}).get("numbers") or {}
    verdict = an.verdict if hasattr(an, "verdict") else (an or {}).get("verdict")
    if verdict == "GREEN" and n.get("counter"):
        return float(n["counter"])
    if n.get("our_counter"):
        return float(n["our_counter"])
    return None


def _drop_email(session: Session, contact: Contact, why: str) -> None:
    """A bounced address comes off the agent's card (kept in the notes); their phone stays."""
    bad, stamp = contact.email, now_eastern().strftime("%Y-%m-%d")
    contact.notes = " | ".join(x for x in (contact.notes, f"{stamp}: e-mail {bad} bounced ({why})") if x)
    ao.record_bounce(session, bad or "")          # never sent to again, whichever card still carries it
    session.flush()
    try:
        with session.begin_nested():
            contact.email = None
            session.flush()
    except IntegrityError:
        contact.confidence_score = 0           # an identical row without the e-mail exists: retire this one
        session.flush()


def handle(session: Session, mail: Mail, known: Known, now: datetime | None = None) -> InboundReply | None:
    """Store one matched message and act on it. Raises IntegrityError when another process got it first."""
    from engine import reply_negotiator as rn
    when = mail.received_at or now_eastern(now)
    if mail.bounce:
        leads = {known.thread[m] for m in mail.bounced_ids if m in known.thread}
        q = select(Contact).where(Contact.contact_type == ContactType.LISTING_AGENT, Contact.email.is_not(None))
        hit = [c for c in session.scalars(q.where(func.lower(Contact.email).in_(mail.bounced_addresses)))] \
            if mail.bounced_addresses else []
        if not hit and leads:
            hit = [c for c in session.scalars(q.where(Contact.lead_id.in_(leads)))]
        firm_bounced(session, mail)
        if not hit:
            return None
        row = InboundReply(message_id=mail.message_id, lead_id=hit[0].lead_id, contact_id=hit[0].id,
                           from_email=mail.from_email, subject=mail.subject[:300], received_at=when, status="bounce",
                           note=f"delivery failed to {', '.join(sorted({c.email for c in hit}))}: the address is off "
                                "the card; call the agent or find a new e-mail")
        session.add(row)
        session.flush()
        for c in hit:
            _drop_email(session, c, f"bounce {when:%Y-%m-%d}")
            lead = session.get(Lead, c.lead_id) if c.lead_id else None
            if lead is not None and c.phone and c.dnc_status != "dnc":
                from engine import reply_negotiator as rn
                lead.next_followup_at = rn._call_slot(now)      # their e-mail is dead: the call lands on the calendar
        session.flush()
        return row
    lead_id, method = match(known, mail)
    if lead_id is None:
        return None
    lead = session.get(Lead, lead_id)
    if lead is None:
        return None
    agent = next((c for c in lead.contacts if c.email and c.email.strip().lower() == mail.from_email), None) \
        or ao.agent_contact(lead)
    voice = (method or "").startswith("voice")
    if voice:
        # The Voice relay address would turn an e-mail into a text from the operator's number: never store it.
        mail = replace(mail, text=strip_voice(mail.text))
    row = InboundReply(message_id=mail.message_id, lead_id=lead.id, contact_id=agent.id if agent else None,
                       from_email=None if voice else mail.from_email, from_name=mail.from_name[:160],
                       subject=mail.subject[:300], received_at=when, in_reply_to=mail.in_reply_to,
                       references_json=mail.references[-20:], body_text=mail.text, match_method=method)
    session.add(row)
    session.flush()                            # the unique Message-ID claims this mail before anything else is written
    if mail.auto_reply:
        row.status, row.note, row.resolved_at = "auto_reply", "out-of-office / automatic reply: nothing to do", when
        return row
    if not voice and rn.is_ack(mail.text):
        # "Sounds good! Thank you!" after a no-price note: nothing to answer, no call (2026-11-04: it became a 📞 call
        # event and "waiting on you"). After a priced offer it may be an acceptance, so it is read like any reply.
        last = session.scalar(select(Interaction).where(Interaction.lead_id == lead.id,
                                                        Interaction.outcome.in_(ao.TOUCH_OUTCOMES))
                              .order_by(Interaction.occurred_at.desc()).limit(1))
        if last is not None and last.outcome != ao.SENT_OUTCOME:
            row.status, row.resolved_at, row.verdict = "dismissed", when, "INFO"
            row.headline = "THANKS / ACKNOWLEDGED: nothing to answer"
            row.note = "a thank-you after a no-price note: nothing to answer, no call"
            return row
    if not voice and rn.is_checkin_ok(mail.text) and asked_to_check_back(session, lead.id, row.id):
        # "Sure, you can absolutely" to our "mind if I check back in about 30 days?" (2026-11-04: read as "still
        # available", it got a second "what would the seller need to net?" right after the agent named a number).
        row.status, row.resolved_at, row.verdict = "dismissed", when, "INFO"
        row.headline = "OK TO CHECK BACK: nothing to answer (the check-in date stands)"
        row.note = "they agreed to our check-in: nothing to answer, no call; the follow-up already set stays"
        return row
    an = rn.analyze(session, lead, mail.text, now=now, agent=agent)
    inter = rn.record(session, lead, an, mail.text, now=now)
    if voice:
        inter.channel = "sms_in" if method == "voice_text" else "voicemail"     # not an e-mail thread to answer in
    else:
        inter.email_message_id = mail.message_id
    row.interaction_id = inter.id
    row.verdict, row.headline = an.verdict, an.headline[:200]
    row.intents_json = sorted(an.parsed.intents)
    row.counter_amount = an.parsed.counter_amount
    row.analysis_json = _analysis_json(an)
    row.price = reply_price(an)
    if "OPT_OUT" in an.parsed.intents:
        row.status, row.resolved_at = "opted_out", when
        row.note = "they asked to stop: no more e-mails or calls to this agent (internal stop list)"
        return row
    block = auto_touch2_block(session, lead, an, method, mail.text)
    if block is None:
        lo, hi = (_cfg().get("touch2_delay_minutes") or [4, 11])[:2]
        row.status = "auto_queued"
        row.send_after = max(when, now_eastern(now)) + timedelta(minutes=random.uniform(float(lo), float(hi)))
        row.note = ("still available, but the price needs one fact first: a no-price answer asking for it goes out by "
                    "itself in their thread" if getattr(an, "kind", "") == "fact_check" else
                    "still available: the offer goes out by itself in their thread")
    else:
        row.status, row.note = "needs_approval", block
    session.flush()
    return row


# -- state --------------------------------------------------------------------------------------------------------
def _state(session: Session) -> dict[str, Any]:
    row = session.get(SwarmState, STATE_KEY)
    return dict(row.value_json or {}) if row else {}


def _save_state(session: Session, **values: Any) -> None:
    row = session.get(SwarmState, STATE_KEY)
    if row is None:
        row = SwarmState(key=STATE_KEY, value_json={})
        session.add(row)
    row.value_json = {**(row.value_json or {}), **values}
    session.flush()


_poll_lock = threading.Lock()


def poll_gmail_replies(*, now: datetime | None = None, smtp_factory: Any = None) -> dict[str, Any]:
    """One inbox check: new messages -> matched replies -> negotiator -> queue / Action Needed; then due auto-sends.

    The network part (IMAP) runs with no database transaction open; each message is then its own transaction."""
    ok, why = configured()
    if not ok:
        return {"ok": False, "error": why}
    user, pw = credentials() or ("", "")
    with _poll_lock:
        with session_scope() as s:
            refresh_open_replies(s, now)
            known, state = load_known(s), _state(s)
            from engine import firm_outbox as fo
            fthread, femails = fo.known(s)
        since = now_eastern(now).date() - timedelta(days=int(_cfg().get("lookback_days", 14)))
        mails: list[Mail] = []
        with Mailbox(user, pw) as mb:
            same = state.get("account") == user.lower() and state.get("uidvalidity") == mb.uidvalidity
            last = int(state.get("last_uid") or 0) if same else 0
            uids = mb.search(last, since)
            for uid, raw in sorted(mb.headers(uids).items()):
                head = parse_headers(raw, uid, mb.uidvalidity)
                if wanted(known, head) or _firm_target(known, fthread, femails, head):
                    mails.append(parse_message(mb.message(uid), uid, mb.uidvalidity))
            uidvalidity = mb.uidvalidity
        counts = {"checked": len(uids), "matched": 0, "auto_queued": 0, "needs_approval": 0, "opted_out": 0,
                  "bounce": 0, "auto_reply": 0}
        failed: list[int] = []
        for mail in mails:
            tid = None if mail.bounce else _firm_target(known, fthread, femails, mail)
            if tid is not None:
                # A B2B prospect of the research firm (engine/firm_outbox.py): stored there, never a lead reply.
                try:
                    with session_scope() as s:
                        if fo.handle_reply(s, mail, tid, now) is not None:
                            counts["firm_replies"] = counts.get("firm_replies", 0) + 1
                except IntegrityError:
                    pass
                except Exception:                         # noqa: BLE001
                    log.exception("gmail autopilot: firm reply uid %s", mail.uid)
                    failed.append(mail.uid)
                continue
            try:
                with session_scope() as s:
                    row = handle(s, mail, known, now)
                    if row is not None:
                        counts["matched"] += 1
                        counts[row.status] = counts.get(row.status, 0) + 1
            except IntegrityError:
                continue                                  # another process stored this Message-ID first
            except Exception:                             # noqa: BLE001 - one odd e-mail must not stop the rest
                log.exception("gmail autopilot: could not process message uid %s", mail.uid)
                failed.append(mail.uid)
        top = max(uids) if uids else last
        with session_scope() as s:
            _save_state(s, account=user.lower(), uidvalidity=uidvalidity,
                        last_uid=(min(failed) - 1) if failed else top, last_poll=now_eastern(now).isoformat())
    counts["sent"] = send_due(now=now, smtp_factory=smtp_factory)
    return {"ok": True, **counts, "failed": len(failed)}


# -- sending ------------------------------------------------------------------------------------------------------
def _base_packet(session: Session, lead: Lead, agent: Contact | None) -> ao.AgentPacket:
    try:
        return ao.build_packet(lead, agent=agent, generate_loi=False, session=session)
    except ValueError:
        return ao.build_opener(lead, agent)


def send_due(*, now: datetime | None = None, smtp_factory: Any = None) -> int:
    """Send the queued Touch 2 offers whose human-paced delay is over (inside send_hours)."""
    stale = now_eastern(now) - timedelta(minutes=15)
    with session_scope() as s:
        for row in s.scalars(select(InboundReply).where(InboundReply.status == "sending")):
            if (row.resolved_at or row.created_at or stale) <= stale:
                row.status = "needs_approval"
                row.note = "the automatic send may not have gone out: check Gmail's Sent folder before approving"
        ids = list(s.scalars(select(InboundReply.id).where(InboundReply.status == "auto_queued",
                                                           InboundReply.send_after <= now_eastern(now))))
    if not ids or not in_send_hours(now):
        return 0
    return send_queued(ids, now=now, smtp_factory=smtp_factory)[0]


def send_queued(ids: list[int], *, now: datetime | None = None, smtp_factory: Any = None) -> tuple[int, str | None]:
    """Send these queued offers one after another, a few seconds apart (`reply_pace_seconds`: Gmail drops the line
    on a burst of sign-ins). A failure every e-mail would share (sign-in, network, Gmail hanging up three times in a
    row) stops the rest; they stay queued for the next check. Returns (sent, the stopping sentence or None)."""
    lo, hi = (float(x) for x in _cfg().get("reply_pace_seconds", [8, 20]))
    sent, error = 0, None
    if ids:
        log.info("%d answer(s) to agents to send, %.0f-%.0f seconds apart", len(ids), lo, hi)
    for rid in ids:
        if sent:
            wait = random.uniform(lo, hi)
            log.info("next one in %.0f s", wait)
            _sleep(wait)
        try:
            ok = _send_auto(rid, now=now, smtp_factory=smtp_factory)
            sent += int(ok)
            if ok:
                log.info("sent %d of %d", sent, len(ids))
        except GmailSendError as exc:                     # fatal: the next one would fail the same way
            error = str(exc)
            log.warning("gmail autopilot: stopped sending offers: %s", exc)
            break
        except Exception:                                 # noqa: BLE001
            log.exception("gmail autopilot: auto Touch 2 failed for reply %s", rid)
    return sent, error


_sleep = __import__("time").sleep          # tests swap it out (no real waits)


def _send_auto(rid: int, *, now: datetime | None, smtp_factory: Any) -> bool:
    from engine import reply_negotiator as rn
    with session_scope() as s:
        row = s.get(InboundReply, rid)
        if row is None or row.status != "auto_queued":
            return False
        lead = s.get(Lead, row.lead_id) if row.lead_id else None
        if lead is None:
            row.status, row.note = "needs_approval", "the lead is gone"
            return False
        newer = s.scalar(select(Interaction.id).where(
            Interaction.lead_id == lead.id, Interaction.occurred_at > (row.received_at or row.created_at),
            Interaction.id != row.interaction_id,
            Interaction.outcome.in_((*ao.TOUCH_OUTCOMES, ao.REPLY_OUTCOME, ao.PHONE_YES_OUTCOME))).limit(1))
        writer = s.get(Contact, row.contact_id) if row.contact_id else None
        an = rn.analyze(s, lead, row.body_text or "", now=now, agent=writer)
        block = ("something happened on this lead after the reply" if newer else
                 auto_touch2_block(s, lead, an, row.match_method, row.body_text or ""))
        if block:
            row.status, row.note = "needs_approval", block
            return False
        agent = (s.get(Contact, row.contact_id) if row.contact_id else None) or ao.agent_contact(lead)
        fact = an.kind == "fact_check"
        if not fact:
            packet = ao.build_packet(lead, agent=agent, generate_loi=True, session=s,
                                     reply_flags=set(an.parsed.intents) | {"ANSWERED"})
            if packet.kind != "ask_first" and (packet.warnings or not packet.loi_files.get("pdf")):
                row.status = "needs_approval"
                row.note = "the written offer could not be attached: " + ("; ".join(packet.warnings) or "no PDF")
                return False
            packet = replace(packet, subject=reply_subject(row.subject), agent_email=row.from_email or packet.agent_email)
    if fact:
        return _send_fact_check(rid, an, now=now, smtp_factory=smtp_factory)
    # Claim the row atomically (a second poller in another process can't send it twice), then go to the network
    # with no transaction open.
    with session_scope() as s:
        claimed = s.execute(update(InboundReply).where(InboundReply.id == rid, InboundReply.status == "auto_queued")
                            .values(status="sending", resolved_at=now_eastern(now))).rowcount
    if claimed != 1:
        return False
    with session_scope() as s:
        row = s.get(InboundReply, rid)
        lead = s.get(Lead, row.lead_id)
        try:
            mid = ao.deliver(packet, session=s, now=now, smtp_factory=smtp_factory, in_reply_to=row.message_id,
                             references=list(row.references_json or []))
        except RuntimeError as exc:
            limit = "limit" in str(exc)
            row.status = "auto_queued" if limit else "needs_approval"
            row.note = f"not sent yet: {exc}"
            row.resolved_at = None
            failed = exc
        else:
            failed = None
    if failed is not None:
        if getattr(failed, "fatal", False):
            raise failed                   # the row is saved first; the caller stops the rest of the run
        return False
    with session_scope() as s:
        row = s.get(InboundReply, rid)
        lead = s.get(Lead, row.lead_id)
        ao.record_sent(s, lead, packet, "email_smtp", now, message_id=mid, engaged=True)
        if "CALL_ME" in (row.intents_json or []):
            from engine import reply_negotiator as rn2
            lead.next_followup_at = rn2._call_slot(now)          # they asked for a call: it goes on the calendar
        row.status, row.sent_message_id, row.price = "auto_sent", mid, packet.anchor
        row.analysis_json = {**(row.analysis_json or {}), "sent": {"anchor": packet.anchor, "best": packet.best,
                                                                   "subject": packet.subject}}
        row.resolved_at = now_eastern(now)
        row.note = (f"offer sent in their thread: {hc.money(packet.anchor)} / {hc.money(packet.best)} with the "
                    "1-page written offer; follow-up in 48 hours")
    return True


def _send_fact_check(rid: int, an: Any, *, now: datetime | None, smtp_factory: Any) -> bool:
    """The no-price answer goes out by itself in their thread (claimed first, like Touch 2); no LOI, no number."""
    with session_scope() as s:
        claimed = s.execute(update(InboundReply).where(InboundReply.id == rid, InboundReply.status == "auto_queued")
                            .values(status="sending", resolved_at=now_eastern(now))).rowcount
    if claimed != 1:
        return False
    with session_scope() as s:
        row = s.get(InboundReply, rid)
        lead = s.get(Lead, row.lead_id)
        agent = (s.get(Contact, row.contact_id) if row.contact_id else None) or ao.agent_contact(lead)
        base = _base_packet(s, lead, agent)
        packet = replace(base, kind="followup", subject=reply_subject(row.subject), email_body=an.reply_text,
                         agent_email=row.from_email or base.agent_email, loi_files={})
        try:
            mid = ao.deliver(packet, session=s, now=now, smtp_factory=smtp_factory, in_reply_to=row.message_id,
                             references=list(row.references_json or []))
        except RuntimeError as exc:
            row.status = "auto_queued" if "limit" in str(exc) else "needs_approval"
            row.note, row.resolved_at = f"not sent yet: {exc}", None
            failed = exc
        else:
            failed = None
            when = now_eastern(now)
            follow = an.followup_at or ao.followup_time(now)
            s.add(Interaction(lead_id=lead.id, contact_id=agent.id if agent else None, channel="email_smtp",
                              outcome="AGENT_FOLLOWUP_SENT", notes=f"{packet.subject} (asked for the facts, no price)",
                              next_followup_at=follow, email_message_id=mid, occurred_at=when,
                              debrief_json={"packet": {"kind": "fact_check"}, "engaged": True}))
            lead.last_contact_at, lead.next_followup_at = when, follow
            ask = {"condo": "what same-size units sold for and the HOA", "units": "the rent roll",
                   "basics": "beds, baths, sqft and condition"}.get((an.numbers or {}).get("fact_check") or "basics")
            row.status, row.sent_message_id, row.resolved_at = "auto_sent", mid, when
            row.note = f"answered in their thread and asked for {ask or 'the facts'} (no price yet)"
    if failed is not None:
        if getattr(failed, "fatal", False):
            raise failed
        return False
    return True


def approve(session: Session, reply_id: int, *, subject: str | None = None, body: str | None = None,
            now: datetime | None = None, smtp_factory: Any = None) -> dict[str, Any]:
    """The operator's one click: send the drafted answer (and an LOI at its price) in the agent's thread."""
    from engine.contracts import ContractBlocked, generate
    row = session.get(InboundReply, reply_id)
    if row is None:
        raise ApprovalError(f"reply {reply_id} not found")
    if row.status not in ("needs_approval",):
        raise ApprovalError(f"this reply is already {row.status.replace('_', ' ')}")
    lead = session.get(Lead, row.lead_id) if row.lead_id else None
    if lead is None:
        raise ApprovalError("this reply isn't matched to a lead")
    if (row.match_method or "").startswith("voice"):
        raise ApprovalError("they texted or called you: call them back, or text back from your phone; to answer by "
                            "e-mail instead, use gmail --reply")
    an = row.analysis_json or {}
    text = (body if body is not None and body.strip() else an.get("reply_text") or "").strip()
    if not text:
        raise ApprovalError("there's no drafted answer for this one: call the agent or dismiss it")
    op = hc.operator()
    if op["postal"] and op["postal"] not in text:
        raise ApprovalError("Keep the summary block under your signature: it carries your mailing address (CAN-SPAM).")
    if not any(x in text for x in ('"no more"', "STOP")):
        raise ApprovalError('Keep the opt-out line (reply "no more") in the e-mail.')
    agent = (session.get(Contact, row.contact_id) if row.contact_id else None) or ao.agent_contact(lead)
    if agent is not None and agent.dnc_status == "dnc":
        raise ApprovalError("this agent asked to stop: nothing more goes to them")
    intents = set(row.intents_json or [])
    price = row.price                         # their number we accept, or our counter / best-and-final / hold
    priced = bool(price) and an.get("verdict") in ("GREEN", "YELLOW", "RED") and "UNDER_CONTRACT" not in intents
    base = _base_packet(session, lead, agent)
    warnings: list[str] = []
    files: dict[str, str] = {}
    # "Still available" (with a question the operator answered by approving): the drafted note is Touch 2 itself.
    touch2 = (not priced and {"AVAILABLE", "BACKUP"} & intents and base.kind == "offer" and not row.counter_amount
              and an.get("verdict") == "GREEN" and an.get("kind", "touch2") != "fact_check")
    # (a no-price note, e.g. "confirm the beds / baths / sqft" on a listing matched to the wrong parcel, never gets
    # an LOI built from the numbers it says it can't trust)
    loi_price = price if priced else (base.anchor if touch2 else None)
    if loi_price:
        try:
            g = generate(session, lead, "loi", price=loi_price)
            files = {p.suffix.lstrip("."): str(p) for p in g.paths}
        except ContractBlocked as exc:
            warnings.extend(exc.reasons)
    kind = "offer" if (priced or touch2) else "followup"
    packet = replace(base, kind=kind, subject=(subject or "").strip() or reply_subject(row.subject), email_body=text,
                     agent_email=row.from_email or base.agent_email, loi_files=files,
                     anchor=loi_price if kind == "offer" else base.anchor)
    session.commit()                          # the LOI record is written before we go to the network
    try:
        mid = ao.deliver(packet, session=session, now=now, smtp_factory=smtp_factory, in_reply_to=row.message_id,
                         references=list(row.references_json or []))
    except (RuntimeError, OSError, smtplib.SMTPException) as exc:
        raise ApprovalError(f"Gmail didn't send it: {exc}") from exc
    when = now_eastern(now)
    if kind == "offer":
        ao.record_sent(session, lead, packet, "email_smtp", now, message_id=mid, engaged=True)
        follow = an.get("followup_at")
        try:
            later = datetime.fromisoformat(follow) if follow else None
        except ValueError:
            later = None
        if later and lead.next_followup_at and later > lead.next_followup_at:
            lead.next_followup_at = later    # a RED hold drips in 14 days, not 48 hours
    else:
        follow_at = ao.followup_time(now)
        session.add(Interaction(lead_id=lead.id, contact_id=agent.id if agent else None, channel="email_smtp",
                                outcome="AGENT_FOLLOWUP_SENT", notes=packet.subject, next_followup_at=follow_at,
                                email_message_id=mid, debrief_json={"packet": {"kind": "reply"}, "engaged": True},
                                occurred_at=when))
        lead.last_contact_at, lead.next_followup_at = when, follow_at
    row.status, row.sent_message_id, row.resolved_at = "approved", mid, when
    row.note = (f"sent in their thread{f' at {hc.money(loi_price)}' if loi_price else ''}"
                + (" with the written offer attached" if files.get("pdf") else ""))
    session.flush()
    return {"id": row.id, "lead_id": lead.id, "sent": True, "to": packet.agent_email, "price": loi_price,
            "loi_attached": bool(files.get("pdf")), "warnings": warnings}


ANSWERED_ALREADY = "answered already: an e-mail went to them after this reply"


def answered_since(session: Session, row: InboundReply) -> bool:
    """We already wrote to this agent after their reply came in (a note from the chat, an approved draft, the operator's
    own Gmail). 2026-11-06: a reply the desk read only after Claude's answer went out was answered a second time 14
    minutes later, with a note that contradicted the first."""
    if not row.lead_id:
        return False
    since = row.received_at or row.created_at
    if since is None:
        return False
    return session.scalar(select(Interaction.id).where(
        Interaction.lead_id == row.lead_id, Interaction.occurred_at > since,
        Interaction.id != row.interaction_id, Interaction.outcome.in_(ao.TOUCH_OUTCOMES)).limit(1)) is not None


def approvable(session: Session) -> tuple[list[InboundReply], list[tuple[InboundReply, str]]]:
    """Replies `approve_all` may send (a drafted answer, matched by thread or the agent's own address) and the ones it
    leaves for the operator, with why (no draft, matched only by a street in the subject, the agent asked to stop,
    already answered by a later e-mail)."""
    ok: list[InboundReply] = []
    held: list[tuple[InboundReply, str]] = []
    for row in session.scalars(select(InboundReply).where(InboundReply.status == "needs_approval")
                               .order_by(InboundReply.received_at)):
        an = row.analysis_json or {}
        agent = session.get(Contact, row.contact_id) if row.contact_id else None
        if answered_since(session, row):
            held.append((row, ANSWERED_ALREADY))
        elif not (an.get("reply_text") or "").strip():
            held.append((row, "no drafted answer: call them or dismiss it"))
        elif (row.match_method or "").startswith("voice"):
            held.append((row, "they texted or left a voicemail: a call (or a text from your phone)"))
        elif row.match_method not in ("thread", "agent_email"):
            held.append((row, "matched only by the street in the subject: check it's about this listing"))
        elif agent is not None and agent.dnc_status == "dnc":
            held.append((row, "the agent asked to stop"))
        else:
            ok.append(row)
    return ok, held


# Answers that go out with no person (learned 2026-11-03: the operator handles calls only). No-price notes always;
# a priced hold / counter only when the agent's number was stated plainly ("wants 355k", "we are at 470"), never one
# read out of a signature. Accepts, unclassified questions and anything else stay for a call.
SAFE_NO_PRICE = ("CONFIRM THE FACTS FIRST", "NUMBERS ON HOLD", "LISTING EXPIRED", "OFF THE TABLE", "TOO FAR APART",
                 "NOT NOW", "CALL REQUEST: ANSWERED IN WRITING", "NO PROOF OF FUNDS YET",
                 "PROOF OF FUNDS AT CONTRACT")
# A priced answer goes out by itself only when the agent stated their number plainly AND the valuation passes every
# certainty check (engine/offer_math.py): a number we send is final, so it never rests on old comps or a guessed size.
SAFE_PRICED = ("BACKUP AT", "HOLD FIRM AT", "HOLD AT", "COUNTER AT", "BEST AND FINAL AT")


def auto_approve(*, now: datetime | None = None, smtp_factory: Any = None) -> dict[str, Any]:
    """Send the drafted answers the desk may send by itself (`gmail_autopilot.auto_approve`, default on)."""
    from engine import reply_negotiator as rn
    empty: dict[str, Any] = {"sent": [], "failed": [], "stopped": None}
    if not _cfg().get("auto_approve", True):
        return empty
    with session_scope() as s:
        ok, held = approvable(s)
        for r, why in held:
            if why == ANSWERED_ALREADY:            # closed, so the 📊 report and the call sheet stop asking about it
                r.status, r.resolved_at, r.note = "dismissed", now_eastern(), why
        ids = []
        for r in ok:
            an = r.analysis_json or {}
            head = str(an.get("headline") or r.headline or "").upper()
            if an.get("kind") == "fact_check":
                if r.lead_id and not asked_facts_before(s, r.lead_id, r.id):
                    ids.append(r.id)               # never the same fact question twice on one house
            elif head.startswith(SAFE_NO_PRICE):
                ids.append(r.id)
            elif head.startswith(SAFE_PRICED) and (not r.counter_amount
                                                   or rn.parse_reply(r.body_text or "").counter_explicit):
                from engine.offer_math import certainty_issues
                lead = s.get(Lead, r.lead_id) if r.lead_id else None
                if lead is not None and not certainty_issues(lead, s):
                    ids.append(r.id)
    return approve_all(now=now, smtp_factory=smtp_factory, ids=ids) if ids else empty


def approve_all(*, now: datetime | None = None, smtp_factory: Any = None,
                ids: list[int] | None = None) -> dict[str, Any]:
    """Send every drafted answer waiting on the operator (or just `ids`), each through approve() with its gates (LOI
    at the drafted price, CAN-SPAM lines, stop list), 8-20 seconds apart, stopping at a Gmail failure every e-mail
    would share. The operator confirms the list once (`gmail --approve-all`) instead of one reply at a time."""
    lo, hi = (float(x) for x in _cfg().get("reply_pace_seconds", [8, 20]))
    with session_scope() as s:
        ok, _ = approvable(s)
        todo = [r.id for r in ok if ids is None or r.id in ids]
    out: dict[str, Any] = {"sent": [], "failed": [], "stopped": None}
    for n, rid in enumerate(todo):
        if n:
            _sleep(random.uniform(lo, hi))
        try:
            with session_scope() as s:
                r = approve(s, rid, now=now, smtp_factory=smtp_factory)
            out["sent"].append(r)
            log.info("sent %d of %d", len(out["sent"]), len(todo))
        except ApprovalError as exc:
            out["failed"].append({"id": rid, "error": str(exc)})
            cause = exc.__cause__
            if getattr(cause, "fatal", False):
                out["stopped"] = str(exc)
                break
    return out


def dismiss(session: Session, reply_id: int, note: str = "") -> dict[str, Any]:
    row = session.get(InboundReply, reply_id)
    if row is None:
        raise ApprovalError(f"reply {reply_id} not found")
    if row.status not in OPEN_STATUSES:
        raise ApprovalError(f"this reply is already {row.status.replace('_', ' ')}")
    row.status, row.resolved_at = "dismissed", now_eastern()
    row.note = note or "dismissed: handled by hand"
    session.flush()
    return {"id": row.id, "status": row.status}


def sending_now(session: Session, lead_id: int) -> bool:
    return bool(session.scalar(select(InboundReply.id).where(InboundReply.lead_id == lead_id,
                                                             InboundReply.status == "sending").limit(1)))


def close_open_replies(session: Session, lead_id: int, note: str) -> int:
    """Settled without a desk answer (a call, a polite no Claude answered, "sharing with the sellers"): every reply
    still waiting on the lead is dismissed with the note, so nothing more goes out on it by itself."""
    rows = list(session.scalars(select(InboundReply).where(InboundReply.lead_id == lead_id,
                                                           InboundReply.status.in_(OPEN_STATUSES))))
    rows = [r for r in rows if r.status != "sending"]          # one already going out finishes on its own
    for row in rows:
        row.status, row.resolved_at, row.note = "dismissed", now_eastern(), note[:200]
    if rows:
        session.flush()
    return len(rows)


def resolve_open_replies(session: Session, lead_id: int, note: str) -> int:
    """The operator answered this agent another way (card player, lead page, phone): close what was waiting."""
    rows = list(session.scalars(select(InboundReply).where(InboundReply.lead_id == lead_id,
                                                           InboundReply.status.in_(("auto_queued", "needs_approval")))))
    for row in rows:
        row.status, row.resolved_at, row.note = "approved", now_eastern(), note
    if rows:
        session.flush()
    return len(rows)


def reply_thread(session: Session, lead_id: int) -> dict[str, Any] | None:
    """Thread headers for answering the agent's latest e-mail on this lead (None when they never e-mailed us)."""
    row = session.scalar(select(InboundReply).where(InboundReply.lead_id == lead_id,
                                                    InboundReply.status.not_in(("bounce", "auto_reply")),
                                                    func.coalesce(InboundReply.match_method, "").not_like("voice%"))
                         .order_by(InboundReply.received_at.desc(), InboundReply.id.desc()).limit(1))
    if row is not None:
        return {"message_id": row.message_id, "references": list(row.references_json or []),
                "subject": reply_subject(row.subject)}
    inter = session.scalar(select(Interaction).where(Interaction.lead_id == lead_id,
                                                     Interaction.outcome == ao.REPLY_OUTCOME,
                                                     Interaction.email_message_id.is_not(None))
                           .order_by(Interaction.occurred_at.desc()).limit(1))
    return {"message_id": inter.email_message_id, "references": [], "subject": None} if inter else None


def waiting_reply(session: Session, lead_id: int) -> InboundReply | None:
    """The agent's latest reply on this lead that still waits on the operator (Action Needed or the Touch 2 queue)."""
    return session.scalar(select(InboundReply).where(InboundReply.lead_id == lead_id,
                                                     InboundReply.status.in_(("needs_approval", "auto_queued")))
                          .order_by(InboundReply.received_at.desc(), InboundReply.id.desc()).limit(1))


def send_now(rid: int, *, now: datetime | None = None, smtp_factory: Any = None) -> dict[str, Any]:
    """⏩ Send now on a queued Touch 2: skip the human-paced wait (every other check still runs)."""
    with session_scope() as s:
        row = s.get(InboundReply, rid)
        if row is None:
            raise ApprovalError(f"reply {rid} not found")
        if row.status != "auto_queued":
            raise ApprovalError(f"this reply is {row.status.replace('_', ' ')}, not waiting to go out")
        row.send_after = now_eastern(now)
    sent = _send_auto(rid, now=now, smtp_factory=smtp_factory)
    with session_scope() as s:
        row = s.get(InboundReply, rid)
        return {"id": rid, "sent": sent, "status": row.status, "note": row.note}


def refresh_open_replies(session: Session, now: datetime | None = None, *, promote: bool = True) -> int:
    """Re-read replies still waiting on the operator with today's parser (a signature once read as a counter, ...).
    The stored analysis, light and drafted answer are replaced; a counter the new reading drops is cleared on the
    lead too. Nothing is sent."""
    from engine import reply_negotiator as rn
    n = 0
    for row in session.scalars(select(InboundReply).where(InboundReply.status.in_(("needs_approval", "auto_queued")),
                                                          InboundReply.lead_id.is_not(None))):
        lead = session.get(Lead, row.lead_id)
        if lead is None or not row.body_text:
            continue
        text = strip_quoted(row.body_text)
        an = rn.analyze(session, lead, text, now=now,
                        agent=session.get(Contact, row.contact_id) if row.contact_id else None)
        # (the drafted answer too: after a re-price the same "still available" must not keep yesterday's number)
        same = an.headline == row.headline and an.parsed.counter_amount == row.counter_amount and text == row.body_text \
            and sorted(an.parsed.intents) == sorted(row.intents_json or []) \
            and reply_price(an) == row.price and an.reply_text == (row.analysis_json or {}).get("reply_text")
        if same and (row.status == "auto_queued"
                     or not (promote and auto_touch2_block(session, lead, an, row.match_method, text) is None)):
            continue
        old_counter = row.counter_amount
        row.body_text, row.verdict, row.headline = text, an.verdict, an.headline[:200]
        row.intents_json, row.counter_amount = sorted(an.parsed.intents), an.parsed.counter_amount
        row.analysis_json, row.price = _analysis_json(an), reply_price(an)
        if old_counter and not an.parsed.counter_amount and lead.last_counter_amount == old_counter:
            lead.last_counter_amount = None
        if row.status == "auto_queued":
            n += 1                         # queued already: only the stored reading is refreshed (it sends as planned)
            continue
        if promote and "OPT_OUT" not in an.parsed.intents:
            block = auto_touch2_block(session, lead, an, row.match_method, text)
            if block is None:
                row.status, row.send_after = "auto_queued", now_eastern(now)
                row.note = ("re-read: the price needs one fact first, so a no-price answer asking for it goes out now"
                            if an.kind == "fact_check" else
                            "re-read: they confirmed it's available, so the offer goes out now")
            else:
                row.note = block
        n += 1
    if n:
        session.flush()
    return n


def awaiting_approval_lead_ids(session: Session) -> set[int]:
    """Leads whose latest agent reply is waiting (queued Touch 2 or the operator's click): kept off the card deck."""
    return set(session.scalars(select(InboundReply.lead_id).where(InboundReply.status.in_(OPEN_STATUSES),
                                                                   InboundReply.lead_id.is_not(None))))


# -- the outbound batch -------------------------------------------------------------------------------------------
def auto_sendable(card: dict[str, Any]) -> str | None:
    """None when the batch may send this card; otherwise why it stays for the manual player."""
    a = card.get("agent") or {}
    if not a.get("email") or not card.get("can_smtp"):
        return "no e-mail on file (call or text from the card player)"
    pf = card.get("preflight") or {}
    if pf.get("status") not in ("GO", "VERIFY"):
        return f"pre-flight {pf.get('status')}"
    if card.get("track") == "SHORT_SALE" or "short sale" in str(card.get("label") or "").lower():
        return "lender short sale (low odds): by hand only"
    if card.get("warnings"):
        return "; ".join(card["warnings"])[:160]
    if card.get("packet_kind") == "offer":
        blockers = [i for i in pf.get("issues") or [] if PRICE_BLOCKERS.search(i)]
        if blockers:
            return "priced note with a pre-flight flag: " + blockers[0]
    return None


def agents_written_recently(session: Session, now: datetime | None = None, hours: float = 72) -> set[str]:
    """Listing-agent e-mail addresses we e-mailed (any lead) inside the last `hours`."""
    since = now_eastern(now).replace(tzinfo=None) - timedelta(hours=hours)
    rows = session.execute(select(Contact.email).join(Interaction, Interaction.contact_id == Contact.id)
                           .where(Interaction.channel.in_(("email", "email_smtp")), Interaction.occurred_at >= since,
                                  Contact.email.is_not(None)))
    return {str(e).strip().lower() for (e,) in rows if e}


def held_for_calls(session: Session, day: Any) -> set[int]:
    """Leads whose e-mail waits for today's call: follow-up rows (T0 / T2) not yet dialed without an answer."""
    from engine import call_sheet
    return {i for r in call_sheet.stored(session, day) if r.tier in ("T0", "T2")
            for i in (r.lead_ids or [r.lead_id])} - unanswered_today(session, day)


def unanswered_today(session: Session, day: Any) -> set[int]:
    """Leads whose listing agent was called today without reaching anyone (no answer, voicemail)."""
    start = datetime.combine(day, datetime.min.time())
    return {lid for (lid,) in session.execute(
        select(Interaction.lead_id).where(Interaction.channel == "call", Interaction.occurred_at >= start,
                                          Interaction.outcome.in_(("NO_ANSWER", "LEFT_VM"))).distinct())}


def pick_cards(session: Session, limit: int, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Today's batch (in deck order: urgent listed foreclosures, due follow-ups, MLS fixers) and what was skipped."""
    from engine import autopilot
    deck = autopilot.deck(session, limit=max(int((settings().get("autopilot", {}) or {}).get("deck_size", 20)),
                                             limit * 2), now=now)
    picked, skipped = [], []
    # One new cold e-mail per agent address per `agent_cold_gap_hours` (72): an agent with 20 listings gets one note
    # about one house, not 20 in a day (2026-11-03 volume plan). Follow-ups on a lead already in touch are not limited.
    gap = float(_cfg().get("agent_cold_gap_hours", 72))
    busy = agents_written_recently(session, now, hours=gap) if gap > 0 else set()
    from engine import call_sheet
    # The call sheet and the e-mail work together (2026-11-04: 37% of e-mails got a reply, most calls rang out): a
    # cold row (never contacted, T1/T3/T4) gets its opener e-mail the same morning, so the afternoon call follows it.
    # Only a follow-up row (T0 reply waiting / T2 due follow-up) waits for its call, until a reported no-answer or
    # voicemail releases the e-mail nudge.
    calling = held_for_calls(session, now_eastern(now).date())
    for c in deck["cards"]:
        why = auto_sendable(c)
        item = {"lead_id": c["lead_id"], "address": c["address"], "kind": c["kind"], "touch": c["touch"],
                "agent": (c.get("agent") or {}).get("name")}
        addr = str((c.get("agent") or {}).get("email") or "").strip().lower()
        if why is None and addr and int(c.get("touch") or 1) <= 1 and addr in busy:
            why = "this agent already got a new e-mail from us in the last 3 days"
        if why is None and c["lead_id"] in calling:
            why = "a follow-up on today's call sheet: the call goes first"
        if why is None and len(picked) < limit:
            picked.append(item)
            if addr and gap > 0:
                busy.add(addr)
        elif why is not None:
            skipped.append({**item, "reason": why})
    return picked, skipped


@dataclass
class Campaign:
    running: bool = False
    total: int = 0
    sent: int = 0
    failed: int = 0
    current: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    next_send_at: str | None = None
    message: str = ""
    log: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["progress_text"] = (f"🚀 AI Autopilot Sending: {self.sent} / {self.total} Deals Contacted..." if self.running
                              else self.message)
        return d


campaign = Campaign()
_campaign_lock = threading.Lock()
_stop = threading.Event()


def _pace() -> tuple[float, float]:
    lo, hi = (_cfg().get("pace_seconds") or [25, 60])[:2]
    return float(lo), float(hi)


def run_daily_outbound_autopilot(limit: int | None = None, *, now: datetime | None = None, smtp_factory: Any = None,
                                 sleep: Callable[[float], Any] | None = None, rng: random.Random | None = None,
                                 picked: list[dict[str, Any]] | None = None) -> Campaign:
    """Send today's batch one by one, 25-60 s apart (a person's pace), through engine.autopilot.send."""
    from engine import autopilot
    rng = rng or random.Random()
    wait = sleep or (lambda secs: _stop.wait(secs))
    if picked is None:
        limit = limit or autopilot.daily_goal()
        with session_scope() as s:
            room = int((settings().get("outreach", {}) or {}).get("daily_email_limit", 25)) - ao.sent_last_24h(
                s, now, cold_only=True)
            picked, skipped = pick_cards(s, max(0, min(limit, room)), now)
        campaign.skipped = skipped[:30]
        campaign.total = len(picked)
    for i, item in enumerate(picked):
        if _stop.is_set():
            campaign.message = f"Stopped after {campaign.sent} of {campaign.total}."
            break
        if i:
            secs = rng.uniform(*_pace())
            campaign.next_send_at = (datetime.now() + timedelta(seconds=secs)).isoformat(timespec="seconds")
            if wait(secs) is True:
                campaign.message = f"Stopped after {campaign.sent} of {campaign.total}."
                break
        campaign.current = item["address"]
        try:
            with session_scope() as s:
                r = autopilot.send(s, item["lead_id"], "smtp", now=now, smtp_factory=smtp_factory)
            campaign.sent += 1
            campaign.log.append({**item, "status": "sent", "stage": r.get("stage"),
                                 "at": datetime.now().isoformat(timespec="seconds")})
        except autopilot.AutopilotError as exc:
            campaign.failed += 1
            campaign.log.append({**item, "status": "skipped", "reason": str(exc)})
            if "limit" in str(exc):
                campaign.message = f"Gmail daily limit reached ({exc})."
                break
            if getattr(exc.__cause__, "fatal", False):
                # Sign-in, certificates or network: every other e-mail would fail the same way.
                campaign.message = f"Stopped: {exc} Nothing else was sent; fix it and click again."
                break
        except Exception as exc:                          # noqa: BLE001 - one failed card must not stop the batch
            log.exception("gmail autopilot: send failed for lead %s", item["lead_id"])
            campaign.failed += 1
            campaign.log.append({**item, "status": "error", "reason": str(exc)[:200]})
    campaign.current, campaign.next_send_at = None, None
    if not campaign.message:
        campaign.message = (f"Done: {campaign.sent} e-mail{'s' if campaign.sent != 1 else ''} sent from your Gmail"
                            + (f", {campaign.failed} skipped" if campaign.failed else "") + ".")
    return campaign


def start_campaign(limit: int | None = None, *, now: datetime | None = None, smtp_factory: Any = None,
                   sleep: Callable[[float], Any] | None = None, background: bool = True) -> dict[str, Any]:
    """The 🚀 button: pick today's batch, then send it in a background thread (one batch at a time)."""
    from engine import autopilot
    ok, why = ao.smtp_ready()
    if not ok:
        raise CampaignError(f"Gmail isn't set up yet: {why}. Open ⚙️ My Setup.")
    if not in_send_hours(now):
        raise CampaignError(f"The autopilot sends between {send_hours_text()} so agents get them in working hours. "
                            "Click again then; the inbox is still checked every few minutes.")
    with _campaign_lock:
        if campaign.running:
            raise CampaignError("Already sending today's batch.")
        limit = limit or autopilot.daily_goal()
        with session_scope() as s:
            room = int((settings().get("outreach", {}) or {}).get("daily_email_limit", 25)) - ao.sent_last_24h(
                s, now, cold_only=True)
            if room <= 0:
                raise CampaignError("You've sent 25 e-mails in the last 24 hours (Gmail's safe pace); the next "
                                    "batch can go out tomorrow.")
            picked, skipped = pick_cards(s, min(limit, room), now)
        if not picked:
            raise CampaignError("Nothing is ready to send right now: run the data refresh for new listings, or work "
                                "the manual card player for calls.")
        _stop.clear()
        campaign.__init__()                               # fresh progress for this batch
        campaign.running, campaign.total, campaign.skipped = True, len(picked), skipped[:30]
        campaign.started_at = datetime.now().isoformat(timespec="seconds")

    def run() -> None:
        try:
            run_daily_outbound_autopilot(now=now, smtp_factory=smtp_factory, sleep=sleep, picked=picked)
        finally:
            campaign.running = False
            campaign.finished_at = datetime.now().isoformat(timespec="seconds")
            if poller.thread is not None and poller.thread.is_alive():
                poller.kick()                             # the dashboard's loop: look for early replies right away

    if background:
        threading.Thread(target=run, name="gmail-autopilot-send", daemon=True).start()
    else:
        run()
    return campaign.to_json()


def stop_campaign() -> dict[str, Any]:
    _stop.set()
    return campaign.to_json()


# -- Gmail is the record: what the operator sent from Gmail itself ------------------------------------------------
_OPTION = re.compile(r"Option 1:\s*\$([\d,]+)|(?:offer|cash)[^.$]{0,40}\$([\d,]{6,})", re.I)


def sync_sent(*, now: datetime | None = None) -> dict[str, Any]:
    """Import replies the operator sent into a lead's thread from Gmail itself (phone, web, another computer).

    Each one becomes the interaction it is (an offer when it states a price, else a follow-up), closes whatever was
    waiting for that agent and sets the 48-hour follow-up, so the autopilot never sends a second offer on top of a
    reply the operator already wrote. Sent mail outside our lead threads is never downloaded past its headers."""
    from engine import crm
    ok, why = configured()
    if not ok:
        return {"ok": False, "error": why}
    user, pw = credentials() or ("", "")
    with session_scope() as s:
        known, state = load_known(s), _state(s)
    since = now_eastern(now).date() - timedelta(days=int(_cfg().get("lookback_days", 14)))
    found: list[tuple[Mail, int]] = []
    with Mailbox(user, pw, mailbox="\\Sent") as mb:
        same = state.get("account") == user.lower() and state.get("sent_uidvalidity") == mb.uidvalidity
        last = int(state.get("sent_last_uid") or 0) if same else 0
        uids = mb.search(last, since)
        for uid, raw in sorted(mb.headers(uids).items()):
            head = parse_headers(raw, uid, mb.uidvalidity)
            if head.message_id in known.thread:
                continue                                   # sent by the autopilot itself: already logged
            lead_id = next((known.thread[m] for m in [head.in_reply_to, *reversed(head.references)]
                            if m and m in known.thread), None)
            if lead_id is not None:
                found.append((parse_message(mb.message(uid), uid, mb.uidvalidity), lead_id))
        uidvalidity, top = mb.uidvalidity, max(uids) if uids else last
    imported = 0
    for mail, lead_id in found:
        with session_scope() as s:
            if s.scalar(select(Interaction.id).where(Interaction.email_message_id == mail.message_id).limit(1)):
                continue
            lead = s.get(Lead, lead_id)
            if lead is None:
                continue
            agent = ao.agent_contact(lead)
            when = mail.received_at or now_eastern(now)
            m = _OPTION.search(mail.text or "")
            price = float((m.group(1) or m.group(2)).replace(",", "")) if m else None
            if price:
                r = crm.log_call(s, lead, ao.SENT_OUTCOME, notes=f"{mail.subject} (sent from Gmail)", contact=agent,
                                 channel="email_gmail", followup_at=ao.followup_time(when), now=when,
                                 offer_amount=price, debrief={"engaged": True, "imported": "gmail_sent",
                                                              "packet": {"kind": "offer", "anchor": price}})
                inter = s.get(Interaction, r.interaction_id)
                inter.email_message_id = mail.message_id
            else:
                s.add(Interaction(lead_id=lead.id, contact_id=agent.id if agent else None, channel="email_gmail",
                                  outcome="AGENT_FOLLOWUP_SENT", notes=f"{mail.subject} (sent from Gmail)",
                                  next_followup_at=ao.followup_time(when), email_message_id=mail.message_id,
                                  debrief_json={"engaged": True, "imported": "gmail_sent", "packet": {"kind": "reply"}},
                                  occurred_at=when))
                lead.last_contact_at, lead.next_followup_at = when, ao.followup_time(when)
            resolve_open_replies(s, lead.id, "you answered this one in Gmail")
            imported += 1
    with session_scope() as s:
        _save_state(s, account=user.lower(), sent_uidvalidity=uidvalidity, sent_last_uid=top)
    return {"ok": True, "checked": len(uids), "imported": imported}


def run_inbox(*, send: bool = True, now: datetime | None = None, smtp_factory: Any = None) -> dict[str, Any]:
    """`manage.py inbox` / the /inbox command / one daemon cycle: Gmail Sent → the inbox → offers out → the table.

    send=True sends every queued "still available" offer now (the operator asked for it, so no 4-11 minute wait and
    no send-hours window); counters, questions and the rest stay in Action Needed with a drafted answer."""
    out: dict[str, Any] = {"ok": True}
    try:
        out["sent_import"] = sync_sent(now=now)
    except GmailError as exc:
        out["sent_import"] = {"ok": False, "error": str(exc)}
    try:
        out["poll"] = poll_gmail_replies(now=now, smtp_factory=smtp_factory)
    except GmailError as exc:
        out.update(ok=False, error=str(exc))
        return out
    sent = 0
    if send:
        with session_scope() as s:
            ids = list(s.scalars(select(InboundReply.id).where(InboundReply.status == "auto_queued")))
            for row in s.scalars(select(InboundReply).where(InboundReply.id.in_(ids))):
                row.send_after = now_eastern(now)
        sent, error = send_queued(ids, now=now, smtp_factory=smtp_factory)
        if error:
            out["send_error"] = error
    out["sent_now"] = sent
    with session_scope() as s:
        out["rows"] = inbox_rows(s, now)
    return out


def inbox_rows(session: Session, now: datetime | None = None, days: int = 2) -> list[dict[str, Any]]:
    """Every agent reply of the last `days` days (and anything still waiting): what they said, what went back."""
    since = now_eastern(now) - timedelta(days=days)
    rows = session.scalars(select(InboundReply).where(
        (InboundReply.received_at >= since) | InboundReply.status.in_(OPEN_STATUSES))
        .order_by(InboundReply.received_at.desc()))
    out = []
    for row in rows:
        if row.status == "auto_reply":
            continue
        j = reply_json(session, row)
        sent = (row.analysis_json or {}).get("sent") or {}
        offer = (f"{hc.money(sent.get('anchor'))} to {hc.money(sent.get('best'))}" if sent.get("anchor") and sent.get("best")
                 else hc.money(row.price) if row.price and row.status in ("approved", "auto_sent") else "")
        if not offer and row.status == "approved" and row.lead_id:
            lead = session.get(Lead, row.lead_id)
            offer = hc.money(lead.offer_amount) if lead is not None and lead.offer_amount else ""
        todo = {"needs_approval": "your call: see the drafted answer", "auto_queued": "goes out on the next check",
                "sending": "going out now", "opted_out": "nothing (they asked to stop)",
                "bounce": "find a new e-mail or call", "dismissed": "nothing"}.get(row.status, "nothing")
        if row.status == "auto_sent" and (row.analysis_json or {}).get("kind") == "fact_check":
            todo = "wait for their facts, then check the numbers and send the offer"
        if "CALL_ME" in (row.intents_json or []) and row.status in ("auto_sent", "approved"):
            todo = "call them (it's on your calendar)"
        if row.status == "needs_approval" and (row.match_method or "").startswith("voice"):
            sms = (row.analysis_json or {}).get("sms_text") or ""
            todo = "call them back" + (f", or text: {sms}" if sms else "")
        if row.status in ("needs_approval", "auto_queued") and (row.note or "").startswith("not sent yet"):
            todo = f"nothing: it retries on the next inbox check (or `gmail --approve {row.id}` now)"
        out.append({**{k: j[k] for k in ("id", "lead_id", "address", "town", "agent", "from", "status", "note")},
                    "said": (row.body_text or "").replace("\n", " ")[:140], "light": j["light"]["text"],
                    "message": (row.body_text or "")[:4000],
                    "offer_sent": offer, "you_do": todo, "received_at": j["received_at"],
                    "phone": (j.get("phone") or {}).get("display"),
                    "draft": j["reply_text"] if row.status == "needs_approval" else "",
                    "numbers": j["numbers"], "offer_ok": j.get("offer_ok")})
    return out


def find_leads(session: Session, query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Leads by street address, town, agent name or agent e-mail (for /reply and `gmail --find`)."""
    from db.models import Property
    from engine.scripts import lead_facts
    q = f"%{query.strip()}%"
    ids = list(session.scalars(select(Lead.id).join(Property, Lead.property_id == Property.id)
                               .where(Property.street_address.ilike(q) | Property.municipality.ilike(q)).limit(limit)))
    ids += [i for i in session.scalars(select(Contact.lead_id).where(
        Contact.contact_type == ContactType.LISTING_AGENT, Contact.name.ilike(q) | Contact.email.ilike(q)).limit(limit))
        if i not in ids]
    out = []
    for lid in ids[:limit]:
        lead = session.get(Lead, lid)
        if lead is None:
            continue
        agent = ao.agent_contact(lead)
        waiting = waiting_reply(session, lid)
        out.append({"lead_id": lid, "address": lead_facts(lead)["address"], "stage": lead.status_stage,
                    "agent": agent.name if agent else None, "agent_email": agent.email if agent else None,
                    "last_touch": ao.last_touch(session, lead), "waiting_reply": waiting.id if waiting else None})
    return out


def resolve_lead(session: Session, ref: Any) -> int:
    """A lead id from "5951", "73 Palisade" or the agent's name / e-mail: a unique match only. Streets that start with
    the words win over ones that merely contain them ("73 Palisade" is not "173 Palisade"), and a lead we have
    e-mailed wins over one we never touched."""
    ref = str(ref).strip()
    if ref.isdigit():
        return int(ref)
    rows = find_leads(session, ref, limit=25)
    low = ref.lower().rstrip(".")
    pool = [r for r in rows if (r["address"] or "").lower().startswith(low)] or rows
    pool = [r for r in pool if r["waiting_reply"] or r["last_touch"]] or pool
    if len(pool) == 1:
        return int(pool[0]["lead_id"])
    if not pool:
        raise ApprovalError(f"no lead matches '{ref}' (try `gmail --find` with fewer words)")
    raise ApprovalError(f"'{ref}' matches {len(pool)} leads: " + "; ".join(
        f"{r['lead_id']} = {r['address']}" for r in pool[:6]) + ". Use the number.")


def resolve_reply(session: Session, ref: Any) -> int:
    """A waiting reply's id from its own id or the house ("73 Palisade"): the reply still open on that lead."""
    ref = str(ref).strip()
    if ref.isdigit():
        return int(ref)
    lead_id = resolve_lead(session, ref)
    row = waiting_reply(session, lead_id)
    if row is None:
        raise ApprovalError(f"nothing from the agent is waiting on lead {lead_id} ({ref})")
    return int(row.id)


DAILY_KEY = "daemon_daily_openers"


def daily_openers(limit: int, *, at: str = "10:30", now: datetime | None = None, smtp_factory: Any = None,
                  sleep: Callable[[float], Any] | None = None, background: bool = False) -> dict[str, Any] | None:
    """The weekday cold batch (opt-in: `install-daemon --daily-openers N`; the cloud desk: DAILY_OPENERS). After each
    wave time in `at` (Eastern) on Monday-Friday, inside send hours, once per wave: the same picks, gates, pacing and
    cold-e-mail cap as the 🚀 button. The day is claimed before the first e-mail, so a crash or a restart never sends a second batch.
    None = not due (off, weekend, too early, already done today)."""
    t = now_eastern(now)
    # `at` may name several waves ("10:30,14:00"): the day's limit is split between them, so no single desk run is
    # busy for half an hour, and a wave the desk missed is sent at its next run (first undone wave first).
    waves: list[tuple[int, int]] = []
    for part in str(at).split(","):
        try:
            hh, mm = (int(x) for x in part.strip().split(":", 1))
        except ValueError:
            continue
        waves.append((hh, mm))
    waves = sorted(set(waves)) or [(10, 30)]
    if limit <= 0 or t.weekday() >= 5 or not in_send_hours(now):
        return None
    today = t.date().isoformat()
    with session_scope() as s:
        row = s.get(SwarmState, DAILY_KEY)
        state = dict(row.value_json or {}) if row is not None else {}
        done = set(state.get("waves") or ([0] if state.get("date") == today else [])) if state.get("date") == today else set()
        due = [i for i, w in enumerate(waves) if (t.hour, t.minute) >= w and i not in done]
        if not due:
            return None
        wave = due[0]
        # What earlier waves didn't use carries over (a short supply at 10:30 is made up after the morning calls and
        # the firm-up pass): each wave sends its share of what is left of the day's limit.
        sent_before = int(state.get("sent_total", state.get("sent")) or 0) if state.get("date") == today else 0
        left = len(waves) - len(done)
        remaining = max(0, limit - sent_before)
        limit = -(-remaining // max(left, 1))
        if row is None:
            row = SwarmState(key=DAILY_KEY, value_json={})
            s.add(row)
        row.value_json = {"date": today, "waves": sorted(done | {wave}), "started": t.isoformat(timespec="seconds"),
                          "limit": limit, "sent_total": sent_before}
        if limit <= 0:
            return None
    try:
        start_campaign(limit, now=now, smtp_factory=smtp_factory, sleep=sleep, background=background)
        out = {"started": True, "total": campaign.total, "sent": campaign.sent, "message": campaign.message}
    except CampaignError as exc:
        out = {"started": False, "total": 0, "sent": 0, "message": str(exc)}
    with session_scope() as s:
        row = s.get(SwarmState, DAILY_KEY)
        prev = dict(row.value_json or {})
        row.value_json = {**prev, **{k: v for k, v in out.items() if k != "started"},
                          "sent_total": int(prev.get("sent_total", 0) or 0) + int(out.get("sent") or 0)}
    return out


def send_custom_reply(session: Session, lead_id: int, text: str, *, subject: str | None = None,
                      price: float | None = None, now: datetime | None = None, smtp_factory: Any = None,
                      allow_over_walkaway: bool = False, to: str | None = None) -> dict[str, Any]:
    """`/reply <house> <what to say>`: the operator's own words, in the agent's thread, from Gmail, logged.

    The signature, mailing address and opt-out are added when missing (CAN-SPAM); price=N attaches the 1-page
    offer at N through the normal LOI gates and logs it as an offer. `to`: another address in the same thread
    (2026-11-04: "please contact Geri, who I've CC'd": the answer goes to the colleague, still threaded on the agent's
    message)."""
    from engine.contracts import ContractBlocked, generate
    lead = session.get(Lead, lead_id)
    if lead is None:
        raise ApprovalError(f"lead {lead_id} not found")
    agent = ao.agent_contact(lead)
    if agent is None:
        raise ApprovalError("no listing agent on this lead (or they asked to stop)")
    v = lead.property.current_valuation if lead.property else None
    walk = float(v.mao_aggressive) if v is not None and v.mao_aggressive else None
    if price and walk and price > walk and not allow_over_walkaway:
        # The unattended desk agent can't be talked above the walk-away; only the operator can (--over-walkaway).
        raise ApprovalError(f"{hc.money(price)} is above your walk-away ({hc.money(walk)}): not sent")
    thread = reply_thread(session, lead_id)
    op = hc.operator()
    body = text.strip()
    if op["postal"] and op["postal"] not in body:
        sig, gap = hc.signature_lines(op), "\n\n"
        # Text already signed with the first name ("Thanks,\nMaharsh") keeps one: no "Maharsh / Maharsh" (2026-11-05)
        last = body.splitlines()[-1].strip() if body else ""
        if sig and op["first"] and last.lower() == op["first"].strip().lower():
            sig, gap = sig[1:], "\n"
        foot = list(hc.footer_lines(op))
        if price and not re.search(r"assign", body, re.I):
            # a priced reply written in the chat carries the same investor / may-assign line as the first offer's
            # Offer Summary (CFA: nothing hidden; the attached LOI says it too)
            from engine.contracts import buyer_entity
            foot.insert(0, f"Who's buying: an investor buying as a principal, not a licensed agent. The buyer is "
                           f"\"{buyer_entity()}\"; I may assign the contract to another investor, and the seller's "
                           f"price and terms stay the same.")
        body += gap + "\n".join(sig) + "\n\n" + "\n".join(foot)
    waiting = waiting_reply(session, lead_id)
    to = (to or "").strip().lower() or \
        (waiting.from_email if waiting is not None and waiting.from_email else None) or agent.email
    if not to:
        raise ApprovalError("no e-mail address for this agent")
    base = _base_packet(session, lead, agent)
    files: dict[str, str] = {}
    warnings: list[str] = []
    if price:
        try:
            g = generate(session, lead, "loi", price=price)
            files = {p.suffix.lstrip("."): str(p) for p in g.paths}
        except ContractBlocked as exc:
            warnings.extend(exc.reasons)
    subj = (subject or "").strip() or ((thread or {}).get("subject")) or f"Re: {base.subject}"
    packet = replace(base, kind="offer" if price else "followup", subject=subj, email_body=body, agent_email=to,
                     loi_files=files, anchor=price or base.anchor)
    session.commit()
    try:
        mid = ao.deliver(packet, session=session, now=now, smtp_factory=smtp_factory, engaged=True,
                         in_reply_to=(thread or {}).get("message_id"), references=(thread or {}).get("references"))
    except (RuntimeError, OSError, smtplib.SMTPException) as exc:
        raise ApprovalError(f"Gmail didn't send it: {exc}") from exc
    if price:
        ao.record_sent(session, lead, packet, "email_smtp", now, message_id=mid, engaged=True)
    else:
        when = now_eastern(now)
        session.add(Interaction(lead_id=lead.id, contact_id=agent.id, channel="email_smtp", outcome="AGENT_FOLLOWUP_SENT",
                                notes=subj, next_followup_at=ao.followup_time(now), email_message_id=mid,
                                debrief_json={"packet": {"kind": "reply"}, "engaged": True}, occurred_at=when))
        lead.last_contact_at, lead.next_followup_at = when, ao.followup_time(now)
    resolve_open_replies(session, lead.id, "answered with your own words (/reply)")
    session.flush()
    return {"lead_id": lead.id, "to": to, "subject": subj, "threaded": bool(thread), "price": price,
            "loi_attached": bool(files.get("pdf")), "warnings": warnings}


# -- connection test ----------------------------------------------------------------------------------------------
def test_connection(*, send_to_self: bool = False, smtp_factory: Any = None) -> dict[str, Any]:
    """Sign in to smtp.gmail.com and imap.gmail.com with the saved credentials (My Setup / .env); nothing is sent
    unless send_to_self (a two-line note to your own address). Each side reports ok + the plain reason."""
    from email.message import EmailMessage
    creds = credentials()
    if creds is None:
        msg = "No Gmail address / app password saved yet: open ⚙️ My Setup."
        return {"ok": False, "account": "", "smtp": {"ok": False, "detail": msg}, "imap": {"ok": False, "detail": msg}}
    user, pw = creds
    out: dict[str, Any] = {"account": user}
    try:
        with (smtp_factory or ao.smtp_connection)() as smtp:
            smtp.login(user, pw)
            detail = "signed in to smtp.gmail.com"
            if send_to_self:
                note = EmailMessage()
                note["From"], note["To"], note["Subject"] = user, user, "Wholesale Cockpit: Gmail test"
                note["Message-ID"] = ao.new_message_id()
                note.set_content("This is the dashboard checking that it can send from your Gmail. Nothing to do.")
                smtp.send_message(note)
                detail += f" and sent a test note to {user}"
        out["smtp"] = {"ok": True, "detail": detail}
    except Exception as exc:                              # noqa: BLE001 - report every failure in plain words
        out["smtp"] = {"ok": False, "detail": explain(exc, user)}
    try:
        with Mailbox(user, pw) as mb:
            out["imap"] = {"ok": True, "detail": f"inbox opened read-only (UIDVALIDITY {mb.uidvalidity})"}
    except GmailError as exc:
        out["imap"] = {"ok": False, "detail": str(exc)}
    except Exception as exc:                              # noqa: BLE001
        out["imap"] = {"ok": False, "detail": explain(exc, user, "imap.gmail.com")}
    out["ok"] = out["smtp"]["ok"] and out["imap"]["ok"]
    return out


# -- background poller --------------------------------------------------------------------------------------------
class _Poller:
    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.event = threading.Event()
        self.last: dict[str, Any] = {}
        self.next_at: datetime | None = None
        self.running = False

    def minutes(self) -> float:
        return max(1.0, float(_cfg().get("poll_minutes", 5)))

    def run_once(self) -> dict[str, Any]:
        self.running = True
        try:
            result = poll_gmail_replies()
        except GmailError as exc:
            result = {"ok": False, "error": str(exc)}
        except Exception as exc:                          # noqa: BLE001 - the loop must survive anything
            log.exception("gmail autopilot poll failed")
            result = {"ok": False, "error": f"inbox check failed: {exc}"}
        finally:
            self.running = False
        self.last = {**result, "at": datetime.now().isoformat(timespec="seconds")}
        return self.last

    def _loop(self) -> None:
        self.next_at = datetime.now() + timedelta(seconds=20)
        self.event.wait(20)
        while True:
            self.event.clear()
            if configured()[0]:
                self.run_once()
            self.next_at = datetime.now() + timedelta(minutes=self.minutes())
            self.event.wait(self.minutes() * 60)

    def start(self) -> None:
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._loop, name="gmail-autopilot-poll", daemon=True)
            self.thread.start()

    def kick(self) -> None:
        """Check now (the 📥 button, the end of a batch)."""
        if self.thread is not None and self.thread.is_alive():
            self.event.set()
        else:
            threading.Thread(target=self.run_once, name="gmail-autopilot-check", daemon=True).start()


poller = _Poller()


def start_background() -> bool:
    """Called by `manage.py server`: the loop always runs and idles until My Setup has the Gmail app password."""
    if not _cfg().get("enabled", True):
        return False
    poller.start()
    return True


# -- the simple home ----------------------------------------------------------------------------------------------
def _kmoney(v: float | None) -> str:
    if not v:
        return "?"
    return f"${v / 1e6:.2f}M" if v >= 1e6 else f"${round(v / 1000):,}K"


def light(row: InboundReply) -> dict[str, Any]:
    an = row.analysis_json or {}
    n = an.get("numbers") or {}
    verdict = row.verdict or "INFO"
    if verdict == "GREEN" and n.get("counter"):
        text, profit = f"🟢 DEAL WORKS AT {_kmoney(n['counter'])} — Your Est. Profit: {hc.money(n.get('fee_at_counter'))}", \
            n.get("fee_at_counter")
    elif verdict == "GREEN":
        text, profit = "🟢 STILL AVAILABLE — SEND YOUR OFFER", None
    elif verdict == "YELLOW":
        text, profit = (f"🟡 COUNTER AT {_kmoney(n.get('our_counter'))} — Your Est. Profit: "
                        f"{hc.money(n.get('fee_at_our_counter'))}"), n.get("fee_at_our_counter")
    elif verdict == "RED":
        their = f"THEIR {_kmoney(n['counter'])} IS PAST YOUR MAX — " if n.get("counter") else ""
        text, profit = f"🔴 {their}HOLD AT {_kmoney(n.get('our_counter'))}", n.get("fee_at_our_counter")
    else:
        text, profit = f"ℹ️ {row.headline or 'Read it and answer'}", None
    return {"verdict": verdict, "text": text, "profit": profit}


def reply_json(session: Session, row: InboundReply) -> dict[str, Any]:
    from engine import compliance
    from engine.scripts import lead_facts
    lead = session.get(Lead, row.lead_id) if row.lead_id else None
    f = lead_facts(lead) if lead else {}
    agent = session.get(Contact, row.contact_id) if row.contact_id else None
    phone = None
    if agent is not None and agent.phone and compliance.classify_number(session, agent.phone, b2b=True) != "dnc":
        phone = {"tel": agent.phone, "display": format_phone(agent.phone)}
    an = row.analysis_json or {}
    return {"id": row.id, "lead_id": row.lead_id, "status": row.status, "address": f.get("address"),
            "town": f.get("town"), "agent": row.from_name or (agent.name if agent else None), "from": row.from_email,
            "phone": phone, "received_at": row.received_at.isoformat() if row.received_at else None,
            "subject": row.subject, "text": (row.body_text or "")[:1500], "match": row.match_method,
            "light": light(row), "headline": row.headline, "note": row.note,
            "reply_subject": reply_subject(row.subject), "reply_text": an.get("reply_text") or "",
            "actions": an.get("actions") or [], "warnings": an.get("warnings") or [],
            "numbers": {k: (an.get("numbers") or {}).get(k) for k in ("counter", "buyer_max", "mao_walkaway",
                                                                     "fee_at_counter", "our_counter",
                                                                     "fee_at_our_counter", "opening_offer")},
            "price": row.price, "lead_url": f"#/lead/{row.lead_id}" if row.lead_id else None,
            # The plain Touch 2 offer fits this reply too ("Yes. Attorney", "available, what's your number?").
            "offer_ok": bool(lead and not row.counter_amount and set(row.intents_json or [])
                             <= {"AVAILABLE", "UNDER_CONTRACT", "QUESTION"}),
            "loi": bool((row.price and row.verdict in ("GREEN", "YELLOW", "RED")
                         and "UNDER_CONTRACT" not in (row.intents_json or []))
                        or ("AVAILABLE" in (row.intents_json or []) and not row.counter_amount
                            and row.verdict == "GREEN")),
            "send_after": row.send_after.isoformat() if row.send_after else None}


def status(session: Session, now: datetime | None = None) -> dict[str, Any]:
    from engine import autopilot
    today = datetime.combine(now_eastern(now).date(), datetime.min.time())
    smtp_today = select(func.count(Interaction.id)).where(Interaction.channel == "email_smtp",
                                                          Interaction.occurred_at >= today)
    emails = int(session.scalar(smtp_today) or 0)
    offers = int(session.scalar(smtp_today.where(Interaction.outcome == ao.SENT_OUTCOME)) or 0)
    replies = int(session.scalar(select(func.count(InboundReply.id)).where(
        InboundReply.received_at >= today, InboundReply.status.not_in(("auto_reply", "bounce")))) or 0)
    waiting = list(session.scalars(select(InboundReply).where(InboundReply.status == "needs_approval")
                                   .order_by(InboundReply.received_at.desc()).limit(25)))
    queued = list(session.scalars(select(InboundReply).where(InboundReply.status.in_(("auto_queued", "sending")))
                                  .order_by(InboundReply.send_after).limit(10)))
    handled = list(session.scalars(select(InboundReply).where(InboundReply.status.in_(HANDLED_STATUSES))
                                   .order_by(InboundReply.resolved_at.desc()).limit(8)))
    ok, why = configured()
    st = _state(session)
    nxt = poller.next_at
    return {"configured": ok, "why": "" if ok else why, "account": (credentials() or ("", ""))[0],
            "send_window_open": in_send_hours(now), "send_hours": send_hours_text(),
            "auto_send_touch2": bool(_cfg().get("auto_send_touch2", True)),
            "goal": autopilot.daily_goal(),
            "sent_24h": ao.sent_last_24h(session, now),
            "daily_email_limit": int((settings().get("outreach", {}) or {}).get("daily_email_limit", 25)),
            "today": {"emails_sent": emails, "replies_found": replies, "offers_delivered": offers},
            "poll": {"running": poller.running, "last": poller.last, "last_poll": st.get("last_poll"),
                     "next_at": nxt.isoformat(timespec="seconds") if nxt else None,
                     "minutes": poller.minutes(), "background": poller.thread is not None and poller.thread.is_alive()},
            "campaign": campaign.to_json(),
            "action_needed": [reply_json(session, r) for r in waiting],
            "queued": [reply_json(session, r) for r in queued],
            "handled": [reply_json(session, r) for r in handled]}


# -- swarm agent --------------------------------------------------------------------------------------------------
class GmailRepliesAgent(BaseAgent):
    """`python -m swarm.orchestrator run gmail_replies` (and the daemon, every 5 minutes): one inbox check."""
    name = "gmail_replies"
    description = "Gmail inbox (read-only): agent replies -> negotiator -> auto Touch 2 / Action Needed"

    def run(self, ctx: AgentContext) -> None:
        ok, why = configured()
        if not ok:
            ctx.note(f"Gmail autopilot skipped: {why}")
            return
        r = poll_gmail_replies()
        ctx.records_processed += int(r.get("checked") or 0)
        for k in ("matched", "auto_queued", "needs_approval", "opted_out", "bounce", "sent"):
            if r.get(k):
                ctx.bump(k, int(r[k]))
        if not r.get("ok"):
            ctx.error(str(r.get("error")))
