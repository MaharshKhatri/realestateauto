"""S7 Mansion-tax cliff net sheets: listings priced just over a graduated-fee threshold, where the seller nets less.

P.L.2025, c.69 (deeds recorded on/after 2025-07-10) charges the seller a graduated percent fee on Class 2 residential
deeds over $1,000,000: a percentage of the ENTIRE consideration (1% over $1M, 2% over $2M, 2.5% over $2.5M, 3% over
$3M, 3.5% over $3.5M), on top of the regular Realty Transfer Fee (N.J.S.A. 46:15-7). Because the percentage applies to
the whole price, each threshold is a cliff: a house listed at $1,007,000 leaves the seller less after transfer taxes
than the same house sold at exactly $1,000,000. Most agents already price under the line (the Nov 2 data had 33
listings at $999,000 and 14 at $999,999); the few just over it are this stream.

Where the listings come from: the stale-listing harvest stops at $900K, so once a day this stream first runs its own
high-value pass (`engine/streams/s7_highvalue_harvest.py`): homeharvest for-sale listings in price bands around every
dead zone, in ~45 high-value towns, stored with their listing agents through the stale-listing agent's own helpers.

What it does: for every for-sale listing on one of our leads whose list price sits in a dead zone (above a threshold
T, where price - transfer taxes < T - transfer taxes at T) and was seen by a harvest in the last `fresh_days` (3), it
builds a one-page Transfer-Tax Net Sheet (the arithmetic of the two statutory fees at both prices, the dead-zone range,
the statute / Treasury sources) and proposes the 2-step courtesy note (2026-11-10):
- Touch 1, plain text, no attachment: "Quick heads-up on <street>: at <list price> your seller crosses New Jersey's
  graduated transfer-fee line and pays about <extra> more in transfer tax than at <the line>. Want the 1-page net sheet
  showing both prices?" (the numbers are the message: statutory fee arithmetic, never an opinion of value);
- the sheet itself is the sample (one listing, nothing to mask), held in the outbox and sent in the agent's thread only
  when they say yes, with a one-line no-charge note.
Rates come only from config/nj_deal_costs.yaml through `engine.quant_pricing` (never hard-coded here).

Why it is safe: the sheet is arithmetic of public statutory fees on the public list price, not an opinion of value,
not tax or legal advice, no charge and nothing tied to the sale (so no fee that could look like brokerage, N.J.S.A.
45:15-3); no owner or agent name is printed; Daniel's Law / stop suppressions, agent stop lists, bounces, recent
e-mails about the listing and agents we are in deal talks with (a reply or a lead e-mail in 30 days) are honored; one
note per listing ever, one per agent per 30 days, and the outbox's pitch gap and cold-touch limit per agent.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from db.models import (
    Contact,
    DistressSignal,
    FirmOutbox,
    FirmTarget,
    InboundReply,
    Interaction,
    Lead,
    Listing,
    Property,
    SignalType,
    utcnow,
)
from engine.streams.base import (
    PRIORITY_SPECIALTY,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    address_ok,
    money,
    operator_footer,
    render_table_pdf,
    slug,
)

log = logging.getLogger(__name__)

KEY = "s7"
STEP = 500                                   # the RTF is charged per $500 of consideration: solve on that grid
LISTING_SIGNALS = (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)
# Class 2 is residential (1-4 family incl. condo units). An unmatched parcel has no MOD-IV class: these listing types
# are residential 1-4 family (multi-family is left out: over $1M it may be a 5+ unit Class 4C building, no graduated
# fee).
RESIDENTIAL_TYPES = {"single_family", "condos", "condo", "townhomes", "townhouse", "condo_townhome"}
SOURCES = (
    "N.J.S.A. 46:15-7 et seq. (Realty Transfer Fee) and the Division of Taxation fee schedules: "
    "https://www.nj.gov/treasury/taxation/realty.shtml · "
    "https://www.nj.gov/treasury/taxation/pdf/other_forms/lpt/feeschedover350.pdf",
    "P.L.2025, c.69 Graduated Percent Fee (seller pays; deeds recorded on/after 2025-07-10): "
    "https://nj.gov/treasury/taxation/pdf/lpt/GraduatedPercentFeeNotice.pdf · "
    "https://nj.gov/treasury/taxation/pdf/other_forms/lpt/GraduatedPercentFeePolicies.pdf",
)
DISCLAIMER = "Arithmetic of the statutory fees; not tax or legal advice; confirm with the closing attorney."


# -- the cliff math (pure) ----------------------------------------------------------------------------------------
def transfer_tax(price: float, cfg: dict[str, Any] | None = None) -> float:
    """The seller's two statutory transfer charges at this consideration: RTF + graduated percent fee."""
    from engine.quant_pricing import graduated_percent_fee, realty_transfer_fee
    return round(realty_transfer_fee(price, cfg) + graduated_percent_fee(price, cfg), 2)


def seller_net(price: float, cfg: dict[str, Any] | None = None) -> float:
    """What the price leaves the seller after the transfer taxes only (commission and payoff apply at either price)."""
    return round(price - transfer_tax(price, cfg), 2)


def thresholds(cfg: dict[str, Any] | None = None) -> list[float]:
    """Every tier floor of the graduated percent fee (1.0M, 2.0M, 2.5M, 3.0M, 3.5M in the current YAML)."""
    from engine.quant_pricing import costs
    tiers = (cfg or costs())["realty_transfer_fee"]["graduated_percent_fee"]["tiers"]
    return sorted({float(t["over"]) for t in tiers})


def zone_edge(threshold: float, cfg: dict[str, Any] | None = None, step: int = STEP) -> float:
    """The first price on the $500 grid above the threshold where the seller nets at least what T leaves them.
    Everything strictly between T and this edge is the dead zone."""
    target = seller_net(threshold, cfg)
    p = threshold + step
    for _ in range(10_000):                  # a 3.5% cliff closes within ~$20K; the cap only guards a bad config
        if seller_net(p, cfg) >= target:
            return p
        p += step
    return p


def dead_zones(cfg: dict[str, Any] | None = None) -> list[tuple[float, float]]:
    """[(threshold, edge)] for every tier (computed from the YAML rates)."""
    if cfg is None:
        return list(_default_zones())
    return [(t, zone_edge(t, cfg)) for t in thresholds(cfg)]


@lru_cache(maxsize=1)
def _default_zones() -> tuple[tuple[float, float], ...]:
    return tuple((t, zone_edge(t)) for t in thresholds())


@dataclass
class Cliff:
    price: float
    threshold: float
    edge: float
    rtf_price: float
    grad_price: float
    rtf_threshold: float
    grad_threshold: float

    @property
    def net_price(self) -> float:
        return round(self.price - self.rtf_price - self.grad_price, 2)

    @property
    def net_threshold(self) -> float:
        return round(self.threshold - self.rtf_threshold - self.grad_threshold, 2)

    @property
    def gain(self) -> float:
        """How much more the seller nets by pricing / closing at the threshold."""
        return round(self.net_threshold - self.net_price, 2)

    @property
    def tax_extra(self) -> float:
        """How much more the seller pays in the two transfer charges at the list price than at the threshold."""
        return round(self.rtf_price + self.grad_price - self.rtf_threshold - self.grad_threshold, 2)

    @property
    def width(self) -> float:
        return self.edge - self.threshold

    @property
    def pct_price(self) -> float:
        return self.grad_price / self.price if self.price else 0.0

    @property
    def pct_threshold(self) -> float:
        return self.grad_threshold / self.threshold if self.threshold else 0.0


def cliff(price: float | None, cfg: dict[str, Any] | None = None) -> Cliff | None:
    """The dead zone this price sits in (the seller nets less than at the threshold below it), else None."""
    from engine.quant_pricing import graduated_percent_fee, realty_transfer_fee
    if not price or price <= 0:
        return None
    for t, edge in dead_zones(cfg):
        if t < price < edge and seller_net(price, cfg) < seller_net(t, cfg):
            return Cliff(price=float(price), threshold=t, edge=edge,
                         rtf_price=realty_transfer_fee(price, cfg), grad_price=graduated_percent_fee(price, cfg),
                         rtf_threshold=realty_transfer_fee(t, cfg), grad_threshold=graduated_percent_fee(t, cfg))
    return None


def just_under(price: float | None, pct: float = 0.02, cfg: dict[str, Any] | None = None) -> bool:
    """Priced within `pct` under a threshold (the agent already knows the cliff): a count for the run log only."""
    return bool(price) and any(t * (1 - pct) <= price <= t for t in thresholds(cfg))


def residential(prop_class: str | None, listing_type: str | None) -> bool:
    """Class 2 on MOD-IV, or no class on record and a 1-4 family residential listing type."""
    c = (prop_class or "").strip().upper()
    if c:
        return c == "2"
    return (listing_type or "").strip().lower() in RESIDENTIAL_TYPES


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$][A-Za-z0-9$,.'/&%-]*", text or ""))


def greeting(agent_name: str | None) -> str:
    """"Hi Pat," only when the listing's agent name reads as a person (s2's rule); a brokerage or team gets "Hello,"."""
    from engine.streams.s2_deal_desk_saas import first_name
    first = first_name(agent_name)
    return f"Hi {first}," if first and first.lower() != "there" else "Hello,"


def short_money(v: float) -> str:
    """$1,000,000 -> "$1M", $2,500,000 -> "$2.5M" (subject lines)."""
    return f"${v / 1_000_000:,.2f}".rstrip("0").rstrip(".") + "M" if v >= 1_000_000 else money(v)


def note_body(c: Cliff, street: str, agent_name: str | None = None) -> str:
    """Touch 1 (the 2-step funnel, 2026-11-10): a greeting and exactly 2 plain sentences, no attachment, no link. The
    numbers are the message: the statutory fee arithmetic at the list price and at the line, never a value opinion."""
    return (f"{greeting(agent_name)}\n\n"
            f"Quick heads-up on {street}: at {usd(c.price)} your seller crosses New Jersey's graduated transfer-fee "
            f"line and pays about {usd(c.tax_extra)} more in transfer tax than at {usd(c.threshold)}. "
            "Want the 1-page net sheet showing both prices?")


def note_subject(c: Cliff, street: str) -> str:
    return f"{street} and the {short_money(c.threshold)} transfer-fee line"


def sample_body(street: str) -> str:
    """Touch 2, sent in the agent's thread only after their yes, with the sheet attached: free, no price, no pitch."""
    return (f"Here's the net sheet for {street}; no charge. "
            "If it's useful I send these for any listing near a line.")


# -- the product --------------------------------------------------------------------------------------------------
def _pct(x: float) -> str:
    return f"{x * 100:.1f}%".replace(".0%", "%")


def usd(v: float | None) -> str:
    """Dollars with $0 shown as $0 (a fee that doesn't apply is a fact on a net sheet, not a blank)."""
    return f"${(v or 0):,.0f}"


def render_sheet(path: Any, c: Cliff, street: str, town: str, op: dict[str, str]) -> Any:
    head = ["", f"At the list price ({usd(c.price)})", f"At the threshold ({usd(c.threshold)})", "Difference"]
    rows = [
        ["Price", usd(c.price), usd(c.threshold), usd(c.price - c.threshold)],
        ["Realty Transfer Fee (N.J.S.A. 46:15-7)", usd(c.rtf_price), usd(c.rtf_threshold),
         usd(c.rtf_price - c.rtf_threshold)],
        ["Graduated percent fee (P.L.2025, c.69)",
         f"{usd(c.grad_price)} ({_pct(c.pct_price)} of the whole price)",
         usd(c.grad_threshold) + (f" ({_pct(c.pct_threshold)})" if c.grad_threshold else " (none at or under $1M)"),
         usd(c.grad_price - c.grad_threshold)],
        ["Seller's transfer taxes", usd(c.rtf_price + c.grad_price), usd(c.rtf_threshold + c.grad_threshold),
         usd(c.rtf_price + c.grad_price - c.rtf_threshold - c.grad_threshold)],
        ["<b>Seller's price after transfer taxes</b>", f"<b>{usd(c.net_price)}</b>", f"<b>{usd(c.net_threshold)}</b>",
         f"<b>{usd(c.gain)} more at the threshold</b>"],
    ]
    zones = "; ".join(f"over {usd(t)} to about {usd(e)}" for t, e in dead_zones())
    notes = [
        "Method: the Realty Transfer Fee is charged per $500 of consideration on the Division of Taxation schedule; "
        "the "
        "graduated percent fee is a percentage of the ENTIRE consideration on a Class 2 residential deed over "
        "$1,000,000, and the seller pays both. Seller's price after transfer taxes = price - both fees.",
        "Commission, payoff and other seller costs are left out: they apply at either price, and a percentage "
        "commission is lower at the lower price, so it only adds to the difference. This sheet says nothing about "
        "the property's market value.",
        f"Every dead zone on the current schedule (the seller nets less than at the threshold): {zones}.",
        *[f"Source: {s}" for s in SOURCES],
        f"<b>{DISCLAIMER}</b>",
    ]
    box = (f"<b>{street}, {town}</b> is listed at {usd(c.price)}, inside the {usd(c.width)}-wide dead zone just over "
           f"{usd(c.threshold)}: any price over {usd(c.threshold)} to about {usd(c.edge)} nets the seller less than "
           f"{usd(c.threshold)} does. Pricing or closing at {usd(c.threshold)} leaves the seller about "
           f"<b>{usd(c.gain)} more</b> after the statutory transfer fees than a sale at the current list price.")
    return render_table_pdf(path, title="Transfer-Tax Net Sheet",
                            subtitle=f"{street}, {town} NJ · prepared for the listing agent from the public list price "
                                     "and the statutory fee schedules",
                            head=head, rows=rows, notes=notes, box=box, footer=operator_footer(op),
                            col_widths=[2.3, 1.9, 1.9, 1.5], landscape_page=False,
                            write_sample=False)   # one listing: the sheet itself is the sample (nothing to mask)


# -- database helpers ---------------------------------------------------------------------------------------------
def _emailed_lead(session: Session, lead_id: int, since_eastern: datetime, since_utc: datetime) -> bool:
    """We already e-mailed someone about this listing recently (the desk's agent e-mails or any firm outbox row)."""
    inter = session.scalar(select(Interaction.id).where(
        Interaction.lead_id == lead_id, Interaction.channel.like("email%"), Interaction.channel != "email_in",
        Interaction.occurred_at >= since_eastern).limit(1))
    if inter:
        return True
    return bool(session.scalar(select(FirmOutbox.id).where(
        FirmOutbox.lead_id == lead_id, FirmOutbox.status.in_(("queued", "sent")),
        FirmOutbox.created_at >= since_utc).limit(1)))


def _in_deal_talks(session: Session, email: str, since_eastern: datetime, since_utc: datetime) -> bool:
    """The agent is in a deal conversation with the desk on any listing (they wrote to us, or we e-mailed them about a
    lead, in the window): no courtesy note in the middle of a negotiation."""
    if session.scalar(select(InboundReply.id).where(func.lower(InboundReply.from_email) == email,
                                                    InboundReply.created_at >= since_utc).limit(1)):
        return True
    theirs = select(Contact.id).where(func.lower(Contact.email) == email)
    their_leads = select(Contact.lead_id).where(func.lower(Contact.email) == email, Contact.lead_id.is_not(None))
    return bool(session.scalar(select(Interaction.id).where(
        Interaction.channel.like("email%"), Interaction.occurred_at >= since_eastern,
        or_(Interaction.contact_id.in_(theirs), Interaction.lead_id.in_(their_leads))).limit(1)))


def _agent_stopped(session: Session, email: str) -> bool:
    from engine import agent_outreach as ao
    e = email.strip().lower()
    if e in ao.bounced_emails(session):
        return True
    return bool(session.scalar(select(Contact.id).where(func.lower(Contact.email) == e,
                                                        Contact.dnc_status == "dnc").limit(1)))


def _target(session: Session, email: str, agent: Contact, town: str) -> FirmTarget | None:
    """The agent as a firm target (the held sample is released in their thread when they reply yes). An address that
    is already a target of another stream keeps its row, its stage and its cold-touch count (one agent, one budget)."""
    from engine import firm_outbox as fo
    t = session.scalar(select(FirmTarget).where(FirmTarget.email == fo.norm_email(email)))
    if t is not None:
        return t
    return fo.upsert_target(session, kind="listing_agent", stream=KEY, email=email, name=agent.name,
                            org=agent.company, town=town or None, source="listing data (MLS)")


def _candidates(session: Session, lo: float, hi: float, limit: int,
                seen_since: datetime | None = None) -> list[tuple[Listing, Lead]]:
    """For-sale listings priced inside [lo, hi] that an active listing signal ties to one of our leads; with
    `seen_since`, only listings a harvest saw since then (one nobody has seen for days may have changed price or
    sold)."""
    q = (select(Listing, Lead).join(DistressSignal, DistressSignal.source_ref == Listing.source_ref)
         .join(Lead, Lead.property_id == DistressSignal.property_id)
         .where(Listing.status == "for_sale", Listing.list_price > lo, Listing.list_price < hi,
                DistressSignal.signal_type.in_(LISTING_SIGNALS), DistressSignal.active.is_(True)))
    if seen_since is not None:
        q = q.where(Listing.last_seen_at >= seen_since)
    rows = session.execute(q.order_by(Listing.list_price).limit(limit)).all()
    seen: set[int] = set()
    out: list[tuple[Listing, Lead]] = []
    for lst, lead in rows:
        if lead.id not in seen:
            seen.add(lead.id)
            out.append((lst, lead))
    return out


def run_harvest(session: Session, ctx: StreamContext, lim: dict[str, Any]) -> dict[str, Any]:
    """The day's high-value listing pass (engine/streams/s7_highvalue_harvest.py). A failure in it never stops the
    notes on listings already stored: its writes are dropped and the run goes on."""
    from engine.streams import s7_highvalue_harvest as hv
    try:
        return hv.harvest(session, now=ctx.now, limits=lim)
    except Exception as exc:  # noqa: BLE001
        log.exception("s7: high-value listing pass failed")
        session.rollback()
        return {"status": "error", "error_name": type(exc).__name__}


def harvest_metrics(h: dict[str, Any]) -> dict[str, int]:
    """The pass's counts for the public run log, as numbers (hv_ran / hv_blocked flags instead of a status word)."""
    status = str(h.get("status") or "")
    out = {"hv_ran": int(status in ("ran", "blocked", "errors")),
           "hv_blocked": int(status in ("blocked", "blocked_before")), "hv_failed": int(status == "error")}
    out.update({f"hv_{k}": int(v) for k, v in h.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
    return out


class MansionTaxCliffStream(Stream):
    key = KEY
    name = "Mansion-tax cliff net sheets"
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        from core.text import muni_base, postal_title
        from engine import agent_outreach as ao
        from engine import firm_outbox as fo
        res = StreamResult(stream=self.key)
        lim = ctx.limits or {}
        max_send = int(lim.get("pitches_per_run", lim.get("max_per_run", 3)))
        recent_days = float(lim.get("recent_days", 30))
        min_gain = float(lim.get("min_gain", 1000))
        scan = int(lim.get("scan_limit", 2000))
        fresh_days = float(lim.get("fresh_days", 3))
        hv = harvest_metrics(run_harvest(session, ctx, lim)) if lim.get("harvest", True) else {}
        zones = dead_zones()
        m: dict[str, int] = {k: 0 for k in ("listings_checked", "zone_listings", "just_under", "zone_with_lead",
                                             "sheets", "skipped_class", "skipped_small", "skipped_suppressed",
                                             "skipped_no_agent", "skipped_stop", "skipped_recent", "skipped_talks",
                                             "skipped_target", "skipped_sent_before", "skipped_stage")}
        if not zones:
            res.metrics = {**m, **hv}
            return res
        lo, hi = zones[0][0], zones[-1][1]
        # whole-market counts (any for-sale listing, ours or not): how many sit in a dead zone vs just under a line
        for (price,) in session.execute(select(Listing.list_price).where(
                Listing.status == "for_sale", Listing.list_price >= lo * 0.98, Listing.list_price < hi).limit(scan)):
            m["listings_checked"] += 1
            m["zone_listings"] += int(cliff(price) is not None)
            m["just_under"] += int(just_under(price))
        now = ctx.now
        since_e, since_u = now - timedelta(days=recent_days), utcnow() - timedelta(days=recent_days)
        agents_this_run: set[str] = set()
        for lst, lead in _candidates(session, lo, hi, scan, utcnow() - timedelta(days=fresh_days)):
            res.processed += 1
            c = cliff(lst.list_price)
            if c is None:
                continue
            m["zone_with_lead"] += 1
            prop: Property = lead.property
            if not residential(prop.property_class, lst.property_type):
                m["skipped_class"] += 1
                continue
            if c.gain < min_gain:
                m["skipped_small"] += 1
                continue
            if lead.status_stage in ("DEAD", "DO_NOT_CONTACT", "CLOSED"):
                m["skipped_stage"] += 1
                continue
            if not address_ok(session, prop):
                m["skipped_suppressed"] += 1
                continue
            agent = ao.agent_contact(lead)
            email = (agent.email or "").strip().lower() if agent is not None else ""
            if not email:
                m["skipped_no_agent"] += 1
                continue
            if _agent_stopped(session, email):
                m["skipped_stop"] += 1
                continue
            res.found += 1
            prior = select(FirmOutbox.id).where(FirmOutbox.key.like(f"{self.key}:sheet:{lead.id}:%")).limit(1)
            if session.scalar(prior):
                m["skipped_sent_before"] += 1          # one net sheet per listing, ever (a price cut gets no second)
                continue
            if email in agents_this_run or _emailed_lead(session, lead.id, since_e, since_u) or session.scalar(
                    select(FirmOutbox.id).where(FirmOutbox.stream == self.key, FirmOutbox.to_email == email,
                                                FirmOutbox.created_at >= since_u).limit(1)):
                m["skipped_recent"] += 1
                continue
            if _in_deal_talks(session, email, since_e, since_u):
                m["skipped_talks"] += 1
                continue
            if m["sheets"] >= max_send:
                continue
            street = postal_title((prop.street_address or lst.address or "").split(",")[0].strip()).rstrip(".")
            town = (postal_title(muni_base(prop.municipality)) if prop.municipality
                    else postal_title(prop.city or lst.city or ""))
            target = None
            if ctx.send:
                # checked with the outbox's own rules before anything is proposed: a note the outbox would skip (an
                # agent another stream pitched this week, cold touches used up) waits for a later run instead of
                # using up this listing's one note
                target = _target(session, email, agent, town)
                why = "no valid e-mail" if target is None else (
                    fo.stop_reason(session, email, target) or fo._pitch_block(session, target, now))
                if why:
                    m["skipped_target"] += 1
                    continue
            path = render_sheet(ctx.out_dir / f"s7_net_sheet_{lead.id}_{slug(street, 32)}.pdf", c, street, town,
                                ctx.operator)
            m["sheets"] += 1
            res.products += 1
            agents_this_run.add(email)
            if target is not None:
                res.outbound.append(Outbound(
                    stream=self.key, key=f"{self.key}:sheet:{lead.id}:{int(round(c.price))}", to=email,
                    subject=note_subject(c, street), body=note_body(c, street, agent.name),
                    priority=PRIORITY_SPECIALTY, kind="pitch", attachments=[], target_id=target.id, lead_id=lead.id,
                    commercial=True, sample_body=sample_body(street), sample_attachments=[path]))
        res.metrics = {**m, **hv, "outbound": len(res.outbound), "zones": len(zones)}
        return res


STREAM = MansionTaxCliffStream()
