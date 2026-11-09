"""The flat-fee AI Deal Desk (2026-11-09 pivot): weekly deal digests for named flippers and multi-buy LLCs.

The operator now sells software and research, not deals. A digest shows a flipper ten underwritten properties in
the towns they actually buy (from recorded deeds): stale / fixer MLS listings, sheriff sales with room over the
upset, and pre-foreclosures, each with the ask or upset, our ARV, the repairs and the most a rehabber can pay
(`quant_pricing` buyer max: >= 15% net ROI and >= $45K after rehab + contingency, closing, carry and resale costs).

The pitch is the operator's decision (no real estate license): a flat subscription, $500 for the first 30 days and
then $1,000 a month, zero commission, zero per-deal pay. The client is the named buyer, outreach runs from the
client's own inbox with the client's proof of funds and the client approves every offer. N.J.S.A. 45:15-3 makes one
act of buying or negotiating for another *for a fee* brokerage, so nothing here is ever paid by the deal, and the
digest says so. No owner or defendant names, no address under a Daniel's Law / stop suppression, and a listing
agent's phone only when it is on no stop list.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.text import clean, is_entity, muni_base, postal_title
from db.models import CashBuyer, Lead, Property, SignalType

TRIAL_FEE, MONTHLY_FEE = 500, 1000
MIN_PRICE = 40_000                       # deed prices under this are package deeds / nominal transfers
MAX_ROWS = 10
QUOTAS = (("MLS", 5), ("Sheriff sale", 4), ("Pre-foreclosure", 2))   # a mix first, then the best of the rest


@dataclass
class BuyerProfile:
    key: str                              # family key (shared mailing address) or "b<id>"
    buyer_ids: list[int]
    entity: str                           # the entity name the digest is addressed to
    principal: str | None                 # a named person from the deed records (kept off the public log)
    kind: str                             # "named" / "llc"
    purchases_36m: int
    flips: int
    towns: list[tuple[str, str, str, int]]   # (mun_code, Town, County, purchases), most first
    counties: list[str]
    price_lo: float | None
    price_med: float | None
    price_hi: float | None
    multi_share: float                    # share of purchases with 2+ units
    last_purchase: str | None
    score: float = 0.0

    @property
    def mun_codes(self) -> set[str]:
        return {m for m, *_ in self.towns if m}

    def box_line(self) -> str:
        towns = ", ".join(t for _, t, _, _ in self.towns[:5]) or ", ".join(f"{c} County" for c in self.counties[:3])
        band = (f"${self.price_lo / 1000:,.0f}K-${self.price_hi / 1000:,.0f}K" if self.price_lo and self.price_hi
                else "any price")
        kind = "2-4 family" if self.multi_share >= 0.5 else "1-family" if self.multi_share <= 0.2 else "1-4 family"
        return f"{towns} · {kind} · {band} · {self.purchases_36m} purchases in 36 months"


@dataclass
class DigestRow:
    lead_id: int
    street: str
    town: str
    kind: str                             # "MLS fixer" / "Sheriff sale" / "Pre-foreclosure"
    price_label: str                      # "Ask $X (140 days)" / "Upset $X, sale 11/24" / "Lis pendens 10/02"
    price_point: float | None
    arv: float
    repairs: float
    max_buy: float
    agent: str = ""                       # listing agent's business contact (MLS rows only)
    note: str = ""
    rank: float = 0.0

    @property
    def room(self) -> float | None:
        return self.max_buy - self.price_point if self.price_point else None


@dataclass
class Digest:
    profile: BuyerProfile
    rows: list[DigestRow] = field(default_factory=list)
    week_of: date = field(default_factory=date.today)


# -- buyer boxes --------------------------------------------------------------------------------------------------
def _purchases(buyers: list[CashBuyer], today: date) -> list[dict[str, Any]]:
    cutoff = (today - timedelta(days=1095)).isoformat()
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for b in buyers:
        for x in b.recent_purchases_json or []:
            if not isinstance(x, dict) or not x.get("address") or str(x.get("deed_date") or "") < cutoff:
                continue
            k = (re.sub(r"\W", "", str(x["address"]).upper()), str(x.get("deed_date") or ""))
            if k not in seen:
                seen.add(k)
                out.append(x)
    # one price on one deed date for 2+ parcels = a package deed: its price says nothing about one house
    dup = Counter((x.get("price"), x.get("deed_date")) for x in out if x.get("price"))
    return [x for x in out if not (x.get("price") and dup[(x.get("price"), x.get("deed_date"))] > 1)]


def _quantile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))]


def profile(buyers: list[CashBuyer], key: str, today: date | None = None,
            principal: str | None = None) -> BuyerProfile | None:
    """A buyer family's box. "named" = a person behind it is on record (a deed-export contact that is a person, not
    the entity again, or the likely principal `partner_principals` found); the name stays in the operator's inbox."""
    from engine.partner_principals import partner_score
    today = today or date.today()
    buys = _purchases(buyers, today)
    if not buys:
        return None
    towns: Counter[tuple[str, str, str]] = Counter()
    for x in buys:
        if x.get("mun_code") and x.get("municipality"):
            towns[(str(x["mun_code"]), postal_title(muni_base(x["municipality"])), str(x.get("county") or "").title())] += 1
    prices = [float(x["price"]) for x in buys if x.get("price") and float(x["price"]) >= MIN_PRICE]
    counties = sorted({str(x.get("county") or "").title() for x in buys if x.get("county")})
    named = next((clean(b.contact_name) for b in buyers if b.contact_name and not is_entity(b.contact_name)),
                 None) or (clean(principal) if principal else None)
    entities = sorted({clean(b.entity_name) for b in buyers if b.entity_name}, key=len)
    multi = sum(1 for x in buys if int(x.get("units") or 1) >= 2)
    last = max((str(x.get("deed_date") or "") for x in buys), default=None)
    st = {"purchases_36m": len(buys), "flips": sum(int(b.flips_count or 0) for b in buyers), "counties": counties,
          "deployed_36m": sum(prices), "last_purchase": {"date": last}, "entities": entities}
    return BuyerProfile(
        key=key, buyer_ids=[b.id for b in buyers], entity=entities[0] if entities else (named or "Investor"),
        principal=named, kind="named" if named else "llc", purchases_36m=len(buys), flips=st["flips"],
        towns=[(m, t, c, n) for (m, t, c), n in towns.most_common()], counties=counties,
        price_lo=_quantile(prices, 0.25) if prices else None, price_med=median(prices) if prices else None,
        price_hi=_quantile(prices, 0.75) if prices else None, multi_share=multi / len(buys),
        last_purchase=last, score=partner_score(st, today))


def buyer_profiles(session: Session, today: date | None = None) -> list[BuyerProfile]:
    """One profile per buyer family (records sharing a mailing address are one operator), best first."""
    from db.models import JVPartner
    from engine.partner_principals import family_key
    fams: dict[str, list[CashBuyer]] = {}
    for b in session.scalars(select(CashBuyer)):
        fams.setdefault(family_key(b) or f"b{b.id}", []).append(b)
    principals = {jp.buyer_id: jp.principal_name for jp in session.scalars(select(JVPartner)) if jp.principal_name}
    out = [p for k, bs in fams.items()
           if (p := profile(bs, k, today, next((principals[b.id] for b in bs if b.id in principals), None)))]
    return sorted(out, key=lambda p: -p.score)


def select_buyers(session: Session, named: int = 34, llcs: int = 20, min_llc_buys: int = 3,
                  today: date | None = None) -> list[BuyerProfile]:
    """The digest list: every named principal family (best first, up to `named`), then the top multi-buy LLCs; when
    fewer people are on record than `named`, more LLCs fill the list (named + llcs digests in all)."""
    ps = buyer_profiles(session, today)
    first = [p for p in ps if p.kind == "named"][:named]
    rest = [p for p in ps if p.kind == "llc" and p.purchases_36m >= min_llc_buys][:llcs + named - len(first)]
    return first + rest


# -- the deals ----------------------------------------------------------------------------------------------------
def _suppressed(session: Session, prop: Property) -> bool:
    from engine.compliance import address_suppressed
    addr = f"{prop.street_address or ''}, {prop.city or ''} NJ {prop.zip_code or ''}"
    owner = f"{prop.owner_mailing_address or ''}, {prop.owner_city_state_zip or ''}"
    return bool(address_suppressed(session, addr) or (prop.owner_mailing_address and address_suppressed(session, owner)))


def _agent_line(session: Session, lead: Lead) -> str:
    from engine import agent_outreach as ao
    from engine.compliance import dnc_sources, is_fax_contact
    c = ao.agent_contact(lead)
    if c is None:
        return ""
    phone = c.phone if c.phone and not is_fax_contact(c) and not (
        set(dnc_sources(session, c.phone)) & {"internal", "daniels_law"}) else None
    bits = [postal_title(c.name) if c.name else "", clean(c.company) if c.company else "", c.email or "",
            (f"({phone[:3]}) {phone[3:6]}-{phone[6:]}" if phone and len(phone) == 10 else phone or "")]
    return ", ".join(b for b in bits if b)


def deal_row(session: Session, lead: Lead, today: date | None = None) -> DigestRow | None:
    """One digest line for a lead, or None when it doesn't belong in front of a flipper."""
    from engine import offer_math
    from engine.scoring import listing_off_market
    from engine.underwriter import asking_price, known_debt
    today = today or date.today()
    p = lead.property
    v = p.current_valuation if p else None
    if v is None or not v.arv_estimate or (p.property_class or "2") not in ("2", "2 ", "4C"):
        return None
    if lead.status_stage in ("DEAD", "DO_NOT_CONTACT", "CLOSED"):
        return None
    q = (v.assumptions_json or {}).get("quant") or {}
    max_buy = float(q.get("buyer_max") or 0)
    if max_buy < MIN_PRICE:
        return None
    flags = [str(f) for f in ((v.assumptions_json or {}).get("flags") or [])]
    if any(f.startswith(("OUTLIER_CAPPED", "NON_RESIDENTIAL")) for f in flags):
        return None
    sigs = {s.signal_type: s for s in p.active_signals}
    ask = asking_price(p)
    sheriff = sigs.get(SignalType.SHERIFF_SALE)
    lp = sigs.get(SignalType.LIS_PENDENS)
    town = postal_title(muni_base(p.municipality)) or postal_title(p.city)
    base = dict(lead_id=lead.id, street=postal_title(p.street_address), town=town, arv=float(v.arv_estimate),
                repairs=float(v.est_repair_cost or 0), max_buy=round(max_buy, -2))
    if ask and not listing_off_market(p):
        f = offer_math.firm_offer(lead, session, today=today)
        if f.tier == "skip" and any(r.startswith(("move-in-ready", "verdict", "the valuation looks", "not under"))
                                    for r in f.reasons):
            return None
        if any(r.startswith("HOLD") for r in offer_math.certainty_issues(lead, session, today)):
            return None
        if max_buy < 0.75 * ask:
            return None                                    # nothing a flipper can win at a realistic price
        dom = (sigs.get(SignalType.STALE_LISTING) or sigs.get(SignalType.ACTIVE_LISTING))
        days = (dom.raw_metadata_json or {}).get("days_on_market") if dom else None
        cond = offer_math.condition(session, lead)
        words = ", ".join(offer_math.distress_words(session, lead, 2))
        row = DigestRow(**base, kind="MLS fixer" if cond == "fixer" else "MLS listing",
                        price_label=f"Ask ${ask:,.0f}" + (f" ({days} days)" if days else ""), price_point=ask,
                        agent=_agent_line(session, lead), note=f"listing says: {words}" if words else "")
        row.rank = (max_buy - ask) / ask + (0.15 if cond == "fixer" else 0) + min(int(days or 0), 365) / 3650
        return row
    if sheriff and sheriff.scheduled_auction_date and sheriff.scheduled_auction_date >= today:
        debt = known_debt(p) or float(sheriff.amount_owed or 0)
        if not debt or max_buy < debt * 1.05:
            return None                                    # no room over the upset for a bidder
        if debt < 0.25 * max_buy:
            return None                                    # an upset that low is usually a partial / lien figure
        row = DigestRow(**base, kind="Sheriff sale",
                        price_label=f"Upset ~${debt:,.0f}, sale {sheriff.scheduled_auction_date:%m/%d}",
                        price_point=debt, note="cash at auction; sales are often adjourned; title not insured")
        days_out = (sheriff.scheduled_auction_date - today).days
        row.rank = (max_buy - debt) / max_buy + (0.1 if days_out <= 45 else 0)
        return row
    if lp is not None and v.verdict not in ("PASS", "FRFPA_BLOCKED"):
        filed = lp.filing_date.strftime("%m/%d/%y") if lp.filing_date else "recently"
        row = DigestRow(**base, kind="Pre-foreclosure", price_label=f"Lis pendens filed {filed}", price_point=None,
                        note="owner-occupied: NJ FRFPA applies (82% of fair value floor, 10-business-day cancellation)")
        row.rank = 0.05 + (0.05 if lp.filing_date and (today - lp.filing_date).days <= 180 else 0)
        return row
    return None


def candidate_pool(session: Session, mun_codes: set[str], counties: set[str],
                   today: date | None = None) -> dict[int, DigestRow]:
    """Every digest-worthy lead in these towns (or counties), computed once and shared by all digests."""
    stmt = select(Lead).join(Property, Lead.property_id == Property.id).where(
        (Property.mun_code.in_(sorted(mun_codes))) | (Property.county.in_(sorted(counties))))
    pool: dict[int, DigestRow] = {}
    for lead in session.scalars(stmt):
        if lead.property is None or _suppressed(session, lead.property):
            continue
        try:
            row = deal_row(session, lead, today)
        except Exception:                                  # noqa: BLE001 - one odd record never stops a digest
            row = None
        if row is not None:
            pool[lead.id] = row
    return pool


def digest_for(session: Session, prof: BuyerProfile, pool: dict[int, DigestRow],
               today: date | None = None) -> Digest:
    """Top deals in the buyer's towns, within their price band (by max buy), then their counties to fill ten."""
    props = {lead.id: lead.property for lead in session.scalars(select(Lead).where(Lead.id.in_(list(pool))))} \
        if pool else {}
    lo = 0.6 * prof.price_lo if prof.price_lo else 0
    hi = 1.6 * prof.price_hi if prof.price_hi else float("inf")

    def fits(r: DigestRow) -> bool:
        return lo <= r.max_buy <= hi

    in_town = [r for lid, r in pool.items() if props[lid].mun_code in prof.mun_codes and fits(r)]
    in_county = [r for lid, r in pool.items() if props[lid].mun_code not in prof.mun_codes and fits(r)
                 and (props[lid].county or "").title() in prof.counties]
    rows: list[DigestRow] = []
    for group in (in_town, in_county):
        ranked = sorted(group, key=lambda r: -r.rank)
        for kind, cap in QUOTAS:                         # listings, sheriff sales and pre-foreclosures each get a share
            have = sum(1 for r in rows if r.kind.startswith(kind))
            rows += [r for r in ranked if r.kind.startswith(kind) and r not in rows][:max(0, cap - have)]
        rows += [r for r in ranked if r not in rows][:max(0, MAX_ROWS - len(rows))]
    order = {"MLS fixer": 0, "MLS listing": 1, "Sheriff sale": 2, "Pre-foreclosure": 3}
    rows = sorted(rows[:MAX_ROWS], key=lambda r: (order.get(r.kind, 9), -r.rank))
    return Digest(prof, rows, today or date.today())


def build_digests(session: Session, profiles: list[BuyerProfile], today: date | None = None) -> list[Digest]:
    codes = set().union(*(p.mun_codes for p in profiles)) if profiles else set()
    counties = set().union(*(set(p.counties) for p in profiles)) if profiles else set()
    pool = candidate_pool(session, codes, counties, today)
    return [digest_for(session, p, pool, today) for p in profiles]


# -- the page -----------------------------------------------------------------------------------------------------
def pitch_lines(prof: BuyerProfile, op: dict[str, str]) -> list[str]:
    who = prof.entity
    return [
        f"<b>Want this running every day for {who}?</b> The AI Deal Desk finds deals like these each morning, "
        "underwrites them, and drafts your offers to listing agents from <b>your own inbox</b> with <b>your</b> proof "
        "of funds. You approve every offer and you are the buyer on every contract.",
        f"<b>30-day trial: ${TRIAL_FEE:,} flat. Then ${MONTHLY_FEE:,}/month flat. Zero commission, zero per-deal fees "
        "or splits.</b> Reply \"activate\" to start the trial.",
        f"{op.get('name') or ''} provides software and research only: not a licensed real estate broker or agent, never "
        "negotiates, buys or sells on your behalf, and is paid the flat fee only, never on a deal. Figures are estimates "
        "from public records and listing data: verify before you offer.",
    ]


def render_pdf(d: Digest, path: Path, op: dict[str, str] | None = None) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    from engine import human_copy as hc
    from engine.contracts import _pdf_safe
    op = op or hc.operator()
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=8.2, leading=10)
    small = ParagraphStyle("s", parent=body, fontSize=6.8, leading=8.2)
    cell = ParagraphStyle("c", parent=body, fontSize=7, leading=8.4)
    h1 = ParagraphStyle("h", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=14, leading=17, spaceAfter=2)
    box = ParagraphStyle("x", parent=body, backColor=colors.HexColor("#eef6ff"), borderPadding=5,
                         borderColor=colors.HexColor("#1d4ed8"), borderWidth=0.6, spaceBefore=4)

    def P(t: str, st: ParagraphStyle = cell) -> Paragraph:
        return Paragraph(_pdf_safe(t), st)

    k = lambda x: f"${x / 1000:,.0f}K" if x else "-"
    head = ["#", "Property", "Type / price", "ARV", "Repairs", "Max buy*", "Room", "Listing agent / note"]
    data = [[P(f"<b>{h}</b>") for h in head]]
    for i, r in enumerate(d.rows, 1):
        room = r.room
        data.append([P(str(i)), P(f"<b>{r.street}</b><br/>{r.town}"), P(f"{r.kind}<br/>{r.price_label}"), P(k(r.arv)),
                     P(k(r.repairs)), P(f"<b>{k(r.max_buy)}</b>"),
                     P(("+" if room >= 0 else "-") + k(abs(room)) if room is not None else "-"),
                     P("<br/>".join(x for x in (r.agent, r.note) if x))])
    if len(d.rows) == 0:
        data.append([P("-"), P("No match in your towns this week."), *[P("") for _ in range(6)]])
    t = Table(data, colWidths=[0.3 * inch, 1.5 * inch, 1.45 * inch, 0.55 * inch, 0.6 * inch, 0.65 * inch,
                               0.6 * inch, 4.25 * inch], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#9ca3af")),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")])]))
    contact = " · ".join(x for x in (op.get("name"), op.get("phone"), op.get("email"),
                                     op.get("postal")) if x)
    story = [P(f"Weekly Deal Digest for {d.profile.entity}", h1),
             P(f"Week of {d.week_of:%B %-d, %Y} · Your buy box from recorded deeds: {d.profile.box_line()}", body),
             Spacer(1, 5), t, Spacer(1, 4),
             P("*Max buy: the most a rehabber can pay and still clear 15% net ROI and $45,000 after repairs + 10% "
               "contingency, 1.5% closing, 6 months of taxes, insurance, utilities and 11% + 2 pt money, a 5% "
               "commission and the NJ realty transfer fee on the resale. Room = max buy minus the ask or upset.", small),
             Spacer(1, 6),
             P(" ".join(pitch_lines(d.profile, op)[:2]), box),
             Spacer(1, 3), P(pitch_lines(d.profile, op)[2], small),
             P(f"{contact} · Reply \"no more\" and you won't get these again.", small)]
    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(path), pagesize=landscape(LETTER), title=f"Weekly Deal Digest - {d.profile.entity}",
                      leftMargin=0.45 * inch, rightMargin=0.45 * inch, topMargin=0.4 * inch,
                      bottomMargin=0.35 * inch).build(story)
    return path


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:48] or "buyer"


def write_all(session: Session, out_dir: Path, *, named: int = 34, llcs: int = 20,
              today: date | None = None) -> list[tuple[Digest, Path]]:
    today = today or date.today()
    digests = build_digests(session, select_buyers(session, named, llcs, today=today), today)
    out = []
    for i, d in enumerate(digests, 1):
        out.append((d, render_pdf(d, out_dir / f"{i:02d}_{slug(d.profile.entity)}.pdf")))
    return out


def summary_text(pairs: list[tuple[Digest, Path]]) -> str:
    """The private e-mail's body: who each digest is for and what's on it (the operator's own inbox only)."""
    lines = ["Weekly Deal Digests (1 page each). Forward a PDF only to the buyer it names, from your own Gmail.", ""]
    for i, (d, p) in enumerate(pairs, 1):
        who = f"{d.profile.entity}" + (f" ({d.profile.principal})" if d.profile.principal else "")
        lines.append(f"{i:02d}. {who}: {len(d.rows)} deals, {d.profile.box_line()} -> {p.name}")
    return "\n".join(lines)


# -- the prospect pipeline ----------------------------------------------------------------------------------------
STAGES = ("NEW", "CONTACTED", "SAMPLE_SENT", "CALL_BOOKED", "TRIAL", "PAYING", "LOST")


def prospect_key(entity: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (entity or "").upper().replace("L.L.C", "LLC"))[:200]


def seed_prospects(session: Session, digests: list[Digest], now: Any = None) -> int:
    """Every digest's buyer becomes (or stays) a prospect; the latest digest's size and date are kept. A stage is
    never moved back."""

    from db.models import Prospect, utcnow
    now = now or utcnow()
    made = 0
    for d in digests:
        k = prospect_key(d.profile.entity)
        p = session.scalar(select(Prospect).where(Prospect.key == k))
        if p is None:
            p = Prospect(key=k, entity_name=d.profile.entity, stage="NEW")
            session.add(p)
            made += 1
        p.family_key, p.principal_name = d.profile.key, d.profile.principal or p.principal_name
        p.purchases_36m, p.buy_box = d.profile.purchases_36m, d.profile.box_line()[:300]
        p.digest_rows, p.digest_at = len(d.rows), now
    session.flush()
    return made


def update_prospect(session: Session, item: dict[str, Any], now: Any = None) -> str:
    """A sealed note's {"prospect": {...}}: public business contacts found for a flipper, or where the pitch stands
    ("stage", "notes", "next_touch" ISO, "fee"). Matched by entity name; created when new. Returns the stage."""
    from datetime import datetime

    from db.models import Prospect, utcnow
    now = now or utcnow()
    k = prospect_key(str(item.get("entity") or ""))
    if not k:
        raise ValueError("prospect needs an entity")
    p = session.scalar(select(Prospect).where(Prospect.key == k))
    if p is None:
        p = Prospect(key=k, entity_name=clean(str(item["entity"]))[:200], stage="NEW")
        session.add(p)
    stage = str(item.get("stage") or "").strip().upper()
    if stage:
        if stage not in STAGES:
            raise ValueError("unknown prospect stage")
        if stage != p.stage:
            p.stage, p.last_touch_at = stage, now
    email = str(item.get("email") or "").strip().lower()
    if email:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
            raise ValueError("bad prospect email")
        p.email = email
    for f in ("website", "phone", "linkedin", "source", "principal_name"):
        if item.get(f):
            setattr(p, f, str(item[f]).strip()[:400])
    if item.get("notes"):
        p.notes = ((p.notes + "\n") if p.notes else "") + f"{now:%Y-%m-%d}: {str(item['notes'])[:600]}"
    if item.get("next_touch"):
        p.next_touch_at = datetime.fromisoformat(str(item["next_touch"])).replace(tzinfo=None)
    if item.get("fee"):
        p.monthly_fee = float(item["fee"])
    session.flush()
    return p.stage


def pipeline_counts(session: Session) -> dict[str, int]:
    from db.models import Prospect
    c = Counter(session.scalars(select(Prospect.stage)))
    return {s.lower(): int(c.get(s, 0)) for s in STAGES if c.get(s)}


# -- flipper-connected listing agents -----------------------------------------------------------------------------
def _num(x: Any) -> str:
    return str(x or "").split(".")[0].lstrip("0")


# a brokerage's shared inbox lists everyone's houses: it isn't a person a flipper works with
OFFICE_MAILBOX = re.compile(r"(?i)^(info|office|admin|listings?|front ?desk|reception|contact|team|sales|broker|mls|"
                            r"hello|support|mail|frontoffice|manager)[0-9._-]*@")


def flipper_agents(session: Session, limit: int = 50, today: date | None = None) -> list[dict[str, Any]]:
    """Listing agents closest to the flippers: +5 for each listing of a house a recorded cash buyer owns (a flip's
    resale), +0.5 per active cash-buyer family in the town for each fixer listing (0.15 for any other listing, max 5
    families; these town points cap at 12 so volume alone can't win), +3 when the agent already wrote back to us. Agents who asked to stop, bounced or sit on a stop list are
    left out; a phone shows only when it is on no stop list."""
    from db.models import ContactType, DistressSignal, InboundReply
    from engine import offer_math
    from engine.compliance import dnc_sources, is_fax_contact
    today = today or date.today()
    owned: set[tuple[str, str, str]] = set()
    names: set[str] = set()
    town_fams: Counter[str] = Counter()
    for prof in buyer_profiles(session, today):
        for m in prof.mun_codes:
            town_fams[m] += 1
    for b in session.scalars(select(CashBuyer)):
        names.add(prospect_key(b.entity_name or ""))
        for x in b.recent_purchases_json or []:
            if isinstance(x, dict) and x.get("mun_code") and x.get("block") and x.get("lot"):
                owned.add((str(x["mun_code"]), _num(x["block"]), _num(x["lot"])))
    names.discard("")
    replied = {str(e).lower() for e in session.scalars(select(InboundReply.from_email).where(
        InboundReply.status.notin_(("bounce", "auto_reply")))) if e}
    from engine import agent_outreach as ao
    bounced = ao.bounced_emails(session)
    props = session.scalars(select(Property).join(DistressSignal, DistressSignal.property_id == Property.id).where(
        DistressSignal.signal_type.in_((SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING)),
        DistressSignal.active.is_(True))).unique().all()
    agents: dict[str, dict[str, Any]] = {}
    for p in props:
        lead = p.lead
        if lead is None or _suppressed(session, p):
            continue
        resale = (str(p.mun_code), _num(p.block), _num(p.lot)) in owned or prospect_key(p.owner_name or "") in names
        try:
            fixer = offer_math.condition(session, lead) == "fixer"
        except Exception:                                  # noqa: BLE001 - no listing text: unknown condition
            fixer = False
        fams = min(int(town_fams.get(str(p.mun_code), 0)), 5)
        for c in lead.contacts:
            if c.contact_type != ContactType.LISTING_AGENT or not c.email or c.dnc_status == "dnc" \
                    or (c.confidence_score or 0) <= 0 or c.email.lower() in bounced or OFFICE_MAILBOX.match(c.email):
                continue
            a = agents.setdefault(c.email.lower(), {"name": postal_title(c.name) if c.name else "", "company":
                                                    clean(c.company) if c.company else "", "email": c.email.lower(),
                                                    "phone": "", "score": 0.0, "resales": 0, "fixers": 0,
                                                    "listings": 0, "towns": Counter(), "warm": False})
            if c.phone and not a["phone"] and not is_fax_contact(c) and not (
                    set(dnc_sources(session, c.phone)) & {"internal", "daniels_law"}):
                a["phone"] = c.phone
            a["listings"] += 1
            a["resales"] += int(resale)
            a["fixers"] += int(fixer and fams > 0)
            a["resale_pts"] = a.get("resale_pts", 0.0) + 5.0 * resale
            a["town_pts"] = a.get("town_pts", 0.0) + (0.5 if fixer else 0.15) * fams
            a["towns"][postal_title(muni_base(p.municipality)) or ""] += 1
    for e, a in agents.items():
        # volume alone (a team / office inbox with 80 listings) can't outrank a flip resale: town points cap at 12
        a["score"] = a.pop("resale_pts", 0.0) + min(a.pop("town_pts", 0.0), 12.0)
        if e in replied:
            a["warm"], a["score"] = True, a["score"] + 3.0
    rows = sorted(agents.values(), key=lambda a: (-a["score"], -a["resales"], a["email"]))[:limit]
    for a in rows:
        a["towns"] = ", ".join(t for t, _ in a["towns"].most_common(3) if t)
        a["score"] = round(a["score"], 1)
    return rows


def agents_csv(rows: list[dict[str, Any]]) -> bytes:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank", "name", "brokerage", "email", "phone", "score", "flip_resales_listed",
                "fixer_listings_in_flipper_towns", "active_listings", "towns", "already_wrote_back"])
    for i, a in enumerate(rows, 1):
        w.writerow([i, a["name"], a["company"], a["email"], a["phone"], a["score"], a["resales"], a["fixers"],
                    a["listings"], a["towns"], "yes" if a["warm"] else ""])
    return buf.getvalue().encode("utf-8")
