"""Stream 1: acquisitions from listing agents (the original desk, now one stream of ten).

The desk's own machinery does the work on every run (gmail_autopilot: read replies, answer fact checks and call /
proof-of-funds asks in writing, send firm offers within offer_math's numbers). This stream adds the firm-level view
and the two halves of getting ready to sell a contract the day a seller signs:

- internal: every lead with a live agent conversation is matched to the recorded cash buyers most likely to buy it
  (buyer_matcher; stored as buyer_matches), so when an agent accepts, the operator already knows who to call;
- "buyers ready" (operator, 2026-11-10): a buy-box intake to cash buyers and investor agents / brokers in the counties
  where our written offers are live. We do NOT have those houses under contract, so nothing about any one house is
  marketed: no address, no price of a house, no spread (N.J.S.A. 45:15-3: offering another's property for a fee is
  brokerage; selling an equitable interest we don't hold yet is daisy-chaining). The note states only the aggregate
  (how many, which counties, a rounded price band across all live offers) and asks what they buy, so the first house
  can go to them once it is under contract and its attorney review has ended (CLAUDE.md: remote disposition). Nothing
  is attached and nothing goes out on a yes: their answer lands as a firm reply for the operator.

Targets: firm targets of kind flipper / investor_agent / investor_friendly_broker (prospects with a business e-mail
become flipper targets) whose stated county, town or own records overlap those counties; at most `buyers_per_run` per
run, one note per target per `buyers_gap_days` (14), `buyers_max` (2) in all, under the outbox's own cold-touch limits.
Never an agent we are negotiating a house with. Counts only in the metrics.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from db.models import (
    BuyerMatch,
    CashBuyer,
    Contact,
    ContactType,
    FirmOutbox,
    FirmTarget,
    InboundReply,
    Interaction,
    Lead,
    Property,
)
from engine.streams.base import (
    PRIORITY_PITCH,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
)
from engine.streams.s2_deal_desk_saas import (
    greeting,
    in_deal_talks,
    queued_targets,
    sync_flipper_prospects,
    week_key,
)

CLOSED = ("DEAD", "DO_NOT_CONTACT", "CLOSED")
NOT_LIVE = CLOSED + ("UNDER_CONTRACT", "ASSIGNED")       # a signed contract goes through disposition, not this note
BUYER_KINDS = ("flipper", "investor_agent", "investor_friendly_broker")
BAND_STEP = 25_000


def warm_leads(session: Session, since_days: int = 21, now=None) -> list[Lead]:
    """Leads whose listing agent wrote back in the last few weeks (a conversation, not a cold row)."""
    since = (now or datetime.now()) - timedelta(days=since_days)
    ids = {lid for (lid,) in session.execute(select(InboundReply.lead_id).where(
        InboundReply.lead_id.is_not(None), InboundReply.created_at >= since,
        InboundReply.status.notin_(("bounce", "auto_reply"))).distinct())}
    return [ld for ld in session.scalars(select(Lead).where(Lead.id.in_(ids))) if ld.status_stage not in CLOSED]


# -- live offers --------------------------------------------------------------------------------------------------
def live_offers(session: Session, now: datetime, days: int = 21) -> list[Lead]:
    """Leads with a written offer out (`offer_amount`) and an agent conversation in the last `days` (they wrote back,
    or our offer e-mail went out), not dead / closed / under contract, on a listing that is still for sale."""
    from engine.scoring import listing_off_market
    since = now - timedelta(days=days)
    ids = {lid for (lid,) in session.execute(select(InboundReply.lead_id).where(
        InboundReply.lead_id.is_not(None), InboundReply.created_at >= since,
        InboundReply.status.notin_(("bounce", "auto_reply"))).distinct())}
    ids |= {lid for (lid,) in session.execute(select(Interaction.lead_id).where(
        Interaction.outcome == "OFFER_SENT_TO_AGENT", Interaction.occurred_at >= since).distinct())}
    if not ids:
        return []
    out = []
    for ld in session.scalars(select(Lead).where(Lead.id.in_(ids), Lead.offer_amount.is_not(None),
                                                 Lead.offer_amount > 0)):
        if ld.status_stage in NOT_LIVE or ld.property is None or listing_off_market(ld.property):
            continue
        out.append(ld)
    return out


def _k(v: int) -> str:
    """$25K steps: "$275K", "$1.025M", "$1M"."""
    if v >= 1_000_000:
        return "$" + f"{v / 1_000_000:.3f}".rstrip("0").rstrip(".") + "M"
    return f"${v // 1000:,}K"


@dataclass
class OfferBook:
    """The only thing the note says about our live offers: how many, where (counties), and a rounded band."""
    count: int
    counties: list[str]                 # most offers first
    lo: int
    hi: int

    @property
    def band(self) -> str:
        return f"{_k(self.lo)}-{_k(self.hi)}"

    @property
    def where(self) -> str:
        return county_phrase(self.counties)


def county_phrase(cs: list[str]) -> str:
    """"Union County" / "Union and Essex counties" / "Monmouth, Union and Essex counties"."""
    cs = [c for c in cs if c]
    if not cs:
        return ""
    if len(cs) == 1:
        return f"{cs[0]} County"
    if len(cs) > 4:
        return f"{', '.join(cs[:3])} and {len(cs) - 3} other counties"
    return f"{', '.join(cs[:-1])} and {cs[-1]} counties"


def offer_book(leads: list[Lead]) -> OfferBook | None:
    """Counties and a price band rounded outward to $25K (at least $50K wide), so no one offer can be read off it."""
    prices = [float(ld.offer_amount) for ld in leads if ld.offer_amount]
    counties = Counter((ld.property.county or "").strip().title() for ld in leads
                       if ld.property is not None and (ld.property.county or "").strip())
    if not prices or not counties:
        return None
    lo = int(min(prices) // BAND_STEP * BAND_STEP)
    hi = int(-(-max(prices) // BAND_STEP) * BAND_STEP)
    hi = max(hi, lo + 2 * BAND_STEP)
    return OfferBook(count=len(leads), counties=sorted(counties, key=lambda c: (-counties[c], c)), lo=lo, hi=hi)


# -- buyer targets ------------------------------------------------------------------------------------------------
def _town_county(session: Session, town: str, cache: dict[str, str | None]) -> str | None:
    """The county our parcel records put a town in (a stated town with no stated county)."""
    key = (town or "").strip().lower()
    if not key:
        return None
    if key not in cache:
        row = session.execute(select(Property.county, func.count(Property.id)).where(
            func.lower(Property.city) == key, Property.county.is_not(None)).group_by(Property.county)
            .order_by(func.count(Property.id).desc()).limit(1)).first()
        cache[key] = row[0].strip().title() if row and row[0] else None
    return cache[key]


def target_counties(session: Session, targets: list[FirmTarget], now: datetime) -> dict[int, set[str]]:
    """Where each target buys or lists: the county / town it states, an agent's own listings, a flipper family's deeds."""
    out: dict[int, set[str]] = {t.id: set() for t in targets}
    cache: dict[str, str | None] = {}
    for t in targets:
        if (t.county or "").strip():
            out[t.id].add(t.county.strip().title())
        if t.town and (c := _town_county(session, t.town, cache)):
            out[t.id].add(c)
    agents = {(t.email or "").lower(): t.id for t in targets if t.kind == "investor_agent" and t.email}
    if agents:
        for email, county in session.execute(
                select(func.lower(Contact.email), Property.county).join(Lead, Contact.lead_id == Lead.id)
                .join(Property, Lead.property_id == Property.id)
                .where(Contact.contact_type == ContactType.LISTING_AGENT, func.lower(Contact.email).in_(list(agents)),
                       Property.county.is_not(None)).distinct()):
            if email in agents and county:
                out[agents[email]].add(county.strip().title())
    flippers = [t for t in targets if t.kind == "flipper"]
    if flippers:
        from engine import deal_desk as dd
        profiles = dd.buyer_profiles(session, now.date())
        by_key = {p.key: p for p in profiles}
        by_name = {dd.prospect_key(p.entity): p for p in profiles}
        for t in flippers:
            p = by_key.get((t.data_json or {}).get("family_key") or "") or by_name.get(dd.prospect_key(t.org or ""))
            if p is not None:
                out[t.id] |= {c for c in p.counties if c}
    return out


def blast_history(session: Session) -> dict[int, list[FirmOutbox]]:
    """This note's rows per target that count against its limits (queued or sent), newest last."""
    hist: dict[int, list[FirmOutbox]] = {}
    for row in session.scalars(select(FirmOutbox).where(FirmOutbox.key.like("s1:buyers:%"),
                                                        FirmOutbox.status.in_(("queued", "sent")))
                               .order_by(FirmOutbox.id)):
        if row.target_id:
            hist.setdefault(row.target_id, []).append(row)
    return hist


def buyer_targets(session: Session, book: OfferBook, offers: list[Lead], now: datetime, *, gap_days: float = 14,
                  max_notes: int = 2, pitch_gap_days: float = 6, max_touches: int = 2
                  ) -> tuple[list[FirmTarget], dict[int, list[FirmOutbox]]]:
    """Every target owed this note now, best first (cash buyers, then investor agents, then brokers): its area overlaps
    a county with a live offer, it isn't closed or out of cold touches, nothing is queued for it, it wasn't pitched in
    the outbox's gap, this note went to it fewer than `max_notes` times and not in `gap_days`, and it isn't an agent
    we are negotiating a house with."""
    pitch_cut = now - timedelta(days=pitch_gap_days)
    note_cut = now - timedelta(days=gap_days)
    rows = session.scalars(select(FirmTarget).where(
        FirmTarget.kind.in_(BUYER_KINDS), FirmTarget.email.is_not(None), FirmTarget.stage.in_(("NEW", "CONTACTED")),
        FirmTarget.touches < max_touches)).all()
    talks = in_deal_talks(session, now)
    lead_ids = [ld.id for ld in offers]
    talks |= {(e or "").lower() for (e,) in session.execute(select(Contact.email).where(
        Contact.lead_id.in_(lead_ids or [-1]), Contact.email.is_not(None)))}
    busy = queued_targets(session)
    # a target with a held sample (another stream's Touch 1 waiting for a yes) is in that funnel: a yes to this note
    # would release that sample (firm_outbox.release_sample picks the target's newest held one)
    busy |= {tid for (tid,) in session.execute(select(FirmOutbox.target_id).where(
        FirmOutbox.status == "held", FirmOutbox.target_id.is_not(None)).distinct())}
    hist = blast_history(session)
    ok = []
    for t in rows:
        mine = hist.get(t.id, [])
        last = max(((r.sent_at or r.created_at) for r in mine), default=None)
        if (t.id in busy or (t.email or "").lower() in talks or len(mine) >= max_notes
                or (t.last_contact_at is not None and t.last_contact_at > pitch_cut)
                or (last is not None and last > note_cut)):
            continue
        ok.append(t)
    if not ok:
        return [], hist
    areas = target_counties(session, ok, now)
    live = set(book.counties)
    order = {k: i for i, k in enumerate(BUYER_KINDS)}
    ok = [t for t in ok if areas.get(t.id, set()) & live]
    ok.sort(key=lambda t: (order.get(t.kind, 9), -(t.score or 0), t.id))
    return ok, hist


def blast_body(t: FirmTarget, book: OfferBook, op: dict[str, str], follow: bool = False) -> tuple[str, str]:
    """The buy-box intake: two plain sentences (one for the follow-up), no address, no house's price, no attachment."""
    hello = greeting(t.name) if t.kind == "investor_agent" else "Hello,"
    buyer = t.kind == "flipper"
    who, box = ("Do you", "your buy box") if buyer else ("Do you or your investor clients", "the buy box")
    subject = f"As-is houses in {book.where}"
    if follow:
        text = (f"{who} buy cash in {book.where}, and if so, what's {box} so I can send you the first as-is house "
                "once it's under contract?")
    else:
        few = "a few" if book.count >= 3 else "a couple of"
        text = (f"I'm working on {few} as-is houses in {book.where} in the {book.band} range and expect to have one "
                f"under contract soon. {who} buy cash in those areas, and what's {box} (towns, price, rehab level) so "
                "I can send you the first one once it's under contract?")
    return subject, "\n".join([hello, "", text, "", op.get("first") or ""]).strip()


class AcquisitionsStream(Stream):
    key = "s1"
    name = "Acquisitions (listing agents)"
    priority = PRIORITY_PITCH
    cadence_hours = 6.0
    run_hours = (7, 20)

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        from engine import buyer_matcher as bm
        from engine.outreach_stats import funnel
        res = StreamResult(stream=self.key)
        f = funnel(session, ctx.now)
        res.processed = int(f.get("listed", 0))
        res.found = int(f.get("convertible", 0))
        res.metrics.update({k: v for k, v in f.items() if isinstance(v, (int, float)) and not k.startswith("why_")})
        warm = warm_leads(session, int(ctx.limits.get("warm_days", 21)), ctx.now)
        centroids = bm.town_centroids(session)
        buyers = [bm.buyer_profile(b, centroids) for b in session.scalars(select(CashBuyer))]
        matched = 0
        for lead in warm:
            matches = bm.match_lead(lead, buyers, limit=int(ctx.limits.get("buyers_per_deal", 3)))
            if not matches:
                continue
            session.execute(delete(BuyerMatch).where(BuyerMatch.lead_id == lead.id))
            for rank, m in enumerate(matches, 1):
                session.add(BuyerMatch(lead_id=lead.id, buyer_id=m.buyer_id, rank=rank, score=m.score,
                                       reasons=m.reasons))
            matched += 1
        session.flush()
        res.metrics.update(warm_leads=len(warm), warm_matched=matched,
                           replies_waiting=int(session.query(InboundReply).filter(
                               InboundReply.status == "needs_approval").count()))
        self.buyers_ready(session, ctx, res)
        return res

    def buyers_ready(self, session: Session, ctx: StreamContext, res: StreamResult) -> None:
        """The buy-box intake to cash buyers in the counties where our written offers are live (counts only)."""
        from engine import firm_outbox as fo
        lim = ctx.limits
        offers = live_offers(session, ctx.now, int(lim.get("live_offer_days", lim.get("warm_days", 21))))
        res.metrics.update(live_offers=len(offers), buyer_targets=0, buyers_queued=0)
        book = offer_book(offers)
        if book is None or len(offers) < int(lim.get("min_live_offers", 2)):
            return                                       # one house can't be described without describing it
        if ctx.now.weekday() >= 5 or not ctx.send:
            return
        sync_flipper_prospects(session)
        st = fo.cfg()
        targets, hist = buyer_targets(session, book, offers, ctx.now,
                                      gap_days=float(lim.get("buyers_gap_days", 14)),
                                      max_notes=int(lim.get("buyers_max", 2)),
                                      pitch_gap_days=float(st.get("pitch_gap_days", 6)),
                                      max_touches=int(st.get("max_touches", 2)))
        res.metrics["buyer_targets"] = len(targets)
        wk = week_key(ctx.now)
        for t in targets[:int(lim.get("buyers_per_run", 8))]:
            prev = next((r for r in reversed(hist.get(t.id, [])) if r.status == "sent"), None)
            follow = prev is not None
            subject, body = blast_body(t, book, ctx.operator, follow)
            reply_to = None
            if follow and prev is not None and prev.message_id:
                reply_to = prev.message_id                # the nudge goes in the thread of the first note
                subject = "Re: " + (prev.subject or subject).removeprefix("Re: ")
            res.outbound.append(Outbound(stream=self.key, key=f"s1:buyers:{t.id}:{wk}", to=t.email or "",
                                         subject=subject, body=body, priority=PRIORITY_PITCH, kind="pitch",
                                         target_id=t.id, in_reply_to=reply_to, commercial=True))
        res.metrics["buyers_queued"] = len(res.outbound)


STREAM = AcquisitionsStream()
