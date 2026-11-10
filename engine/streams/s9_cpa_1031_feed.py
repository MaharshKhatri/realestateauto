"""S9 Cost-seg and 1031 trigger feed: a weekly public-record feed for cost-segregation firms, 1031 qualified
intermediaries and real-estate CPAs (flat monthly subscription).

Two public facts start most of those engagements:
- Cost segregation: a business entity just bought a 2+ unit residential or a commercial / apartment (class 4A / 4C)
  property for $500K-$5M. A study reclassifies building components into shorter recovery periods; under the 2025
  federal law components of property acquired after January 19, 2025 may qualify for 100% bonus depreciation. The
  feed says "may qualify" and nothing more: eligibility, recovery periods and the state treatment (New Jersey's GIT
  and CBT do not follow federal bonus depreciation) are the client's CPA's call.
- 1031 exchanges: a multi-family / class 4 property is listed for sale, its last recorded sale is 8+ years old and
  the ask is 1.5x or more that sale, so a large gain is likely on a sale. Long holds also keep out flip inventory
  ("held primarily for sale" never qualifies).

Sources, all already in the database: recorded purchases on our cash-buyer records (`recent_purchases_json`), deed
exports (`deed_transfers`), MOD-IV last-sale fields on parcels, and the public MLS listings our harvest stores (each
linked to its parcel through the STALE_LISTING signal that shares its `source_ref`). MOD-IV lags months behind the
deed records, so cost-seg triggers look back 18 months.

Privacy: buyers are shown only when they are business entities (trusts, estates, lenders, churches and governments
are left out: a personal trust carries a person's name; a lender's REO deed or a tax-exempt owner is no cost-seg
client). Cost-seg rows show the property's street, the business-entity buyer, the price and the deed month; 1031 rows
show the public listing address, the town, the ask, the last recorded sale and the listing brokerage, never an owner
(often individuals). Every row passes the Daniel's Law / stop suppression list.

The pitch is the 2-step funnel (2026-11-10): Touch 1 is two plain sentences with no attachment and no "$" (how many
rows we pulled in the prospect's county, one public-record street, "Want the property and buyer-entity list?"). The
feed built for that county, with the named row first, waits as the held sample: its masked copy (`sample_path`: rows
1-2 in full, the property and the entity / brokerage hidden from row 3) with the flat price, released in the
prospect's own thread when they say yes. The second cold touch is one sentence in the first one's thread.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from db.models import (
    CashBuyer,
    Contact,
    ContactType,
    DeedTransfer,
    DistressSignal,
    FirmTarget,
    Lead,
    Listing,
    Property,
    SignalType,
)
from engine.streams.base import (
    PRIORITY_SPECIALTY,
    SAMPLE_FULL_ROWS,
    Outbound,
    Stream,
    StreamContext,
    StreamResult,
    entity_like,
    money,
    operator_footer,
    render_table_pdf,
    sample_path,
    slug,
)
from engine.streams.s6_tax_appeals import (
    earlier_pitch,
    names_street,
    pdf_pages,
    pitch_due,
    place_ok,
    thread_subject,
)

KEY = "s9"
TARGET_KINDS = ("cost_segregation_firm", "qualified_intermediary_1031", "real_estate_cpa")
COMMERCIAL = ("4A", "4C")
QI = "qualified_intermediary_1031"
STATEWIDE = {"", "statewide", "nj", "new jersey", "all"}
ACTIVE_STATUSES = ("for_sale", "active")
ELIGIBILITY_LINE = "Triggers from public records; eligibility is for the client's CPA to determine."
BONUS_LINE = ("Under the 2025 federal law, building components with recovery periods of 20 years or less of property "
              "acquired after January 19, 2025 may qualify for 100% bonus depreciation; New Jersey's gross income and "
              "corporation business taxes do not follow federal bonus depreciation.")
DEFAULTS: dict[str, Any] = {
    "min_price": 500_000, "max_price": 5_000_000, "lookback_months": 18, "min_hold_years": 8,
    "min_gain_multiple": 1.5, "max_annual_growth": 0.12, "min_sale_price": 25_000, "rows_costseg": 12,
    "rows_1031": 12, "pitches_per_run": 3, "price_monthly": 500, "max_scan": 5000, "min_rows_to_pitch": 3,
    "max_parcel_miles": 1.0,
}
# Names that pass entity_like but are no cost-seg client or carry a private person's name.
_NOT_A_BUSINESS = re.compile(
    r"\b(TRUST|TRUSTS|TRUSTEE|TRUSTEES|TR|TRS|REVOCABLE|IRREVOCABLE|(?<!REAL )ESTATE|EST|EXECUTOR|EXECUTRIX|"
    r"ADMINISTRATOR|HEIRS|C/O|ET ?ALS?|CHURCH|CONGREGATION|MINISTR(?:Y|IES)|CONDOMINIUM|CONDO|ASSOCIATION|ASSN|"
    r"AUTHORITY)\b")


def _lim(ctx: StreamContext, key: str) -> Any:
    v = (ctx.limits or {}).get(key)
    return DEFAULTS[key] if v is None else v


# -- small pure helpers -------------------------------------------------------------------------------------------
def business_entity(name: str | None) -> bool:
    """A business that buys investment property: entity_like, and not a trust / estate (a person's name), a lender
    (an REO deed), a government, a church or an association (tax-exempt or not an investor)."""
    from core.text import clean, is_government, is_lender
    if not entity_like(name):
        return False
    up = clean(name).upper().replace(",", " ")
    return not (_NOT_A_BUSINESS.search(up) or is_government(up) or is_lender(up))


def units_label(cls: str | None, units: int | None) -> str | None:
    """The trigger's property kind, or None when it is not a 2+ unit residential / class 4A / 4C property."""
    c = (cls or "").upper().strip()
    if c == "4C":
        return "apartment (class 4C)"
    if c == "4A":
        return "commercial / mixed use (class 4A)"
    if units and int(units) >= 2 and c in ("2", ""):
        return f"{int(units)} units (class 2)" if c else f"{int(units)} units"
    return None


def months_before(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - int(n), 12)
    return date(y, m + 1, min(d.day, 28))


def years_before(d: date, n: int) -> date:
    return date(d.year - int(n), d.month, min(d.day, 28))


def gain_multiple(list_price: float | None, last_price: float | None) -> float | None:
    return float(list_price) / float(last_price) if list_price and last_price else None


def annual_growth(multiple: float, sold: date, today: date) -> float:
    """Compound yearly growth from the last recorded sale to the ask. Above ~12% a year sustained for 8+ years the
    recorded "sale" was nominal (a family deed) or the house was rebuilt since: either way not the basis."""
    years = max((today - sold).days / 365.25, 1.0)
    return multiple ** (1 / years) - 1


def street_key(street: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (street or "").upper())


def _d(v: Any) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _e(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


def clip(text: str | None, n: int) -> str:
    """One table line per row: long names are cut (the full public record is one search away)."""
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 3].rstrip(" ,.-") + "..."


def _postal(text: str | None) -> str:
    from core.text import postal_title
    return postal_title(text or "")


def _title(text: str | None) -> str:
    from core.text import muni_base, postal_title
    return postal_title(muni_base(text or "")) if text else ""


def town_names(session: Session) -> dict[str, str]:
    """mun_code -> town name, from the Chapter 123 table and our own parcels (deed exports carry only the code)."""
    from engine.streams.s6_tax_appeals import load_ratio_table
    out = {code: str(row.get("name") or "") for code, row in (load_ratio_table().get("ratios") or {}).items()}
    for code, name in session.execute(select(Property.mun_code, Property.municipality).where(
            Property.mun_code.is_not(None), Property.municipality.is_not(None)).distinct()).all():
        out.setdefault(str(code), str(name))
    return out


def _mun_from_parcel(parcel_id: str | None) -> str | None:
    parts = (parcel_id or "").split("-")
    return parts[1] if len(parts) > 2 and parts[1].isdigit() and len(parts[1]) == 4 else None


# -- cost-seg triggers --------------------------------------------------------------------------------------------
@dataclass
class CostSegRow:
    entity: str
    town: str
    county: str
    price: float
    deed_date: date
    kind: str
    source: str                      # "deed export" / "MOD-IV" / "recorded purchase"
    keys: tuple[tuple[Any, ...], ...]  # one purchase seen in two sources shares a key
    street: str = ""                 # the property's street (blank when the record has none)


def purchase_keys(entity: str | None, street: str | None, price: float | None, when: date) -> tuple[tuple[Any, ...], ...]:
    """Two ways to recognise one purchase across sources (their town fields and street spellings differ): the street
    and the month, or the buyer, the price and the month."""
    month = when.strftime("%Y-%m")
    keys: list[tuple[Any, ...]] = [("deal", street_key(entity), round(float(price or 0)), month)]
    if street_key(street):
        keys.append(("addr", street_key(street), month))
    return tuple(keys)


def _in_band(price: Any, lo: float, hi: float) -> bool:
    try:
        return lo <= float(price or 0) <= hi
    except (TypeError, ValueError):
        return False


def cost_seg_rows(session: Session, today: date, limits: dict[str, Any], metrics: dict[str, int]) -> list[CostSegRow]:
    """Entity purchases of 2+ unit / class 4A / 4C property inside the price band and the lookback window."""
    lo, hi = float(limits["min_price"]), float(limits["max_price"])
    since = months_before(today, int(limits["lookback_months"]))
    cap = int(limits["max_scan"])
    towns = town_names(session)
    found: list[CostSegRow] = []
    seen: set[tuple[Any, ...]] = set()

    def add(row: CostSegRow, *places: Any) -> None:
        if seen.intersection(row.keys):
            return
        seen.update(row.keys)                    # a suppressed purchase stays out under every source
        if not all(place_ok(session, pl) for pl in places if pl is not None):
            metrics["suppressed"] += 1
            return
        found.append(row)

    # 1. deed exports (the freshest public record)
    parcels = {}
    deeds = session.scalars(select(DeedTransfer).where(
        DeedTransfer.deed_date >= since, DeedTransfer.sale_price >= lo, DeedTransfer.sale_price <= hi)
        .order_by(DeedTransfer.deed_date.desc()).limit(cap)).all()
    if deeds:
        parcels = {p.parcel_id: p for p in session.scalars(select(Property).where(
            Property.parcel_id.in_({d.parcel_id for d in deeds})))}
    for d in deeds:
        metrics["deeds_seen"] += 1
        prop = parcels.get(d.parcel_id)
        kind = units_label(d.property_class or (prop.property_class if prop else None),
                           prop.dwellings if prop else None)
        if not kind or not business_entity(d.grantee) or not d.deed_date:
            continue
        mun = (prop.mun_code if prop else None) or _mun_from_parcel(d.parcel_id) or ""
        town = d.municipality or (prop.municipality if prop else None) or towns.get(mun, "")
        street = d.property_location or (prop.street_address if prop else "") or ""
        place = SimpleNamespace(street_address=street, city=town, zip_code=prop.zip_code if prop else "",
                                owner_mailing_address=d.grantee_address, owner_city_state_zip="")
        add(CostSegRow(entity=str(d.grantee).strip(), town=_title(town), county=(d.county or "").title(),
                       price=float(d.sale_price or 0), deed_date=d.deed_date, kind=kind, source="deed export",
                       keys=purchase_keys(d.grantee, street, d.sale_price, d.deed_date), street=street), place, prop)

    # 2. purchases recorded on our cash-buyer records
    for cb in session.scalars(select(CashBuyer).where(or_(CashBuyer.last_purchase_date >= since,
                                                         CashBuyer.last_purchase_date.is_(None))).limit(cap)):
        if not business_entity(cb.entity_name):
            continue
        for x in cb.recent_purchases_json or []:
            if not isinstance(x, dict):
                continue
            metrics["purchases_seen"] += 1
            when = _d(x.get("deed_date"))
            try:
                units = int(x.get("units") or 0)
            except (TypeError, ValueError):
                units = 0
            kind = units_label(str(x.get("class") or ""), units)
            if not kind or not when or when < since or not _in_band(x.get("price"), lo, hi):
                continue
            mun = str(x.get("mun_code") or "")
            town = str(x.get("municipality") or towns.get(mun, ""))
            place = SimpleNamespace(street_address=x.get("address") or "", city=town, zip_code="",
                                    owner_mailing_address=cb.mailing_address, owner_city_state_zip="")
            add(CostSegRow(entity=cb.entity_name.strip(), town=_title(town), county=str(x.get("county") or "").title(),
                           price=float(x["price"]), deed_date=when, kind=kind, source="recorded purchase",
                           keys=purchase_keys(cb.entity_name, x.get("address"), x["price"], when),
                           street=str(x.get("address") or "")), place)

    # 3. MOD-IV last sale on parcels owned by an entity
    props = session.scalars(select(Property).where(
        Property.last_sale_date >= since, Property.last_sale_price >= lo, Property.last_sale_price <= hi,
        or_(Property.dwellings >= 2, Property.property_class.in_(COMMERCIAL))).limit(cap)).all()
    for p in props:
        metrics["parcels_seen"] += 1
        kind = units_label(p.property_class, p.dwellings)
        if not kind or not business_entity(p.owner_name) or not p.last_sale_date:
            continue
        add(CostSegRow(entity=str(p.owner_name).strip(), town=_title(p.municipality), county=(p.county or "").title(),
                       price=float(p.last_sale_price or 0), deed_date=p.last_sale_date, kind=kind, source="MOD-IV",
                       keys=purchase_keys(p.owner_name, p.street_address, p.last_sale_price, p.last_sale_date),
                       street=p.street_address or ""), p)
    return sorted(found, key=lambda r: (-r.price, -r.deed_date.toordinal()))


# -- 1031 triggers ------------------------------------------------------------------------------------------------
@dataclass
class ExchangeRow:
    town: str
    county: str
    street: str
    kind: str
    list_price: float
    last_sale_date: date
    last_sale_price: float
    multiple: float
    brokerage: str


def _listing_pairs(session: Session, cap: int) -> list[tuple[Listing, Property, DistressSignal | None]]:
    """Active listings with their parcel: a direct link, else the active STALE_LISTING signal sharing source_ref."""
    out: dict[int, tuple[Listing, Property, DistressSignal | None]] = {}
    direct = session.execute(select(Listing, Property).join(Property, Listing.property_id == Property.id).where(
        Listing.status.in_(ACTIVE_STATUSES)).limit(cap)).all()
    for lst, prop in direct:
        out[lst.id] = (lst, prop, None)
    linked = session.execute(
        select(Listing, Property, DistressSignal)
        .join(DistressSignal, and_(DistressSignal.source_ref == Listing.source_ref,
                                   DistressSignal.signal_type == SignalType.STALE_LISTING,
                                   DistressSignal.active.is_(True)))
        .join(Property, DistressSignal.property_id == Property.id)
        .where(Listing.status.in_(ACTIVE_STATUSES)).limit(cap)).all()
    for lst, prop, sig in linked:
        if lst.id not in out or out[lst.id][2] is None:
            out[lst.id] = (lst, prop, sig)
    return list(out.values())


def _brokerage(session: Session, prop: Property, sig: DistressSignal | None) -> str:
    from core.text import clean
    broker = clean(str((sig.raw_metadata_json or {}).get("listing_broker") or "")) if sig is not None else ""
    if broker:
        return broker
    lead_id = session.scalar(select(Lead.id).where(Lead.property_id == prop.id))
    if lead_id is None:
        return ""
    company = session.scalar(select(Contact.company).where(
        Contact.lead_id == lead_id, Contact.contact_type == ContactType.LISTING_AGENT,
        Contact.company.is_not(None)).limit(1))
    return clean(company or "")


def _far(lst: Listing, prop: Property, max_miles: float) -> bool:
    """The listing's own point is more than `max_miles` from the parcel it was linked to: a different house."""
    from core.geo import haversine_miles
    if None in (lst.lat, lst.lon, prop.lat, prop.lon):
        return False
    return haversine_miles(float(lst.lat), float(lst.lon), float(prop.lat), float(prop.lon)) > max_miles


def exchange_rows(session: Session, today: date, limits: dict[str, Any], metrics: dict[str, int]) -> list[ExchangeRow]:
    held_since = years_before(today, int(limits["min_hold_years"]))
    multiple = float(limits["min_gain_multiple"])
    out: list[ExchangeRow] = []
    for lst, prop, sig in _listing_pairs(session, int(limits["max_scan"])):
        metrics["listings_seen"] += 1
        cls = (prop.property_class or "").upper()
        multi = (lst.property_type or "").lower() == "multi_family"
        if not (multi or cls in COMMERCIAL) or cls[:1] in ("1", "3"):     # vacant land, exempt, farm parcels
            continue
        last, last_price = prop.last_sale_date, float(prop.last_sale_price or 0)
        if not last or last > held_since or last_price < float(limits["min_sale_price"]):
            continue
        m = gain_multiple(lst.list_price, last_price)
        if m is None or m < multiple:
            continue
        if annual_growth(m, last, today) > float(limits["max_annual_growth"]):
            metrics["nominal_sale"] += 1
            continue
        if _far(lst, prop, float(limits["max_parcel_miles"])):
            metrics["wrong_parcel"] += 1
            continue
        street = lst.address or prop.street_address or ""
        town = lst.city or prop.city or _title(prop.municipality)       # a listing's city is already a postal name
        place = SimpleNamespace(street_address=street, city=town, zip_code=lst.zip_code or prop.zip_code or "")
        if not place_ok(session, prop) or not place_ok(session, place):
            metrics["suppressed"] += 1
            continue
        kind = units_label(cls, prop.dwellings) or "multi-family (listing)"
        out.append(ExchangeRow(town=_postal(town), county=(prop.county or "").title(), street=street, kind=kind,
                               list_price=float(lst.list_price or 0), last_sale_date=last, last_sale_price=last_price,
                               multiple=m, brokerage=_brokerage(session, prop, sig)))
    return sorted(out, key=lambda r: -(r.list_price - r.last_sale_price))


# -- the feed -----------------------------------------------------------------------------------------------------
def street_text(row: CostSegRow | ExchangeRow) -> str:
    """The street line only ("12-14 ELM ST, ELIZABETH" -> "12-14 Elm St"): the town has its own column."""
    from core.text import postal_title
    return postal_title((row.street or "").split(",")[0]).strip(" .,")


def short_kind(kind: str) -> str:
    """The table's Kind column: "apartment (class 4C)" -> "Apartments (4C)", "3 units (class 2)" -> "3 units"."""
    k = kind or ""
    if "4C" in k:
        return "Apartments (4C)"
    if "4A" in k:
        return "Commercial (4A)"
    m = re.match(r"(\d+) units", k)
    if m:
        return f"{m.group(1)} units"
    return "Multi-family" if "multi" in k.lower() else k


def feed_rows(cs: list[CostSegRow], ex: list[ExchangeRow], exchange_first: bool = False) -> list[list[str]]:
    """One line per row. The Property and entity / brokerage columns are the identifying ones (masked from row 3 in
    the sample); town, kind, price and the public-record detail stay visible."""
    def place(town: str, county: str) -> str:
        return _e(clip(f"{town} ({county})" if town and county else town or f"{county} County", 28))

    a = [["A cost seg", place(r.town, r.county), _e(clip(street_text(r), 30)) or "-", _e(short_kind(r.kind)),
          f"<b>{_e(clip(r.entity, 38))}</b>", f"{money(r.price)} paid",
          f"Bought {r.deed_date:%b %Y} ({r.source})"] for r in cs]
    b = [["B 1031", place(r.town, r.county), _e(clip(street_text(r), 30)) or "-", _e(short_kind(r.kind)),
          _e(clip(r.brokerage, 38)) or "-", f"{money(r.list_price)} ask",
          f"Last sale {r.last_sale_date.year} at {money(r.last_sale_price)}; ask {r.multiple:.1f}x"] for r in ex]
    return b + a if exchange_first else a + b


FEED_HEAD = ["Table", "Town (county)", "Property", "Kind", "A: buyer entity · B: listing brokerage", "Price",
             "Public-record detail"]


def render_feed(path: Path, cs: list[CostSegRow], ex: list[ExchangeRow], *, week_of: date, op: dict[str, str],
                limits: dict[str, Any], scope: str = "", exchange_first: bool = False) -> Path:
    lo, hi = float(limits["min_price"]), float(limits["max_price"])
    note_a = (f"<b>A. Cost-seg triggers</b> ({len(cs)}): business-entity purchases of 2+ unit residential or class 4A / "
              f"4C property for {money(lo)}-{money(hi)} recorded in the last {int(limits['lookback_months'])} months "
              "(deed exports, recorded purchases, MOD-IV; MOD-IV lags the deed records). Trusts, estates, lenders and "
              "tax-exempt owners are left out. Largest first.")
    note_b = (f"<b>B. 1031 triggers</b> ({len(ex)}): multi-family / class 4 properties listed for sale whose last "
              f"recorded sale is {int(limits['min_hold_years'])}+ years old and whose ask is "
              f"{float(limits['min_gain_multiple']):.1f}x or more that sale. Last sale from MOD-IV: a later or "
              "non-arm's-length transfer changes the picture. No owner names; addresses are the public listing "
              "addresses. Largest ask minus last sale first.")
    notes = [note_b, note_a] if exchange_first else [note_a, note_b]
    where = f" · {scope}" if scope else ""
    return render_table_pdf(
        path, title="NJ Cost-Seg & 1031 Trigger Feed" + (f": {scope}" if scope and scope != "New Jersey" else ""),
        subtitle=(f"Week of {week_of:%B} {week_of.day}, {week_of.year}{where} · Table A: {len(cs)} recent entity "
                  f"purchases · Table B: {len(ex)} long-held multi-family listings · {ELIGIBILITY_LINE}"),
        head=FEED_HEAD, rows=feed_rows(cs, ex, exchange_first),
        notes=notes + [BONUS_LINE],
        box=f"<b>{ELIGIBILITY_LINE}</b> Research data only, not tax advice. A dash (-) means not on record.",
        footer=operator_footer(op), col_widths=[0.62, 1.45, 1.75, 1.0, 2.2, 0.95, 2.13])


def fit_feed(out: Path, cs: list[CostSegRow], ex: list[ExchangeRow], *, exchange_first: bool = False, **kw: Any
             ) -> tuple[Path, list[CostSegRow], list[ExchangeRow]]:
    """One page, always, for the feed and its masked sample (whose subtitle is a line longer): drop a row from the end
    of the longer table (the other table on a tie, so the lead table's first row, the one a pitch names, stays) until
    both fit. The sample is rewritten with every try; a stale one from a longer try is removed first."""
    sample = sample_path(out)
    while True:
        sample.unlink(missing_ok=True)
        path = render_feed(out, cs, ex, exchange_first=exchange_first, **kw)
        if (pdf_pages(path) <= 1 and (not sample.exists() or pdf_pages(sample) <= 1)) or len(cs) + len(ex) <= 1:
            return path, cs, ex
        lead, other = (ex, cs) if exchange_first else (cs, ex)
        if len(lead) > len(other) or not other:
            lead = lead[:-1]
        else:
            other = other[:-1]
        cs, ex = (other, lead) if exchange_first else (lead, other)


class FeedSet:
    """The week's feeds: the statewide product plus one per pitched scope, each rendered once (the same rows in the
    same order reuse the file already written)."""

    def __init__(self, ctx: StreamContext, limits: dict[str, Any], week: str, monday: date) -> None:
        self.ctx, self.limits, self.week, self.monday = ctx, limits, week, monday
        self.cache: dict[tuple[Any, ...], tuple[Path, list[CostSegRow], list[ExchangeRow]]] = {}

    @property
    def rendered(self) -> int:
        return len(self.cache)

    def get(self, cs: list[CostSegRow], ex: list[ExchangeRow], *, exchange_first: bool = False, scope: str = "",
            stem: str = "") -> tuple[Path, list[CostSegRow], list[ExchangeRow]]:
        k = (tuple(map(id, cs)), tuple(map(id, ex)), exchange_first)
        if k not in self.cache:
            out = self.ctx.out_dir / f"{stem or f'{KEY}_feed_{self.week}'}.pdf"
            if any(out == v[0] for v in self.cache.values()):      # another order of rows already took the name
                out = out.with_name(f"{out.stem}_{len(self.cache)}.pdf")
            self.cache[k] = fit_feed(out, cs, ex, exchange_first=exchange_first, week_of=self.monday,
                                     op=self.ctx.operator or {}, limits=self.limits, scope=scope)
        return self.cache[k]


# -- the pitch: the 2-step funnel (2026-11-10) ---------------------------------------------------------------------
def county_key(text: str | None) -> str:
    return re.sub(r"\s+county$", "", (text or "").strip().lower())


def spoken_price(v: float) -> str:
    """A recorded price in words for Touch 1, which carries no "$": "1.2 million dollars", "650,000 dollars"."""
    if v >= 1_000_000:
        return f"{f'{v / 1_000_000:.2f}'.rstrip('0').rstrip('.')} million dollars"
    return f"{v:,.0f} dollars"


def lead_table(kind: str, cs: list[CostSegRow], ex: list[ExchangeRow]) -> str | None:
    """Which table the pitch is about: a 1031 intermediary hears about long-held listings, a cost-seg firm about entity
    purchases, a CPA about purchases first (listings when there are none). Only rows with a street can be named."""
    has_cs, has_ex = any(r.street for r in cs), any(r.street for r in ex)
    if kind == QI:
        return "ex" if has_ex else None
    if kind == "cost_segregation_firm":
        return "cs" if has_cs else None
    return "cs" if has_cs else "ex" if has_ex else None


def choose_scope(t: FirmTarget, cs_all: list[CostSegRow], ex_all: list[ExchangeRow], min_rows: int
                 ) -> tuple[str, str, list[CostSegRow], list[ExchangeRow], str] | None:
    """The prospect's own county when its rows can carry a pitch, else New Jersey: (label, key, cs, ex, lead)."""
    ck = county_key(t.county)
    options: list[tuple[str, str, list[CostSegRow], list[ExchangeRow]]] = []
    if ck not in STATEWIDE:
        options.append((f"{ck.title()} County", ck, [r for r in cs_all if county_key(r.county) == ck],
                        [r for r in ex_all if county_key(r.county) == ck]))
    options.append(("New Jersey", "nj", cs_all, ex_all))
    for label, key, cs, ex in options:
        lead = lead_table(t.kind, cs, ex)
        if lead and len(cs) + len(ex) >= min_rows:
            return label, key, cs, ex, lead
    return None


def pitch_subject(lead: str, where: str, street: str, n: int) -> str:
    one, many = ("an entity purchase", "entity purchases") if lead == "cs" else ("a long-held listing",
                                                                                "long-held listings")
    return f"{street} + {n - 1} more {many} in {where}" if n > 1 else f"{street}: {one} in {where}"


def pitch_body(lead: str, where: str, n: int, row: CostSegRow | ExchangeRow, hold_years: int, first: str) -> str:
    """Touch 1: two plain sentences, no attachment, no "$", no tax advice: how many rows we pulled where, one
    public-record street (never an owner who is a person: buyers are business entities, listings name no owner),
    and a yes/no question."""
    street, town = street_text(row), f" in {row.town}" if row.town else ""
    which = "including " if n > 1 else ""
    if lead == "cs":
        what = (f"{n} recent multi-family and commercial purchases by business entities" if n > 1
                else "a recent multi-family or commercial purchase by a business entity")
        line = (f"We pulled {what} in {where} from public deed and tax records, {which}{street}{town} (bought for "
                f"{spoken_price(row.price)} in {row.deed_date:%B %Y}), that may fit cost-segregation or 1031 work. "
                "Want the property and buyer-entity list?")
    else:
        what = (f"{n} multi-family and commercial properties listed for sale" if n > 1
                else "a multi-family or commercial property listed for sale")
        line = (f"We pulled {what} in {where} whose last recorded sale was {hold_years} or more years ago, from public "
                f"listings and tax records, {which}{street}{town} (last sold in {row.last_sale_date.year}), that may "
                "fit 1031 work. Want the property and listing-brokerage list?")
    return f"Hello,\n\n{line}\n\n{first}".strip()


def followup_body(where: str, first: str) -> str:
    """The second cold touch: one sentence, no attachment, in the first touch's thread."""
    return f"Hello,\n\nShould I send the {where} list over?\n\n{first}".strip()


def sample_body(where: str, n_cs: int, n_ex: int, masked: bool, price: float) -> str:
    """Touch 2, sent with the sample only after their yes: what is attached, that eligibility is the client's CPA's
    call, the flat price and how to start."""
    hidden = ("the property and the buyer entity or brokerage" if n_cs and n_ex
              else "the property and buyer entity" if n_cs else "the property and listing brokerage")
    rows = f"the first {SAMPLE_FULL_ROWS} rows in full, the rest with {hidden} hidden" if masked else "every row in full"
    return (f"Here's this week's {where} list (attached): {rows}, all from public records, and the client's CPA "
            f"decides eligibility. The full weekly feed is ${price:,.0f} a month flat, no other charge, with no "
            "commission or per-deal fee. Reply \"start\" and I'll set it up.")


def pitches(session: Session, ctx: StreamContext, feeds: FeedSet, cs_all: list[CostSegRow],
            ex_all: list[ExchangeRow]) -> list[Outbound]:
    """One cold touch per due target (the outbox's gates checked first). Touch 1 names the first row of its lead
    table in the prospect's county (else New Jersey); the feed for that scope, with that row first, is the held
    sample. A target touched before gets the one-sentence follow-up in that thread. Both are kind "pitch", so the
    outbox's gap and max-touches rules hold for each. One key per target per ISO week."""
    limits = {k: _lim(ctx, k) for k in DEFAULTS}
    min_rows = int(limits["min_rows_to_pitch"])
    if not ctx.send or len(cs_all) + len(ex_all) < min_rows:
        return []
    first = (ctx.operator or {}).get("first", "")
    out: list[Outbound] = []
    targets = session.scalars(select(FirmTarget).where(
        FirmTarget.kind.in_(TARGET_KINDS), FirmTarget.stage.in_(("NEW", "CONTACTED")), FirmTarget.email.is_not(None))
        .order_by(FirmTarget.score.desc(), FirmTarget.id)).all()
    for t in targets:
        if len(out) >= int(limits["pitches_per_run"]):
            break
        key = f"{KEY}:pitch:{t.id}:{feeds.week}"
        scope = choose_scope(t, cs_all, ex_all, min_rows)
        if scope is None or not pitch_due(session, t, key, ctx.now):
            continue
        where, skey, cs, ex, lead = scope
        earlier = None    # Touch 1 only: the outbox sends the in-thread nudges (firm_outbox.queue_nudges)
        lead_all: list[Any] = list(cs if lead == "cs" else ex)
        named_ok = [r for r in lead_all if r.street]
        named = next((r for r in named_ok if earlier is not None and names_street(earlier.body, street_text(r))),
                     named_ok[0])
        lead_rows = [named] + [r for r in lead_all if r is not named]
        cs_in = lead_rows[: int(limits["rows_costseg"])] if lead == "cs" else cs[: int(limits["rows_costseg"])]
        ex_in = lead_rows[: int(limits["rows_1031"])] if lead == "ex" else ex[: int(limits["rows_1031"])]
        path, cs_shown, ex_shown = feeds.get(
            cs_in, ex_in, exchange_first=lead == "ex", scope=where,
            stem=f"{KEY}_feed_{feeds.week}_{slug(skey, 16)}_{lead}_{slug(street_text(named), 24)}")
        n = len(cs_shown if lead == "cs" else ex_shown)
        if not n:
            continue
        sample = sample_path(path)
        if earlier is not None:
            subject, body = thread_subject(earlier.subject), followup_body(where, first)
        else:
            subject = pitch_subject(lead, where, street_text(named), n)
            body = pitch_body(lead, where, n, named, int(limits["min_hold_years"]), first)
        out.append(Outbound(
            stream=KEY, key=key, to=t.email or "", subject=subject, body=body, priority=PRIORITY_SPECIALTY,
            kind="pitch", target_id=t.id, in_reply_to=earlier.message_id if earlier is not None else None,
            sample_body=sample_body(where, len(cs_shown), len(ex_shown), sample.exists(),
                                    float(limits["price_monthly"])),
            sample_attachments=[sample if sample.exists() else path]))
    return out


# -- the stream ---------------------------------------------------------------------------------------------------
class CostSegFeedStream(Stream):
    key = KEY
    name = "CPA cost-seg / 1031 feed"
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0             # the feed is weekly: its file and every pitch key carry the ISO week

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        res = StreamResult(stream=self.key)
        limits = {k: _lim(ctx, k) for k in DEFAULTS}
        today = ctx.now.date() if isinstance(ctx.now, datetime) else date.today()
        metrics = {"deeds_seen": 0, "purchases_seen": 0, "parcels_seen": 0, "listings_seen": 0, "suppressed": 0,
                   "wrong_parcel": 0, "nominal_sale": 0}
        cs_all = cost_seg_rows(session, today, limits, metrics)
        ex_all = exchange_rows(session, today, limits, metrics)
        cs, ex = cs_all[: int(limits["rows_costseg"])], ex_all[: int(limits["rows_1031"])]
        res.processed = metrics["deeds_seen"] + metrics["purchases_seen"] + metrics["parcels_seen"] + metrics["listings_seen"]
        res.found = len(cs_all) + len(ex_all)
        iso = ctx.now.isocalendar()
        feeds = FeedSet(ctx, limits, f"{iso[0]}-W{iso[1]:02d}", date.fromisocalendar(iso[0], iso[1], 1))
        if cs or ex:
            _, cs, ex = feeds.get(cs, ex)        # the week's statewide feed (the product)
            res.outbound = pitches(session, ctx, feeds, cs_all, ex_all)
        res.products = feeds.rendered
        res.metrics = {**metrics, "costseg_found": len(cs_all), "exchange_found": len(ex_all),
                       "feed_rows": len(cs) + len(ex), "pitches": len(res.outbound)}
        res.notes.append(f"{len(cs_all)} cost-seg and {len(ex_all)} 1031 triggers; feed rows {len(cs) + len(ex)}")
        return res


STREAM = CostSegFeedStream()
