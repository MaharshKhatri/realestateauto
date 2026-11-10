"""Stream 5: a weekly fix-and-flip borrower feed for hard-money lenders (2026-11-09).

A private lender's best prospect is a company that buys houses often and has a loan need right now: a purchase still
in rehab (draws, an extension, a refinance) or a house of theirs listed for resale (a payoff, then the next loan).
Recorded deeds show exactly that. The cash-buyer list already groups deed records into buyer families (records that
share a mailing address are one operator: `partner_principals.family_key`) and `deal_desk.buyer_profiles` computes each
family's box (36-month purchases, towns, price band, 2-4 family share, last purchase). This stream reuses those boxes,
adds "likely in rehab" (purchases in the last `in_progress_months`) and "resale listed" (a parcel they bought has a live
listing now), ranks by activity and prints the top `feed_rows` on one page.

Why it stays legal: it is public deed and listing records compiled for a flat monthly data fee, never pay per loan or
per borrower (N.J.S.A. 45:15-3), and the lender contacts the businesses itself. Business entities only: a family whose
name reads as a person (`entity_like` false) or an unnamed "Owner @ <mailing>" cluster is left out, no mailing address
is ever printed, and a family whose mailing address is on a Daniel's Law / stop list is dropped. It is marketing
research about businesses, not a consumer or credit report.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.text import clean
from db.models import CashBuyer, DistressSignal, FirmOutbox, FirmTarget, Property, SignalType
from engine.streams.base import (
    week_of,
    PRIORITY_SPECIALTY,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    entity_like,
    kmoney,
    operator_footer,
    render_table_pdf,
)

LISTING_SIGNALS = (SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)
TARGET_KINDS = ("hard_money_lender", "mortgage_broker")


@dataclass
class Borrower:
    entity: str
    purchases_36m: int
    last_purchase: str | None          # ISO date of the last recorded purchase
    towns: list[str]
    counties: list[str]
    price_lo: float | None
    price_hi: float | None
    multi_share: float
    in_progress: int                   # purchases in the last `in_progress_months` (likely mid-rehab)
    resales: int                       # parcels they bought that are listed for sale now
    score: float = 0.0

    @property
    def resale_listed(self) -> bool:
        return self.resales > 0


# -- small pure helpers -------------------------------------------------------------------------------------------
def months_since(iso: str | None, today: date) -> float | None:
    try:
        return max(0.0, (today - date.fromisoformat(str(iso)[:10])).days / 30.4)
    except (TypeError, ValueError):
        return None


def activity_score(purchases_36m: int, in_progress: int, resales: int, months_since_last: float | None) -> float:
    """Rank for a lender: repeat volume first (3 per purchase, capped at 25 so one giant operator doesn't bury the
    rest), then the loan need now (4 per house likely in rehab, 5 per house listed for resale: a payoff or refinance),
    then recency (up to 6 points, fading over two years; MOD-IV sale data runs months behind)."""
    s = 3.0 * min(max(purchases_36m, 0), 25) + 4.0 * min(max(in_progress, 0), 10) + 5.0 * min(max(resales, 0), 5)
    if months_since_last is not None:
        s += max(0.0, 6.0 - 0.25 * months_since_last)
    return round(s, 2)


def block_lot(x: Any) -> str:
    """'0031' / '31.00' / ' 31 ' -> '31'; '322.61' stays '322.61' (a sub-block is a different parcel)."""
    s = str(x or "").strip().upper()
    s = re.sub(r"\.0+$", "", s)
    head, dot, tail = s.partition(".")
    return (head.lstrip("0") or "0") + (dot + tail if tail else "") if s else ""


def parcel_key(mun: Any, block: Any, lot: Any) -> tuple[str, str, str]:
    return (str(mun or "").strip(), block_lot(block), block_lot(lot))


def in_progress_count(purchases: list[dict[str, Any]], today: date, months: int = 9) -> int:
    """Purchases recorded in the last `months`: a flipper is usually still mid-rehab (or about to refinance)."""
    since = today - timedelta(days=int(months * 30.4))
    n = 0
    for x in purchases:
        try:
            d = date.fromisoformat(str(x.get("deed_date") or "")[:10])
        except ValueError:
            continue
        if since <= d <= today:
            n += 1
    return n


def resale_parcels(purchases: list[dict[str, Any]], listed: set[tuple[str, str, str]],
                   owned_listed: set[tuple[str, str, str]] | None = None) -> set[tuple[str, str, str]]:
    """Listed-now parcels that this family bought (by block / lot) or that its entity owns on the tax list; a parcel
    found both ways counts once."""
    bought = {parcel_key(x.get("mun_code"), x.get("block"), x.get("lot")) for x in purchases
              if x.get("mun_code") and x.get("block") and x.get("lot")}
    return (bought & listed) | set(owned_listed or ())


def month_label(iso: str | None) -> str:
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%b %Y")
    except (TypeError, ValueError):
        return "-"


def band(lo: float | None, hi: float | None) -> str:
    if lo and hi:
        return kmoney(lo) if round(lo, -3) == round(hi, -3) else f"{kmoney(lo)}-{kmoney(hi)}"
    return "-"


def is_business_name(name: str | None) -> bool:
    """A business that may be named in a sold list: an entity, never an unnamed "Owner @ <mailing>" cluster (whose
    "name" is somebody's mailing address)."""
    n = clean(name or "")
    return entity_like(n) and not n.upper().startswith("OWNER @")


# -- the feed -----------------------------------------------------------------------------------------------------
def _listed_now(session: Session) -> tuple[set[tuple[str, str, str]], dict[str, set[tuple[str, str, str]]]]:
    """Parcels with a live listing signal now, and the same parcels by owner name (an entity that still owns a listed
    house on the tax list is reselling it too)."""
    from engine.deal_desk import prospect_key
    parcels: set[tuple[str, str, str]] = set()
    owners: dict[str, set[tuple[str, str, str]]] = {}
    for pid, mun, block, lot, owner in session.execute(
            select(Property.id, Property.mun_code, Property.block, Property.lot, Property.owner_name)
            .join(DistressSignal, DistressSignal.property_id == Property.id)
            .where(DistressSignal.signal_type.in_(LISTING_SIGNALS), DistressSignal.active.is_(True)).distinct()):
        k = parcel_key(mun, block, lot) if mun and block and lot else ("id", str(pid), "")
        parcels.add(k)
        if owner and prospect_key(owner):
            owners.setdefault(prospect_key(owner), set()).add(k)
    return parcels, owners


def build_feed(session: Session, today: date, *, min_purchases: int = 2, in_progress_months: int = 9,
               max_families: int = 3000, counts: Counter[str] | None = None) -> list[Borrower]:
    """Every entity buyer family with `min_purchases`+ recorded purchases in 36 months, ranked by activity."""
    from engine.compliance import address_suppressed
    from engine.deal_desk import _purchases, buyer_profiles, prospect_key
    counts = counts if counts is not None else Counter()
    buyers = {b.id: b for b in session.scalars(select(CashBuyer))}
    listed, owners = _listed_now(session)
    out: list[Borrower] = []
    for prof in buyer_profiles(session, today)[:max_families]:
        counts["families"] += 1
        if not is_business_name(prof.entity):
            counts["not_entity"] += 1                  # a person's name or an unnamed mailing cluster: never sold
            continue
        fam = [buyers[i] for i in prof.buyer_ids if i in buyers]
        if any(b.mailing_address and address_suppressed(session, b.mailing_address) for b in fam):
            counts["suppressed"] += 1
            continue
        if prof.purchases_36m < min_purchases:
            counts["too_few"] += 1
            continue
        buys = _purchases(fam, today)                  # the same deduped, package-free deeds behind the profile
        names = {prospect_key(x.entity_name) for x in fam if is_business_name(x.entity_name)}
        names.add(prospect_key(prof.entity))
        resales = len(resale_parcels(buys, listed, set().union(*(owners.get(k, set()) for k in names if k))))
        b = Borrower(entity=prof.entity, purchases_36m=prof.purchases_36m, last_purchase=prof.last_purchase,
                     towns=[t for _, t, _, _ in prof.towns if t], counties=list(prof.counties),
                     price_lo=prof.price_lo, price_hi=prof.price_hi, multi_share=prof.multi_share,
                     in_progress=in_progress_count(buys, today, in_progress_months), resales=resales)
        b.score = activity_score(b.purchases_36m, b.in_progress, b.resales, months_since(b.last_purchase, today))
        out.append(b)
    counts["qualified"] += len(out)
    return sorted(out, key=lambda x: (-x.score, -x.purchases_36m, x.entity))


# -- PDF ----------------------------------------------------------------------------------------------------------
# Portrait, one line per company: the base renderer pads each cell 6 pt a side and 3 pt top and bottom, so 40 rows fit
# one page only when no cell wraps. Entity and towns are cut to their column's measured width; headers stay one line.
HEAD = ["#", "Entity", "Buys", "Last buy", "Towns", "Price band", "Multi", "Rehab", "Resale"]
WIDTHS = [0.3, 2.12, 0.44, 0.62, 1.6, 0.95, 0.48, 0.5, 0.55]
_PAD_PT = 13.0                                     # 6 pt each side plus a hair so a full-width string never wraps


def fit_text(text: str, width_in: float, *, bold: bool = False, size: float = 7.0) -> str:
    """The longest prefix of `text` that fits one line of a `width_in` column ("..." marks a cut)."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    font = "Helvetica-Bold" if bold else "Helvetica"
    room = width_in * 72 - _PAD_PT
    if stringWidth(text, font, size) <= room:
        return text
    while text and stringWidth(text.rstrip(" ,") + "...", font, size) > room:
        text = text[:-1]
    return text.rstrip(" ,") + "..."


def _cells(i: int, b: Borrower) -> list[str]:
    from xml.sax.saxutils import escape
    where = ", ".join(b.towns[:4]) or ", ".join(f"{c} County" for c in b.counties[:2]) or "-"
    return [str(i), f"<b>{escape(fit_text(b.entity, WIDTHS[1], bold=True))}</b>", str(b.purchases_36m),
            month_label(b.last_purchase), escape(fit_text(where, WIDTHS[4])), band(b.price_lo, b.price_hi),
            f"{round(100 * b.multi_share):d}%", str(b.in_progress), "yes" if b.resale_listed else "no"]


def feed_kwargs(rows: list[Borrower], ctx: StreamContext, price: float, months: int) -> dict[str, Any]:
    return dict(
        title="NJ Active Fix-and-Flip Borrower Feed",
        subtitle=(f"Week of {ctx.now:%B %-d, %Y} · the {len(rows)} most active investor companies in recorded NJ deeds · "
                  "business entities only · data, not an appraisal"),
        head=HEAD, rows=[_cells(i, b) for i, b in enumerate(rows, 1)], col_widths=WIDTHS, landscape_page=False,
        notes=[
            "Buys: deeds in 36 months by the company's buyer family (records sharing a mailing address), package deeds "
            f"excluded. Price band: middle half of prices. Multi: 2-4 unit share. Rehab: bought in the last {months} "
            "months. Resale: a house they bought is listed now (payoff / refinance). Entities only, no names or mailing "
            "addresses; public records for business marketing, not a consumer or credit report. You contact the "
            "companies yourself.",
        ],
        box=(f"<b>Weekly feed: ${price:,.0f}/month flat data retainer.</b> No per-loan or per-borrower fee, no "
             "commission."),
        footer=operator_footer(ctx.operator))


def page_count(path: Path) -> int:
    """Pages in a ReportLab PDF (its page objects are written uncompressed), without a PDF library at run time."""
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", path.read_bytes())) or 1


def render_fit(path: Path, rows: list[Any], build: Callable[[list[Any]], dict[str, Any]], min_rows: int = 5) -> int:
    """Render, and drop the lowest-ranked rows until the feed fits one page. Returns the rows kept."""
    n = len(rows)
    while True:
        render_table_pdf(path, **build(rows[:n]))
        if n <= min_rows or page_count(path) <= 1:
            return n
        n = max(min_rows, n - 1)


# -- pitch --------------------------------------------------------------------------------------------------------
def pitch_body(n: int, price: float, first: str) -> str:
    """3-5 plain sentences under 110 words: what it is, what is in it, where it comes from, the flat price."""
    return "\n".join([
        "Hello,",
        "",
        "I compile a weekly NJ Active Fix-and-Flip Borrower Feed from public deed and listing records, and this week's "
        f"copy is attached. It ranks the {n} most active investor companies by purchases in the last 36 months, with "
        "their last purchase month, towns, price band, houses likely still in rehab and whether one is listed for "
        "resale. It names business entities only, with no mailing addresses, and your team contacts the companies "
        f"directly. The feed is a flat ${price:,.0f}/month data retainer, with no per-loan fee and no commission. "
        "Reply \"yes\" and I'll send next week's feed.",
        "",
        first,
    ]).rstrip()


def _first(op: dict[str, str]) -> str:
    return (op.get("first") or (op.get("name") or "").split(" ")[0] or "").strip()


def eligible_targets(session: Session, kinds: list[str], stream: str, key_for: Callable[[FirmTarget], str],
                     now: datetime) -> list[FirmTarget]:
    """Targets the outbox would accept now (stage NEW / CONTACTED, an e-mail on no stop list, touches and the pitch gap
    left, this period's key unused), best score first, so a capped run never wastes a slot on a sure skip."""
    from engine import firm_outbox as fo
    c = fo.cfg()
    gap = timedelta(days=float(c.get("pitch_gap_days", 6)))
    used = set(session.scalars(select(FirmOutbox.key).where(FirmOutbox.stream == stream)))
    out: list[FirmTarget] = []
    for t in session.scalars(select(FirmTarget).where(FirmTarget.kind.in_(kinds),
                                                      FirmTarget.stage.in_(("NEW", "CONTACTED")),
                                                      FirmTarget.email.is_not(None))
                             .order_by(FirmTarget.score.desc(), FirmTarget.id)):
        if key_for(t) in used or not fo.pitch_ok(t, now):
            continue
        if t.last_contact_at and t.last_contact_at > now - gap:
            continue
        if fo.stop_reason(session, t.email or "", t):
            continue
        out.append(t)
    return out


# -- the stream ---------------------------------------------------------------------------------------------------
class BorrowerFeedStream(Stream):
    key = "s5"
    name = "Hard-money borrower feed"
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        lim = ctx.limits or {}
        res = StreamResult(stream=self.key)
        today = ctx.now.date()
        price = float(lim.get("price_monthly", 750))
        months = int(lim.get("in_progress_months", 9))
        counts: Counter[str] = Counter()
        feed = build_feed(session, today, min_purchases=int(lim.get("min_purchases", 2)), in_progress_months=months,
                          max_families=int(lim.get("max_families", 3000)), counts=counts)
        res.processed = counts["families"]
        res.found = len(feed)
        y, w, _ = ctx.now.isocalendar()
        top = feed[:int(lim.get("feed_rows", 40))]
        path: Path | None = None
        kept = 0
        if len(top) >= int(lim.get("min_rows", 5)):
            path = ctx.out_dir / f"s5_borrower_feed_{y}W{w:02d}.pdf"
            kept = render_fit(path, top, lambda rs: feed_kwargs(rs, ctx, price, months))
            res.products = 1
        shown = top[:kept]
        pitches = 0
        if ctx.send and path is not None:
            cap = int(lim.get("pitches_per_run", 3))
            kinds = [str(k) for k in (lim.get("target_kinds") or TARGET_KINDS)]

            def key_for(t: FirmTarget) -> str:
                return f"{self.key}:pitch:{t.id}:{y}-W{w}"

            for t in eligible_targets(session, kinds, self.key, key_for, ctx.now)[:cap]:
                res.outbound.append(Outbound(
                    stream=self.key, key=key_for(t), to=t.email or "", priority=PRIORITY_SPECIALTY, kind="pitch",
                    subject=f"NJ fix-and-flip borrower feed, week of {week_of(ctx.now)}",
                    body=pitch_body(len(shown), price, _first(ctx.operator)), attachments=[path], target_id=t.id))
                pitches += 1
        res.metrics.update({k: int(v) for k, v in counts.items()})
        res.metrics.update(feed_rows=len(shown), in_progress_total=sum(b.in_progress for b in shown),
                           resale_listed=sum(1 for b in shown if b.resale_listed), pitches=pitches)
        res.notes.append(f"{len(shown)} companies in the feed, {pitches} pitches proposed")
        return res


STREAM = BorrowerFeedStream()
