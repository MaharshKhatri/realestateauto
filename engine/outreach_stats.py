"""Where the outreach pipeline stands: counts only (the cloud desk prints them in its public run log).

listed → valued → offer tier (firm / ask first / skip, with the top skip reasons) → convertible (a deal that can
sign: engine/offer_math.py::convertible) → reachable (agent e-mail, callable phone) → never contacted yet; plus the
last 24 hours / 7 days of cold e-mail, replies and bounces. It answers "what limits volume today": supply of good
deals, the cold-e-mail cap, or contacts."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import DistressSignal, InboundReply, Interaction, Lead

LISTING = ("STALE_LISTING", "ACTIVE_LISTING")
CLOSED = ("DEAD", "DO_NOT_CONTACT", "CLOSED", "ASSIGNED")
EMAIL_CHANNELS = ("email_smtp", "email_gmail", "email")


def _norm(reason: str) -> str:
    """'our final is 63% of the ask (under 75%)…' → 'our final is #% of the ask (under #%)…': countable, no addresses."""
    return re.sub(r"\$?\d[\d,.]*", "#", str(reason or "")).strip()[:70] or "(none)"


def funnel(session: Session, now: datetime | None = None) -> dict[str, Any]:
    from engine import agent_outreach as ao
    from engine import compliance
    from engine.offer_math import convertible, firm_offer
    from engine.scoring import listing_off_market
    now = now or datetime.now()
    listed_ids = select(DistressSignal.property_id).where(DistressSignal.active.is_(True),
                                                          DistressSignal.signal_type.in_(LISTING))
    leads = list(session.scalars(select(Lead).where(Lead.property_id.in_(listed_ids),
                                                    Lead.status_stage.notin_(CLOSED))))
    emailed = {lid for (lid,) in session.execute(
        select(Interaction.lead_id).where(Interaction.channel.in_(EMAIL_CHANNELS)).distinct())}
    out: Counter[str] = Counter()
    skip_why: Counter[str] = Counter()
    not_conv_why: Counter[str] = Counter()
    for lead in leads:
        out["listed"] += 1
        if listing_off_market(lead.property):
            out["off_market"] += 1
            continue
        if lead.property.current_valuation is None:
            out["unvalued"] += 1
            continue
        out["valued"] += 1
        try:
            f = firm_offer(lead, session)
        except Exception:                          # noqa: BLE001 - a broken valuation is counted, never fatal
            out["price_error"] += 1
            continue
        out[f"tier_{f.tier}"] += 1
        if f.tier == "skip":
            skip_why[_norm(f.reasons[0] if f.reasons else "")] += 1
            continue
        ok, why = convertible(session, lead, f)
        if not ok:
            not_conv_why[_norm(why)] += 1
            continue
        out["convertible"] += 1
        agent = ao.agent_contact(lead)
        has_email = bool(agent and agent.email)
        has_phone = bool(agent and agent.phone and compliance.classify_number(session, agent.phone, b2b=True) != "dnc")
        out["conv_email"] += int(has_email)
        out["conv_phone"] += int(has_phone)
        out["conv_no_contact"] += int(not has_email and not has_phone)
        if lead.id not in emailed:
            out["conv_never_emailed"] += 1
            out["conv_never_emailed_with_email"] += int(has_email)
    since7 = now - timedelta(days=7)
    sent7 = session.scalar(select(func.count(Interaction.id)).where(Interaction.channel.in_(EMAIL_CHANNELS),
                                                                    Interaction.occurred_at >= since7)) or 0
    bounces7 = session.scalar(select(func.count(InboundReply.id)).where(InboundReply.status == "bounce",
                                                                        InboundReply.created_at >= since7)) or 0
    replies7 = session.scalar(select(func.count(InboundReply.id)).where(
        InboundReply.status.notin_(("bounce", "auto_reply")), InboundReply.created_at >= since7)) or 0
    result: dict[str, Any] = dict(out)
    result.update(cold_24h=ao.sent_last_24h(session, now, cold_only=True), all_24h=ao.sent_last_24h(session, now),
                  sent_7d=int(sent7), replies_7d=int(replies7), bounces_7d=int(bounces7),
                  bounce_pct_7d=round(100.0 * bounces7 / sent7, 1) if sent7 else 0.0,
                  skip_top=dict(skip_why.most_common(6)), not_convertible_top=dict(not_conv_why.most_common(5)))
    return result
