"""Cold-mail safety rules for the operator's one personal Gmail (the B2B playbook, research 2026-11-10).

The real risk of cold B2B mail from a free Gmail account is Google, not the FTC: Gmail's program policies forbid
unsolicited commercial mail and an account can be disabled, which would also end every live deal thread. The
operator chose to stay on the personal Gmail at $0 (2026-11-10). These rules keep the volume where practitioners
report safe results and stop at the first sign of trouble:

- **Ramp** (`research_firm.ramp`): B2B first touches per weekday by week since the first B2B pitch went out, e.g.
  [20, 30]: 20 a day in week 1, 30 from week 2 (the configured p2 + p3 caps are the ceiling).
- **Bounces**: 3+ delivery failures in 48 hours, or more than 2% of the last 7 days' cold sends (with 50+ sent),
  pause every cold e-mail (B2B pitches, nudges, listing-agent openers) until the rule clears. Replies always go.
- **Block codes**: Gmail's `550 5.7.1 ... unsolicited`, `421 4.7.28` (unusual rate), `550 5.7.28` or `550 5.4.5`
  (daily sending limit), on a send or in a bounce, pause cold mail for 7 days (`cold_pause` in swarm_state).
- **Quiet inbox**: no B2B reply in 5 business days with 25+ first touches out = probably the spam folder; the status
  says to send a seed test to friends' Gmail / Outlook addresses. Reported, not enforced.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.config import settings
from db.models import SwarmState, utcnow

PAUSE_KEY = "cold_pause"
BOUNCE_LOG_KEY = "bounce_log"
_HARD_BLOCK = re.compile(r"\b(4\.7\.28|5\.7\.28|5\.4\.5)\b")
_SPAM_5_7_1 = re.compile(r"\b5\.7\.1\b.{0,200}\b(unsolicited|spam|reputation|block ?list|black ?list|"
                         r"blocked|bulk|rate)\b", re.I | re.S)


def cfg() -> dict[str, Any]:
    return dict(settings().get("research_firm") or {})


def _utc(now: datetime | None) -> datetime:
    """One clock for the pause and the bounce log: naive UTC (an aware `now` is converted; None = the real time)."""
    if now is None:
        return utcnow()
    return now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now


def _state(session: Session, key: str) -> dict[str, Any]:
    row = session.get(SwarmState, key)
    return dict(row.value_json or {}) if row else {}


def _save(session: Session, key: str, value: dict[str, Any]) -> None:
    row = session.get(SwarmState, key)
    if row is None:
        row = SwarmState(key=key, value_json={})
        session.add(row)
    row.value_json = value
    session.flush()


# -- block codes ----------------------------------------------------------------------------------------------------
def block_code(text: str | None) -> str | None:
    """The Gmail code that means "stop cold mail", from an SMTP error or a bounce body (None = not one)."""
    t = text or ""
    m = _HARD_BLOCK.search(t)
    if m:
        return m.group(1)
    if _SPAM_5_7_1.search(t):
        return "5.7.1"
    return None


def pause(session: Session, days: float, why: str, now: datetime | None = None) -> None:
    until = _utc(now) + timedelta(days=days)
    cur = _state(session, PAUSE_KEY)
    if cur.get("until") and str(cur["until"]) > until.isoformat(timespec="seconds"):
        return                                        # a longer pause already stands
    _save(session, PAUSE_KEY, {"until": until.isoformat(timespec="seconds"), "why": why[:120],
                               "set": _utc(now).isoformat(timespec="seconds")})


def note_error(session: Session, exc_or_text: Any, now: datetime | None = None) -> str | None:
    """After a failed send or a bounce: a block code pauses cold mail for 7 days. Returns the code."""
    text = exc_or_text if isinstance(exc_or_text, str) else f"{type(exc_or_text).__name__}: {exc_or_text}"
    code = block_code(text)
    if code:
        pause(session, float(cfg().get("block_pause_days", 7)), f"Gmail {code}", now)
    return code


# -- bounces --------------------------------------------------------------------------------------------------------
def log_bounce(session: Session, email: str, now: datetime | None = None) -> None:
    log = list(_state(session, BOUNCE_LOG_KEY).get("items") or [])
    log.append([(email or "").lower()[:160], _utc(now).isoformat(timespec="seconds")])
    _save(session, BOUNCE_LOG_KEY, {"items": log[-500:]})


def bounces_since(session: Session, since: datetime) -> list[str]:
    out = []
    for item in _state(session, BOUNCE_LOG_KEY).get("items") or []:
        try:
            if datetime.fromisoformat(str(item[1])) >= since:
                out.append(str(item[0]))
        except (ValueError, IndexError):
            continue
    return out


def cold_sent_since(session: Session, since: datetime) -> int:
    """Cold e-mails since a naive-UTC time: B2B pitches / nudges and listing-agent e-mails to agents who never wrote
    back (interactions are stored in Eastern time)."""
    from db.models import FirmOutbox, Interaction
    from engine.gmail_autopilot import now_eastern
    firm = int(session.scalar(select(func.count(FirmOutbox.id)).where(
        FirmOutbox.status == "sent", FirmOutbox.sent_at >= since,
        FirmOutbox.kind.in_(("pitch", "followup", "buyer_note")))) or 0)
    since_et = now_eastern(since.replace(tzinfo=timezone.utc)).replace(tzinfo=None)
    rows = session.execute(select(Interaction.debrief_json).where(
        Interaction.channel.in_(("email_smtp", "email_gmail")), Interaction.occurred_at >= since_et)).all()
    return firm + sum(1 for (d,) in rows if not (d or {}).get("engaged"))


# -- the gate -------------------------------------------------------------------------------------------------------
def paused(session: Session, now: datetime | None = None) -> str | None:
    """Why cold e-mail must wait right now (None = it may go). Replies are never held by this."""
    t = _utc(now)
    p = _state(session, PAUSE_KEY)
    if p.get("until"):
        try:
            if datetime.fromisoformat(str(p["until"])) > t:
                return f"cold e-mail paused until {str(p['until'])[:16]} ({p.get('why') or 'Gmail warning'})"
        except ValueError:
            pass
    c = cfg()
    recent = bounces_since(session, t - timedelta(hours=48))
    if len(recent) >= int(c.get("bounce_pause_count", 3)):
        return f"{len(recent)} bounces in 48 hours: cold e-mail waits until they age out (check the list)"
    week = bounces_since(session, t - timedelta(days=7))
    sent = cold_sent_since(session, t - timedelta(days=7))
    if sent >= 50 and len(week) / max(sent, 1) > float(c.get("bounce_pause_rate", 0.02)):
        return f"bounce rate {len(week)}/{sent} in 7 days is over 2%: cold e-mail waits (check the list)"
    return None


# -- ramp -----------------------------------------------------------------------------------------------------------
def first_b2b_day(session: Session) -> date | None:
    from db.models import FirmOutbox
    first = session.scalar(select(func.min(FirmOutbox.sent_at)).where(FirmOutbox.status == "sent",
                                                                       FirmOutbox.kind == "pitch"))
    return first.date() if first else None


def first_touch_cap(session: Session, ceiling: int, today: date | None = None) -> int:
    """B2B first touches allowed per day this week (`ramp`, by week since the first B2B pitch), never above the
    configured caps."""
    ramp = [int(x) for x in (cfg().get("ramp") or [])]
    if not ramp:
        return ceiling
    start = first_b2b_day(session)
    today = today or utcnow().date()
    week = 0 if start is None else max(0, (today - start).days // 7)
    return min(ceiling, ramp[min(week, len(ramp) - 1)])


# -- spam-folder signal ---------------------------------------------------------------------------------------------
def quiet_inbox(session: Session, now: datetime | None = None) -> str | None:
    """No B2B reply in 5 business days with 25+ first touches out: likely the spam folder (a seed test tells)."""
    from db.models import FirmOutbox, FirmReply
    t = _utc(now)
    since, d = t, 0
    while d < 5:
        since -= timedelta(days=1)
        if since.weekday() < 5:
            d += 1
    sent = int(session.scalar(select(func.count(FirmOutbox.id)).where(
        FirmOutbox.status == "sent", FirmOutbox.kind == "pitch", FirmOutbox.sent_at >= since)) or 0)
    first = first_b2b_day(session)
    if sent < 25 or first is None or first > since.date():
        return None
    replies = int(session.scalar(select(func.count(FirmReply.id)).where(FirmReply.created_at >= since)) or 0)
    if replies:
        return None
    return (f"0 B2B replies to {sent} first touches in 5 business days: probably the spam folder. Seed test: send one "
            "pitch to a friend's Gmail, Outlook and work address and see where it lands; rewrite the hook if it's spam.")
