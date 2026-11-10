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
name reads as a person, a family trust or an estate, or an unnamed "Owner @ <mailing>" cluster is left out
(`s4.company_like`), no mailing address is ever printed, and a family whose mailing address is on a Daniel's Law / stop
list is dropped. It is marketing research about businesses, not a consumer or credit report.

The 2-step funnel (2026-11-10): Touch 1 is two plain sentences with no attachment, link or price: from public deed
records we track the N most active investor companies buying NJ houses, one of them by its company name with one house
it bought (a street that passes the suppression list, in the lender's county when one is there, and the deed month),
and "want me to send the 1-page list?". Their yes releases the held sample (the feed with the top 2 companies in full
and the rest with the company name hidden) and the flat price. The second cold touch is one sentence in that thread.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.text import clean, muni_base, postal_title
from db.models import CashBuyer, DeedTransfer, DistressSignal, FirmOutbox, FirmTarget, Property, SignalType
from engine.streams.base import (
    PRIORITY_SPECIALTY,
    SAMPLE_FULL_ROWS,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    address_ok,
    kmoney,
    masked_rows,
    nice_name,
    operator_footer,
    render_table_pdf,
    sample_path,
)
from engine.streams.s4_contractor_leads import (
    company_like,
    county_key,
    deed_street,
    last_thread,
    month_phrase,
    re_subject,
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
    buys: list[dict[str, Any]] = field(default_factory=list)   # the family's deeds, newest first (Touch 1 names one)

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
    """A business that may be named in a sold list or an e-mail: an entity, never an unnamed "Owner @ <mailing>"
    cluster (whose "name" is somebody's mailing address), never a family trust or an estate (a person's name)."""
    return company_like(name)


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
                     in_progress=in_progress_count(buys, today, in_progress_months), resales=resales,
                     buys=sorted(buys, key=lambda x: str(x.get("deed_date") or ""), reverse=True))
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
    # no commas in the name: the sample's mask (base.mask_text) hides a cell up to its first ", " and would leave the
    # rest of "SMITH, JONES & CO LLC" readable
    name = " ".join(b.entity.replace(",", " ").split())
    return [str(i), f"<b>{escape(fit_text(name, WIDTHS[1], bold=True))}</b>", str(b.purchases_36m),
            month_label(b.last_purchase), escape(fit_text(where, WIDTHS[4])), band(b.price_lo, b.price_hi),
            f"{round(100 * b.multi_share):d}%", str(b.in_progress), "yes" if b.resale_listed else "no"]


def feed_kwargs(rows: list[Borrower], ctx: StreamContext, price: float, months: int,
                sample: bool = False) -> dict[str, Any]:
    """The feed's page. `sample`: the title and one-line subtitle of the masked sample Touch 2 sends after a yes (what
    it hides is the company name, so it never borrows the generic "street hidden" line)."""
    who = (f"Sample: the top {SAMPLE_FULL_ROWS} of {len(rows)} companies in full, the rest with the name hidden" if sample
           else f"the {len(rows)} most active investor companies in recorded NJ deeds · business entities only")
    return dict(
        title="NJ Active Fix-and-Flip Borrower Feed" + (" (sample)" if sample else ""),
        subtitle=f"Week of {ctx.now:%B %-d, %Y} · {who} · data, not an appraisal",
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


def render_feed(path: Path, rows: list[Any], build: Callable[[list[Any], bool], dict[str, Any]]) -> None:
    """The feed (every company in full) and, with 3+ companies, its sample (`sample_path`): the top 2 in full, the rest
    with the company name masked (`masked_rows` on the "Entity" column)."""
    sample = sample_path(path)
    sample.unlink(missing_ok=True)                         # never a stale sample from an earlier render
    render_table_pdf(path, **build(rows, False), write_sample=False)
    kw = build(rows, True)
    hidden = masked_rows(kw["head"], kw["rows"])
    if hidden is not None:
        render_table_pdf(sample, **{**kw, "rows": hidden}, write_sample=False)


def render_fit(path: Path, rows: list[Any], build: Callable[[list[Any], bool], dict[str, Any]],
               min_rows: int = 5) -> int:
    """Render, and drop the lowest-ranked rows until the feed and its sample each fit one page. Returns the rows kept."""
    n = len(rows)
    while True:
        render_feed(path, rows[:n], build)
        sample = sample_path(path)
        if n <= min_rows or (page_count(path) <= 1 and (not sample.exists() or page_count(sample) <= 1)):
            return n
        n = max(min_rows, n - 1)


# -- the 2-step funnel (2026-11-10) -------------------------------------------------------------------------------
SUBJECT_MAX = 70                                   # characters: a subject that shows whole in an inbox list


@dataclass
class Named:
    """The one company and house Touch 1 names: a public deed record, never a person."""
    borrower: Borrower
    street: str
    town: str
    bought: date


def _iso(d: Any) -> date | None:
    try:
        return date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return None


def purchase_place(session: Session, x: dict[str, Any], today: date,
                   towns: dict[str, str]) -> tuple[str, str, date] | None:
    """(street, town, deed date) of one recorded purchase when it may be named in an e-mail: a real street address
    (house number first), a known town, a deed date on or before today, and neither the street nor the parcel on a
    Daniel's Law / stop suppression."""
    addr = clean(x.get("address") or "")
    d = _iso(x.get("deed_date"))
    if not re.match(r"^\d", addr) or d is None or d > today:
        return None
    mun, block, lot = str(x.get("mun_code") or ""), str(x.get("block") or ""), str(x.get("lot") or "")
    prop, deed_town = None, None
    if not mun:
        # county deed exports carry no town: the recorded deed (its parcel id COUNTY-MUN-BLOCK-LOT) supplies it
        key = re.sub(r"\W", "", addr.upper())
        deed = next((dt for dt in session.scalars(select(DeedTransfer).where(DeedTransfer.deed_date == d))
                     if dt.parcel_id and re.sub(r"\W", "", str(dt.property_location or "").upper()) == key), None)
        if deed is not None:
            deed_town = deed.municipality
            prop = session.scalar(select(Property).where(Property.parcel_id == deed.parcel_id).limit(1))
            bits = deed.parcel_id.split("-")
            if len(bits) >= 4:
                mun, block, lot = bits[1], bits[2], bits[3]
    if prop is None and mun and block and lot:
        prop = session.scalar(select(Property).where(Property.mun_code == mun, Property.block == block,
                                                     Property.lot == lot).limit(1))
    town_raw = (x.get("municipality") or (prop.municipality if prop is not None else None) or deed_town
                or towns.get(mun))
    town = postal_title(muni_base(town_raw)) if town_raw else ""
    if not town:
        return None
    street = deed_street(addr)
    if prop is not None and not address_ok(session, prop):
        return None
    if not all(address_ok(session, SimpleNamespace(street_address=a, city=town, zip_code="")) for a in {street, addr}):
        return None
    return street, town, d


RECENT_DAYS = 365                                  # Touch 1 prefers a house bought in the last year


def pick_named(session: Session, shown: list[Borrower], today: date, county: str | None = None) -> Named | None:
    """The highest-ranked company in the feed with a nameable purchase, newest purchase first: a purchase of the last
    year in the lender's own county, else one of the last year anywhere in NJ, else an older one (same order)."""
    towns: dict[str, str] = {}
    for code, name in session.execute(select(Property.mun_code, Property.municipality).where(
            Property.mun_code.is_not(None), Property.municipality.is_not(None)).distinct()):
        towns.setdefault(str(code), name)
    recent = (today - timedelta(days=RECENT_DAYS)).isoformat()
    passes = [(local, fresh) for fresh in (True, False) for local in ((True, False) if county else (False,))]
    for local, fresh in passes:
        for b in shown:
            if not company_like(b.entity):
                continue
            for x in b.buys:
                if local and county_key(x.get("county")) != county:
                    continue
                if fresh and str(x.get("deed_date") or "")[:10] < recent:
                    break                                  # newest first: the rest of this family is older still
                got = purchase_place(session, x, today, towns)
                if got is not None:
                    return Named(b, *got)
    return None


def touch1(n: int, named: Named, today: date, first: str = "") -> tuple[str, str]:
    """Touch 1 (subject, body): two plain sentences, no price, no link, no attachment. What we track and from where,
    one company by name with one house it bought, and a yes/no question."""
    name = nice_name(named.borrower.entity)
    said = (f"From public deed records, we track the {n} most active investor companies buying NJ houses, including "
            f"{name}, which bought {named.street} in {named.town} {month_phrase(named.bought, today)}. "
            "Want me to send the 1-page list?")
    options = ([f"{name} + {n - 1} more NJ investor compan{'y' if n == 2 else 'ies'}", f"{name} + {n - 1} more",
                f"The {n} most active NJ investor companies"] if n > 1 else
               [f"{name}, an active NJ investor company", name, "An active NJ investor company"])
    subject = next((x for x in options if len(x) <= SUBJECT_MAX), options[-1])   # a long company name: a shorter line
    return subject, f"Hello,\n\n{said}\n\n{first}".rstrip()


def followup_body(first: str = "") -> str:
    """The second cold touch, in Touch 1's thread: one sentence, no attachment."""
    return f"Hello,\n\nShould I send this week's 1-page list over?\n\n{first}".rstrip()


def sample_body(price: float, masked: bool) -> str:
    """Touch 2, sent with the sample only after their yes: what is attached, the flat price, how to start."""
    what = (f"Here's this week's feed, attached: the top {SAMPLE_FULL_ROWS} companies in full, the rest with the name "
            "hidden." if masked else "Here's this week's feed, attached.")
    return (f"{what} The weekly feed is ${price:,.0f} a month flat, a data retainer with no per-loan fee or commission; "
            "it is not a consumer or credit report. Reply \"start\" and I'll set it up.")


def _target_counties(t: FirmTarget) -> list[str]:
    cs = [county_key(t.county)] if t.county else []
    extra = (t.data_json or {}).get("counties") if isinstance(t.data_json, dict) else None
    if isinstance(extra, list):
        cs += [county_key(str(c)) for c in extra if c]
    return [c for i, c in enumerate(cs) if c and c not in cs[:i]]


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
            kept = render_fit(path, top, lambda rs, sample: feed_kwargs(rs, ctx, price, months, sample))
            res.products = 1
        shown = top[:kept]
        pitches = followups = 0
        if ctx.send and path is not None:
            cap = int(lim.get("pitches_per_run", 3))
            kinds = [str(k) for k in (lim.get("target_kinds") or TARGET_KINDS)]
            first = _first(ctx.operator)
            masked = sample_path(path).exists()
            # Touch 2, held until they say yes: the masked sample (the feed itself when no sample was written)
            touch2, attach = sample_body(price, masked), [sample_path(path) if masked else path]
            picks: dict[str | None, Named | None] = {}

            def key_for(t: FirmTarget) -> str:
                return f"{self.key}:pitch:{t.id}:{y}-W{w}"

            for t in eligible_targets(session, kinds, self.key, key_for, ctx.now):
                if pitches >= cap:
                    break
                # a lender we already wrote to gets the one-sentence follow-up in that thread; the key stays one per
                # target per week, so max_touches holds
                prior = last_thread(session, self.key, t.id) if int(t.touches or 0) > 0 else None
                if prior is not None:
                    res.outbound.append(Outbound(
                        stream=self.key, key=key_for(t), to=t.email or "", priority=PRIORITY_SPECIALTY,
                        kind="followup", subject=re_subject(prior[1]), body=followup_body(first),
                        in_reply_to=prior[0], target_id=t.id, sample_body=touch2, sample_attachments=attach))
                    pitches += 1
                    followups += 1
                    continue
                county = next(iter(_target_counties(t)), None)
                if county not in picks:
                    picks[county] = pick_named(session, shown, today, county)
                named = picks[county]
                if named is None:
                    continue                           # no company and house we may name: no Touch 1
                subject, body = touch1(len(shown), named, today, first)
                res.outbound.append(Outbound(
                    stream=self.key, key=key_for(t), to=t.email or "", priority=PRIORITY_SPECIALTY, kind="pitch",
                    subject=subject, body=body, target_id=t.id, sample_body=touch2, sample_attachments=attach))
                pitches += 1
        res.metrics.update({k: int(v) for k, v in counts.items()})
        res.metrics.update(feed_rows=len(shown), in_progress_total=sum(b.in_progress for b in shown),
                           resale_listed=sum(1 for b in shown if b.resale_listed), pitches=pitches,
                           followups=followups)
        res.notes.append(f"{len(shown)} companies in the feed, {pitches} pitches proposed")
        return res


STREAM = BorrowerFeedStream()
