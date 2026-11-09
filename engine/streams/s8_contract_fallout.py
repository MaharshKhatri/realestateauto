"""S8 Contract fallout: listings that come back from pending / contingent, and "under contract" replies gone quiet.

In New Jersey a contract can die in the 3-business-day attorney review, at inspection or at the mortgage contingency;
the listing then returns to "for sale", often with a seller who now wants certainty. That moment is the best time to
be the first call, and an investor who already put a number in writing is the obvious one. Two detectors:

(a) status transitions: each run keeps the listings last seen under contract in swarm_state `s8_listing_status`
    ({listing_id: "pending" | "contingent"}; only under-contract rows are stored, since only they can fall out, which
    keeps the snapshot to ~2K entries). A listing stored there that is now `for_sale` fell out of contract. A pending
    listing the harvest stopped seeing (`off_market`, raw_json status_before_off_market pending) still counts as under
    contract. The first run only records the snapshot.
(b) quiet "under contract" replies: an agent told us it was under contract / in attorney review / taking backups at
    least `review_days` (14) ago, the lead still has an active listing signal and nobody wrote since: "did it close or
    fall through?".

Every fallout is kept in swarm_state `s8_fallout` ([{lead_id, detected, via}], `keep_days`) so the deal-desk digest
can flag "back on market" rows. For agents we already wrote to (or who wrote to us) and whose e-mail is on no stop
list, the stream proposes one short note in their existing Gmail thread: "my offer of $X still stands" only when our
written offer exists and still fits under today's best and final (`offer_math.locked(...).final`), otherwise "what
would the seller need now?". No other number is ever stated. A follow-up waits when anything was e-mailed about the
lead in the last 7 days. The internal "Back on market today" PDF lists the day's fallouts (streets only, no names).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (
    Contact,
    DistressSignal,
    FirmOutbox,
    InboundReply,
    Interaction,
    Lead,
    Listing,
    SignalType,
    SwarmState,
    utcnow,
)
from engine.streams.base import (
    PRIORITY_PITCH,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    address_ok,
    money,
    operator_footer,
    render_table_pdf,
)

KEY = "s8"
STATE_KEY = "s8_listing_status"
FALLOUT_KEY = "s8_fallout"
UNDER_CONTRACT = ("pending", "contingent")
LISTING_SIGNALS = (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)
CLOSED_STAGES = ("DEAD", "DO_NOT_CONTACT", "CLOSED")


# -- pure helpers -------------------------------------------------------------------------------------------------
def contract_status(status: str | None, raw: dict[str, Any] | None = None) -> str | None:
    """"pending" / "contingent" when the listing is (last known) under contract, else None. A listing that dropped
    out of the feed while pending (`off_market`, status_before_off_market pending) is still under contract."""
    st = (status or "").strip().lower()
    if st in UNDER_CONTRACT:
        return st
    if st == "off_market":
        before = str((raw or {}).get("status_before_off_market") or "").strip().lower()
        if before in UNDER_CONTRACT:
            return before
    return None


def transitions(previous: dict[str, str], current: dict[str, str | None]) -> list[str]:
    """Listing ids stored as under contract last run that are for sale now (fell out of contract)."""
    return sorted((lid for lid, st in current.items()
                   if (st or "").lower() == "for_sale" and str(previous.get(lid) or "").lower() in UNDER_CONTRACT),
                  key=lambda x: int(x) if str(x).isdigit() else 0)


_UC_TEXT = re.compile(r"(?<!not )(?<!n't )(?<!no longer )(?<!never )(?<!was )(?<!no )"
                      r"\b(under contract|in attorney review|back-?up offer|as a back-?up|accepted an offer)\b", re.I)


def said_under_contract(intents: list[str] | None, analysis: dict[str, Any] | None = None,
                        headline: str | None = None) -> bool:
    """The agent's reply said under contract / attorney review / backup: the parsed intents first, then our own stored
    reading (analysis kind / headline). "Not under contract" / "no longer under contract" never counts."""
    if {"UNDER_CONTRACT", "BACKUP"} & {str(i).upper() for i in intents or []}:
        return True
    a = analysis or {}
    if str(a.get("kind") or "").lower() in ("under_contract", "backup"):
        return True
    if {"UNDER_CONTRACT", "BACKUP"} & {str(i).upper() for i in a.get("intents") or []}:
        return True
    if intents:                                         # parsed without either intent: the headline adds nothing
        return False
    return any(_UC_TEXT.search(str(t or "")) for t in (a.get("headline"), headline))


def followup_body(street: str, first: str, via: str, offer: float | None) -> str:
    """3 plain sentences + the operator's first name. The only number ever stated is our written offer that stands."""
    if via == "reply":
        opener = (f"Checking back on {street}: did the contract go through, or did it fall out in review or "
                  "inspection?")
        cond = "If it's available again"
    else:
        opener = f"I saw {street} is back on the market and wanted to check whether the earlier deal fell through."
        cond = "If it did"
    ask = (f"{cond}, my offer of {money(offer)} still stands, and I can send it back over in writing today."
           if offer else f"{cond}, what would the seller need now to make a deal?")
    return "\n\n".join(["Hi,", f"{opener} {ask} Thanks either way.", first]).strip()


def merge_fallouts(history: list[dict[str, Any]], new: list[dict[str, Any]], today: date,
                   keep_days: int) -> list[dict[str, Any]]:
    """History + this run's detections: one entry per (lead, day), older than keep_days dropped, newest first."""
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    seen: set[tuple[int, str]] = set()
    out: list[dict[str, Any]] = []
    for e in [*new, *history]:
        try:
            k = (int(e["lead_id"]), str(e["detected"])[:10])
        except (KeyError, TypeError, ValueError):
            continue
        if k[1] < cutoff or k in seen:
            continue
        seen.add(k)
        out.append({**e, "lead_id": k[0], "detected": k[1]})
    return sorted(out, key=lambda e: (e["detected"], e["lead_id"]), reverse=True)[:1000]


# -- database helpers ---------------------------------------------------------------------------------------------
def _leads_for(session: Session, lst: Listing) -> list[Lead]:
    """The leads a listing belongs to: its property link, else any listing signal carrying its source_ref."""
    out: list[Lead] = []
    if lst.property_id:
        lead = session.scalar(select(Lead).where(Lead.property_id == lst.property_id))
        if lead is not None:
            out.append(lead)
    for lead in session.scalars(select(Lead).join(DistressSignal, DistressSignal.property_id == Lead.property_id)
                                .where(DistressSignal.source_ref == lst.source_ref,
                                       DistressSignal.signal_type.in_(LISTING_SIGNALS))):
        if lead not in out:
            out.append(lead)
    return out


def _has_live_listing(lead: Lead) -> bool:
    return any(s.active for s in lead.property.signals if s.signal_type in LISTING_SIGNALS)


def _out_emails(session: Session, lead_id: int):
    return select(Interaction.id).where(Interaction.lead_id == lead_id, Interaction.channel.like("email%"),
                                        Interaction.channel != "email_in")


def _recently_emailed(session: Session, lead_id: int, since_eastern: datetime, since_utc: datetime) -> bool:
    if session.scalar(_out_emails(session, lead_id).where(Interaction.occurred_at >= since_eastern).limit(1)):
        return True
    return bool(session.scalar(select(FirmOutbox.id).where(
        FirmOutbox.lead_id == lead_id, FirmOutbox.status.in_(("queued", "sent")),
        FirmOutbox.created_at >= since_utc).limit(1)))


def _prior_contact(session: Session, lead_id: int) -> bool:
    """We e-mailed this listing's agent before, or they wrote to us: a follow-up, not a cold e-mail."""
    if session.scalar(_out_emails(session, lead_id).limit(1)):
        return True
    return bool(session.scalar(select(InboundReply.id).where(
        InboundReply.lead_id == lead_id, InboundReply.status.not_in(("bounce", "auto_reply"))).limit(1)))


def _agent_stopped(session: Session, email: str) -> bool:
    from engine import agent_outreach as ao
    e = email.strip().lower()
    if e in ao.bounced_emails(session):
        return True
    if session.scalar(select(Contact.id).where(func.lower(Contact.email) == e, Contact.dnc_status == "dnc").limit(1)):
        return True
    return bool(session.scalar(select(InboundReply.id).where(func.lower(InboundReply.from_email) == e,
                                                             InboundReply.status == "opted_out").limit(1)))


def standing_offer(session: Session, lead: Lead, today: date) -> float | None:
    """Our written offer when it still fits under today's best and final (no re-trade, never above the final)."""
    from engine import offer_math
    sent = float(lead.offer_amount or 0)
    if not sent:
        return None
    try:
        f = offer_math.locked(lead, offer_math.firm_offer(lead, session, today=today))
    except Exception:                                  # noqa: BLE001 - a broken valuation states no number
        return None
    return sent if f.final and f.final >= sent else None


@dataclass
class Fallout:
    lead: Lead
    via: str                                           # "status" / "reply"
    detected: str
    listing: Listing | None = None
    was: str | None = None                             # the listing's last under-contract status
    reply: InboundReply | None = None


class ContractFalloutStream(Stream):
    key = KEY
    name = "Contract fallout"
    priority = PRIORITY_PITCH
    cadence_hours = 1.0
    run_hours = (8, 20)

    # -- detection ----------------------------------------------------------------------------------------------
    def _status_pass(self, session: Session, m: dict[str, int],
                     cap: int) -> list[tuple[Listing, list[Lead], str]]:
        row = session.get(SwarmState, STATE_KEY)
        first = row is None
        previous = {str(k): str(v) for k, v in ((row.value_json or {}) if row else {}).items()}
        statuses = {str(i): st for i, st in session.execute(select(Listing.id, Listing.status))}
        raws = {str(i): raw for i, raw in session.execute(
            select(Listing.id, Listing.raw_json).where(Listing.status == "off_market"))}
        snapshot = {i: cs for i, st in statuses.items() if (cs := contract_status(st, raws.get(i)))}
        m["listings"] = len(statuses)
        m["snapshot"] = len(snapshot)
        m["first_run"] = int(first)
        if row is None:
            row = SwarmState(key=STATE_KEY, value_json={})
            session.add(row)
        row.value_json = snapshot
        if first:
            return []
        ids = transitions(previous, statuses)
        m["transitions"] = len(ids)
        out: list[tuple[Listing, list[Lead], str]] = []
        for lst in session.scalars(select(Listing).where(Listing.id.in_([int(i) for i in ids[:cap]]))):
            out.append((lst, _leads_for(session, lst), previous.get(str(lst.id), "pending")))
        return out

    def _reply_pass(self, session: Session, ctx: StreamContext, done_replies: set[int],
                    m: dict[str, int], cap: int) -> list[InboundReply]:
        lim = ctx.limits or {}
        review = float(lim.get("review_days", 14))
        oldest = float(lim.get("review_max_days", 60))
        rows = session.scalars(select(InboundReply).where(
            InboundReply.lead_id.is_not(None), InboundReply.received_at.is_not(None),
            InboundReply.received_at <= ctx.now - timedelta(days=review),
            InboundReply.received_at >= ctx.now - timedelta(days=oldest),
            InboundReply.status.not_in(("bounce", "auto_reply", "opted_out")))
            .order_by(InboundReply.received_at.desc()).limit(cap * 10)).all()
        picked: dict[int, InboundReply] = {}
        for r in rows:
            if r.lead_id in picked or not said_under_contract(r.intents_json, r.analysis_json, r.headline):
                continue
            picked[r.lead_id] = r                       # the newest under-contract reply per lead
        out: list[InboundReply] = []
        for lead_id, r in picked.items():
            if r.id in done_replies:
                continue
            newer = session.scalar(select(InboundReply.id).where(
                InboundReply.lead_id == lead_id, InboundReply.received_at > r.received_at,
                InboundReply.status.not_in(("bounce", "auto_reply"))).limit(1))
            if newer:
                continue
            lead = session.get(Lead, lead_id)
            if lead is None or not _has_live_listing(lead):
                continue
            asked = session.scalar(select(FirmOutbox.id).where(FirmOutbox.key.like(f"{self.key}:back:{lead_id}:%"),
                                                               FirmOutbox.created_at >= r.received_at).limit(1))
            if asked:
                continue
            out.append(r)
            if len(out) >= cap:
                break
        m["reply_checks"] = len(out)
        return out

    # -- the run ------------------------------------------------------------------------------------------------
    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        res = StreamResult(stream=self.key)
        lim = ctx.limits or {}
        cap = int(lim.get("max_detect", 200))
        today = ctx.now.date()
        m: dict[str, int] = {k: 0 for k in ("listings", "snapshot", "first_run", "transitions", "reply_checks",
                                             "fallouts", "followups", "skipped_suppressed", "skipped_no_agent",
                                             "skipped_stop", "skipped_recent", "skipped_cold", "skipped_stage",
                                             "offer_stands")}
        state = session.get(SwarmState, FALLOUT_KEY)
        history = list(((state.value_json or {}) if state else {}).get("items") or [])
        done_replies = {int(e["reply_id"]) for e in history if e.get("reply_id")}
        found: list[Fallout] = []
        for lst, leads, was in self._status_pass(session, m, cap):
            for lead in leads:
                found.append(Fallout(lead=lead, via="status", detected=today.isoformat(), listing=lst, was=was))
        for r in self._reply_pass(session, ctx, done_replies, m, cap):
            lead = session.get(Lead, r.lead_id)
            if lead is not None:
                found.append(Fallout(lead=lead, via="reply", detected=today.isoformat(), reply=r))
        new: list[dict[str, Any]] = []
        for f in found:
            res.processed += 1
            if f.lead.property is None or not address_ok(session, f.lead.property):
                m["skipped_suppressed"] += 1
                continue
            e: dict[str, Any] = {"lead_id": f.lead.id, "detected": f.detected, "via": f.via}
            if f.listing is not None:
                e["listing_id"], e["was"] = f.listing.id, f.was
            if f.reply is not None:
                e["reply_id"] = f.reply.id
            new.append(e)
        m["fallouts"] = len(new)
        res.found = len(new)
        keep = int(lim.get("keep_days", 60))
        merged = merge_fallouts(history, new, today, keep)
        if state is None:
            state = SwarmState(key=FALLOUT_KEY, value_json={})
            session.add(state)
        state.value_json = {"items": merged, "updated": ctx.now.isoformat(timespec="minutes")}
        session.flush()

        # -- follow-ups: recent fallouts not yet written about (a capped run leaves the rest for the next one)
        plan: dict[int, str] = {}                         # lead_id -> what happened (for the PDF)
        window = (today - timedelta(days=int(lim.get("followup_days", 3)))).isoformat()
        max_send = int(lim.get("max_per_run", 5))
        gap = float(lim.get("recent_email_days", 7))
        since_e, since_u = ctx.now - timedelta(days=gap), utcnow() - timedelta(days=gap)
        cold_ok = bool(lim.get("cold_ok", False))
        seen: set[int] = set()
        for e in merged:
            lid = int(e["lead_id"])
            if e["detected"] < window or lid in seen:
                continue
            seen.add(lid)
            key = f"{self.key}:back:{lid}:{e['detected']}"
            if session.scalar(select(FirmOutbox.id).where(FirmOutbox.key == key)):
                plan[lid] = "follow-up already queued or sent"
                continue
            plan[lid] = self._why_not(session, lid, since_e, since_u, cold_ok, m) or ""
            if plan[lid]:
                continue
            if len(res.outbound) >= max_send:
                plan[lid] = "waits for the next run (per-run cap)"
                continue
            lead = session.get(Lead, lid)
            ob = self._outbound(session, ctx, lead, e, key, today, m)
            if ctx.send:
                res.outbound.append(ob)
                plan[lid] = "e-mail queued in their thread" if ob.in_reply_to else "e-mail queued"
            else:
                plan[lid] = "dry run: no e-mail"
        m["followups"] = len(res.outbound)

        # -- the internal page: today's fallouts, only when this run found new ones
        todays = [e for e in merged if e["detected"] == today.isoformat()]
        if new and todays:
            self._render(session, ctx, todays, plan)
            res.products = 1
        res.metrics = m
        return res

    def _why_not(self, session: Session, lead_id: int, since_e: datetime, since_u: datetime, cold_ok: bool,
                 m: dict[str, int]) -> str | None:
        from engine import agent_outreach as ao
        lead = session.get(Lead, lead_id)
        if lead is None or lead.property is None:
            return "lead gone"
        if lead.status_stage in CLOSED_STAGES:
            m["skipped_stage"] += 1
            return f"lead {lead.status_stage.lower()}"
        if not address_ok(session, lead.property):
            m["skipped_suppressed"] += 1
            return "address suppressed"
        agent = ao.agent_contact(lead)
        email = (agent.email or "").strip().lower() if agent is not None else ""
        if not email:
            m["skipped_no_agent"] += 1
            return "no listing-agent e-mail"
        if _agent_stopped(session, email):
            m["skipped_stop"] += 1
            return "agent on a stop list"
        if not cold_ok and not _prior_contact(session, lead_id):
            m["skipped_cold"] += 1
            return "never contacted (no cold e-mail)"
        if _recently_emailed(session, lead_id, since_e, since_u):
            m["skipped_recent"] += 1
            return "e-mailed in the last 7 days"
        return None

    def _outbound(self, session: Session, ctx: StreamContext, lead: Lead, e: dict[str, Any], key: str, today: date,
                  m: dict[str, int]) -> Outbound:
        from engine import agent_outreach as ao
        from engine.gmail_autopilot import reply_thread
        from engine.human_copy import street as street_of
        st = street_of(lead.property.street_address).rstrip(".") or "the house"
        offer = standing_offer(session, lead, today)
        m["offer_stands"] += int(offer is not None)
        thread = reply_thread(session, lead.id)
        if thread and thread.get("message_id"):
            subject = thread.get("subject") or f"Re: {st}"
            in_reply_to = thread["message_id"]
        else:
            subject = f"{st}: back on the market?" if e.get("via") == "status" else f"{st}: did it close?"
            in_reply_to = None
        return Outbound(stream=self.key, key=key, to=ao.agent_contact(lead).email.strip().lower(), subject=subject,
                        body=followup_body(st, ctx.operator.get("first", ""), str(e.get("via")), offer),
                        priority=PRIORITY_PITCH, kind="followup", lead_id=lead.id, in_reply_to=in_reply_to,
                        commercial=False)

    def _render(self, session: Session, ctx: StreamContext, entries: list[dict[str, Any]],
                plan: dict[int, str]) -> None:
        from core.text import muni_base, postal_title
        from engine import offer_math
        from engine.human_copy import street as street_of
        rows: list[list[str]] = []
        for e in entries:
            lead = session.get(Lead, int(e["lead_id"]))
            if lead is None or lead.property is None or not address_ok(session, lead.property):
                continue
            p = lead.property
            lst = (session.get(Listing, int(e["listing_id"])) if e.get("listing_id")
                   else offer_math.listing_for(session, lead))
            town = postal_title(muni_base(p.municipality)) if p.municipality else postal_title(p.city or "")
            if e.get("via") == "status":
                what = f"was {e.get('was') or 'pending / contingent'}, now for sale"
            else:
                r = session.get(InboundReply, int(e["reply_id"])) if e.get("reply_id") else None
                when = f" {r.received_at:%m/%d}" if r is not None and r.received_at else ""
                what = f"reply check: under contract as of{when or ' 14+ days ago'}, still listed, nothing since"
            offer = standing_offer(session, lead, ctx.now.date())
            written = (f"{money(offer)} stands" if offer else
                       "in writing, but over today's final: no number" if lead.offer_amount else "none in writing")
            rows.append([str(len(rows) + 1), f"<b>{street_of(p.street_address).rstrip('.')}</b><br/>{town}", what,
                         money(lst.list_price) if lst is not None and lst.list_price else "-",
                         str(lst.days_on_market) if lst is not None and lst.days_on_market else "-",
                         written, plan.get(lead.id, "outside the follow-up window")])
            if len(rows) >= 18:
                break
        render_table_pdf(ctx.out_dir / f"s8_back_on_market_{ctx.now:%Y%m%d_%H%M}.pdf",
                         title="Back on market today",
                         subtitle=f"{ctx.now:%A, %B %-d, %Y} · listings that left pending / contingent, and "
                                  "\"under contract\" replies with the listing still up (internal)",
                         head=["#", "Property", "What changed", "List price", "Days on market", "Our written offer",
                               "Follow-up"],
                         rows=rows,
                         notes=["Statuses come from the public listing feed; replies are summarized by type only. "
                                "A follow-up states no number except a written offer that still fits under today's "
                                "best and final."],
                         footer=operator_footer(ctx.operator),
                         col_widths=[0.3, 2.0, 2.4, 0.9, 0.8, 1.6, 2.1])


STREAM = ContractFalloutStream()
