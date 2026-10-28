"""Remote disposition: where cash buyers pay the most, and how they walk our contracts without us driving there.

* **Buy-box hotzones** (`buyer_hotzones`): every recorded cash-buyer purchase (``cash_buyers.recent_purchases_json``,
  from county deed exports and NJGIN mailing clusters) is bucketed into a micro-market — town + product
  (1-family / 2-4 family / 5+ apartments / commercial / land) + price band. A pocket ranks by the buyer money
  deployed there (recent purchases weigh more; one buyer's bulk portfolio buy counts at most three times) and
  needs at least two different buyers. Our underwritten deals that fall in a pocket are listed under it.
* **Remote walkthroughs**: once the seller contract's attorney review (and, for FRFPA-covered owners, the
  10-business-day cancellation) has ended, matched buyers walk the house during the inspection window with the
  listing agent (lockbox showing), the seller or the tenant's notice. They are introduced truthfully — "my buying
  partner / the investor I may assign the contract to" — never as a contractor (CLAUDE.md, Consumer Fraud Act).
* **Bids** (`buyer_bids`): invited → scheduled → walked → bid → accepted / backup / declined / withdrawn.
  ``bid_amount`` is the buyer's total price (our contract price + assignment fee). Accepting a bid fills in the
  Assignment of Contract (or a single-deal JV Agreement) with the winning buyer and price.
"""
from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from core.config import settings
from core.dates import is_business_day, long_date, nj_business_days_after
from core.text import is_entity, muni_base, split_person_name, street_key
from db.models import BuyerBid, BuyerMatch, CashBuyer, DeedTransfer, Interaction, Lead, LeadStage, Occupancy, Property, Valuation
from engine.contracts import DEAL_VERDICTS, Generated, end_buyer_price, generate
from engine.crm import advance_stage
from engine.scripts import buyer_display_name, lead_facts, marketable, money

PRICE_BANDS: list[tuple[float, float, str]] = [
    (0, 200_000, "under $200K"), (200_000, 350_000, "$200K–350K"), (350_000, 500_000, "$350K–500K"),
    (500_000, 750_000, "$500K–750K"), (750_000, 1_200_000, "$750K–1.2M"), (1_200_000, math.inf, "$1.2M+"),
]
RESIDENTIAL = ("1-family", "2-4 family", "1-4 family", "5+ apartments")
MIN_ARMS_LENGTH_PRICE = 25_000      # $1 / $10 / family transfers are not buyer demand
LOOKBACK_MONTHS = 36
MAX_PER_BUYER = 3                   # one buyer's bulk portfolio purchase can't make a pocket on its own
MIN_BUYERS = 2
PRICE_CAP = 1_000_000               # a $9M apartment building doesn't make a pocket for a $15–40K assignment
BAND_TOLERANCE = 0.05               # a deal just past a band's edge still fits the pocket
BID_STATUSES = ("invited", "scheduled", "walked", "bid", "accepted", "backup", "declined", "withdrawn")
OPEN_BID = ("bid", "accepted", "backup")


class DispositionBlocked(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


# -- buy-box hotzones -------------------------------------------------------------------------------
def product(prop_class: str | None, units: int | None) -> str:
    c = (prop_class or "").upper()
    if c == "1":
        return "land"
    if c == "4C":
        return "5+ apartments"
    if c in ("4A", "4B"):
        return "commercial / mixed-use"
    if not units:
        return "1-4 family"            # class 2 with no unit count (county deed exports)
    return "1-family" if units == 1 else "2-4 family" if units <= 4 else "5+ apartments"


def products_compatible(zone_product: str, deal_product: str) -> bool:
    if zone_product == deal_product:
        return True
    pair = {zone_product, deal_product}
    return "1-4 family" in pair and bool(pair & {"1-family", "2-4 family"})


def band_of(price: float) -> str:
    return next(label for lo, hi, label in PRICE_BANDS if lo <= price < hi)


def band_fits(label: str, price: float, tolerance: float = BAND_TOLERANCE) -> bool:
    lo, hi, _ = next(b for b in PRICE_BANDS if b[2] == label)
    return lo * (1 - tolerance) <= price < (hi * (1 + tolerance) if math.isfinite(hi) else math.inf)


def recency_weight(deed_date: date | None, today: date) -> float:
    if deed_date is None:
        return 0.4
    months = (today - deed_date).days / 30.44
    return 1.0 if months <= 12 else 0.7 if months <= 24 else 0.4


@dataclass
class Purchase:
    buyer_id: int
    county: str
    town: str
    product: str
    band: str
    price: float
    units: int | None
    sqft: int | None
    deed_date: date | None
    flip: bool = False


@dataclass
class Hotzone:
    county: str
    town: str
    product: str
    band: str
    purchases: int
    buyers: int
    median_price: float
    median_per_unit: float | None
    median_per_sqft: float | None
    purchases_12m: int
    last_purchase: date | None
    flips: int
    score: float
    top_buyers: list[dict[str, Any]] = field(default_factory=list)
    leads: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.town.title()} ({self.county}) · {self.product} · {self.band}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["label"] = self.label
        d["last_purchase"] = self.last_purchase.isoformat() if self.last_purchase else None
        return d


def buyer_label(b: CashBuyer) -> str:
    """Display name; NJGIN mailing-cluster buyers have no name on record (Daniel's Law blanks owner names)."""
    return buyer_display_name(b) or f"unnamed buyer #{b.id} ({b.cash_purchases_count or 0} parcels, same mailing address)"


def greet_name(b: CashBuyer) -> str:
    """First name for a greeting — only when we know a person (an LLC's first word is not a name)."""
    if b.contact_name and not is_entity(b.contact_name.upper()):
        return b.contact_name.split()[0].title()
    name = buyer_display_name(b)
    if not name or is_entity(name.upper()):
        return ""
    first, _ = split_person_name(name)
    return first


def _parse_date(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _town_lookup(session: Session) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """(county, mun_code) → municipality from our parcels, and (county, street_key) → municipality from recorded
    deeds — used for county deed-export purchases that carry an address but no town."""
    by_code: dict[tuple[str, str], str] = {}
    for county, code, mun in session.execute(select(Property.county, Property.mun_code, Property.municipality)
                                             .where(Property.mun_code.is_not(None), Property.municipality.is_not(None))
                                             .distinct()):
        by_code.setdefault(((county or "").upper(), code), mun)
    by_addr: dict[tuple[str, str], str] = {}
    for county, parcel_id, loc in session.execute(select(DeedTransfer.county, DeedTransfer.parcel_id,
                                                         DeedTransfer.property_location)):
        parts = (parcel_id or "").split("-")
        mun = by_code.get(((county or "").upper(), parts[1])) if len(parts) > 1 else None
        if mun and loc:
            by_addr[((county or "").upper(), street_key(loc))] = mun
    return by_code, by_addr


def buyer_purchases(session: Session, today: date | None = None, lookback_months: int = LOOKBACK_MONTHS) -> list[Purchase]:
    today = today or date.today()
    cutoff = today - timedelta(days=int(lookback_months * 30.44))
    by_code, by_addr = _town_lookup(session)
    out: list[Purchase] = []
    seen: set[tuple[int, str, str]] = set()
    for b in session.scalars(select(CashBuyer)):
        for x in b.recent_purchases_json or []:
            if not isinstance(x, dict):
                continue
            price = float(x.get("price") or 0)
            when = _parse_date(x.get("deed_date"))
            if price < MIN_ARMS_LENGTH_PRICE or (when and when < cutoff):
                continue
            county = str(x.get("county") or "").upper()
            town = x.get("municipality") or by_code.get((county, str(x.get("mun_code") or ""))) or \
                by_addr.get((county, street_key(x.get("address"))))
            if not town:
                continue
            key = (b.id, street_key(x.get("address")), str(when))
            if key in seen:
                continue
            seen.add(key)
            units = int(x["units"]) if x.get("units") else None
            out.append(Purchase(buyer_id=b.id, county=county.title(), town=muni_base(town), price=price,
                                product=product(x.get("class"), units), band=band_of(price), units=units,
                                sqft=int(x["sqft"]) if x.get("sqft") else None, deed_date=when, flip=bool(x.get("flip"))))
    return out


def deal_pocket(lead: Lead) -> tuple[str, str, float] | None:
    """(town, product, buyer price) for an underwritten deal; None when there is no deal to sell."""
    v = lead.property.current_valuation
    price = end_buyer_price(lead)
    if v is None or v.verdict not in DEAL_VERDICTS or not price:
        return None
    p = lead.property
    return muni_base(p.municipality or p.city), product(p.property_class, p.dwellings), float(price)


def build_hotzones(purchases: list[Purchase], buyers: dict[int, CashBuyer], today: date | None = None,
                   min_buyers: int = MIN_BUYERS, products: tuple[str, ...] | None = RESIDENTIAL) -> list[Hotzone]:
    """Score = recency-weighted buyer dollars deployed in the pocket ($K), at most MAX_PER_BUYER purchases per buyer."""
    today = today or date.today()
    groups: dict[tuple[str, str, str, str], list[Purchase]] = defaultdict(list)
    for x in purchases:
        if products and x.product not in products:
            continue
        groups[(x.county, x.town, x.product, x.band)].append(x)
    zones: list[Hotzone] = []
    for (county, town, prod, band), rows in groups.items():
        per_buyer: dict[int, list[Purchase]] = defaultdict(list)
        for x in rows:
            per_buyer[x.buyer_id].append(x)
        if len(per_buyer) < min_buyers:
            continue
        score = 0.0
        for buys in per_buyer.values():
            buys.sort(key=lambda x: x.deed_date or date.min, reverse=True)
            score += sum(min(x.price, PRICE_CAP) * recency_weight(x.deed_date, today) for x in buys[:MAX_PER_BUYER])
        prices = [x.price for x in rows]
        per_unit = [x.price / x.units for x in rows if x.units]
        per_sqft = [x.price / x.sqft for x in rows if x.sqft]
        recent = [x for x in rows if x.deed_date and (today - x.deed_date).days <= 365]
        dated = [x.deed_date for x in rows if x.deed_date]
        top = sorted(per_buyer.items(), key=lambda kv: (-len(kv[1]), -max(x.price for x in kv[1])))[:3]
        zone = Hotzone(
            county=county, town=town, product=prod, band=band, purchases=len(rows), buyers=len(per_buyer),
            median_price=round(statistics.median(prices), -3),
            median_per_unit=round(statistics.median(per_unit), -3) if per_unit else None,
            median_per_sqft=round(statistics.median(per_sqft)) if per_sqft else None,
            purchases_12m=len(recent), last_purchase=max(dated) if dated else None,
            flips=sum(1 for x in rows if x.flip), score=round(score / 1000, 1),
            top_buyers=[{"buyer_id": bid, "name": buyer_label(buyers[bid]) if bid in buyers else f"buyer #{bid}",
                         "purchases": len(buys),
                         "phone": bool(buyers[bid].phone) if bid in buyers else False} for bid, buys in top])
        zone.reasons = [f"{zone.buyers} different cash buyers, {zone.purchases} purchases "
                        f"({zone.purchases_12m} in the last 12 months)",
                        f"median price paid {money(zone.median_price)}"
                        + (f" · {money(zone.median_per_unit)}/unit" if zone.median_per_unit else "")
                        + (f" · ${zone.median_per_sqft}/sqft" if zone.median_per_sqft else "")]
        if zone.flips:
            zone.reasons.append(f"{zone.flips} recorded flip(s) — retail resale demand is proven here")
        zones.append(zone)
    zones.sort(key=lambda z: (-z.score, -z.buyers))
    return zones


def attach_leads(zones: list[Hotzone], leads: list[Lead], per_zone: int = 5) -> None:
    pockets = [(lead, pk) for lead in leads if (pk := deal_pocket(lead))]
    for z in zones:
        hits = []
        for lead, (town, prod, price) in pockets:
            if town == z.town and products_compatible(z.product, prod) and band_fits(z.band, price):
                f = lead_facts(lead)
                hits.append({"lead_id": lead.id, "address": f["address"], "buyer_price": price, "arv": f["arv"],
                             "verdict": f["verdict"], "priority": lead.priority_rank_score, "stage": lead.status_stage,
                             "headroom": round(z.median_price - price, -2),
                             "under_contract": bool(lead.contract_date)})
        hits.sort(key=lambda h: (not h["under_contract"], h["headroom"] < 0, -(h["priority"] or 0)))
        z.leads = hits[:per_zone]
        if hits:
            under = sum(1 for h in hits if h["buyer_price"] <= z.median_price)
            z.reasons.append(f"{len(hits)} of our deals sit in this pocket; {under} priced at or below the median "
                             "buyers have paid here")


def deal_leads(session: Session) -> list[Lead]:
    stmt = (select(Lead).join(Property, Lead.property_id == Property.id)
            .join(Valuation, (Valuation.property_id == Property.id) & Valuation.is_current.is_(True))
            .where(Valuation.verdict.in_(DEAL_VERDICTS), Lead.status_stage.not_in(
                [LeadStage.DEAD, LeadStage.DO_NOT_CONTACT, LeadStage.CLOSED]))
            .options(selectinload(Lead.property)))
    return list(session.scalars(stmt).unique())


def buyer_hotzones(session: Session, top: int = 10, today: date | None = None,
                   include_commercial: bool = False) -> list[Hotzone]:
    buyers = {b.id: b for b in session.scalars(select(CashBuyer))}
    zones = build_hotzones(buyer_purchases(session, today), buyers, today,
                           products=None if include_commercial else RESIDENTIAL)
    attach_leads(zones, deal_leads(session))
    return zones[:top]


# -- remote walkthroughs ----------------------------------------------------------------------------
def _next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def access_route(lead: Lead) -> str:
    has_agent = any(c.contact_type == "LISTING_AGENT" for c in lead.contacts)
    if has_agent:
        return "listing agent showing (lockbox / ShowingTime) — the agent or their showing assistant lets the buyer in"
    if lead.occupancy_status == Occupancy.TENANT_OCCUPIED:
        return "seller or property manager gives the tenants written notice under the lease (at least 24 hours) and hosts"
    if lead.occupancy_status == Occupancy.VACANT:
        return "seller hosts, or leaves a lockbox code with the seller's written permission"
    return "seller hosts the showing at a time that suits them"


def disposition_gate(lead: Lead, today: date | None = None) -> list[str]:
    """Reasons a deal may not be shown to buyers yet (empty list = OK)."""
    today = today or date.today()
    if not lead.contract_date:
        return ["No signed seller contract on file — record it first (manage.py contract <id> --price … --signed …)."]
    if not marketable(lead, today):
        from engine.underwriter import frfpa_decision
        covered = frfpa_decision(lead.property).applies
        return [f"Attorney review{' / FRFPA cancellation' if covered else ''} runs through "
                f"{lead.attorney_review_ends or 'an unknown date'} — no buyer walkthroughs, invitations or bids until "
                "it has ended with no disapproval."]
    return []


def walkthrough_window(lead: Lead, today: date | None = None) -> dict[str, Any]:
    """Inspection window after attorney review: when buyers can walk, how they get in, and proposed slots."""
    today = today or date.today()
    days = int(settings().get("contracts", {}).get("inspection_business_days", 10))
    blocked = disposition_gate(lead, today)
    if not lead.contract_date or not lead.attorney_review_ends:
        return {"status": "no_contract", "blocked": blocked, "access": access_route(lead), "slots": []}
    opens = _next_business_day(lead.attorney_review_ends)
    closes = nj_business_days_after(lead.attorney_review_ends, days)
    status = "not_open" if today < opens else "closed" if today > closes else "open"
    # Earliest slot is the next business day (tenants need notice), alternating late morning / late afternoon.
    d = max(opens, _next_business_day(today))
    slots: list[datetime] = []
    while len(slots) < 4 and d <= closes:
        slots.append(datetime.combine(d, time(11, 0) if len(slots) % 2 == 0 else time(16, 0)))
        d = _next_business_day(d)
    return {"status": status, "opens": opens, "closes": closes, "blocked": blocked, "access": access_route(lead),
            "slots": slots[:4],
            "tip": "Book every interested buyer into the same 90-minute block — buyers who see each other bid higher "
                   "and faster, and the seller is disturbed once."}


def _slot_text(slots: list[datetime]) -> str:
    def one(s: datetime) -> str:
        return f"{s:%A} {long_date(s.date(), year=False)} at {s.hour % 12 or 12}{'am' if s.hour < 12 else 'pm'}"
    return " or ".join(one(s) for s in slots[:2]) if slots else "a time that suits you this week"


def access_request(lead: Lead, who: str = "seller", slots: list[datetime] | None = None) -> str:
    """Message to the listing agent / seller asking to let our buying partner walk the house."""
    from engine.human_copy import operator, plain, street
    op = operator()
    f = lead_facts(lead)
    slots = slots if slots is not None else walkthrough_window(lead).get("slots", [])
    me = " ".join(x for x in (op["first"] or op["name"], "with " + op["company"] if op["company"] else "") if x)
    st = street(f["address"])
    when = f"on {_slot_text(slots)}" if slots else "sometime this week"
    opening = (f"Hi, this is {me} about {st}." if who == "seller" else
               f"Hi, it's {me}, following up on our signed contract for {st}.")
    tenant = (" If tenants live there, please give them written notice under their lease; we'll keep it to 45 "
              "minutes and won't touch their things.") if lead.occupancy_status == "TENANT_OCCUPIED" else ""
    return plain(f"{opening} Our agreement allows inspections during the inspection period, so my buying partner, the "
                 "investor I may assign the contract to, would like to walk through and may bring their own "
                 f"contractor for a repair estimate. Could we do 60 to 90 minutes {when}? You're "
                 f"welcome to be there, and nobody needs to clean or move anything.{tenant} Thank you!")


def _listed(lead: Lead) -> bool:
    return any(s.signal_type in ("STALE_LISTING", "ACTIVE_LISTING") for s in lead.property.active_signals)


def buyer_invite(lead: Lead, buyer: CashBuyer, slots: list[datetime] | None = None) -> dict[str, str]:
    """SMS + e-mail inviting a matched cash buyer to walk the house and bid (only after attorney review)."""
    from engine import human_copy as hc
    op = hc.operator()
    cfg = settings().get("contracts", {})
    f = lead_facts(lead)
    slots = slots if slots is not None else walkthrough_window(lead).get("slots", [])
    name = greet_name(buyer) or "there"
    price = asking_price(lead)
    units = f"{f['units']}-family" if f.get("units") else "house"
    st, town = hc.street(f["address"]), f.get("town") or ""
    detail = ", ".join(x for x in [f"built {f['year_built']}" if f.get("year_built") else "",
                                   f"{f['sqft']:,} sqft" if f.get("sqft") else ""] if x)
    numbers = " ".join(x for x in [
        f"Price to you is {hc.money(price)}." if price else "",
        (f"Repairs look like about {hc.money(f['repairs'])}" + (f" with an ARV around {hc.money(f['arv'])}"
                                                                if f.get("arv") else "") + ".")
        if f.get("repairs") else (f"ARV looks like about {hc.money(f['arv'])}." if f.get("arv") else ""),
    ] if x)
    walk = _slot_text(slots) if slots else "this week (time to be set)"
    walk_first = _slot_text(slots[:1]) if slots else "this week (time to be set)"
    # "Off-market" only when it's true: a house that is (or was) on the MLS is not off-market.
    subject = (f"off-market fixer in {town} ({st})" if not _listed(lead) else f"fixer under contract in {town} ({st})")
    sms = (f"Hi {name}, it's {op['first'] or op['name']}" + (f" with {op['company']}" if op["company"] else "")
           + f". I have a {units} under contract in {town} that looks like what you've been buying. "
           + (f"Price to you {hc.money(price)}. " if price else "")
           + f"Walkthrough {walk_first}. Want a slot? Reply STOP to opt out.")
    paragraphs = [
        f"I have a {units} in {town} under contract that looks like what you've been buying"
        + (f" ({detail})" if detail else "") + f". {numbers} The walkthrough is {walk}, and you're "
        "welcome to bring your contractor. Want a slot?"]
    summary = [
        f"Property: {f['address']}, {f['county']} County" + (f" ({units}, {detail})" if detail else f" ({units})"),
        f"Price: {hc.money(price)} (contract price plus assignment fee, paid at closing)" if price else "",
        f"Access: {access_route(lead).split(' — ')[0]}",
        "Bids: walk it, then send your best price in writing within 24 hours with proof of funds. Please send every "
        "question to me and don't discuss price or terms with the seller, tenants or agent.",
        f"Next step: the accepted buyer signs an Assignment of Contract and deposits "
        f"{hc.money(cfg.get('assignee_deposit', 5000))} with the title company within 1 business day.",
        "What you're buying: I'm a real estate investor acting as a principal, not a licensed agent, and I'm offering "
        "my contract (equitable interest) in the property, not the property itself. All figures are estimates; rely on "
        "your own inspection, valuation and title review.",
        *hc.footer_lines(op, "Reply STOP or \"remove me\" and I won't send you deals again."),
    ]
    email = hc.compose(subject, f"Hi {name},", paragraphs, summary, summary_title="--- Deal Summary ---", op=op)
    return {"sms": hc.plain(sms), "email": f"Subject: {email.subject}\n\n{email.text}", "subject": email.subject,
            "body": email.text}


def post_walk_followup(lead: Lead, buyer: CashBuyer) -> str:
    from engine.human_copy import operator, street
    op = operator()
    f = lead_facts(lead)
    name = greet_name(buyer)
    return (f"Thanks for walking {street(f['address'])}{', ' + name if name else ''}. What number works for you? I'm "
            "taking written bids with proof of funds until 5pm tomorrow and will let everyone know the result that "
            f"evening. {op['first'] or op['name']}")


def asking_price(lead: Lead) -> float | None:
    """What we ask buyers: the 75%-of-ARV buyer price, never less than our contract price plus the target fee."""
    v = lead.property.current_valuation
    base = end_buyer_price(lead)
    floor = (lead.offer_amount + (v.target_assignment_fee or 0)) if lead.offer_amount and v else None
    prices = [x for x in (base, floor) if x]
    return round(max(prices), -2) if prices else None


def walkthrough_plan(lead: Lead, buyers: list[CashBuyer], today: date | None = None) -> dict[str, Any]:
    window = walkthrough_window(lead, today)
    plan: dict[str, Any] = {"lead_id": lead.id, "address": lead_facts(lead)["address"], **window}
    if window["blocked"]:
        return plan
    plan["access_request"] = access_request(lead, "agent" if "listing agent" in window["access"] else "seller",
                                            window["slots"])
    plan["invites"] = [{"buyer_id": b.id, "buyer": buyer_label(b), "phone": b.phone,
                        "email": b.email, **buyer_invite(lead, b, window["slots"])} for b in buyers]
    plan["steps"] = [
        "1. Send the access request to the listing agent / seller and lock one 90-minute block.",
        "2. Invite the top matched buyers (manage.py walkthrough <id> --invite) and record who confirms.",
        "3. During the block: stay on the phone with the buyer or agent; ask them to video the basement, roof line "
        "and mechanicals.",
        "4. Within 2 hours send the post-walk text; record every number with manage.py bid <id> --buyer … --amount ….",
        "5. Accept the best bid with proof of funds (manage.py bid <id> --accept <bid_id>) — the assignment fills in.",
    ]
    return plan


# -- bids -------------------------------------------------------------------------------------------
def _bid(session: Session, lead: Lead, buyer: CashBuyer) -> BuyerBid:
    bid = session.scalar(select(BuyerBid).where(BuyerBid.lead_id == lead.id, BuyerBid.buyer_id == buyer.id))
    if bid is None:
        bid = BuyerBid(lead_id=lead.id, buyer_id=buyer.id, status="invited")
        session.add(bid)
    return bid


def _log(session: Session, lead: Lead, outcome: str, notes: str, data: dict[str, Any]) -> None:
    session.add(Interaction(lead_id=lead.id, channel="disposition", outcome=outcome, notes=notes, debrief_json=data,
                            occurred_at=datetime.now()))


def invite(session: Session, lead: Lead, buyer: CashBuyer, walkthrough_at: datetime | None = None,
           notes: str = "", today: date | None = None) -> BuyerBid:
    blocked = disposition_gate(lead, today)
    if blocked:
        raise DispositionBlocked(blocked)
    bid = _bid(session, lead, buyer)
    if walkthrough_at:
        bid.walkthrough_date = walkthrough_at
        if bid.status in ("invited",):
            bid.status = "scheduled"
    if notes:
        bid.notes = "\n".join(x for x in (bid.notes, notes) if x)
    session.flush()
    return bid


def _closing_step(session: Session, lead: Lead, key: str, notes: str) -> None:
    """Tick the matching closing milestone when the deal has a closing file (engine/closing_coordinator.py)."""
    from engine import closing_coordinator as cc
    if not cc.milestones(session, lead):
        return
    cc.sync(session, lead)
    m = next((x for x in cc.milestones(session, lead) if x.milestone == key), None)
    if m is not None and m.status == "pending":
        cc.complete(session, lead, key, notes)


def mark_walked(session: Session, bid: BuyerBid, when: datetime | None = None, notes: str = "") -> BuyerBid:
    bid.walkthrough_date = when or bid.walkthrough_date or datetime.now()
    if bid.status in ("invited", "scheduled"):
        bid.status = "walked"
    lead = session.get(Lead, bid.lead_id)
    if lead is not None:
        _closing_step(session, lead, "VIRTUAL_WALKTHROUGH_AND_OIL_TANK_SWEEP",
                      f"walked by {buyer_label(bid.buyer)}; confirm the oil tank sweep is ordered")
    if notes:
        bid.notes = "\n".join(x for x in (bid.notes, notes) if x)
    session.flush()
    return bid


def record_bid(session: Session, lead: Lead, buyer: CashBuyer, amount: float, emd: float | None = None,
               walkthrough_at: datetime | None = None, notes: str = "", today: date | None = None
               ) -> tuple[BuyerBid, list[str]]:
    """Record (or update) a buyer's written bid. Returns (bid, warnings)."""
    blocked = disposition_gate(lead, today)
    if blocked:
        raise DispositionBlocked(blocked)
    if amount <= 0:
        raise DispositionBlocked(["Bid amount must be positive."])
    bid = _bid(session, lead, buyer)
    bid.bid_amount, bid.emd_amount = float(amount), (float(emd) if emd is not None else bid.emd_amount)
    if walkthrough_at:
        bid.walkthrough_date = walkthrough_at
    if bid.status not in ("accepted",):
        bid.status = "bid"
    if notes:
        bid.notes = "\n".join(x for x in (bid.notes, notes) if x)
    warnings: list[str] = []
    contract = lead.offer_amount or 0
    if contract and amount < contract:
        warnings.append(f"{money(amount)} is below our contract price of {money(contract)} — accepting it loses money.")
    elif contract:
        warnings.append(f"Assignment fee at this bid: {money(amount - contract)}.")
    deposit = float(settings().get("contracts", {}).get("assignee_deposit", 5000))
    if bid.emd_amount is not None and bid.emd_amount < deposit:
        warnings.append(f"Deposit {money(bid.emd_amount)} is below the {money(deposit)} assignment deposit we require.")
    _log(session, lead, "BUYER_BID", f"{buyer_label(buyer)}: {money(amount)}",
         {"buyer_id": buyer.id, "amount": amount, "emd": bid.emd_amount})
    session.flush()
    return bid, warnings


def bids_for(session: Session, lead: Lead) -> list[BuyerBid]:
    rows = session.scalars(select(BuyerBid).where(BuyerBid.lead_id == lead.id).options(selectinload(BuyerBid.buyer)))
    return sorted(rows, key=lambda b: (b.status != "accepted", -(b.bid_amount or 0), b.id))


def best_bid(session: Session, lead: Lead) -> BuyerBid | None:
    """Highest open bid; a bid that meets the deposit requirement beats a slightly higher one that doesn't."""
    deposit = float(settings().get("contracts", {}).get("assignee_deposit", 5000))
    live = [b for b in bids_for(session, lead) if b.status in OPEN_BID and b.bid_amount]
    if not live:
        return None
    return max(live, key=lambda b: ((b.emd_amount or 0) >= deposit, b.bid_amount))


def accept_bid(session: Session, bid: BuyerBid, structure: str = "assignment", split_pct: float | None = None,
               assignee_name: str | None = None, out_root: Any = None, today: date | None = None
               ) -> tuple[Generated, list[str]]:
    """Accept one bid: others become backups, the lead moves to ASSIGNED and the Assignment (or JV) is generated.
    Buyers found through mailing clusters have no name on record — pass the legal name/entity they gave you."""
    lead = session.get(Lead, bid.lead_id)
    assert lead is not None
    blocked = disposition_gate(lead, today)
    if blocked:
        raise DispositionBlocked(blocked)
    if structure not in ("assignment", "jv"):
        raise ValueError("structure must be 'assignment' or 'jv'")
    if not bid.bid_amount:
        raise DispositionBlocked(["Record the buyer's bid amount first."])
    contract = lead.offer_amount or 0
    if contract and bid.bid_amount < contract:
        raise DispositionBlocked([f"Bid {money(bid.bid_amount)} is below our {money(contract)} contract price."])
    buyer = bid.buyer
    if assignee_name:
        buyer.contact_name = assignee_name.strip()
    if not buyer_display_name(buyer):
        raise DispositionBlocked(["This buyer has no name on record — pass the assignee's legal name or entity "
                                  "(--assignee \"ABC Homes LLC\") so the assignment names the right party."])
    if structure == "assignment":
        doc = generate(session, lead, "assignment", price=bid.bid_amount, buyer=buyer, out_root=out_root, today=today)
    else:
        doc = generate(session, lead, "jv", partner=buyer_label(buyer),
                       split_pct=split_pct, out_root=out_root, today=today)
    for other in bids_for(session, lead):
        if other.id != bid.id and other.status in ("bid", "accepted"):
            other.status = "backup"
    bid.status = "accepted"
    _closing_step(session, lead, "ASSIGNMENT_OR_DOUBLE_CLOSE_EXECUTED",
                  f"{structure} with {buyer_label(buyer)} at {money(bid.bid_amount)}; buyer deposit "
                  f"{money(bid.emd_amount)} to escrow")
    lead.status_stage = advance_stage(lead.status_stage, LeadStage.ASSIGNED)
    fee = bid.bid_amount - contract if contract else None
    _log(session, lead, "BUYER_BID_ACCEPTED",
         f"{buyer_label(buyer)}: {money(bid.bid_amount)} ({structure})",
         {"buyer_id": buyer.id, "amount": bid.bid_amount, "fee": fee, "structure": structure, "doc": str(doc.paths)})
    session.flush()
    notes = [f"Accepted {money(bid.bid_amount)} from {buyer_label(buyer)}"
             + (f" — assignment fee {money(fee)}" if fee is not None else "") + ".",
             "Send the signed assignment and the buyer's deposit receipt to the title company; tell the backup "
             "bidders they are backups (not declined)."]
    return doc, notes


def matched_buyers(session: Session, lead: Lead, limit: int = 5) -> list[CashBuyer]:
    rows = session.scalars(select(BuyerMatch).where(BuyerMatch.lead_id == lead.id).order_by(BuyerMatch.rank)
                           .options(selectinload(BuyerMatch.buyer)).limit(limit))
    return [m.buyer for m in rows]


def find_buyer(session: Session, ref: str) -> CashBuyer | None:
    """A buyer by id or a case-insensitive name fragment (entity or contact)."""
    if re.fullmatch(r"\d+", ref.strip()):
        return session.get(CashBuyer, int(ref))
    like = f"%{ref.strip()}%"
    return session.scalars(select(CashBuyer).where((CashBuyer.entity_name.ilike(like)) | (CashBuyer.contact_name.ilike(like)))
                           .order_by(CashBuyer.reliability_score.desc())).first()
