"""Stream 2: the flat-fee AI Deal Desk, pitched with a deal digest built for each prospect (2026-11-09 pivot).

Two audiences, both reached only through public business e-mail:
- flippers: the buyer families whose business e-mail was found on their own sites (prospects table), each pitched
  with a one-page Weekly Deal Digest of deals in the towns they actually buy (engine/deal_desk.py);
- flipper-connected agents: the top listing agents by flip resales they list and fixer listings in towns where cash
  buyers buy (`deal_desk.flipper_agents`, top `agents_top` = 150), each pitched with a digest of investor deals in
  the towns they list in, never their own listings. For an agent the product is a desk that drafts offers for their
  buyer clients; their clients are the buyers and the agent keeps their normal role and pay.
Offer: $500 for the first 30 days, then $1,000 a month flat; zero commission, zero per-deal pay (the operator is not
licensed: N.J.S.A. 45:15-3). Each run pitches at most `pitches_per_run` prospects, so the digests are built only for
the targets due a pitch; the outbox spaces pitches (one per target per 6 days, two cold touches in all).
"""
from __future__ import annotations

import re
from datetime import timedelta
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Contact, ContactType, FirmTarget, Lead, Prospect
from engine import deal_desk as dd
from engine import firm_outbox as fo
from engine.streams.base import PRIORITY_PITCH, Outbound, Stream, StreamContext, StreamResult, nice_name, slug


def week_key(now: Any) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


_BROKERAGE = re.compile(r"(?i)\b(realty|realtors?|real estate|banker|group|team|llc|inc|properties|homes|keller|"
                        r"weichert|re/?max|century|sotheby|compass|exp|brokers?|associates|agency|office)\b")


def first_name(name: str | None) -> str:
    """A greeting name only when the listing's agent name looks like a person ("Pat Agent" -> "Pat"); a brokerage in
    the name field ("Coldwell Banker") or a lone token gets "there". Initials keep their case ("JC")."""
    raw = (name or "").strip()
    parts = [p for p in re.split(r"[\s,]+", raw) if p]
    if len(parts) < 2 or _BROKERAGE.search(raw) or not parts[0].replace(".", "").isalpha():
        return "there"
    w = parts[0].replace(".", "")
    if len(w) < 2:
        return "there"
    if (len(w) <= 3 and w.isupper()) or (len(w) == 2 and not re.search(r"(?i)[aeiouy]", w)):
        return w.upper()                                         # "JC", "Jc" -> "JC"; "Al", "Ed" stay names
    return w.title()


def agent_hook(t: FirmTarget, towns: str) -> str:
    """Why this agent: only what the listing data shows (their counts), never "a lot of"."""
    data = t.data_json if isinstance(t.data_json, dict) else {}
    resales, fixers = int(data.get("resales") or 0), int(data.get("fixers") or 0)
    plural = lambda n, w: f"{n} {w}{'s' if n != 1 else ''}"     # noqa: E731
    if resales and fixers:
        return (f"You have {plural(fixers, 'fixer listing')} and {plural(resales, 'investor resale')} around {towns}, "
                "so")
    if resales:
        return f"You're listing {plural(resales, 'investor resale')} around {towns}, so"
    if fixers:
        return f"You have {plural(fixers, 'fixer listing')} around {towns}, so"
    return f"You list around {towns}, so"


# -- targets ------------------------------------------------------------------------------------------------------
def sync_targets(session: Session, agents_top: int, now: Any) -> dict[str, int]:
    """Flipper prospects with a business e-mail and the top flipper-connected agents become firm targets."""
    n = {"flippers": 0, "agents": 0}
    for p in session.scalars(select(Prospect).where(Prospect.email.is_not(None),
                                                    Prospect.stage.notin_(("LOST", "PAYING")))):
        t = fo.upsert_target(session, kind="flipper", stream="s2", email=p.email, org=p.entity_name,
                             phone=p.phone, website=p.website, source=p.source,
                             score=float(p.purchases_36m or 0), data={"prospect_key": p.key,
                                                                     "family_key": p.family_key})
        n["flippers"] += int(t is not None)
    for rank, a in enumerate(dd.flipper_agents(session, agents_top, today=now.date()), 1):
        t = fo.upsert_target(session, kind="investor_agent", stream="s2", email=a["email"], org=a["company"] or None,
                             name=a["name"] or None, phone=a["phone"] or None, source="listing data (MLS)",
                             score=float(a["score"]), data={"rank": rank, "towns": a["towns"], "resales": a["resales"],
                                                           "fixers": a["fixers"]})
        n["agents"] += int(t is not None)
    return n


def in_deal_talks(session: Session, now: Any, days: int = 30) -> set[str]:
    """Agents we are negotiating a house with (they wrote to us, or we e-mailed them about a lead, in the last month):
    no software pitch in the middle of a deal conversation."""
    from db.models import InboundReply, Interaction
    since = now - timedelta(days=days)
    out = {(e or "").lower() for (e,) in session.execute(select(InboundReply.from_email).where(
        InboundReply.created_at >= since, InboundReply.from_email.is_not(None)))}
    out |= {(e or "").lower() for (e,) in session.execute(
        select(Contact.email).join(Interaction, Interaction.contact_id == Contact.id).where(
            Interaction.occurred_at >= since, Interaction.channel.like("email%"), Contact.email.is_not(None)))}
    return {e for e in out if e}


def due_targets(session: Session, now: Any, limit: int, gap_days: float, max_touches: int) -> list[FirmTarget]:
    """Best targets owed a pitch (never pitched, or one follow-up after the gap), flippers first, then by score;
    never an agent we are in deal talks with."""
    cutoff = now - timedelta(days=gap_days)
    rows = session.scalars(select(FirmTarget).where(
        FirmTarget.stream == "s2", FirmTarget.email.is_not(None), FirmTarget.stage.in_(("NEW", "CONTACTED")),
        FirmTarget.touches < max_touches)).all()
    talks = in_deal_talks(session, now)
    ok = [t for t in rows if (t.last_contact_at is None or t.last_contact_at <= cutoff)
          and not (t.kind == "investor_agent" and (t.email or "").lower() in talks)]
    ok.sort(key=lambda t: (t.kind != "flipper", int(t.touches or 0), -(t.score or 0)))
    return ok[:limit]


# -- digests ------------------------------------------------------------------------------------------------------
def flipper_profile(session: Session, t: FirmTarget, profiles: dict[str, dd.BuyerProfile]) -> dd.BuyerProfile | None:
    fam = (t.data_json or {}).get("family_key")
    if fam and fam in profiles:
        return profiles[fam]
    key = dd.prospect_key(t.org or "")
    return next((p for p in profiles.values() if dd.prospect_key(p.entity) == key), None)


def agent_profile(session: Session, t: FirmTarget) -> tuple[dd.BuyerProfile | None, set[int]]:
    """Towns, counties and price band from the agent's own live listings (those leads are left out of their digest)."""
    from engine.scoring import listing_off_market
    from engine.underwriter import asking_price
    from core.text import muni_base, postal_title
    towns: dict[tuple[str, str, str], int] = {}
    prices: list[float] = []
    own: set[int] = set()
    for c in session.scalars(select(Contact).where(Contact.contact_type == ContactType.LISTING_AGENT,
                                                   Contact.email.is_not(None), Contact.lead_id.is_not(None))):
        if (c.email or "").lower() != (t.email or "").lower():
            continue
        lead = session.get(Lead, c.lead_id)
        if lead is None or lead.property is None or listing_off_market(lead.property):
            continue
        p = lead.property
        own.add(lead.id)
        k = (str(p.mun_code or ""), postal_title(muni_base(p.municipality)) or "", (p.county or "").title())
        towns[k] = towns.get(k, 0) + 1
        a = asking_price(p)
        if a:
            prices.append(float(a))
    if not towns:
        return None, own
    ranked = sorted(towns.items(), key=lambda kv: -kv[1])
    prof = dd.BuyerProfile(key=f"agent:{t.id}", buyer_ids=[], entity=" ".join(x for x in (t.name, f"({t.org})" if t.org else "") if x) or (t.org or "you"),
                           principal=None, kind="agent", purchases_36m=len(own), flips=0,
                           towns=[(m, tw, co, n) for (m, tw, co), n in ranked], counties=sorted({co for (_, _, co) in towns if co}),
                           price_lo=min(prices) if prices else None, price_med=median(prices) if prices else None,
                           price_hi=max(prices) if prices else None, multi_share=0.3, last_purchase=None)
    return prof, own


def pitch_body(t: FirmTarget, d: dd.Digest, op: dict[str, str], follow: bool) -> tuple[str, str]:
    towns = ", ".join(tw for _, tw, _, _ in d.profile.towns[:3] if tw) or "your towns"
    top = d.profile.towns[0][1] if d.profile.towns and d.profile.towns[0][1] else "your towns"
    name = first_name(t.name) if t.kind == "investor_agent" else "there"
    sign = op.get("first") or ""
    if t.kind == "investor_agent":
        subject = f"Investor deals this week in {top}"
        lines = [f"Hi {name},",
                 "",
                 (f"Following up with this week's version: {len(d.rows)} investor deals in {towns}, one page, attached. "
                  if follow else
                  f"{agent_hook(t, towns)} I put together this week's investor deals there: {len(d.rows)} on one "
                  "page, attached, with ARV, repairs and the most a flipper can pay. ")
                 + "I run this as software for investor-focused agents: deals found and underwritten daily, offers "
                 "drafted from your own inbox for your buyer clients to approve. The first 30 days are $500 flat, then "
                 "$1,000 a month, with no commission split and nothing tied to a deal.",
                 "",
                 sign]
    else:
        org = nice_name(t.org) if t.org else ""
        subject = f"Deals this week in {top} for {org}" if org else f"Deals this week in {top}"
        lines = [f"Hi {name},",
                 "",
                 (f"Following up with this week's deals in {towns}, one page, attached. " if follow else
                  f"I put together this week's deals in the towns {org or 'you'} has been buying: {len(d.rows)} on one "
                  "page, attached, with ARV, repairs and the most a flipper can pay. ")
                 + "I run this as software: it finds and underwrites deals daily and drafts your offers from your own "
                 "inbox, and you approve every one. If it's useful, the first 30 days are $500 flat, then $1,000 a "
                 "month, with no commission or per-deal fee.",
                 "",
                 sign]
    return subject, "\n".join(lines).strip()


class DealDeskSaaSStream(Stream):
    key = "s2"
    name = "AI Deal Desk SaaS"
    priority = PRIORITY_PITCH
    cadence_hours = 1.0
    run_hours = (9, 16)

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        res = StreamResult(stream=self.key)
        lim = ctx.limits
        if ctx.now.weekday() >= 5:
            res.notes.append("weekend: no pitches")
            return res
        st = fo.cfg()
        synced = sync_targets(session, int(lim.get("agents_top", 150)), ctx.now)
        res.metrics.update(targets_flippers=synced["flippers"], targets_agents=synced["agents"])
        targets = due_targets(session, ctx.now, int(lim.get("pitches_per_run", 4)),
                              float(st.get("pitch_gap_days", 6)), int(st.get("max_touches", 2)))
        res.processed = len(targets)
        if not targets or not ctx.send:
            return res
        profiles = {p.key: p for p in dd.buyer_profiles(session, ctx.now.date())}
        plans: list[tuple[FirmTarget, dd.BuyerProfile, set[int]]] = []
        for t in targets:
            if t.kind == "flipper":
                prof, own = flipper_profile(session, t, profiles), set()
            else:
                prof, own = agent_profile(session, t)
            if prof is not None:
                plans.append((t, prof, own))
        if not plans:
            res.notes.append("no buy box for the due targets")
            return res
        codes = set().union(*(p.mun_codes for _, p, _ in plans))
        counties = set().union(*(set(p.counties) for _, p, _ in plans))
        pool = dd.candidate_pool(session, codes, counties, ctx.now.date())
        wk = week_key(ctx.now)
        for t, prof, own in plans:
            mine = {lid: r for lid, r in pool.items() if lid not in own}
            d = dd.digest_for(session, prof, mine, ctx.now.date())
            if len(d.rows) < dd.MIN_ROWS:
                continue
            res.found += 1
            path = dd.render_pdf(d, ctx.out_dir / f"Deal_Digest_{slug(t.org or t.name or 'investor')}.pdf",
                                 ctx.operator, audience="agent" if t.kind == "investor_agent" else "buyer")
            res.products += 1
            follow = int(t.touches or 0) > 0
            subject, body = pitch_body(t, d, ctx.operator, follow)
            res.outbound.append(Outbound(stream=self.key, key=f"s2:{'follow' if follow else 'pitch'}:{t.id}:{wk}",
                                         to=t.email or "", subject=subject, body=body, priority=PRIORITY_PITCH,
                                         kind="followup" if follow else "pitch", attachments=[path], target_id=t.id))
        res.metrics.update(pitches=len(res.outbound))
        return res


STREAM = DealDeskSaaSStream()
