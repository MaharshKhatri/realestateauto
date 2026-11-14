"""Operator-approved partner notes (2026-11-14, the operator: "send a 42-word note to the top 15 Monmouth County
investor-agents and buyers at 9:00 AM today").

A sealed note item `{"partner_blast": {...}}` (engine/cloud_desk.py) picks the best cash buyers and investor agents in
one county and queues ONE short, operator-written note to each through the firm outbox (`kind="partner_note"`):

- who: firm targets that buy or list in the county (cash buyers / flippers first, then investor agents and brokers,
  `s1.target_counties`), then the listing agents closest to the flippers there (`deal_desk.flipper_agents`);
- never: a stopped, bounced, opted-out or closed address (`firm_outbox.stop_reason`), an agent we are negotiating a
  house with (`s2.in_deal_talks`), the listing agents of our live offers or anyone at their brokerage's own domain
  (they would learn we are looking for a partner), anyone with an open reply waiting on us, anyone e-mailed in the last
  `recent_days`, or anyone this note (its `key`) already went to;
- what: the operator's own words (`agent_body` / `buyer_body`), greeted by first name when the target is a person,
  signed with the operator's first name; the outbox adds the solicitation line, signature, postal address, "no more"
  opt-out and List-Unsubscribe (CAN-SPAM) and checks the domain in DNS before it goes;
- when: any day 8 AM-8 PM Eastern (the operator's call: the deposit answer is due Tuesday), 45-90 seconds apart, inside
  the day's first-touch ceiling; a cold-mail pause holds it like every other cold e-mail;
- a target that gets it has its other queued first touches set aside (one cold note a week), and a reply comes back as
  an s1 firm reply for a person (nothing is released or answered by itself).

`preview: true` queues nothing and mails the list (who, why, the e-mail each would get) to the operator's own inbox
as "[njw-blast] <date> <county> preview", so Claude can check it in Gmail before the real run. The public run log
carries counts only.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import FirmOutbox, FirmReply, FirmTarget, utcnow

log = logging.getLogger(__name__)

FREE_MAIL = {"gmail.com", "yahoo.com", "aol.com", "hotmail.com", "outlook.com", "icloud.com", "me.com", "msn.com",
             "comcast.net", "verizon.net", "optonline.net", "live.com", "protonmail.com", "att.net", "ymail.com"}
BUYER_KINDS = ("flipper", "sheriff_bidder")
AGENT_KINDS = ("investor_agent", "investor_friendly_broker")


@dataclass
class Candidate:
    email: str
    kind: str                       # flipper / sheriff_bidder / investor_agent / investor_friendly_broker
    audience: str                   # "buyer" or "agent" (which body they get)
    name: str = ""
    org: str = ""
    why: str = ""
    target_id: int | None = None
    score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def _county(v: str | None) -> str:
    return re.sub(r"(?i)\s+county$", "", (v or "").strip()).title()


def brokerage_key(name: str | None) -> str:
    """"RE/MAX Gateway - Broker" and "RE/MAX GATEWAY" are one office."""
    n = re.sub(r"(?i)\s*-\s*broker\s*$", "", (name or "").strip())
    return re.sub(r"[^a-z0-9]+", "", n.lower())


def blocked(session: Session, county: str, now: datetime, recent_days: float, key: str,
            partner_gap_days: float = 0) -> tuple[set[str], set[str], set[str]]:
    """(addresses, domains, brokerages) this note never goes to: our live-offer agents' own office is out by its
    domain AND by its name (an agent there may write from a gmail.com address)."""
    from engine.streams import s1_acquisitions as s1
    from engine.streams.s2_deal_desk_saas import in_deal_talks
    emails = set(in_deal_talks(session, now))
    domains: set[str] = set()
    offices: set[str] = set()
    for lead in s1.live_offers(session, now):
        for c in lead.contacts:
            if c.company and brokerage_key(c.company):
                offices.add(brokerage_key(c.company))
            if c.email:
                emails.add(c.email.lower())
                if _domain(c.email) not in FREE_MAIL:
                    domains.add(_domain(c.email))
    emails |= {(e or "").lower() for (e,) in session.execute(select(FirmReply.from_email).where(
        FirmReply.status == "new", FirmReply.from_email.is_not(None)))}
    # already in a conversation with us (they replied, said yes, are on a trial / paying): never a cold note
    emails |= {(e or "").lower() for (e,) in session.execute(select(FirmTarget.email).where(
        FirmTarget.email.is_not(None), FirmTarget.stage.notin_(("NEW", "CONTACTED"))))}
    since = utcnow() - timedelta(days=recent_days)
    emails |= {(e or "").lower() for (e,) in session.execute(select(FirmOutbox.to_email).where(
        FirmOutbox.status == "sent", FirmOutbox.sent_at >= since))}
    emails |= {(e or "").lower() for (e,) in session.execute(select(FirmOutbox.to_email).where(
        FirmOutbox.key.like(f"partner:{key}:%")))}
    if partner_gap_days:
        # one partner note per address per `recipient_gap_days`, whatever the house (automatic blasts)
        emails |= {(e or "").lower() for (e,) in session.execute(select(FirmOutbox.to_email).where(
            FirmOutbox.kind == "partner_note", FirmOutbox.status.in_(("queued", "sent")),
            FirmOutbox.created_at >= utcnow() - timedelta(days=partner_gap_days)))}
    return {e for e in emails if e}, domains, offices


def pick(session: Session, county: str, limit: int, *, now: datetime | None = None, recent_days: float = 3,
         key: str = "blast", exclude: list[str] | None = None, buyer_share: float = 0.5,
         include: list[str] | None = None, partner_gap_days: float = 0) -> list[Candidate]:
    """The best `limit` buyers and investor agents in the county, buyers first (about `buyer_share` of the list).
    `include` (addresses Claude researched for this note) go first when they pass every rule below."""
    from engine import deal_desk as dd
    from engine import firm_outbox as fo
    from engine.streams import s1_acquisitions as s1
    now = now or utcnow()
    county = _county(county)
    no_mail, no_domain, no_office = blocked(session, county, now, recent_days, key, partner_gap_days)
    no_mail |= {(e or "").strip().lower() for e in exclude or [] if e}

    def ok(email: str | None, target: FirmTarget | None = None, office: str | None = None) -> str | None:
        e = fo.norm_email(email)
        if not e or e in no_mail or _domain(e) in no_domain:
            return None
        if office and brokerage_key(office) in no_office:
            return None
        if fo.stop_reason(session, e, target):
            return None
        return e

    buyers: list[Candidate] = []
    agents: list[Candidate] = []
    rows = session.scalars(select(FirmTarget).where(
        FirmTarget.kind.in_(BUYER_KINDS + AGENT_KINDS), FirmTarget.email.is_not(None),
        FirmTarget.stage.in_(("NEW", "CONTACTED")))).all()
    areas = s1.target_counties(session, list(rows), now) if rows else {}
    for t in rows:
        if county not in {_county(c) for c in areas.get(t.id, set())} and _county(t.county) != county:
            continue
        e = ok(t.email, t, t.org)
        if not e:
            continue
        cand = Candidate(email=e, kind=t.kind, audience="buyer" if t.kind in BUYER_KINDS else "agent",
                         name=t.name or "", org=t.org or "", target_id=t.id, score=float(t.score or 0),
                         why=f"{t.kind.replace('_', ' ')} active in {county} ({t.source or 'our records'})")
        (buyers if cand.audience == "buyer" else agents).append(cand)
    seen = {c.email for c in buyers + agents}
    for a in dd.flipper_agents(session, limit=5000, today=now.date()):
        if county not in {_county(c) for c in a.get("counties") or []}:
            continue
        e = ok(a["email"], office=a.get("company"))
        if not e or e in seen:
            continue
        seen.add(e)
        agents.append(Candidate(email=e, kind="investor_agent", audience="agent", name=a.get("name") or "",
                                org=a.get("company") or "", score=float(a.get("score") or 0),
                                why=(f"listing agent, {a.get('listings', 0)} live listings ({a.get('towns') or county}),"
                                     f" {a.get('resales', 0)} flip resales, {a.get('fixers', 0)} fixers"),
                                extra={"phone": a.get("phone") or ""}))
    buyers.sort(key=lambda c: (-c.score, c.email))
    agents.sort(key=lambda c: (-c.score, c.email))
    wanted = [e for e in (fo.norm_email(x) for x in include or []) if e]
    first = [c for e in wanted for c in buyers + agents if c.email == e][:limit]
    buyers = [c for c in buyers if c not in first]
    agents = [c for c in agents if c not in first]
    room = limit - len(first)
    n_buy = min(len(buyers), max(0, round(room * buyer_share)))
    picked = first + buyers[:n_buy] + agents[:room - n_buy]
    if len(picked) < limit:                      # fewer agents than their share: fill with the next buyers
        picked += buyers[n_buy:n_buy + limit - len(picked)]
    return picked[:limit]


def greeting(c: Candidate) -> str:
    from engine.streams.s2_deal_desk_saas import first_name
    first = first_name(c.name) if c.name else "there"
    return f"Hi {first}," if first != "there" else "Hello,"


def body_for(c: Candidate, spec: dict[str, Any], op_first: str) -> str:
    text = str(spec.get(f"{c.audience}_body") or spec.get("body") or "").strip()
    text = re.sub(r"^(Hi|Hello)\s*(\{first\}|[A-Z][a-z]+)?,\s*", "", text)     # the greeting is ours
    return "\n\n".join(x for x in (greeting(c), text, op_first) if x).strip()


def preview_text(cands: list[Candidate], spec: dict[str, Any], op_first: str) -> str:
    lines = [f"Partner note preview: {len(cands)} recipients in {_county(spec.get('county'))} County "
             f"(nothing was sent). Subject: {spec.get('subject')}", ""]
    for i, c in enumerate(cands, 1):
        who = " / ".join(x for x in (c.name, c.org) if x) or c.email
        lines += [f"{i}. {who} <{c.email}> [{c.kind}, {c.audience} text]", f"   why: {c.why}"]
    if cands:
        lines += ["", "--- buyer text ---", body_for(Candidate("x@y", "flipper", "buyer", name="Pat Sample"), spec,
                                                     op_first),
                  "", "--- agent text ---", body_for(Candidate("x@y", "investor_agent", "agent", name="Pat Sample"),
                                                     spec, op_first)]
    return "\n".join(lines)


def queue(session: Session, spec: dict[str, Any], now: datetime | None = None) -> dict[str, int]:
    """Queue the note for each picked recipient (or, with `preview`, only return the list). Returns counts."""
    from engine import firm_outbox as fo
    from engine import human_copy as hc
    from engine.streams.base import Outbound
    from engine.firm_outbox import PRIORITY_PITCH
    from engine.gmail_autopilot import now_eastern
    now = now_eastern(now).replace(tzinfo=None)          # naive Eastern, like the streams (swarm_firm.tick)
    county = _county(spec.get("county"))
    key = re.sub(r"[^a-z0-9_-]+", "-", str(spec.get("key") or f"{county.lower()}-{now:%Y%m%d}").lower())[:60]
    subject = str(spec.get("subject") or "").strip()
    if not county or not subject or not (spec.get("body") or spec.get("buyer_body") or spec.get("agent_body")):
        raise ValueError("partner_blast needs county, subject and a body")
    cands = pick(session, county, int(spec.get("limit") or 15), now=now, key=key,
                 recent_days=float(spec.get("recent_days", 3)), exclude=list(spec.get("exclude") or []),
                 buyer_share=float(spec.get("buyer_share", 0.5)), include=list(spec.get("include") or []),
                 partner_gap_days=float(spec.get("partner_gap_days", 0) or 0))
    op_first = hc.operator().get("first") or ""
    out = {"blast_picked": len(cands), "blast_buyers": sum(c.audience == "buyer" for c in cands),
           "blast_agents": sum(c.audience == "agent" for c in cands), "blast_queued": 0}
    if spec.get("preview"):
        out["preview_text"] = preview_text(cands, spec, op_first)        # mailed to the operator, never logged
        return out
    for c in cands:
        t = session.get(FirmTarget, c.target_id) if c.target_id else None
        if t is None:
            t = fo.upsert_target(session, kind=c.kind, stream="s1", email=c.email, org=c.org or None,
                                 name=c.name or None, phone=(c.extra or {}).get("phone") or None, county=county,
                                 source="our listing records (listing agent)", score=c.score)
        if t is None:
            continue
        row = fo.enqueue(session, Outbound(stream="s1", key=f"partner:{key}:{c.email}"[:200], to=c.email,
                                           subject=subject, body=body_for(c, spec, op_first), target_id=t.id,
                                           kind="partner_note", priority=PRIORITY_PITCH, commercial=True), now)
        if row is None or row.status != "queued":
            continue
        out["blast_queued"] += 1
        # one cold note a week: this target's other first touches waiting in the outbox step aside
        for other in session.scalars(select(FirmOutbox).where(
                FirmOutbox.target_id == t.id, FirmOutbox.status == "queued", FirmOutbox.id != row.id,
                FirmOutbox.kind.in_(fo.FIRST_TOUCH_KINDS))):
            other.status, other.reason = "skipped", "partner note sent instead"
    session.flush()
    return out


def mail_preview(text: str, county: str, now: datetime, smtp_factory: Any = None) -> bool:
    """The preview goes to the operator's own inbox (it names people and addresses: never into the public log)."""
    import os

    from engine.cloud_desk import _send_self_files
    user, pw = os.environ.get("SMTP_USER", "").strip(), os.environ.get("SMTP_APP_PASSWORD", "")
    if not user or not pw:
        return False
    try:
        _send_self_files(user, pw, f"[njw-blast] {now:%Y-%m-%d} {_county(county)} preview", text, [], smtp_factory)
    except Exception:                                    # noqa: BLE001 - a preview never stops the run
        log.exception("partner blast preview not mailed")
        return False
    return True


def count_open(session: Session) -> int:
    return int(session.scalar(select(func.count(FirmOutbox.id)).where(
        FirmOutbox.kind == "partner_note", FirmOutbox.status == "queued")) or 0)


# -- the automatic trigger (2026-11-14, the operator's CEO directive) ---------------------------------------------
AUTO_KEY = "partner_auto"
PRODUCT = {1: "single-family", 2: "2-family", 3: "3-family", 4: "4-family"}


def auto_cfg() -> dict[str, Any]:
    from core.config import settings
    return dict(settings().get("partner_auto") or {})


def band(x: float, step: int = 25_000) -> str:
    """A rounded band around one number, at least $50K wide, never the number itself."""
    lo = int((x - step) // step) * step
    hi = -int(-(x + step) // step) * step
    return f"${lo // 1000}K-${hi // 1000}K" if lo < 1_000_000 else f"${lo / 1e6:.2f}M-${hi / 1e6:.2f}M"


def auto_spec(lead: Any, stage: str) -> dict[str, Any] | None:
    """The note for one warm house: county, product and a rounded band only (never the address or our number)."""
    p = lead.property
    county = _county(p.county if p is not None else None)
    price = float(lead.offer_amount or 0)
    if not county or not price:
        return None
    product = PRODUCT.get(int(p.dwellings or 1), "multi-family")
    where = {"accepted": "with the seller's yes on price", "close": "where the seller is close on price",
             "pof": "that I'm negotiating now"}[stage]
    ask = (f"Would you co-buy with me, funding the deposit/purchase for an attorney-reviewed profit split?")
    head = f"I have an as-is {product} in {county} County in the {band(price)} range {where}."
    return {"county": county, "limit": int(auto_cfg().get("limit", 10)), "key": f"auto-{lead.id}",
            "subject": f"As-is {product} in {county} County", "buyer_body": f"{head} {ask}",
            "agent_body": f"{head} " + ask.replace("Would you co-buy", "Would you (or an investor client you represent "
                                                                    "on resales) co-buy"),
            "partner_gap_days": float(auto_cfg().get("recipient_gap_days", 14))}


def warm_stage(session: Session, lead: Any, reply: Any) -> str | None:
    """accepted / close (their number within `counter_within` of our final) / pof, on a house we can price."""
    from engine import offer_math as om
    intents = set(reply.intents_json or [])
    try:
        f = om.firm_offer(lead, session)
    except Exception:                                    # noqa: BLE001 - an unpriceable house gets no blast
        return None
    if f.tier == "skip" or not lead.offer_amount:
        return None
    if "ACCEPT" in intents and "UNDER_CONTRACT" not in intents:
        return "accepted"
    if reply.counter_amount and f.final and float(reply.counter_amount) <= float(f.final) + float(
            auto_cfg().get("counter_within", 25000)):
        return "close"
    if "POF_REQUEST" in intents:
        return "pof"
    return None


def auto_run(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Queue partner notes for houses that just turned warm (one blast per house, `max_per_day` a day, no county
    blasted in the last 7 days). Counts only."""
    from db.models import InboundReply, Lead, SwarmState
    c = auto_cfg()
    if not c.get("enabled"):
        return {}
    from engine.gmail_autopilot import now_eastern
    today = now_eastern(now).date().isoformat()
    row = session.get(SwarmState, AUTO_KEY)
    state = dict(row.value_json or {}) if row is not None else {}
    done = dict(state.get("leads") or {})
    used_today = sum(1 for d in done.values() if d == today)
    out = {"auto_blasts": 0, "auto_queued": 0}
    since = utcnow() - timedelta(days=2)
    replies = session.scalars(select(InboundReply).where(
        InboundReply.created_at >= since, InboundReply.lead_id.is_not(None),
        InboundReply.status.notin_(("bounce", "auto_reply"))).order_by(InboundReply.created_at.desc())).all()
    for rep in replies:
        if used_today >= int(c.get("max_per_day", 2)):
            break
        if str(rep.lead_id) in done:
            continue
        lead = session.get(Lead, rep.lead_id)
        stage = warm_stage(session, lead, rep) if lead is not None else None
        spec = auto_spec(lead, stage) if stage else None
        if spec is None:
            continue
        recent = session.scalar(select(FirmOutbox.id).join(FirmTarget, FirmTarget.id == FirmOutbox.target_id).where(
            FirmOutbox.kind == "partner_note", FirmOutbox.created_at >= utcnow() - timedelta(days=7),
            FirmTarget.county == spec["county"]).limit(1))
        done[str(rep.lead_id)] = today                    # decided once per house, blasted or not
        if recent:
            continue                                      # that county heard from us this week
        res = queue(session, spec, now)
        out["auto_blasts"] += 1
        out["auto_queued"] += int(res.get("blast_queued", 0))
        used_today += 1
    if row is None:
        row = SwarmState(key=AUTO_KEY, value_json={})
        session.add(row)
    row.value_json = {"leads": done}
    session.flush()
    return out
