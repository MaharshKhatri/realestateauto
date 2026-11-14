"""The forensic audit (2026-11-14, the operator's CEO directive): what the last three weeks of agent e-mail, offers and
B2B pitches actually produced, so the desk spends its daily e-mail budget only where deals and partners come from.

Desk task `audit` runs `build()` on the live database and mails the report to the operator's own inbox
("[njw-audit] <date>"): it names streets and agents, so it never goes into the public run log (counts only there).

What it measures, per lead the desk e-mailed a listing agent about:
- the house: county / town, units, condition words (fixer / retail / unknown), tenant and estate words, days on market,
  a price cut, living area on record;
- our numbers: the written offer (`lead.offer_amount`) or, with no offer yet, the desk's final, as a share of the ask;
- what came back, classed from the replies the inbox reader stored (`inbound_replies.intents_json`, the counter) and
  the logged outcomes: accepted, warm (proof-of-funds ask, presenting / forwarding the offer, a counter within 15% of
  ours, call me, showing, backup, best-and-final), dead (no, under contract, expired, stop, a counter far above), engaged
  (available / a question only) or silent;
and then the same outcome rates by offer-to-ask band, units, condition, tenants, estate, days on market, price cut and
county; the opener and Touch-2 progression; every firm stream's sends against its replies; and the threads where the
agent wrote last and nobody answered (dropped).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import FirmOutbox, FirmReply, InboundReply, Interaction, Lead, utcnow

EMAIL_OUT = ("AGENT_OPENER_SENT", "OFFER_SENT_TO_AGENT", "AGENT_FOLLOWUP_SENT", "AGENT_INQUIRY_SENT")
NOT_REPLIES = ("bounce", "auto_reply")
WARM_INTENTS = {"POF_REQUEST", "CALL_ME", "ASKS_SHOWING", "BACKUP", "HIGHEST_AND_BEST", "OTHER_LISTINGS"}
DEAD_INTENTS = {"REJECT", "UNDER_CONTRACT", "EXPIRED", "OPT_OUT"}
TENANT_WORDS = re.compile(r"(?i)\b(tenant|tenants|tenant[- ]occupied|occupied|lease[ds]?|rented|income[- ]producing|"
                          r"rent roll|month[- ]to[- ]month|section 8)\b")
ESTATE_WORDS = re.compile(r"(?i)\b(estate sale|estate|probate|executor|heirs?)\b(?! agent)")
WON = ("UNDER_CONTRACT", "ASSIGNED", "CLOSED_WON")


def _eastern(r: InboundReply) -> datetime | None:
    """When the reply came in, naive Eastern (the convention interactions are stored in; created_at is UTC)."""
    if r.received_at:
        return r.received_at
    if r.created_at is None:
        return None
    from engine.gmail_autopilot import now_eastern
    from datetime import timezone
    return now_eastern(r.created_at.replace(tzinfo=timezone.utc))


@dataclass
class Row:
    lead_id: int
    street: str
    town: str
    county: str
    units: int
    condition: str
    tenants: bool
    estate: bool
    dom: int | None
    cut: bool
    sqft_on_record: bool
    ask: float | None
    offer: float | None
    final: float | None
    tier: str
    ratio: float | None             # offer / ask (or final / ask when no offer went out)
    openers: int
    offers: int
    replies: int
    intents: list[str]
    counter: float | None
    outcome: str                    # accepted / warm / dead / engaged / silent
    first_touch: datetime | None
    last_out: datetime | None
    last_in: datetime | None
    agent: str = ""
    why: list[str] = field(default_factory=list)


def _band(r: float | None) -> str:
    if r is None:
        return "no number"
    for lo, label in ((0.90, "90%+"), (0.85, "85-90%"), (0.80, "80-85%"), (0.75, "75-80%"), (0.65, "65-75%")):
        if r >= lo:
            return label
    return "<65%"


def _dom_band(d: int | None) -> str:
    if d is None:
        return "unknown"
    return "120+ days" if d >= 120 else "75-119 days" if d >= 75 else "<75 days"


def _units(n: int) -> str:
    return "1 unit" if n <= 1 else "2 units" if n == 2 else "3-4 units" if n <= 4 else "5+ units"


def classify(intents: set[str], latest: set[str], counter: float | None, offer: float | None, accepted: bool,
             presenting: bool, replies: int, stage: str = "") -> tuple[str, list[str]]:
    """accepted > (the latest reply closes the door) dead > warm > engaged > silent."""
    why: list[str] = []
    if accepted:
        return "accepted", ["accepted / under contract"]
    close = bool(counter and offer and counter <= offer * 1.15)
    far = bool(counter and offer and counter > offer * 1.15)
    if latest & DEAD_INTENTS:
        return "dead", sorted(latest & DEAD_INTENTS)
    warm = intents & WARM_INTENTS
    if stage == "NEGOTIATING" and not warm:
        warm = {"negotiating (offer on the table)"}
    if warm or presenting or close:
        why = sorted(warm) + (["presenting / forwarding our offer"] if presenting else []) + \
            ([f"counter within 15% ({counter:,.0f})"] if close else [])
        return "warm", why
    if intents & DEAD_INTENTS or far:
        return "dead", sorted(intents & DEAD_INTENTS) + ([f"counter {counter:,.0f} far above"] if far else [])
    if replies:
        return "engaged", sorted(intents) or ["replied"]
    return "silent", []


def lead_rows(session: Session, today: datetime | None = None) -> list[Row]:
    from engine import offer_math as om
    from engine.underwriter import asking_price
    today = today or datetime.now()
    ids = {lid for (lid,) in session.execute(select(Interaction.lead_id).where(
        Interaction.outcome.in_(EMAIL_OUT)).distinct())}
    ids |= {lid for (lid,) in session.execute(select(InboundReply.lead_id).where(
        InboundReply.lead_id.is_not(None)).distinct())}
    rows: list[Row] = []
    for lead in session.scalars(select(Lead).where(Lead.id.in_(ids or [-1]))):
        p = lead.property
        if p is None:
            continue
        acts = sorted(lead.interactions, key=lambda i: i.occurred_at or datetime.min)
        outs = [i for i in acts if i.outcome in EMAIL_OUT]
        reps = sorted((r for r in session.scalars(select(InboundReply).where(InboundReply.lead_id == lead.id))
                       if r.status not in NOT_REPLIES), key=lambda r: r.created_at or datetime.min)
        intents = {i for r in reps for i in (r.intents_json or [])}
        latest = set(reps[-1].intents_json or []) if reps else set()
        counter = next((r.counter_amount for r in reversed(reps) if r.counter_amount), None)
        # "accepted the offer ... under contract" in one reply is another buyer's offer (2026-11-13, 86-88 N 8th St)
        accepted = any(i.outcome == "OFFER_ACCEPTED" for i in acts) or str(lead.status_stage or "") in WON or any(
            "ACCEPT" in (r.intents_json or []) and "UNDER_CONTRACT" not in (r.intents_json or []) for r in reps)
        presenting = any(i.outcome == "AGENT_WILL_PRESENT_LOI" for i in acts) or any(
            "forward" in str((r.analysis_json or {}).get("notes") or "").lower() for r in reps)
        try:
            f = om.firm_offer(lead, session, today=today.date())
            tier, final = f.tier, f.final
        except Exception:                                   # noqa: BLE001 - an unpriceable house still counts
            tier, final = "unknown", None
        try:
            cond = om.condition(session, lead)
            text = om.listing_text(session, lead) or ""
        except Exception:                                   # noqa: BLE001
            cond, text = "unknown", ""
        lst = om.listing_for(session, lead)
        ask = asking_price(p) or (float(lst.list_price) if lst is not None and lst.list_price else None)
        offer = float(lead.offer_amount) if lead.offer_amount else None
        ratio = (offer or final) / ask if ask and (offer or final) else None
        v = p.current_valuation
        outcome, why = classify(intents, latest, counter, offer, accepted, presenting, len(reps),
                                str(lead.status_stage or ""))
        agent = next((c.name or c.email or "" for c in lead.contacts if c.contact_type == "LISTING_AGENT"), "")
        rows.append(Row(
            lead_id=lead.id, street=(p.street_address or "").title(), town=(p.city or p.municipality or "").title(),
            county=(p.county or "").title(), units=int(p.dwellings or 1), condition=cond,
            tenants=bool(TENANT_WORDS.search(text)) or "TENANTS" in intents, estate=bool(ESTATE_WORDS.search(text))
            or "ESTATE" in intents or bool(p.is_estate_owned),
            dom=int(lst.days_on_market) if lst is not None and lst.days_on_market is not None else None,
            cut=bool(lst is not None and lst.price_reduced),
            sqft_on_record=bool(v and (v.assumptions_json or {}).get("sqft_source") == "record"),
            ask=ask, offer=offer, final=final, tier=tier, ratio=round(ratio, 3) if ratio else None,
            openers=sum(i.outcome == "AGENT_OPENER_SENT" for i in outs),
            offers=sum(i.outcome == "OFFER_SENT_TO_AGENT" for i in outs), replies=len(reps), intents=sorted(intents),
            counter=counter, outcome=outcome, first_touch=outs[0].occurred_at if outs else None,
            last_out=outs[-1].occurred_at if outs else None, last_in=_eastern(reps[-1]) if reps else None,
            agent=agent, why=why))
    return rows


def rates(rows: list[Row], key: Any) -> list[tuple[str, int, float, float, float]]:
    """(group, n, reply %, positive % (warm + accepted), dead %) per group, biggest first."""
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        groups[str(key(r))].append(r)
    out = []
    for g, rs in groups.items():
        n = len(rs)
        rep = sum(r.replies > 0 for r in rs)
        pos = sum(r.outcome in ("warm", "accepted") for r in rs)
        dead = sum(r.outcome == "dead" for r in rs)
        out.append((g, n, 100.0 * rep / n, 100.0 * pos / n, 100.0 * dead / n))
    return sorted(out, key=lambda x: (-x[1], x[0]))


def stream_roi(session: Session) -> list[tuple[str, str, int, int, int]]:
    """(stream, kind, sent, replies, interested) for every firm-outbox stream and kind."""
    sent = Counter()
    for stream, kind, n in session.execute(select(FirmOutbox.stream, FirmOutbox.kind, func.count(FirmOutbox.id)).where(
            FirmOutbox.status == "sent").group_by(FirmOutbox.stream, FirmOutbox.kind)):
        sent[(stream, kind)] += int(n)
    reps: Counter = Counter()
    hot: Counter = Counter()
    for stream, intent in session.execute(select(FirmReply.stream, FirmReply.intent)):
        if intent == "auto":
            continue
        reps[stream] += 1
        hot[stream] += int(intent == "interested")
    out = []
    for (stream, kind), n in sorted(sent.items()):
        first = kind in ("pitch", "buyer_note", "partner_note")
        out.append((stream, kind, n, reps[stream] if first else 0, hot[stream] if first else 0))
    return out


def dropped(session: Session, rows: list[Row], now: datetime) -> list[str]:
    """Threads where the agent wrote last (6+ hours ago) and nothing of ours went out since, and replies still
    waiting on a person for 12+ hours."""
    from datetime import timezone

    from engine.gmail_autopilot import now_eastern
    now_e = now_eastern(now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now)
    out = []
    for r in rows:
        if r.outcome in ("dead",) or not r.last_in:
            continue
        if (r.last_out is None or r.last_in > r.last_out) and r.last_in < now_e - timedelta(hours=6):
            out.append(f"#{r.lead_id} {r.street}, {r.town}: the agent wrote {r.last_in:%b %d %H:%M} ET, nothing sent "
                       f"since ({', '.join(r.intents) or 'reply'})")
    for rep in session.scalars(select(InboundReply).where(
            InboundReply.status.in_(("needs_approval", "auto_queued")), InboundReply.created_at < now - timedelta(hours=12))):
        out.append(f"reply #{rep.id} (lead #{rep.lead_id}) waiting since {rep.created_at:%b %d %H:%M} UTC: {rep.headline or ''}")
    return out


def _table(title: str, data: list[tuple[str, int, float, float, float]]) -> list[str]:
    lines = [title, f"  {'group':<26}{'n':>5}{'reply%':>9}{'warm+won%':>11}{'dead%':>8}"]
    for g, n, rep, pos, dead in data:
        lines.append(f"  {g[:26]:<26}{n:>5}{rep:>9.0f}{pos:>11.0f}{dead:>8.0f}")
    return lines + [""]


def build(session: Session, now: datetime | None = None) -> tuple[str, dict[str, int]]:
    """The report text (for the operator's inbox) and the counts (for the public run log)."""
    from engine.outreach_stats import funnel
    now = now or utcnow()
    rows = lead_rows(session, now)
    by_out = Counter(r.outcome for r in rows)
    lines = [f"Desk audit {now:%Y-%m-%d %H:%M} UTC: {len(rows)} houses we e-mailed a listing agent about.", "",
             "Outcomes: " + ", ".join(f"{k} {by_out.get(k, 0)}" for k in
                                     ("accepted", "warm", "dead", "engaged", "silent")), ""]
    lines += _table("By our number as a share of the ask (written offer, else the desk's final):",
                    rates(rows, lambda r: _band(r.ratio)))
    lines += _table("By units:", rates(rows, lambda r: _units(r.units)))
    lines += _table("By the listing's words:", rates(rows, lambda r: r.condition))
    lines += _table("Tenants mentioned (listing or reply):", rates(rows, lambda r: "tenants" if r.tenants else "no"))
    lines += _table("Estate / probate words:", rates(rows, lambda r: "estate" if r.estate else "no"))
    lines += _table("Days on market:", rates(rows, lambda r: _dom_band(r.dom)))
    lines += _table("Price cut:", rates(rows, lambda r: "cut" if r.cut else "no cut"))
    lines += _table("County:", rates(rows, lambda r: r.county or "?"))
    lines += _table("Living area on record:", rates(rows, lambda r: "on record" if r.sqft_on_record else "missing"))
    op = [r for r in rows if r.openers]
    op_rep = sum(r.replies > 0 for r in op)
    t2 = [r for r in rows if r.offers and r.replies]
    lines += ["Openers and offers:",
              f"  houses that got an opener: {len(op)}; agent replied: {op_rep} "
              f"({100.0 * op_rep / max(len(op), 1):.0f}%); went warm or accepted: "
              f"{sum(r.outcome in ('warm', 'accepted') for r in op)}",
              f"  houses with a written offer and a reply: {len(t2)}; warm or accepted after it: "
              f"{sum(r.outcome in ('warm', 'accepted') for r in t2)}", ""]
    lines.append("Warm and accepted, house by house:")
    for r in sorted((r for r in rows if r.outcome in ("accepted", "warm")), key=lambda r: (r.outcome, -(r.ratio or 0))):
        lines.append(f"  [{r.outcome}] #{r.lead_id} {r.street}, {r.town} ({r.county}): {_units(r.units)}, {r.condition}"
                     f"{', tenants' if r.tenants else ''}{', estate' if r.estate else ''}, "
                     f"DOM {r.dom if r.dom is not None else '?'}{', cut' if r.cut else ''}; ask "
                     f"{r.ask or 0:,.0f}, ours {(r.offer or r.final or 0):,.0f} ({_band(r.ratio)}); "
                     f"{'; '.join(r.why)}; agent {r.agent}")
    lines += ["", "Dead, house by house (latest reason):"]
    for r in sorted((r for r in rows if r.outcome == "dead"), key=lambda r: -(r.ratio or 0)):
        lines.append(f"  #{r.lead_id} {r.street}, {r.town}: {_units(r.units)}, {r.condition}, ask {r.ask or 0:,.0f}, "
                     f"ours {(r.offer or r.final or 0):,.0f} ({_band(r.ratio)}): {'; '.join(r.why)}")
    roi = stream_roi(session)
    lines += ["", "Firm streams (sent all-time / replies / interested; first touches only):"]
    for stream, kind, n, rep, hot in roi:
        lines.append(f"  {stream:<5}{kind:<14}{n:>5} sent{rep:>5} replies{hot:>4} interested")
    gone = dropped(session, rows, now)
    lines += ["", f"Dropped threads (agent wrote last, nothing sent since): {len(gone)}"] + [f"  {g}" for g in gone]
    from engine import lead_scoring
    clones = lead_scoring.rank(session, 15)
    lines += ["", "Top 15 clone deals not yet e-mailed (engine/lead_scoring.py: our offer at 78%+ of the ask, "
              "units / tenants / estate / days / cut / fixer / investor town):"]
    for i, c in enumerate(clones, 1):
        fa = c.facts
        lines.append(f"  {i:>2}. #{c.lead_id} {fa.get('street')}, {fa.get('town')} ({fa.get('county')}): score "
                     f"{c.score:.0f}; ask {fa.get('ask') or 0:,.0f}, our offer ~{fa.get('offer') or 0:,.0f} "
                     f"({(c.ratio or 0):.0%}); " + ", ".join(c.parts))
    f = funnel(session, now)
    lines += ["", "Pipeline now: " + ", ".join(f"{k} {v}" for k, v in f.items() if isinstance(v, (int, float)))]
    counts = {"audit_leads": len(rows), "audit_warm": by_out.get("warm", 0) + by_out.get("accepted", 0),
              "audit_dropped": len(gone), "audit_streams": len(roi), "audit_clones": len(clones)}
    return "\n".join(lines), counts


def mail(text: str, now: datetime, smtp_factory: Any = None, subject: str | None = None) -> bool:
    import os

    from engine.cloud_desk import _send_self_files
    user, pw = os.environ.get("SMTP_USER", "").strip(), os.environ.get("SMTP_APP_PASSWORD", "")
    if not user or not pw:
        return False
    _send_self_files(user, pw, subject or f"[njw-audit] {now:%Y-%m-%d %H:%M}", text, [], smtp_factory)
    return True


def _m(x: Any) -> str:
    return f"${float(x):,.0f}" if x else "?"


def brief(session: Session, lead: Lead) -> str:
    """One house's numbers for a call: the facts, the listing, our valuation, comps, the firm offer and the thread."""
    from engine import offer_math as om
    from engine.underwriter import asking_price
    p = lead.property
    v = p.current_valuation
    a = (v.assumptions_json or {}) if v else {}
    q = a.get("quant") or {}
    lst = om.listing_for(session, lead)
    ask = asking_price(p) or (lst.list_price if lst is not None else None)
    f = om.firm_offer(lead, session)
    lines = [f"{(p.street_address or '').title()}, {(p.city or p.municipality or '').title()} ({(p.county or '').title()})",
             f"  units {p.dwellings or 1}; beds {p.beds or (lst.beds if lst else None) or '?'}; baths "
             f"{p.baths or (lst.baths if lst else None) or '?'}; living area {p.sqft or (lst.sqft if lst else None) or '?'} "
             f"sq ft ({p.sqft_source or ('listing' if lst is not None and lst.sqft else 'not on record')}); built "
             f"{p.year_built or '?'}; lot {p.lot_size or '?'}; taxes {_m(p.annual_taxes)}/yr",
             f"  ask {_m(ask)}; {lst.days_on_market if lst is not None and lst.days_on_market is not None else '?'} days on "
             f"market{'; price cut' if lst is not None and lst.price_reduced else ''}",
             "", "Our numbers:",
             f"  after-repair value {_m(v.arv_estimate if v else None)}; as-is {_m(getattr(v, 'as_is_value', None))}; "
             f"repairs {_m(a.get('rehab_estimate') or q.get('rehab'))}; confidence {getattr(v, 'confidence_score', '?')}",
             f"  investor ceiling (buyer max) {_m(f.buyer_max)}; our offer {_m(f.offer or f.opening)}; best and final "
             f"{_m(f.final)} ({f.tier}{', ' + str(round((f.reach or 0) * 100)) + '% of ask' if f.reach else ''})",
             f"  written offer on record: {_m(lead.offer_amount)}"]
    if f.reasons:
        lines.append("  not a firm number yet because: " + "; ".join(f.reasons[:4]))
    comps = sorted((c for c in (v.comps_json or []) if c.get("sold_price")) if v else [],
                   key=lambda c: -float(c.get("time_adjusted_price") or c.get("sold_price") or 0))
    if comps:
        lines += ["", "Comparable sales:"]
        for c in comps[:8]:
            lines.append(f"  {c.get('address') or '?'}: {_m(c.get('sold_price'))} on {c.get('sold_date') or '?'}"
                         f"{', ' + str(c.get('sqft')) + ' sq ft' if c.get('sqft') else ''}"
                         f"{', ' + str(round(float(c.get('distance_mi')), 2)) + ' mi' if c.get('distance_mi') else ''}"
                         f"{', ' + str(c.get('bucket')) if c.get('bucket') else ''}")
    agent = next((c for c in lead.contacts if c.contact_type == "LISTING_AGENT"), None)
    if agent is not None:
        lines += ["", f"Listing agent: {agent.name or ''} {('(' + agent.company + ')') if agent.company else ''} "
                      f"{agent.email or ''} {agent.phone or ''}".rstrip()]
    reps = sorted((r for r in session.scalars(select(InboundReply).where(InboundReply.lead_id == lead.id))
                   if r.status not in NOT_REPLIES), key=lambda r: r.created_at or datetime.min)[-5:]
    if reps:
        lines += ["", "Their replies (newest last):"]
        for r in reps:
            lines.append(f"  {r.created_at:%b %d}: {', '.join(r.intents_json or []) or 'reply'}"
                         f"{' (their number ' + _m(r.counter_amount) + ')' if r.counter_amount else ''}; {r.headline or ''}")
    if lst is not None and lst.description:
        lines += ["", "Listing says: " + " ".join(lst.description.split())[:500]]
    return "\n".join(lines)
