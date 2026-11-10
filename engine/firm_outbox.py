"""The firm's one way out: every stream's proposed e-mail, deduplicated, checked, allocated and sent (2026-11-09).

Priorities (`research_firm.caps`, rolling 24 hours, counted from what this outbox actually sent):
1. replies in a live B2B thread: no cap of their own (Gmail's safety ceiling `outreach.reply_daily_limit` covers
   everything the account sends, agent e-mails included);
2. deal-desk pitches (flippers, flipper-connected agents) and warm follow-ups: `research_firm.caps.p2` (25);
3. specialty B2B (attorneys, contractors, lenders, CPAs, net sheets): `research_firm.caps.p3` (10).
Cold pitches go out only on weekdays inside `research_firm.business_hours` (9 AM-5 PM Eastern), at most `research_firm.per_run` (6) per
desk run, 8-20 s apart; replies inside `outreach.send_hours`.

Every message carries the operator's signature, postal address and "no more" opt-out (CAN-SPAM 15 U.S.C. 7704);
a commercial pitch also says it is a commercial message. Nothing goes to an address that opted out, bounced, sits on
a listing agent's stop list or belongs to a target marked OPTED_OUT / LOST. A target gets at most one cold pitch per
`research_firm.pitch_gap_days` (6) and `research_firm.max_touches` (2) cold touches in all; a reply ends cold touches.
"""
from __future__ import annotations

import base64
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.config import settings
from db.models import Contact, ContactType, FirmOutbox, FirmReply, FirmTarget, utcnow
from engine.streams.base import PRIORITY_PITCH, PRIORITY_REPLY, PRIORITY_SPECIALTY, Outbound

log = logging.getLogger(__name__)

STAGES = ("NEW", "CONTACTED", "REPLIED", "INTERESTED", "TRIAL", "PAYING", "LOST", "OPTED_OUT")
CLOSED = ("LOST", "OPTED_OUT")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def cfg() -> dict[str, Any]:
    return dict(settings().get("research_firm") or {})


def _now(now: datetime | None) -> datetime:
    from engine.gmail_autopilot import now_eastern
    return now_eastern(now).replace(tzinfo=None)


# -- targets ------------------------------------------------------------------------------------------------------
def norm_email(email: str | None) -> str | None:
    e = (email or "").strip().lower()
    return e if _EMAIL.match(e) else None


def upsert_target(session: Session, *, kind: str, stream: str, email: str | None, org: str | None = None,
                  name: str | None = None, phone: str | None = None, website: str | None = None,
                  town: str | None = None, county: str | None = None, source: str | None = None,
                  score: float | None = None, data: dict[str, Any] | None = None) -> FirmTarget | None:
    """One row per e-mail address (a target without an e-mail is kept only when it has a website or phone)."""
    e = norm_email(email)
    row = session.scalar(select(FirmTarget).where(FirmTarget.email == e)) if e else None
    if row is None and not e:
        if not (website or phone) or not org:
            return None
        row = session.scalar(select(FirmTarget).where(FirmTarget.email.is_(None), FirmTarget.org == org,
                                                      FirmTarget.kind == kind))
    if row is None:
        row = FirmTarget(kind=kind, stream=stream, email=e, stage="NEW")
        session.add(row)
    for attr, val in (("org", org), ("name", name), ("phone", phone), ("website", website), ("town", town),
                      ("county", county), ("source", source)):
        if val:
            setattr(row, attr, str(val).strip()[:400])
    if score is not None:
        row.score = float(score)
    if data:
        row.data_json = {**(row.data_json or {}), **data}
    session.flush()
    return row


def stop_reason(session: Session, email: str, target: FirmTarget | None = None) -> str | None:
    """Why nothing may go to this address (None = it may)."""
    from engine import agent_outreach as ao
    e = norm_email(email)
    if not e:
        return "no valid e-mail"
    if e in ao.bounced_emails(session):
        return "bounced before"
    t = target or session.scalar(select(FirmTarget).where(FirmTarget.email == e))
    if t is not None and t.stage in CLOSED:
        return f"target {t.stage.lower()}"
    if session.scalar(select(Contact.id).where(func.lower(Contact.email) == e, Contact.dnc_status == "dnc").limit(1)):
        return "on a stop list"
    from engine.gmail_autopilot import credentials
    own = ((credentials() or ("", ""))[0] or "").lower()
    if own and e == own:
        return "own address"
    return None


# -- queueing -----------------------------------------------------------------------------------------------------
def _attachments(paths: list[Path]) -> list[dict[str, str]]:
    out = []
    for p in paths or []:
        p = Path(p)
        if p.exists() and p.stat().st_size <= 4_000_000:
            out.append({"name": p.name, "b64": base64.b64encode(p.read_bytes()).decode("ascii")})
    return out


SAMPLE_DEFAULT = ("Here's the 1-page sheet (attached): the first rows in full, the rest with the street hidden. The "
                  "full weekly version is a flat monthly research fee with no commission or per-deal pay; reply "
                  "\"start\" and I'll set it up.")


def enqueue(session: Session, ob: Outbound, now: datetime | None = None) -> FirmOutbox | None:
    """Store one proposed e-mail under its key. Returns None when the key was already used (never twice).

    The 2-step funnel (2026-11-10): a cold pitch / follow-up never carries an attachment. Its PDFs become the held
    sample (the masked `sample_path` version when one was written), released in the prospect's thread by
    `handle_reply` when they answer yes."""
    if session.scalar(select(FirmOutbox.id).where(FirmOutbox.key == ob.key)):
        return None
    if ob.kind in ("pitch", "followup") and ob.attachments:
        from engine.streams.base import sample_path
        if not ob.sample_attachments:
            ob.sample_attachments = [sample_path(a) if sample_path(a).exists() else Path(a) for a in ob.attachments]
        ob.attachments = []
    target = session.get(FirmTarget, ob.target_id) if ob.target_id else None
    why = stop_reason(session, ob.to, target)
    if why is None and ob.kind == "pitch" and target is not None:
        why = _pitch_block(session, target, now)
    row = FirmOutbox(key=ob.key[:200], stream=ob.stream, priority=int(ob.priority), kind=ob.kind,
                     target_id=ob.target_id, lead_id=ob.lead_id, to_email=(norm_email(ob.to) or ob.to)[:160],
                     subject=ob.subject[:250], body=ob.body, in_reply_to=ob.in_reply_to,
                     attachments_json=_attachments(ob.attachments) if why is None else [],
                     status="skipped" if why else "queued", reason=why)
    if not ob.commercial and row.kind in ("pitch", "followup"):
        row.kind = "notice"                       # a non-commercial note (no ad line), still logged and gated
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    if row.status == "queued" and ob.sample_attachments and ob.target_id:
        files = _attachments(ob.sample_attachments)
        if files and not session.scalar(select(FirmOutbox.id).where(FirmOutbox.key == f"{ob.key}:sample")):
            subj = ob.subject if ob.subject.lower().startswith("re:") else f"Re: {ob.subject}"
            session.add(FirmOutbox(key=f"{ob.key}:sample"[:200], stream=ob.stream, priority=PRIORITY_REPLY,
                                   kind="sample", target_id=ob.target_id, lead_id=ob.lead_id, to_email=row.to_email,
                                   subject=subj[:250], body=(ob.sample_body or SAMPLE_DEFAULT).strip(),
                                   attachments_json=files, status="held", reason="waits for a yes"))
            session.flush()
    return row


def release_sample(session: Session, target: FirmTarget, reply_message_id: str | None,
                   reply_subject: str | None = None) -> FirmOutbox | None:
    """A prospect said yes: their newest held sample goes out next in their own thread (priority 1)."""
    row = session.scalar(select(FirmOutbox).where(FirmOutbox.target_id == target.id, FirmOutbox.kind == "sample",
                                                  FirmOutbox.status == "held").order_by(FirmOutbox.id.desc()).limit(1))
    if row is None:
        return None
    row.status, row.reason = "queued", "they said yes"
    if reply_message_id:
        row.in_reply_to = reply_message_id
    if reply_subject:
        subj = re.sub(r"(?i)^\s*(re|fwd?):\s*", "", reply_subject).strip()
        row.subject = f"Re: {subj}"[:250]
    for older in session.scalars(select(FirmOutbox).where(FirmOutbox.target_id == target.id, FirmOutbox.kind == "sample",
                                                          FirmOutbox.status == "held", FirmOutbox.id != row.id)):
        older.status, older.reason, older.attachments_json = "skipped", "a newer sample went", []
    session.flush()
    return row


def sample_sent(session: Session, target: FirmTarget) -> bool:
    return bool(session.scalar(select(FirmOutbox.id).where(FirmOutbox.target_id == target.id,
                                                           FirmOutbox.kind == "sample",
                                                           FirmOutbox.status.in_(("queued", "sent"))).limit(1)))


def _pitch_block(session: Session, target: FirmTarget, now: datetime | None) -> str | None:
    c = cfg()
    if target.stage not in ("NEW", "CONTACTED"):
        return f"target {target.stage.lower()}: no more cold pitches"
    if int(target.touches or 0) >= int(c.get("max_touches", 2)):
        return "cold touches used up"
    if target.last_contact_at and target.last_contact_at > _now(now) - timedelta(days=float(c.get("pitch_gap_days", 6))):
        return "pitched recently"
    queued = session.scalar(select(func.count(FirmOutbox.id)).where(FirmOutbox.target_id == target.id,
                                                                   FirmOutbox.status == "queued"))
    if queued:
        return "a pitch is already queued"
    return None


# -- sending ------------------------------------------------------------------------------------------------------
def sent_24h(session: Session, now: datetime | None = None, priority: int | None = None) -> int:
    since = utcnow() - timedelta(hours=24)
    q = select(func.count(FirmOutbox.id)).where(FirmOutbox.status == "sent", FirmOutbox.sent_at >= since)
    if priority is not None:
        q = q.where(FirmOutbox.priority == priority)
    return int(session.scalar(q) or 0)


def caps() -> dict[int, int]:
    c = dict(cfg().get("caps") or {})
    return {PRIORITY_REPLY: int(c.get("p1", 150)), PRIORITY_PITCH: int(c.get("p2", 25)),
            PRIORITY_SPECIALTY: int(c.get("p3", 10))}


def in_business_hours(now: datetime | None = None) -> bool:
    t = _now(now)
    a, b = (int(x) for x in cfg().get("business_hours", [8, 20]))
    return t.weekday() < 5 and a <= t.hour < b


def footer(op: dict[str, str], commercial: bool) -> str:
    from engine import human_copy as hc
    sig = hc.signature_lines(op)
    lines = ["", *sig, ""]
    if commercial:
        # CAN-SPAM 7704(a)(5)(A)(i): a cold commercial message identifies itself as a solicitation; the law leaves the
        # wording open, so it is one plain footer line (never "Ad:" in the subject)
        who = max((op.get("name") or "", op.get("company") or ""), key=len) or "the sender"
        lines.append(str(cfg().get("ad_line") or "Business offer from {who}.").format(who=who))
    if op.get("postal"):
        lines.append(f"Mail: {op['postal']}")
    lines.append('Reply "no more" and I won\'t e-mail you again.')
    return "\n".join(lines)


def compose(row: FirmOutbox, op: dict[str, str], message_id: str, user: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = user, row.to_email, row.subject, message_id
    if row.in_reply_to:
        msg["In-Reply-To"] = row.in_reply_to
        msg["References"] = row.in_reply_to
    if row.kind in ("pitch", "followup", "sample"):
        # one-click opt-out in the mail client (Gmail's sender rules; CAN-SPAM opt-out); an "unsubscribe" reply is
        # read as an opt-out by classify()
        msg["List-Unsubscribe"] = f"<mailto:{user}?subject=unsubscribe>"
    body = row.body.rstrip()
    first = (op.get("first") or "").strip()
    if first and body.splitlines() and body.splitlines()[-1].strip().lower() == first.lower():
        body = "\n".join(body.splitlines()[:-1]).rstrip()          # the signature carries the name
    lines = body.splitlines()
    if not (lines and re.fullmatch(r"(?i)(thanks|thank you|best|regards|best regards|sincerely|cheers)[,.!]?",
                                   lines[-1].strip())):
        body += "\n\nThanks,"                         # the house sign-off above the signature, as in agent e-mails
    msg.set_content(body + footer(op, row.kind in ("pitch", "followup")))
    for a in row.attachments_json or []:
        try:
            msg.add_attachment(base64.b64decode(a["b64"]), maintype="application",
                               subtype="pdf" if a["name"].lower().endswith(".pdf") else "octet-stream",
                               filename=a["name"])
        except (KeyError, ValueError):
            continue
    return msg


def _ready() -> tuple[bool, str]:
    from engine import agent_outreach as ao
    from engine import human_copy as hc
    ok, why = ao.smtp_ready()
    if not ok:
        return False, why
    op = hc.operator()
    if not (op.get("name") and op.get("postal")):
        return False, "operator name / postal address missing (CAN-SPAM)"
    return True, ""


def send_due(session_factory: Any = None, *, now: datetime | None = None, smtp_factory: Any = None,
             limit: int | None = None, pace: bool = True) -> dict[str, Any]:
    """Send what the caps allow, best priority first. Returns counts (and the error name that stopped it, if any)."""
    from core.mail import clean_app_password
    from db.database import session_scope
    from engine import agent_outreach as ao
    from engine import human_copy as hc
    from engine.gmail_autopilot import in_send_hours
    ok, why = _ready()
    out: dict[str, Any] = {"sent": 0}
    if not ok:
        out["blocked"] = why
        return out
    scope = session_factory or session_scope
    per_run = int(limit if limit is not None else cfg().get("per_run", 6))
    cap = caps()
    biz, replies_ok = in_business_hours(now), in_send_hours(now)
    user = os.environ["SMTP_USER"].strip()
    password = clean_app_password(os.environ["SMTP_APP_PASSWORD"])
    op = hc.operator()
    gap = [float(x) for x in cfg().get("pace_seconds", [8, 20])]
    cold_gap = timedelta(minutes=float(cfg().get("cold_gap_minutes", 12)))
    with scope() as s:
        expire_samples(s, now)
    while out["sent"] < per_run:
        with scope() as s:
            used = {p: sent_24h(s, now, p) for p in cap}
            total = sent_24h(s, now) + ao.sent_last_24h(s, now)
            if total >= int((settings().get("outreach") or {}).get("reply_daily_limit", 150)):
                out["blocked"] = "gmail ceiling"
                break
            # cold first touches are spread out (one every `cold_gap_minutes`, 2026-11-10: "spaced naturally"), so a
            # run sends at most one; replies and released samples are never held back by the gap
            last_cold = s.scalar(select(func.max(FirmOutbox.sent_at)).where(
                FirmOutbox.status == "sent", FirmOutbox.priority > PRIORITY_REPLY))
            cold_ok = biz and not (last_cold and utcnow() - last_cold < cold_gap)
            allowed = [p for p in sorted(cap) if used[p] < cap[p] and (cold_ok if p > PRIORITY_REPLY else replies_ok)]
            if not allowed:
                break
            row = s.scalar(select(FirmOutbox).where(FirmOutbox.status == "queued", FirmOutbox.priority.in_(allowed))
                           .order_by(FirmOutbox.priority, FirmOutbox.created_at).limit(1))
            if row is None:
                break
            target = s.get(FirmTarget, row.target_id) if row.target_id else None
            why = stop_reason(s, row.to_email, target)
            if why is None and row.kind == "pitch" and target is not None and target.stage not in ("NEW", "CONTACTED"):
                why = f"target {target.stage.lower()}"
            if why:
                row.status, row.reason, row.attachments_json = "skipped", why, []
                continue
            mid = ao.new_message_id()
            msg = compose(row, op, mid, user)
            rid = row.id
        try:
            with (smtp_factory or ao.smtp_connection)() as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        except Exception as exc:                        # noqa: BLE001 - recorded by name, the batch stops
            with scope() as s:
                r = s.get(FirmOutbox, rid)
                r.attempts = int(r.attempts or 0) + 1
                r.reason = type(exc).__name__
                if r.attempts >= int(cfg().get("max_attempts", 3)):
                    r.status, r.attachments_json = "failed", []
            out["error"] = type(exc).__name__
            break                                       # one failure stops this run's batch; the next run retries
        with scope() as s:
            r = s.get(FirmOutbox, rid)
            r.status, r.message_id, r.sent_at, r.attachments_json = "sent", mid, utcnow(), []
            t = s.get(FirmTarget, r.target_id) if r.target_id else None
            if t is not None:
                t.last_contact_at = _now(now)
                if r.kind in ("pitch", "followup"):
                    t.touches = int(t.touches or 0) + 1
                    if t.stage == "NEW":
                        t.stage = "CONTACTED"
        out["sent"] += 1
        if pace and out["sent"] < per_run:
            time.sleep(random.uniform(*gap))
    return out


def expire_samples(session: Session, now: datetime | None = None) -> int:
    """Held samples nobody asked for within `sample_hold_days` (30) are dropped (the data is stale by then)."""
    cutoff = utcnow() - timedelta(days=float(cfg().get("sample_hold_days", 30)))
    n = 0
    for row in session.scalars(select(FirmOutbox).where(FirmOutbox.status == "held", FirmOutbox.created_at < cutoff)):
        row.status, row.reason, row.attachments_json = "expired", "no yes within the hold", []
        n += 1
    return n


# -- replies from targets -----------------------------------------------------------------------------------------
_OPT_OUT = re.compile(r"\b(no more|unsubscribe|remove me|take me off|stop (e-?mailing|sending|contacting)|"
                      r"do not (e-?mail|contact)|don't (e-?mail|contact) (me|us))\b", re.I)
_NOT = re.compile(r"\b(not interested|no thanks|no thank you|we'?re (all )?set|not at this time|not right now|"
                  r"pass on this|we('ll| will) pass|not for us|no need)\b", re.I)
_YES = re.compile(r"(^\s*(yes|yep|yeah|sure|absolutely|definitely|ok|okay|please)\b|"
                  r"\b(i'?m|we'?re|we are|i am) interested\b|\bplease send\b|\bsend (it |that |them |this )?over\b|"
                  r"\bgo ahead\b|\bwould (love|like) (to see|it|that|a copy)\b|\bplease do\b|\bshare it\b|"
                  r"\bi'?d (love|like) (to see|it|that|a copy)\b|\bsure thing\b|"
                  r"\binterested\b(?![^.?!]*\bnot\b)|\bsign (me|us) up\b|\bactivate\b|\bstart (the |a )?trial\b|"
                  r"\blet'?s (do it|try it|talk|set up)\b|\bsend (me |us )?(more|the next|it)\b|\btell me more\b|"
                  r"\bhow do (we|i) (start|sign up|get started)\b|\bsounds (good|great|interesting)\b)", re.I)


def classify(text: str) -> str:
    t = (text or "").strip()
    if _OPT_OUT.search(t):
        return "opt_out"
    if _NOT.search(t):
        return "not_interested"
    if _YES.search(t):
        return "interested"
    return "question"


def known(session: Session) -> tuple[dict[str, int], dict[str, int]]:
    """(our sent Message-ID -> target, target e-mail -> target) for matching replies."""
    thread = {mid: tid for mid, tid in session.execute(
        select(FirmOutbox.message_id, FirmOutbox.target_id).where(FirmOutbox.message_id.is_not(None),
                                                                  FirmOutbox.target_id.is_not(None)))}
    emails = {e: tid for e, tid in session.execute(
        select(FirmTarget.email, FirmTarget.id).where(FirmTarget.email.is_not(None),
                                                      FirmTarget.touches > 0))}
    return thread, emails


def match(thread: dict[str, int], emails: dict[str, int], mail: Any) -> int | None:
    for mid in [getattr(mail, "in_reply_to", None), *reversed(getattr(mail, "references", []) or [])]:
        if mid and mid in thread:
            return thread[mid]
    return emails.get((getattr(mail, "from_email", "") or "").lower())


def handle_reply(session: Session, mail: Any, target_id: int, now: datetime | None = None) -> FirmReply | None:
    """Store a target's reply, set its stage, and stop cold touches. An opt-out closes the target at once."""
    if session.scalar(select(FirmReply.id).where(FirmReply.message_id == mail.message_id)):
        return None
    t = session.get(FirmTarget, target_id)
    text = (getattr(mail, "text", "") or "")[:4000]
    intent = "auto" if getattr(mail, "auto_reply", False) else classify(text)
    row = FirmReply(message_id=mail.message_id, target_id=target_id, stream=t.stream if t else None,
                    from_email=getattr(mail, "from_email", None), subject=(getattr(mail, "subject", "") or "")[:250],
                    received_at=getattr(mail, "received", None), body_text=text, intent=intent,
                    references_json=[*(getattr(mail, "references", []) or []), mail.message_id],
                    status="closed" if intent in ("opt_out", "not_interested", "auto") else "new")
    session.add(row)
    if t is not None and intent != "auto":
        first_yes = intent == "interested" and not sample_sent(session, t)
        t.stage = {"opt_out": "OPTED_OUT", "not_interested": "LOST", "interested": "INTERESTED"}.get(intent, "REPLIED")
        t.next_touch_at = None
        for q in session.scalars(select(FirmOutbox).where(FirmOutbox.target_id == t.id, FirmOutbox.status == "queued",
                                                          FirmOutbox.kind.in_(("pitch", "followup")))):
            q.status, q.reason, q.attachments_json = "skipped", f"target replied ({intent})", []
        if intent in ("opt_out", "not_interested"):
            for q in session.scalars(select(FirmOutbox).where(FirmOutbox.target_id == t.id,
                                                              FirmOutbox.status == "held")):
                q.status, q.reason, q.attachments_json = "skipped", f"target replied ({intent})", []
        elif first_yes and release_sample(session, t, mail.message_id, getattr(mail, "subject", None)) is not None:
            # Touch 2: "yes, send it" gets the sample in their thread by itself; the next yes (to the price) is the
            # warm action for the operator
            row.status = "sample_sent"
    session.flush()
    return row


def answer_reply(session: Session, reply_id: int, text: str, *, stage: str | None = None,
                 now: datetime | None = None) -> FirmOutbox | None:
    """A sealed note's {"firm_reply": id, "reply": "..."}: the answer goes in the target's own thread (priority 1)."""
    r = session.get(FirmReply, reply_id)
    if r is None:
        raise ValueError("no such firm reply")
    t = session.get(FirmTarget, r.target_id) if r.target_id else None
    if t is None or not r.from_email:
        raise ValueError("reply has no target")
    if t.stage == "OPTED_OUT":
        raise ValueError("target opted out")
    subj = r.subject or "Re:"
    ob = Outbound(stream=t.stream, key=f"reply:{r.id}:{len(text)}", to=r.from_email, priority=PRIORITY_REPLY,
                  kind="reply", subject=subj if subj.lower().startswith("re:") else f"Re: {subj}", body=text.strip(),
                  target_id=t.id, in_reply_to=r.message_id, commercial=False)
    row = enqueue(session, ob, now)
    r.status = "answered"
    if stage:
        set_stage(session, t, stage)
    return row


def set_stage(session: Session, target: FirmTarget, stage: str) -> None:
    st = stage.strip().upper()
    if st not in STAGES:
        raise ValueError("unknown stage")
    target.stage = st
    session.flush()


def warm(session: Session, limit: int = 5) -> list[FirmReply]:
    """Replies where someone said yes to a trial / more information, not yet answered."""
    return list(session.scalars(select(FirmReply).where(FirmReply.intent == "interested", FirmReply.status == "new")
                                .order_by(FirmReply.created_at).limit(limit)))
