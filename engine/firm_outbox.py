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

from sqlalchemy import case, func, or_, select
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
    if why is None and ob.kind == "followup" and target is not None and ob.commercial and \
            not ob.key.startswith("nudge:"):
        # one sequence per target: the outbox's own in-thread nudge and close-the-loop note (queue_nudges) are the
        # only follow-ups, whatever a stream proposes
        why = "the in-thread nudges follow up (one sequence per target)"
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
                   reply_subject: str | None = None, stream: str | None = None) -> FirmOutbox | None:
    """A prospect said yes: their newest held sample (of the stream they answered, when known) goes out next in their
    own thread (priority 1). A yes to an s1 buy-box note releases nothing: there is no sample in that conversation."""
    q = select(FirmOutbox).where(FirmOutbox.target_id == target.id, FirmOutbox.kind == "sample",
                                 FirmOutbox.status == "held")
    if stream:
        q = q.where(FirmOutbox.stream == stream)
    row = session.scalar(q.order_by(FirmOutbox.id.desc()).limit(1))
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


def sample_sent(session: Session, target: FirmTarget, stream: str | None = None) -> bool:
    q = select(FirmOutbox.id).where(FirmOutbox.target_id == target.id, FirmOutbox.kind == "sample",
                                    FirmOutbox.status.in_(("queued", "sent")))
    if stream:
        q = q.where(FirmOutbox.stream == stream)
    return bool(session.scalar(q.limit(1)))


def pitch_ok(target: FirmTarget, now: datetime | None = None) -> bool:
    """Streams pick their Touch-1 targets with this (the same rule `enqueue` enforces): never pitched, or a finished
    sequence that has rested `rest_days`."""
    return _sequence_block(target, now) is None


def _sequence_block(target: FirmTarget, now: datetime | None) -> str | None:
    c = cfg()
    if target.stage not in ("NEW", "CONTACTED"):
        return f"target {target.stage.lower()}: no more cold pitches"
    touches = int(target.touches or 0)
    if touches:
        rest = float(c.get("rest_days", 0) or 0)
        done = touches >= int(c.get("max_touches", 3))
        if not (done and rest and target.last_contact_at and target.last_contact_at <= _now(now) - timedelta(days=rest)):
            return "in a sequence: the in-thread nudges follow up" if not done else "sequence finished: resting"
    if target.last_contact_at and target.last_contact_at > _now(now) - timedelta(days=float(c.get("pitch_gap_days", 6))):
        return "pitched recently"
    return None


def _pitch_block(session: Session, target: FirmTarget, now: datetime | None) -> str | None:
    """A new Touch 1 only for a target that hasn't been pitched this round. The round is Touch 1 + the in-thread nudge
    (day 3-4) + close-the-loop (day 9-10) (`queue_nudges`, the B2B playbook 2026-11-10); after that the target rests
    `rest_days` (0 = never pitched again) before a new round with fresh data."""
    why = _sequence_block(target, now)
    if why:
        return why
    queued = session.scalar(select(func.count(FirmOutbox.id)).where(FirmOutbox.target_id == target.id,
                                                                   FirmOutbox.status == "queued"))
    if queued:
        return "a pitch is already queued"
    return None


# Cold kinds carry the solicitation line and List-Unsubscribe. A "buyer_note" (s1: the buy-box question to cash buyers
# while our offers are live) is its own conversation: it counts against the first-touch caps but never starts or uses
# up a target's data-product sequence (`touches`).
FIRST_TOUCH_KINDS = ("pitch", "buyer_note")
COLD_KINDS = ("pitch", "followup", "buyer_note")


# -- the in-thread sequence ---------------------------------------------------------------------------------------
# What each stream's sample is called in a nudge ("want me to send ...?") and in the close-the-loop line ("if ... isn't
# useful"). Counts and facts stay in Touch 1; the nudges add nothing new to check.
SEQUENCE_WORDS = {
    "s2": ("this week's deal sheet for your towns", "a weekly deal sheet"),
    "s3": ("this week's county sheet", "the sheriff-sale case file"),
    "s4": ("this week's county list for your trade", "the weekly listing sheet"),
    "s5": ("the 1-page list", "the investor-company list"),
    "s6": ("the comparable-sales packet", "the Chapter 123 packet"),
    "s7": ("the net sheet", "the net sheet"),
    "s9": ("this week's county list", "the purchases list"),
    "s10": ("this week's docket sheet for your town", "the docket sheet"),
    "s11": ("this week's county bid sheet", "the weekly bid sheet"),
    "s12": ("this month's town report", "the monthly town report"),
    "s13": ("this week's teardown sheet", "the teardown sheet"),
}
PROFESSIONAL_KINDS = {"property_tax_appeal_attorney", "foreclosure_surplus_attorney", "real_estate_cpa",
                      "cost_segregation_firm", "qualified_intermediary_1031", "estate_attorney", "probate_attorney"}


def _greeting(target: FirmTarget) -> str:
    from engine.streams.s2_deal_desk_saas import first_name
    return first_name(target.name) if target.name else "there"


def nudge_body(target: FirmTarget, stream: str, step: int) -> str:
    noun, topic = SEQUENCE_WORDS.get(stream, ("the one-page sheet", "the sheet"))
    hi = f"Hi {_greeting(target)},"
    if step == 2:
        return f"{hi}\n\nQuick follow-up: want me to send {noun}? It's one page and free."
    return f"{hi}\n\nLast note from me. If {topic} isn't useful, no problem, and I won't follow up."


def queue_nudges(session: Session, now: datetime | None = None, limit: int = 40) -> int:
    """The rest of each cold sequence, in the prospect's own thread: a nudge `nudge_days` (3) days after Touch 1 and a
    close-the-loop note `close_days` (6) days after the nudge (day 9-10), each plain text with no attachment. A reply,
    an opt-out, a bounce or a stage change ends the sequence (stage is no longer CONTACTED). Weekdays only."""
    c = cfg()
    t = _now(now)
    if t.weekday() >= 5:
        return 0
    max_touches = int(c.get("max_touches", 3))
    if max_touches < 2:
        return 0
    waits = {2: float(c.get("nudge_days", 3)), 3: float(c.get("close_days", 6))}
    n = 0
    targets = session.scalars(select(FirmTarget).where(FirmTarget.stage == "CONTACTED",
                                                       FirmTarget.touches.between(1, max_touches - 1))
                              .order_by(FirmTarget.last_contact_at))
    for target in targets:
        if n >= limit:
            break
        step = int(target.touches or 0) + 1
        if not target.last_contact_at or target.last_contact_at > t - timedelta(days=waits.get(step, 6)):
            continue
        pitch = session.scalar(select(FirmOutbox).where(FirmOutbox.target_id == target.id, FirmOutbox.kind == "pitch",
                                                        FirmOutbox.status == "sent").order_by(FirmOutbox.sent_at.desc())
                               .limit(1))
        if pitch is None or not pitch.message_id:
            continue
        key = f"nudge:{pitch.id}:{step}"
        if session.scalar(select(FirmOutbox.id).where(FirmOutbox.key == key)):
            continue
        subj = re.sub(r"(?i)^\s*(re|fwd?):\s*", "", pitch.subject).strip()
        ob = Outbound(stream=pitch.stream, key=key, to=pitch.to_email, subject=f"Re: {subj}",
                      body=nudge_body(target, pitch.stream, step), priority=pitch.priority, kind="followup",
                      target_id=target.id, in_reply_to=pitch.message_id)
        if enqueue(session, ob, now) is not None:
            n += 1
    return n


# -- sending ------------------------------------------------------------------------------------------------------
def sent_24h(session: Session, now: datetime | None = None, priority: int | None = None) -> int:
    since = utcnow() - timedelta(hours=24)
    q = select(func.count(FirmOutbox.id)).where(FirmOutbox.status == "sent", FirmOutbox.sent_at >= since)
    if priority is not None:
        q = q.where(FirmOutbox.priority == priority)
    return int(session.scalar(q) or 0)


# Wording fixes applied to e-mails already waiting in the outbox (a stream's copy changed after it queued them):
# (pattern, replacement). The outbox never sends the old sentence.
COPY_FIXES: list[tuple[str, str]] = [
    (r"and expect to have one under contract soon", "and may have one under contract soon"),
]


def rewrite_queued(session: Session) -> int:
    """Apply COPY_FIXES to every queued / held message body. Returns the number of rows changed."""
    n = 0
    for row in session.scalars(select(FirmOutbox).where(FirmOutbox.status.in_(("queued", "held")))):
        body = row.body or ""
        for pat, rep in COPY_FIXES:
            body = re.sub(pat, rep, body)
        if body != row.body:
            row.body = body
            n += 1
    if n:
        session.flush()
    return n


def pitches_24h(session: Session, priority: int) -> int:
    """First touches sent in the last 24 hours at that priority (nudges have their own `caps.nudges`)."""
    return int(session.scalar(select(func.count(FirmOutbox.id)).where(
        FirmOutbox.status == "sent", FirmOutbox.sent_at >= utcnow() - timedelta(hours=24),
        FirmOutbox.priority == priority, FirmOutbox.kind.in_(FIRST_TOUCH_KINDS))) or 0)


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
        lines.append(str(cfg().get("ad_line") or "A business solicitation from {who}.").format(who=who))
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
    if row.kind in ("pitch", "followup", "sample", "buyer_note"):
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
    msg.set_content(body + footer(op, row.kind in COLD_KINDS))
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
    g = cfg().get("cold_gap_minutes", [8, 15])
    lo_gap, hi_gap = (float(g[0]), float(g[-1])) if isinstance(g, (list, tuple)) and g else (float(g or 12),) * 2
    cold_gap = timedelta(minutes=random.uniform(lo_gap, hi_gap))   # one draw per run: spacing varies 8-15 minutes
    nudge_cap = int(dict(cfg().get("caps") or {}).get("nudges", 20))
    t_now = _now(now)
    pro_ok = t_now.hour < int(cfg().get("professional_last_hour", 18)) and not (t_now.weekday() == 4 and t_now.hour >= 12)
    with scope() as s:
        expire_samples(s, now)
        from engine import cold_guard
        held = cold_guard.paused(s, now)
        # The weekly ramp (`ramp`): first touches a day this week, split between deal-desk and specialty pitches in
        # the same proportion as their caps.
        ceiling = cap[PRIORITY_PITCH] + cap[PRIORITY_SPECIALTY]
        allowed_total = cold_guard.first_touch_cap(s, ceiling, t_now.date())
        if allowed_total < ceiling and ceiling:
            p2 = round(cap[PRIORITY_PITCH] * allowed_total / ceiling)
            cap = {**cap, PRIORITY_PITCH: p2, PRIORITY_SPECIALTY: allowed_total - p2}
    if held:
        out["cold_paused"] = held
    while out["sent"] < per_run:
        with scope() as s:
            used = {p: pitches_24h(s, p) for p in (PRIORITY_PITCH, PRIORITY_SPECIALTY)}
            used[PRIORITY_REPLY] = sent_24h(s, now, PRIORITY_REPLY)
            nudges_used = int(s.scalar(select(func.count(FirmOutbox.id)).where(
                FirmOutbox.status == "sent", FirmOutbox.sent_at >= utcnow() - timedelta(hours=24),
                FirmOutbox.kind.in_(("followup", "notice")))) or 0)
            total = sent_24h(s, now) + ao.sent_last_24h(s, now)
            if total >= int((settings().get("outreach") or {}).get("reply_daily_limit", 150)):
                out["blocked"] = "gmail ceiling"
                break
            # cold touches are spread out (one every 8-15 minutes, 2026-11-10: "spaced naturally"), so a run sends at
            # most one; replies and released samples are never held back by the gap or by a pause
            last_cold = s.scalar(select(func.max(FirmOutbox.sent_at)).where(
                FirmOutbox.status == "sent", FirmOutbox.priority > PRIORITY_REPLY))
            cold_ok = biz and not held and not (last_cold and utcnow() - last_cold < cold_gap)
            conds = []
            if replies_ok and used[PRIORITY_REPLY] < cap[PRIORITY_REPLY]:
                conds.append(FirmOutbox.priority == PRIORITY_REPLY)
            if cold_ok:
                pitch_ps = [p for p in (PRIORITY_PITCH, PRIORITY_SPECIALTY) if used[p] < cap[p]]
                if pitch_ps:
                    conds.append(FirmOutbox.kind.in_(FIRST_TOUCH_KINDS) & FirmOutbox.priority.in_(pitch_ps))
                if nudges_used < nudge_cap:
                    conds.append(FirmOutbox.kind.in_(("followup", "notice")) & (FirmOutbox.priority > PRIORITY_REPLY))
            if not conds:
                break
            q = select(FirmOutbox).where(FirmOutbox.status == "queued", or_(*conds))
            if not pro_ok:
                # no attorney / CPA cold mail after 6 PM or on a Friday afternoon (the playbook's timing rule)
                pro_ids = select(FirmTarget.id).where(FirmTarget.kind.in_(PROFESSIONAL_KINDS))
                q = q.where(or_(FirmOutbox.priority == PRIORITY_REPLY, FirmOutbox.target_id.is_(None),
                                FirmOutbox.target_id.not_in(pro_ids)))
            # replies first, then nudges (they finish a sequence already started), then first touches
            row = s.scalar(q.order_by(FirmOutbox.priority, case((FirmOutbox.kind.in_(FIRST_TOUCH_KINDS), 1), else_=0),
                                      FirmOutbox.created_at).limit(1))
            if row is None:
                break
            target = s.get(FirmTarget, row.target_id) if row.target_id else None
            why = stop_reason(s, row.to_email, target)
            if why is None and row.kind in COLD_KINDS and target is not None and \
                    target.stage not in ("NEW", "CONTACTED"):
                why = f"target {target.stage.lower()}"
            if why is None and cfg().get("check_recipient_domain", True):
                from engine.cold_guard import dead_domain
                dead = dead_domain(row.to_email)     # MX (or A) checked before every send: a dead domain can only bounce
                if dead:
                    ao.record_bounce(s, row.to_email, sent=False)
                    why = dead
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
                from engine import cold_guard
                code = cold_guard.note_error(s, exc, now)   # Gmail's spam / rate codes stop cold mail for 7 days
            out["error"] = type(exc).__name__ + (f" {code}" if code else "")
            break                                       # one failure stops this run's batch; the next run retries
        with scope() as s:
            r = s.get(FirmOutbox, rid)
            r.status, r.message_id, r.sent_at, r.attachments_json = "sent", mid, utcnow(), []
            t = s.get(FirmTarget, r.target_id) if r.target_id else None
            if t is not None:
                t.last_contact_at = _now(now)
                if r.kind == "pitch":
                    t.touches = 1                       # Touch 1 of a (new) round
                elif r.kind == "followup":
                    t.touches = int(t.touches or 0) + 1
                if r.kind in COLD_KINDS and t.stage == "NEW":
                    t.stage = "CONTACTED"            # a buyer note (s1) leaves `touches` alone: no data sequence
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


_START = re.compile(r"(^\s*(start|let'?s start|start it|sign (me|us) up|i'?m in|we'?re in|count (me|us) in)\b|"
                    r"\bset (it|me|us|that) up\b|"
                    r"\bstart (the |a |my |our )?(trial|subscription)\b|\bhow (do|can) (i|we) pay\b|"
                    r"\bsend (me |us )?(the |an )?(invoice|payment link|link to pay)\b|\bwe'?ll take it\b)", re.I)


def classify(text: str) -> str:
    t = (text or "").strip()
    if _OPT_OUT.search(t):
        return "opt_out"
    if _NOT.search(t):
        return "not_interested"
    if _START.search(t):
        return "start"                            # they want to subscribe: payment step (warm action)
    if _YES.search(t):
        return "interested"
    return "question"


def payment_link(stream: str | None) -> str:
    """The operator's payment link for a stream's product (research_firm.payment_links.<stream>, else
    research_firm.payment_link); empty until the operator creates one (Stripe Payment Link / PayPal / Square)."""
    c = cfg()
    links = dict(c.get("payment_links") or {})
    return str(links.get(stream or "") or c.get("payment_link") or "").strip()


def price_line(stream: str | None) -> str:
    from engine.swarm_firm import stream_cfg
    sc = stream_cfg(stream or "") if stream else {}
    trial, monthly = sc.get("price_trial"), sc.get("price_monthly")
    tax = " plus NJ sales tax" if cfg().get("sales_tax_line", True) else ""
    if sc.get("price_text"):
        return f"{sc['price_text']}{tax}"           # a tiered product (s2: one county / all eleven / the full desk)
    if trial and monthly:
        return f"${float(trial):,.0f} for the first 30 days, then ${float(monthly):,.0f} a month{tax}"
    if monthly:
        return f"${float(monthly):,.0f} a month{tax}"
    return f"the flat monthly fee{tax}"


def start_reply_text(stream: str | None, link: str) -> str:
    return (f"Great, glad it's useful. Here's the link to start ({price_line(stream)}, flat, cancel any time by "
            f"replying \"stop\"): {link}\n\nThe first full report goes out as soon as it's through, then every week. "
            "It's research data only, with no commission or anything tied to a deal.")


def known(session: Session) -> tuple[dict[str, int], dict[str, int]]:
    """(our sent Message-ID -> target, target e-mail -> target) for matching replies."""
    thread = {mid: tid for mid, tid in session.execute(
        select(FirmOutbox.message_id, FirmOutbox.target_id).where(FirmOutbox.message_id.is_not(None),
                                                                  FirmOutbox.target_id.is_not(None)))}
    emails = {e: tid for e, tid in session.execute(
        select(FirmTarget.email, FirmTarget.id).where(FirmTarget.email.is_not(None),
                                                      or_(FirmTarget.touches > 0, FirmTarget.stage != "NEW")))}
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
    # The stream of the message they answered (an s1 buy-box note to an s2 target counts under s1), else the target's
    ids = [x for x in [getattr(mail, "in_reply_to", None), *(getattr(mail, "references", []) or [])] if x]
    thread_stream = session.scalar(select(FirmOutbox.stream).where(FirmOutbox.message_id.in_(ids),
                                                                   FirmOutbox.target_id == target_id)
                                   .order_by(FirmOutbox.id.desc()).limit(1)) if ids else None
    stream = thread_stream or (t.stream if t else None)
    row = FirmReply(message_id=mail.message_id, target_id=target_id, stream=stream,
                    from_email=getattr(mail, "from_email", None), subject=(getattr(mail, "subject", "") or "")[:250],
                    received_at=getattr(mail, "received", None), body_text=text, intent=intent,
                    references_json=[*(getattr(mail, "references", []) or []), mail.message_id],
                    status="closed" if intent in ("opt_out", "not_interested", "auto") else "new")
    session.add(row)
    if t is not None and intent != "auto":
        first_yes = intent == "interested" and not sample_sent(session, t, stream)
        t.stage = {"opt_out": "OPTED_OUT", "not_interested": "LOST", "interested": "INTERESTED"}.get(intent, "REPLIED")
        t.next_touch_at = None
        for q in session.scalars(select(FirmOutbox).where(FirmOutbox.target_id == t.id, FirmOutbox.status == "queued",
                                                          FirmOutbox.kind.in_(COLD_KINDS))):
            q.status, q.reason, q.attachments_json = "skipped", f"target replied ({intent})", []
        if intent in ("opt_out", "not_interested"):
            for q in session.scalars(select(FirmOutbox).where(FirmOutbox.target_id == t.id,
                                                              FirmOutbox.status == "held")):
                q.status, q.reason, q.attachments_json = "skipped", f"target replied ({intent})", []
        elif first_yes and release_sample(session, t, mail.message_id, getattr(mail, "subject", None),
                                          stream=stream) is not None:
            # Touch 2: "yes, send it" gets the sample in their thread by itself; the next yes (to the price) is the
            # warm action for the operator
            row.status = "sample_sent"
        elif intent == "start":
            t.stage = "INTERESTED"
            t.data_json = {**(t.data_json or {}), "start_requested": _now(now).isoformat(timespec="minutes")}
            link = payment_link(t.stream)
            if link and getattr(mail, "from_email", None):
                # the payment step goes out by itself in their thread; the reply stays a warm action until the
                # operator confirms the payment (a sealed note sets the target TRIAL / PAYING, which starts delivery)
                subj = re.sub(r"(?i)^\s*(re|fwd?):\s*", "", getattr(mail, "subject", "") or "").strip()
                enqueue(session, Outbound(stream=t.stream or "s0", key=f"start:{mail.message_id}"[:200],
                                          to=mail.from_email, subject=f"Re: {subj}"[:250],
                                          body=start_reply_text(t.stream, link), priority=PRIORITY_REPLY,
                                          kind="reply", target_id=t.id, in_reply_to=mail.message_id,
                                          commercial=False), now)
                t.data_json = {**(t.data_json or {}), "payment_link_sent": True}
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
    """Replies where someone said yes to a trial / the price ("start"), not yet settled by the operator."""
    return list(session.scalars(select(FirmReply).where(FirmReply.intent.in_(("interested", "start")),
                                                        FirmReply.status == "new")
                                .order_by(FirmReply.created_at).limit(limit)))


# -- delivery to subscribers ----------------------------------------------------------------------------------------
SUBSCRIBED = ("TRIAL", "PAYING")


def subscribers(session: Session, stream: str) -> list[FirmTarget]:
    """Targets in TRIAL / PAYING for this stream's product (their own stream, or listed in data_json["products"])."""
    out = []
    for t in session.scalars(select(FirmTarget).where(FirmTarget.stage.in_(SUBSCRIBED), FirmTarget.email.is_not(None))):
        products = (t.data_json or {}).get("products") if isinstance(t.data_json, dict) else None
        if t.stream == stream or (isinstance(products, list) and stream in products):
            out.append(t)
    return out


def delivery_files(out_dir: Path, county: str | None, limit: int = 3) -> list[Path]:
    """This run's full reports for a subscriber: the ones naming their county first, else the newest few (a statewide
    feed). Samples are never delivered to a paying client."""
    files = [p for p in sorted(Path(out_dir).glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
             if ".sample." not in p.name]
    if county:
        key = re.sub(r"[^a-z]+", "_", county.lower()).strip("_")
        mine = [p for p in files if key and key in p.name.lower()]
        if mine:
            return mine[:limit]
    return files[:limit]


def deliver(session: Session, stream: str, label: str, out_dir: Path, now: datetime | None = None) -> int:
    """Once a week per subscriber: this run's full report(s) in a plain note (priority 1: they asked for it)."""
    from engine.streams.base import week_of
    y, w, _ = _now(now).isocalendar()
    n = 0
    for t in subscribers(session, stream):
        files = delivery_files(out_dir, t.county)
        if not files:
            continue
        first = (t.name or "").split(" ")[0] if t.name and " " in (t.name or "") else ""
        body = (f"Hi {first or 'there'},\n\nHere's this week's {label} (week of {week_of(_now(now))}), attached. "
                "Reply with any questions or a county you'd like added.")
        row = enqueue(session, Outbound(stream=stream, key=f"deliver:{stream}:{t.id}:{y}-W{w:02d}", to=t.email,
                                        subject=f"{label}: week of {week_of(_now(now))}", body=body,
                                        priority=PRIORITY_REPLY, kind="delivery", attachments=files, target_id=t.id,
                                        commercial=False), now)
        n += int(row is not None)
    return n
