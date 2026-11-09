"""Stream 1: acquisitions from listing agents (the original desk, now one stream of ten).

The desk's own machinery does the work on every run (gmail_autopilot: read replies, answer fact checks and call /
proof-of-funds asks in writing, send firm offers within offer_math's numbers). This stream adds the firm-level view
and the internal half of disposition: every lead with a live agent conversation is matched to the recorded cash
buyers most likely to buy it (buyer_matcher; stored as buyer_matches), so when an agent accepts, the operator already
knows who to call. Nothing is marketed to a buyer before a signed contract's attorney review ends (CLAUDE.md:
remote disposition), so this stream sends no e-mail: it measures and prepares.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from db.models import BuyerMatch, CashBuyer, InboundReply, Lead
from engine.streams.base import PRIORITY_PITCH, Stream, StreamContext, StreamResult

CLOSED = ("DEAD", "DO_NOT_CONTACT", "CLOSED")


def warm_leads(session: Session, since_days: int = 21, now=None) -> list[Lead]:
    """Leads whose listing agent wrote back in the last few weeks (a conversation, not a cold row)."""
    from datetime import datetime
    since = (now or datetime.now()) - timedelta(days=since_days)
    ids = {lid for (lid,) in session.execute(select(InboundReply.lead_id).where(
        InboundReply.lead_id.is_not(None), InboundReply.created_at >= since,
        InboundReply.status.notin_(("bounce", "auto_reply"))).distinct())}
    return [ld for ld in session.scalars(select(Lead).where(Lead.id.in_(ids))) if ld.status_stage not in CLOSED]


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
        return res


STREAM = AcquisitionsStream()
