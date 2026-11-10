"""Stream 2: the flat-fee AI Deal Desk, sold through the 2-step funnel (2026-11-09 pivot, 2026-11-10 funnel).

Two audiences, both reached only through public business e-mail:
- flippers: the buyer families whose business e-mail was found on their own sites (prospects table), each offered a
  one-page Weekly Deal Digest of deals in the towns they actually buy (engine/deal_desk.py);
- flipper-connected agents: the top listing agents by flip resales they list and fixer listings in towns where cash
  buyers buy (`deal_desk.flipper_agents`, top `agents_top`), each offered a digest of investor deals in the towns they
  list in, never their own listings. For an agent the product is a desk that drafts offers for their buyer clients;
  their clients are the buyers and the agent keeps their normal role and pay.

The funnel (operator, 2026-11-10):
- Touch 1 is two plain sentences with no attachment, no link and no subscription price: why them (what the deed
  records or their own listing counts show), one real street we underwrote this week with its public ask (a sheriff
  sale shows only its sale date: an upset is a defendant's debt figure), the true count of the other deals on the
  sheet, and "want the 1-page numbers sheet?". The street is never the agent's own listing, never a house the
  flipper owns, never a house we have an offer out on (no competing buyer is pointed at our own deal), never a
  pre-foreclosure and always clear of the Daniel's Law / stop suppressions.
- Their yes releases the held sample (`base.sample_path` of the digest: 2 rows in full, the street and listing agent
  hidden on the rest) with the flat price: $500 for the first 30 days, then $1,000 a month, no commission or per-deal
  fee (the operator is not licensed: N.J.S.A. 45:15-3).
- One follow-up after the outbox's gap is a one-sentence nudge, also without an attachment, in the same thread.
Each run pitches at most `pitches_per_run` targets, so digests are built only for the targets due a touch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import (
    Contact,
    ContactType,
    FirmOutbox,
    FirmTarget,
    Lead,
    Prospect,
    SignalType,
)
from engine import deal_desk as dd
from engine import firm_outbox as fo
from engine.streams.base import (
    PRIORITY_PITCH,
    SAMPLE_FULL_ROWS,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    address_ok,
    entity_like,
    nice_name,
    sample_path,
    slug,
)


def week_key(now: Any) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


_BROKERAGE = re.compile(r"(?i)\b(realty|realtors?|real estate|banker|group|team|llc|inc|properties|homes|keller|"
                        r"weichert|re/?max|century|sotheby|compass|exp|brokers?|associates|agency|office)\b")


_TRADE_NAME = re.compile(r"(?i)\b(?:home\s?buyers|homes|buyers|properties|property|realty|group|invest\w*|capital|"
                         r"holdings|sales|builders?|development|construction|management|partners|ventures|enterprises?|"
                         r"solutions|company)\b")


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


def greeting(name: str | None) -> str:
    """"Hi Pat," only when we really know a person's first name, else "Hello,"."""
    first = first_name(name) if name else "there"
    return f"Hi {first}," if first != "there" else "Hello,"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def join_and(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def agent_hook(t: FirmTarget, towns: str) -> str:
    """Why this agent, as one sentence: only what the listing data shows (their own counts), never "a lot of"."""
    data = t.data_json if isinstance(t.data_json, dict) else {}
    resales, fixers = int(data.get("resales") or 0), int(data.get("fixers") or 0)
    if resales and fixers:
        return (f"I saw your {_plural(fixers, 'fixer listing')} and {_plural(resales, 'investor resale')} around "
                f"{towns}.")
    if resales:
        return f"I saw you're listing {_plural(resales, 'investor resale')} around {towns}."
    if fixers:
        return f"I saw your {_plural(fixers, 'fixer listing')} around {towns}."
    return f"I saw your listings around {towns}."


# -- targets ------------------------------------------------------------------------------------------------------
def sync_flipper_prospects(session: Session) -> int:
    """Flipper prospects with a business e-mail become (or stay) firm targets (one row per address)."""
    n = 0
    for p in session.scalars(select(Prospect).where(Prospect.email.is_not(None),
                                                    Prospect.stage.notin_(("LOST", "PAYING")))):
        t = fo.upsert_target(session, kind="flipper", stream="s2", email=p.email, org=p.entity_name,
                             phone=p.phone, website=p.website, source=p.source,
                             score=float(p.purchases_36m or 0), data={"prospect_key": p.key,
                                                                     "family_key": p.family_key})
        n += int(t is not None)
    return n


def sync_targets(session: Session, agents_top: int, now: Any) -> dict[str, int]:
    """Flipper prospects with a business e-mail and the top flipper-connected agents become firm targets."""
    n = {"flippers": sync_flipper_prospects(session), "agents": 0}
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


def queued_targets(session: Session) -> set[int]:
    """Targets with an e-mail already waiting in the outbox (any stream): a second one would only be skipped there."""
    return {tid for (tid,) in session.execute(select(FirmOutbox.target_id).where(
        FirmOutbox.status == "queued", FirmOutbox.target_id.is_not(None)).distinct())}


def buyer_note_targets(session: Session, now: Any, days: float) -> set[int]:
    """Targets that got stream 1's buy-box note in the last `days` (the held-sample window): their yes belongs to that
    note, so no software pitch (and no held sample a yes would release) goes to them meanwhile."""
    since = now - timedelta(days=days)
    return {tid for (tid,) in session.execute(select(FirmOutbox.target_id).where(
        FirmOutbox.key.like("s1:buyers:%"), FirmOutbox.status == "sent", FirmOutbox.sent_at >= since,
        FirmOutbox.target_id.is_not(None)).distinct())}


def due_targets(session: Session, now: Any, limit: int, gap_days: float, max_touches: int) -> list[FirmTarget]:
    """Best targets owed a touch (never pitched, or one follow-up after the gap), flippers first, then by score;
    never an agent we are in deal talks with, never a target with an e-mail already queued or in stream 1's
    buy-box conversation."""
    cutoff = now - timedelta(days=gap_days)
    rows = session.scalars(select(FirmTarget).where(
        FirmTarget.stream == "s2", FirmTarget.email.is_not(None), FirmTarget.stage.in_(("NEW", "CONTACTED")))).all()
    talks = in_deal_talks(session, now)
    busy = queued_targets(session) | buyer_note_targets(session, now, float(fo.cfg().get("sample_hold_days", 30)))
    # Touch 1 only (the outbox sends the in-thread nudges): never pitched, or a finished sequence that has rested.
    ok = [t for t in rows if fo.pitch_ok(t, now) and (t.last_contact_at is None or t.last_contact_at <= cutoff)
          and t.id not in busy
          and not (t.kind == "investor_agent" and (t.email or "").lower() in talks)]
    ok.sort(key=lambda t: (t.kind != "flipper", int(t.touches or 0), -(t.score or 0)))
    return ok[:limit]


def last_pitch(session: Session, t: FirmTarget) -> FirmOutbox | None:
    """This stream's Touch 1 to the target, once it went out (a touch another stream used doesn't count)."""
    return session.scalar(select(FirmOutbox).where(FirmOutbox.target_id == t.id, FirmOutbox.status == "sent",
                                                   FirmOutbox.key.like(f"s2:pitch:{t.id}:%"))
                          .order_by(FirmOutbox.id.desc()).limit(1))


# -- digests ------------------------------------------------------------------------------------------------------
def flipper_profile(session: Session, t: FirmTarget, profiles: dict[str, dd.BuyerProfile]) -> dd.BuyerProfile | None:
    fam = (t.data_json or {}).get("family_key")
    if fam and fam in profiles:
        return profiles[fam]
    key = dd.prospect_key(t.org or "")
    return next((p for p in profiles.values() if dd.prospect_key(p.entity) == key), None)


def owned_leads(session: Session, prof: dd.BuyerProfile) -> set[int]:
    """Leads on parcels the buyer family bought (their own resales): never in their own digest or Touch 1."""
    from db.models import CashBuyer, Property
    parcels: set[tuple[str, str, str]] = set()
    for b in session.scalars(select(CashBuyer).where(CashBuyer.id.in_(prof.buyer_ids or [-1]))):
        for x in b.recent_purchases_json or []:
            if isinstance(x, dict) and x.get("mun_code") and x.get("block") and x.get("lot"):
                parcels.add((str(x["mun_code"]), dd._num(x["block"]), dd._num(x["lot"])))
    if not parcels:
        return set()
    codes = sorted({m for m, _, _ in parcels})
    return {lid for lid, m, blk, lot in session.execute(
        select(Lead.id, Property.mun_code, Property.block, Property.lot).join(Property, Lead.property_id == Property.id)
        .where(Property.mun_code.in_(codes))) if (str(m), dd._num(blk), dd._num(lot)) in parcels}


def agent_profile(session: Session, t: FirmTarget) -> tuple[dd.BuyerProfile | None, set[int]]:
    """Towns, counties and price band from the agent's own live listings. Every lead the agent lists (live or not) is
    returned as `own`: those are left out of their digest and never named to them."""
    from core.text import muni_base, postal_title
    from engine.scoring import listing_off_market
    from engine.underwriter import asking_price
    towns: dict[tuple[str, str, str], int] = {}
    prices: list[float] = []
    own: set[int] = set()
    for c in session.scalars(select(Contact).where(Contact.contact_type == ContactType.LISTING_AGENT,
                                                   Contact.email.is_not(None), Contact.lead_id.is_not(None))):
        if (c.email or "").lower() != (t.email or "").lower():
            continue
        own.add(c.lead_id)
        lead = session.get(Lead, c.lead_id)
        if lead is None or lead.property is None or listing_off_market(lead.property):
            continue
        p = lead.property
        k = (str(p.mun_code or ""), postal_title(muni_base(p.municipality)) or "", (p.county or "").title())
        towns[k] = towns.get(k, 0) + 1
        a = asking_price(p)
        if a:
            prices.append(float(a))
    if not towns:
        return None, own
    ranked = sorted(towns.items(), key=lambda kv: -kv[1])
    prof = dd.BuyerProfile(key=f"agent:{t.id}", buyer_ids=[],
                           entity=" ".join(x for x in (t.name, f"({t.org})" if t.org else "") if x) or (t.org or "you"),
                           principal=None, kind="agent", purchases_36m=sum(towns.values()), flips=0,
                           towns=[(m, tw, co, n) for (m, tw, co), n in ranked],
                           counties=sorted({co for (_, _, co) in towns if co}),
                           price_lo=min(prices) if prices else None, price_med=median(prices) if prices else None,
                           price_hi=max(prices) if prices else None, multi_share=0.3, last_purchase=None)
    return prof, own


# -- Touch 1 ------------------------------------------------------------------------------------------------------
@dataclass
class TouchFacts:
    """Everything Touch 1 states, each from the digest we just built (true counts, a public-record street)."""
    street: str            # the one named address ("12 Test St")
    price: str             # "$389,900 ask" / "sheriff sale 11/24"
    others: int            # the other deals on the sheet
    mix: str               # "fixer listings and sheriff sales"
    where: str             # "in Union County" / "in your towns" / "around your towns"
    sheet_for: str         # "Union County" / "your towns" (the follow-up)
    near: str              # the prospect's own towns ("Elizabeth and Linden")
    subject_where: str     # "in Union County" / "near Elizabeth"
    rows: int              # rows on the sheet


def deal_mix(rows: list[dd.DigestRow]) -> str:
    """What the other rows are, in plain words that stay true: "fixer listings" only when every listing reads as a
    fixer, "stale listings" when none does."""
    mls = [r for r in rows if r.kind.startswith("MLS")]
    fixers = sum(1 for r in mls if r.kind == "MLS fixer")
    parts = []
    if mls:
        parts.append("fixer listings" if fixers == len(mls) else "stale listings" if not fixers
                     else "stale and fixer listings")
    if any(r.kind.startswith("Sheriff") for r in rows):
        parts.append("sheriff sales")
    if any(r.kind.startswith("Pre-foreclosure") for r in rows):
        parts.append("pre-foreclosures")
    return join_and(parts) or "deals"


def county_phrase(counties: list[str]) -> str:
    cs = [c for c in counties if c]
    if len(cs) == 1:
        return f"{cs[0]} County"
    return f"{join_and(cs)} counties" if cs else ""


def touch_facts(session: Session, t: FirmTarget, d: dd.Digest, avoid: set[int]) -> TouchFacts | None:
    """The street and counts Touch 1 names, or None when no row may be named (then no touch this run)."""
    ids = [r.lead_id for r in d.rows]
    leads = {ld.id: ld for ld in session.scalars(select(Lead).where(Lead.id.in_(ids or [-1])))}
    props = {lid: ld.property for lid, ld in leads.items() if ld.property is not None}
    named: dd.DigestRow | None = None
    for group in ("MLS", "Sheriff sale"):                 # a public ask first; never a pre-foreclosure
        for r in d.rows:
            ld = leads.get(r.lead_id)
            if (not r.kind.startswith(group) or ld is None or r.lead_id not in props or r.lead_id in avoid
                    or ld.offer_amount or (group == "MLS" and not r.price_point)
                    or not address_ok(session, props[r.lead_id])):
                continue
            named = r
            break
        if named is not None:
            break
    if named is None:
        return None
    others = [r for r in d.rows if r is not named]
    counties = sorted({(props[r.lead_id].county or "").title() for r in d.rows
                       if r.lead_id in props and props[r.lead_id].county})
    in_towns = all(props[r.lead_id].mun_code in d.profile.mun_codes for r in d.rows if r.lead_id in props)
    town_names = [tw for _, tw, _, _ in d.profile.towns if tw]
    agent = t.kind == "investor_agent"
    near = join_and(town_names[:2] if agent else town_names[:1]) or "your towns"
    if not agent and in_towns:
        where = "in your towns"
    elif 0 < len(counties) <= 2:
        where = f"in {county_phrase(counties)}"
    else:
        where = "around your towns" if agent else "in and around your towns"
    if named.kind.startswith("MLS"):
        price = f"${named.price_point:,.0f} ask"
    else:
        sig = next((s for s in props[named.lead_id].active_signals if s.signal_type == SignalType.SHERIFF_SALE), None)
        when = getattr(sig, "scheduled_auction_date", None)
        price = f"sheriff sale {when:%m/%d}" if when else "sheriff sale"
    if len(counties) == 1:
        subject_where = f"in {county_phrase(counties)}"
    else:
        subject_where = f"near {town_names[0]}" if town_names else "this week"
    return TouchFacts(street=re.sub(r"\.", "", named.street).strip(), price=price, others=len(others),
                      mix=deal_mix(others), where=where,
                      sheet_for=county_phrase(counties) if 0 < len(counties) <= 2 else "your towns", near=near,
                      subject_where=subject_where, rows=len(d.rows))


def pitch_body(t: FirmTarget, f: TouchFacts, op: dict[str, str], follow: bool) -> tuple[str, str]:
    """Touch 1 (two sentences; the only dollar figure is the named street's public ask) or the one-sentence nudge."""
    hello = greeting(t.name) if t.kind == "investor_agent" else "Hello,"
    subject = f"{f.street} + {f.others} more {f.subject_where}"
    if follow:
        text = f"Should I send over this week's 1-page numbers sheet for {f.sheet_for}?"
    elif t.kind == "investor_agent":
        text = (f"{agent_hook(t, f.near)} We just underwrote {f.street} ({f.price}) and {f.others} other {f.mix} "
                f"{f.where} this week; want the 1-page numbers sheet?")
    else:
        # a company name only: a buyer family filed under a person ("SOINE, TRAVIS", 2026-11-09) is "you", never
        # named back to them
        org = nice_name(t.org) if t.org and (entity_like(t.org) or _TRADE_NAME.search(t.org)) else ""
        who = f"{org} has" if org else "you've"
        text = (f"I saw in the public deed records that {who} been buying around {f.near}. We just underwrote "
                f"{f.street} ({f.price}) plus {f.others} other {f.mix} {f.where} this week; want the 1-page numbers "
                "sheet?")
    return subject, "\n".join([hello, "", text, "", op.get("first") or ""]).strip()


def sample_body(t: FirmTarget, rows: int, lim: dict[str, Any], op: dict[str, str]) -> str:
    """Touch 2, sent by the outbox in their thread only after their yes: what's attached and the flat price."""
    trial = int(lim.get("price_trial") or dd.TRIAL_FEE)
    monthly = int(lim.get("price_monthly") or dd.MONTHLY_FEE)
    agent = t.kind == "investor_agent"
    approve = "for your buyer clients to approve" if agent else "for you to approve"
    text = (f"Here's the 1-page numbers sheet (attached): the first {SAMPLE_FULL_ROWS} deals in full, the other "
            f"{max(0, rows - SAMPLE_FULL_ROWS)} with the street hidden. The full sheet every week, plus offers drafted "
            f"from your own inbox {approve}, is ${trial:,} for the first 30 days, then ${monthly:,} a month flat, no "
            "commission or per-deal fee; reply \"start\" and I'll set it up.")
    hello = greeting(t.name) if agent else "Hello,"
    return "\n".join([hello, "", text, "", op.get("first") or ""]).strip()


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
                prof = flipper_profile(session, t, profiles)
                own = owned_leads(session, prof) if prof is not None else set()
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
        no_street = 0
        for t, prof, own in plans:
            mine = {lid: r for lid, r in pool.items() if lid not in own}
            d = dd.digest_for(session, prof, mine, ctx.now.date())
            if len(d.rows) < dd.MIN_ROWS:
                continue
            facts = touch_facts(session, t, d, own)
            if facts is None:
                no_street += 1                            # nothing on the sheet we may name: no touch this run
                continue
            res.found += 1
            path = dd.render_pdf(d, ctx.out_dir / f"Deal_Digest_{slug(t.org or t.name or 'investor')}.pdf",
                                 ctx.operator, audience="agent" if t.kind == "investor_agent" else "buyer")
            res.products += 1
            sample = sample_path(path)
            prev = last_pitch(session, t)
            follow = prev is not None
            subject, body = pitch_body(t, facts, ctx.operator, follow)
            reply_to = None
            if follow and prev is not None and prev.message_id:
                reply_to = prev.message_id                # the nudge goes in the thread of the first note
                subject = "Re: " + re.sub(r"(?i)^\s*re:\s*", "", prev.subject or subject)
            res.outbound.append(Outbound(stream=self.key, key=f"s2:{'follow' if follow else 'pitch'}:{t.id}:{wk}",
                                         to=t.email or "", subject=subject, body=body, priority=PRIORITY_PITCH,
                                         kind="followup" if follow else "pitch", attachments=[], target_id=t.id,
                                         in_reply_to=reply_to, commercial=True,
                                         sample_body=sample_body(t, len(d.rows), lim, ctx.operator),
                                         sample_attachments=[sample if sample.exists() else path]))
        res.metrics.update(pitches=sum(1 for o in res.outbound if o.kind == "pitch"),
                           followups=sum(1 for o in res.outbound if o.kind == "followup"))
        if no_street:
            res.metrics["no_nameable_street"] = no_street
        return res


STREAM = DealDeskSaaSStream()
