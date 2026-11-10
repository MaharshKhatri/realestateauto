"""Stream 3: NJ sheriff-sale surplus research, sold to foreclosure-surplus attorneys for a flat fee.

Why there is a product: when a sheriff sale's winning bid is more than the foreclosing plaintiff is owed (plus costs),
the surplus is paid into court (N.J.S.A. 2A:50-37) and held in the Superior Court Trust Fund; junior lienholders and
then the former owner claim it by motion (R. 4:64-3 through the Office of Foreclosure; R. 4:57-2, the Trust Fund Unit
checks the order first), and what nobody claims escheats after 10 years (N.J.S.A. 46:30B-41). Attorneys who bring
those motions need to know which dockets are likely to produce surplus. Everything they need is public: the sheriff's
notice (docket, sheriff number, plaintiff, upset, liens the sale is subject to), the MOD-IV assessment and the recorded
sheriff's deed.

Two lists, one weekly one-page PDF per county (only counties with rows):
- SOLD TO A THIRD PARTY: the status (or the last status-history entry) says a third party bought ("Purchased - 3rd
  Party"; "Purchased" without "Plaintiff"), or a recorded sheriff's deed (NU code 12 / grantor "Sheriff") went to a
  grantee that is neither the plaintiff nor a lender. CivilView shows the status but no winning bid, so the row says
  "winning bid: from the recorded sheriff's deed or the sheriff (OPRA)". Surplus = bid - payoff - prior liens is
  computed only when a price is actually on record: a `sale_price` (or similar) key in the signal's metadata, once
  the scraper captures one, or the sheriff's deed price in deed_transfers.
- SURPLUS WATCHLIST: active sales in the next `window_days` (60) where a value figure (our as-is formula estimate,
  else the ARV estimate, else the equalized assessment) minus the known debt (engine.underwriter.known_debt) leaves
  >= `min_equity` ($75,000): a third-party bid near value would create surplus. Ranked by that estimated equity.

Legal lines this stream holds (CLAUDE.md, engine/streams/base.py): the dossier is for attorneys only; the defendant
is never named (nor any home phone); the street is shown only after `address_ok` (Daniel's Law / stop suppressions);
a private-person plaintiff or deed grantee is never named; value figures are labeled data, not an appraisal
(N.J.S.A. 45:14F). We are not surplus "finders" (N.J.S.A. 46:30B-106 regulates finder agreements; Haven Savings Bank
v. Zanolini, App. Div. 2010, voided one): research is sold to attorneys for a modest flat price, never a percentage,
owners are never contacted, nothing is legal advice. The attorney verifies every docket on eCourts and decides whom,
if anyone, to contact; the price never depends on a case, a claim or a contact (N.J.S.A. 45:15-3; NJ RPC 7.2(c);
Joint Opinion 741 (2021): a per-lead price far above the data's value can look like a disguised referral fee).

The 2-step funnel (2026-11-10): Touch 1 to an attorney is two plain sentences with no attachment, no link and no
price: the true count of sales in their county's dossier and one case from it (the first row, so the sample shows it
in full: street + sheriff #, never a defendant), then "Would the case breakdown be useful for your practice if I
e-mailed it over?". The county PDF's masked sample (base.sample_path: 2 rows in full, the street and docket / sheriff
# hidden on the rest) and the flat price wait as the held Touch 2, released in their thread on a yes. A second cold
touch, a week or more later, is one sentence in the same thread ("Should I send this week's Essex County sheet over?").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from core.text import clean, is_lender, muni_base, postal_title
from db.models import DeedTransfer, DistressSignal, FirmOutbox, FirmTarget, Property, SignalType, Valuation
from engine.streams.base import (PRIORITY_SPECIALTY, SAMPLE_FULL_ROWS, Outbound, Stream, StreamContext, StreamResult,
                                 address_ok, entity_like, kmoney, operator_footer, render_table_pdf, sample_path, slug)

TARGET_KIND = "foreclosure_surplus_attorney"
STATUTORY_ADJOURNMENTS = 5          # N.J.S.A. 2A:17-36 as amended by P.L.2019, c.71: 2 debtor, 2 lender, 1 agreed
STATEWIDE = {"", "statewide", "nj", "new jersey", "all"}
SHERIFF_DEED_NU = "12"              # SR1A non-usable code for a sheriff's deed
BID_SOURCE = "winning bid: from the recorded sheriff's deed or the sheriff (OPRA)"

DEFAULTS: dict[str, Any] = {
    "window_days": 60, "min_equity": 75_000, "lookback_days": 730, "min_surplus": 5_000, "max_signals": 5_000,
    "max_rows_per_pdf": 12, "min_confidence": 45, "partial_debt_share": 0.25, "price_monthly": 400,
    "price_report": 150, "pitches_per_run": 3, "statewide_attachments": 2, "deed_window_days": 240,
}

# -- classifiers (pure) -------------------------------------------------------------------------------------------
# A third-party purchase: "Purchased - 3rd Party", "Sold to Third Party Bidder", "3rd Party Purchase", "Struck off to
# a third party", or a bare "Purchased". A plaintiff's credit bid ("Purchased - Plaintiff") makes no surplus by itself.
_THIRD = r"(?:3rd|third)[\s-]*party"
_BOUGHT = r"(?:purchas\w*|sold|bought|struck\s+off|bid\s+in|awarded)"
THIRD_PARTY_SALE = re.compile(rf"\b{_BOUGHT}\b[^.;]{{0,30}}\b{_THIRD}\b|\b{_THIRD}\b[^.;]{{0,20}}\b{_BOUGHT}\b",
                              re.I)
PURCHASED = re.compile(r"\bpurchased\b", re.I)
PLAINTIFF = re.compile(r"\b(plaintiff|plt?f|lender)\b", re.I)
# ... unless the same status undoes it ("3rd party sale vacated", "purchase set aside") or denies it ("no 3rd party").
UNDONE = re.compile(r"\b(vacat\w*|set\s+aside|rescind\w*|void\w*|cancel\w*|redeem\w*|withdrawn)\b", re.I)
NO_THIRD = re.compile(rf"\b(no|not|without)\s+(a\s+|any\s+)?{_THIRD}", re.I)
GENERIC_SOLD = re.compile(r"(?i)\s*(sold|closed|sold\W*cancell?ed)\s*")
CLOSED = re.compile(r"PURCHASED|SOLD|CANCEL|REDEEMED|BIDDER PAID|SATISFIED|WITHDRAW|VACATED|DISMISSED BY PLAINTIFF|"
                    r"PAID IN FULL|REINSTATED|DEED|WRIT EXPIRED", re.I)
# A foreclosure by a condo / homeowners association or a town is usually junior to a mortgage the sale is subject to:
# value minus that small judgment is not equity, so those sales never reach the watchlist.
JUNIOR_PLAINTIFF = re.compile(r"\b(CONDOMINIUM|CONDO|HOMEOWNERS?|HOA|TOWNHOUSES?|COOPERATIVE|COMMUNITY ASSOCIATION|"
                              r"ASSOCIATION|ASSN|TOWNSHIP OF|CITY OF|BOROUGH OF|TOWN OF|COUNTY OF|VILLAGE OF|"
                              r"MUNICIPAL|SEWERAGE|UTILITIES AUTHORITY)\b", re.I)
SALE_PRICE_KEYS = ("sale_price", "sold_price", "winning_bid", "sold_amount", "purchase_price", "high_bid")
# words every lender / trust name shares: they never prove a deed grantee is the plaintiff
_GENERIC_WORDS = {"THE", "OF", "AS", "FOR", "AND", "BANK", "NATIONAL", "ASSOCIATION", "TRUST", "TRUSTEE", "COMPANY",
                  "LLC", "INC", "CORP", "CORPORATION", "NA", "N.A.", "FSB", "MORTGAGE", "SERVICES", "SERVICING", "LOAN",
                  "LOANS", "FUND", "SERIES", "CERTIFICATEHOLDERS", "HOLDERS", "ITS", "IN", "BUT", "SOLELY", "NOT",
                  "INDIVIDUAL", "CAPACITY", "OWNER", "FKA", "DBA", "D/B/A", "F/K/A", "US", "U.S.", "USA", "FINANCIAL",
                  "LP", "GROUP", "HOME", "HOMES", "CAPITAL", "SAVINGS", "FEDERAL", "ASSET", "BACKED", "TO", "BY"}


def third_party_sale(status: str | None) -> bool:
    """True for a status that says a third party bought at the sheriff sale, or a bare "Purchased" that doesn't name
    the plaintiff, and nothing in it undoes that."""
    s = clean(status)
    if not s or NO_THIRD.search(s) or UNDONE.search(s):
        return False
    if THIRD_PARTY_SALE.search(s):
        return True
    return bool(PURCHASED.search(s)) and not PLAINTIFF.search(s)


def current_status(status: str | None, history: Any) -> str:
    """The signal's own status, else the last entry of CivilView's status history ([[status, date], ...])."""
    if clean(status):
        return clean(status)
    if isinstance(history, list) and history:
        last = history[-1]
        if isinstance(last, (list, tuple)) and last:
            return clean(str(last[0]))
        return clean(str(last))
    return ""


def sold_to_third_party(status: str | None, history: Any) -> bool:
    """The sale happened to a third party: the current status says so, or (status blank or a bare "Sold") the last
    history entry does. An explicit other status ("Scheduled", "Purchased - Plaintiff") always wins."""
    s = clean(status)
    if third_party_sale(s):
        return True
    if s and not GENERIC_SOLD.fullmatch(s):
        return False
    return third_party_sale(current_status(None, history))


def parse_amount(value: Any) -> float | None:
    """A stored figure: a number, or text such as "$1,234,567.89"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    m = re.search(r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)", str(value))
    return float(m.group(1).replace(",", "")) if m else None


def sale_price_of(meta: dict[str, Any]) -> float | None:
    """The stated winning bid / sale price, under whichever key the scraper stored it."""
    for k in SALE_PRICE_KEYS:
        v = parse_amount((meta or {}).get(k))
        if v:
            return v
    return None


def payoff_of(sig: DistressSignal) -> float | None:
    """What the sale had to pay the plaintiff: the highest of the stored payoff, upset and judgment figures."""
    m = sig.raw_metadata_json or {}
    xs = [parse_amount(x) for x in (sig.amount_owed, m.get("payoff_estimate"), m.get("upset_amount"),
                                    m.get("judgment_amount"))]
    xs = [x for x in xs if x]
    return max(xs) if xs else None


def prior_liens_of(sig: DistressSignal) -> float:
    return parse_amount((sig.raw_metadata_json or {}).get("prior_liens")) or 0.0


def surplus_estimate(sale_price: float | None, payoff: float | None, prior_liens: float | None = 0) -> float | None:
    """bid - payoff - liens the sale was subject to (conservative: before sheriff's fees and interest)."""
    if not sale_price or not payoff:
        return None
    return round(sale_price - payoff - float(prior_liens or 0), -2)


def equity_estimate(value: float | None, debt: float | None) -> float | None:
    if not value or not debt:
        return None
    return round(value - debt, -2)


def junior_plaintiff(name: str | None) -> bool:
    """An association / municipal plaintiff (not a lender): its judgment is usually junior to a surviving mortgage."""
    n = clean(name).upper()
    return bool(n) and bool(JUNIOR_PLAINTIFF.search(n)) and not is_lender(n)


def _key_words(name: str | None) -> set[str]:
    return {w for w in re.findall(r"[A-Z0-9][A-Z0-9.&'/-]*", clean(name).upper())
            if w not in _GENERIC_WORDS and len(w) > 2}


def third_party_grantee(grantee: str | None, plaintiff: str | None) -> bool:
    """A sheriff's deed to someone other than the plaintiff: not a lender (plaintiffs often assign the bid to a
    servicer, Fannie Mae or HUD) and sharing no distinctive word with the plaintiff's name."""
    g = clean(grantee).upper()
    if not g or is_lender(g) or re.search(r"SECRETARY OF HOUSING|FEDERAL HOME LOAN|FEDERAL NATIONAL", g):
        return False
    return not (_key_words(g) & _key_words(plaintiff))


def plaintiff_label(name: str | None, n: int = 48) -> str:
    """A lender or other business plaintiff by name, a private person never (product rule): a party line such as
    "QUINN SAMPLE, AND ALEX EXAMPLE AND SAMPLE LIVE LLC" keeps only its business parts ("SAMPLE LIVE LLC")."""
    s = clean(name).strip(" ,;")
    if not s:
        return "-"
    parts = [clean(x).strip(" ,.;") for x in re.split(r";|&|\bAND\b|,", s, flags=re.I)]
    keep = [x for x in parts if x and (entity_like(x) or is_lender(x.upper()))]
    if not keep:
        return "(private party)"
    out = ", ".join(keep)
    return out if len(out) <= n else out[: n - 3].rstrip(" ,") + "..."


def adjournment_line(meta: dict[str, Any]) -> str:
    total = int((meta or {}).get("adjournments_total") or 0)
    d = int((meta or {}).get("defendant_adjournments") or 0)
    p = int((meta or {}).get("plaintiff_adjournments") or 0)
    parts = [f"def {d}"] if d else []
    parts += [f"pl {p}"] if p else []
    return f"{total} adjournment{'s' if total != 1 else ''}" + (f" ({', '.join(parts)})" if parts else "")


# -- values (offline: stored valuations and town ratios only, no network) -----------------------------------------
def stored_equalized(val: Valuation | None) -> float | None:
    if val is None:
        return None
    a = val.assumptions_json or {}
    return parse_amount(a.get("subject_equalized"))


def trusted(val: Valuation | None, min_confidence: float) -> bool:
    """A valuation we put a number on: not capped by the outlier guard, residential, and confident enough."""
    if val is None or not (val.as_is_value or val.arv_estimate):
        return False
    flags = [str(f) for f in ((val.assumptions_json or {}).get("flags") or [])]
    if any(f.startswith(("OUTLIER_CAPPED", "NON_RESIDENTIAL")) for f in flags):
        return False
    return float(val.confidence_score or 0) >= min_confidence


def town_ratios(session: Session, mun_codes: set[str], min_samples: int = 3) -> dict[str, float]:
    """Median (stored equalized value / assessed total) per town, from valuations already made: the town's recorded
    sale-to-assessment ratio without asking NJGIN again."""
    if not mun_codes:
        return {}
    acc: dict[str, list[float]] = {}
    rows = session.execute(select(Property.mun_code, Property.assessed_total, Valuation.assumptions_json)
                           .join(Valuation, Valuation.property_id == Property.id)
                           .where(Valuation.is_current.is_(True), Property.mun_code.in_(sorted(mun_codes)),
                                  Property.assessed_total > 0))
    for mun, assessed, a in rows:
        eq = parse_amount((a or {}).get("subject_equalized"))
        if eq and assessed:
            acc.setdefault(str(mun), []).append(eq / float(assessed))
    return {m: round(median(xs), 4) for m, xs in acc.items() if len(xs) >= min_samples}


@dataclass
class ValueData:
    value: float | None                  # the figure equity is computed from
    basis: str                           # "as-is formula" / "ARV formula" / "equalized assessment" / ""
    as_is: float | None = None
    arv: float | None = None
    equalized: float | None = None

    def cell(self) -> str:
        bits = [f"as-is {kmoney(self.as_is)}" if self.as_is else "", f"eq. {kmoney(self.equalized)}"
                if self.equalized else "", f"ARV {kmoney(self.arv)}" if self.arv and not self.as_is else ""]
        lo_hi = [x for x in (self.as_is, self.equalized) if x] or ([self.arv] if self.arv else [])
        if not lo_hi:
            return "-"
        rng = (f"{kmoney(min(lo_hi))}-{kmoney(max(lo_hi))}" if min(lo_hi) != max(lo_hi) else kmoney(lo_hi[0]))
        return f"{rng}<br/>{' · '.join(b for b in bits if b)}"


def value_data(prop: Property, ratios: dict[str, float], min_confidence: float = 45) -> ValueData:
    """The value figure for equity: our as-is formula estimate, else the ARV estimate (when the valuation is trusted),
    else the equalized assessment (stored, or assessed x the town's ratio)."""
    val = prop.current_valuation
    eq = stored_equalized(val)
    if not eq and prop.assessed_total and ratios.get(str(prop.mun_code)):
        eq = round(prop.assessed_total * ratios[str(prop.mun_code)], -2)
    ok = trusted(val, min_confidence)
    as_is = float(val.as_is_value) if ok and val.as_is_value else None
    arv = float(val.arv_estimate) if ok and val.arv_estimate else None
    if as_is:
        return ValueData(as_is, "as-is formula", as_is, arv, eq)
    if arv:
        return ValueData(arv, "ARV formula", None, arv, eq)
    if eq:
        return ValueData(eq, "equalized assessment", None, None, eq)
    return ValueData(None, "")


# -- rows ---------------------------------------------------------------------------------------------------------
SHORT_BASIS = {"as-is formula": "as-is formula", "ARV formula": "ARV formula", "equalized assessment": "eq. assessment"}


@dataclass
class SurplusRow:
    kind: str                            # "sold" (third-party sale) / "watch"
    signal_id: int
    lead_id: int | None
    county: str
    docket: str
    sheriff_no: str
    sale_date: date | None
    status: str
    adjournments: str
    street: str
    town: str
    plaintiff: str
    payoff: float | None
    prior_liens: float
    value: ValueData
    amount: float | None                 # surplus (sold, priced), equity (watch / sold without a price)
    sale_price: float | None = None
    price_source: str = ""               # "sale notice" / "sheriff's deed" / ""

    @property
    def rank(self) -> tuple[int, float]:
        tier = 0 if self.kind == "sold" and self.sale_price else 1 if self.kind == "sold" else 2
        return tier, -(self.amount or 0)

    def cells(self, i: int, today: date) -> list[str]:
        when = f"{self.sale_date:%m/%d/%y}" if self.sale_date else "no date"
        if self.kind == "sold":
            date_cell = f"Sold {when}<br/>{self.status[:30]}"
            if self.sale_price:
                est = (f"<b>~{kmoney(self.amount)} surplus</b><br/>bid {kmoney(self.sale_price)} "
                       f"({self.price_source})")
            else:
                est = ("<b>Surplus if the bid beat the payoff</b><br/>bid: sheriff's deed or OPRA"
                       + (f"<br/>value data - debt ~{kmoney(self.amount)}" if self.amount else ""))
        else:
            days = (self.sale_date - today).days if self.sale_date else None
            date_cell = (f"{when}" + (f" ({days}d)" if days is not None else "")
                         + f" · {self.adjournments}<br/>{self.status[:30]}")
            est = f"<b>~{kmoney(self.amount)} equity</b><br/>from {SHORT_BASIS.get(self.value.basis, '-')}"
        # docket and sheriff # on one line, so a sample's masked rows hide both (a visible sheriff # names the case)
        return [str(i), f"{self.docket or '-'} · Sh. #{self.sheriff_no or '-'}", date_cell,
                f"<b>{self.street}</b><br/>{self.town}", self.plaintiff, kmoney(self.payoff),
                kmoney(self.prior_liens) if self.prior_liens else "-", self.value.cell(), est]


def county_of(sig: DistressSignal, prop: Property) -> str:
    c = (sig.raw_metadata_json or {}).get("county") or prop.county or ""
    return postal_title(re.sub(r"(?i)\s+county$", "", clean(str(c)))) or "Unknown"


def sheriff_no(sig: DistressSignal) -> str:
    n = (sig.raw_metadata_json or {}).get("sheriff_number")
    return clean(str(n)) if n else clean(sig.source_ref.split(":", 1)[-1]) if ":" in (sig.source_ref or "") else ""


def town_of(prop: Property) -> str:
    return postal_title(muni_base(prop.municipality)) or postal_title(prop.city) or ""


def _signal_debt(prop: Property, sig: DistressSignal) -> float | None:
    """engine.underwriter.known_debt for the property's (one) active sale; this signal's own figures otherwise."""
    from engine.underwriter import known_debt
    actives = [s for s in prop.active_signals if s.signal_type == SignalType.SHERIFF_SALE]
    if sig.active and len(actives) == 1:
        d = known_debt(prop)
        if d:
            return float(d)
    payoff = payoff_of(sig)
    return payoff + prior_liens_of(sig) if payoff else None


def sheriff_deeds(session: Session, parcel_ids: set[str]) -> dict[str, list[DeedTransfer]]:
    """Recorded sheriff's deeds (SR1A NU code 12, or a grantor naming the sheriff) for these parcels."""
    if not parcel_ids:
        return {}
    out: dict[str, list[DeedTransfer]] = {}
    for d in session.scalars(select(DeedTransfer).where(
            DeedTransfer.parcel_id.in_(sorted(parcel_ids)),
            or_(DeedTransfer.nu_code == SHERIFF_DEED_NU, func.upper(DeedTransfer.grantor).like("%SHERIFF%")))):
        out.setdefault(d.parcel_id, []).append(d)
    return out


def deed_for_sale(deeds: list[DeedTransfer], sale_date: date | None, window_days: int) -> DeedTransfer | None:
    """The sheriff's deed recorded for this sale: dated from a week before the sale to `window_days` after it."""
    if not sale_date:
        return None
    fits = [d for d in deeds if d.deed_date and sale_date - timedelta(days=7) <= d.deed_date
            <= sale_date + timedelta(days=window_days)]
    return min(fits, key=lambda d: d.deed_date) if fits else None


def collect(session: Session, today: date, lim: dict[str, Any]) -> tuple[list[SurplusRow], dict[str, int]]:
    """Both lists in one pass over the sheriff-sale signals (capped), with skip counts for the run log."""
    stats = {k: 0 for k in ("signals_examined", "third_party_sold", "sold_without_price", "sold_by_deed",
                            "sold_no_surplus", "suppressed", "junior_lien_skipped", "partial_debt_skipped",
                            "no_value", "no_debt", "below_equity")}
    since = today - timedelta(days=int(lim["lookback_days"]))
    horizon = today + timedelta(days=int(lim["window_days"]))
    sigs = list(session.scalars(
        select(DistressSignal).where(DistressSignal.signal_type == SignalType.SHERIFF_SALE,
                                     (DistressSignal.scheduled_auction_date.is_(None))
                                     | (DistressSignal.scheduled_auction_date >= since))
        .options(selectinload(DistressSignal.property).selectinload(Property.valuations),
                 selectinload(DistressSignal.property).selectinload(Property.signals),
                 selectinload(DistressSignal.property).selectinload(Property.lead))
        .order_by(DistressSignal.scheduled_auction_date.desc(), DistressSignal.id)
        .limit(int(lim["max_signals"]))))
    stats["signals_examined"] = len(sigs)
    need_ratio = {str(s.property.mun_code) for s in sigs if s.property is not None and s.property.mun_code
                  and stored_equalized(s.property.current_valuation) is None}
    ratios = town_ratios(session, need_ratio)
    deeds = sheriff_deeds(session, {s.property.parcel_id for s in sigs if s.property is not None
                                    and s.scheduled_auction_date and s.scheduled_auction_date <= today})
    rows: list[SurplusRow] = []
    seen_watch: set[int] = set()
    for sig in sigs:
        prop = sig.property
        if prop is None:
            continue
        meta = sig.raw_metadata_json or {}
        status = current_status(sig.status, meta.get("status_history"))
        sold = sold_to_third_party(sig.status, meta.get("status_history"))
        deed = deed_for_sale(deeds.get(prop.parcel_id, []), sig.scheduled_auction_date, int(lim["deed_window_days"]))
        if deed is not None and not sold and third_party_grantee(deed.grantee, sig.plaintiff_or_lienholder):
            sold, status = True, "Sheriff's deed to a third party"
            stats["sold_by_deed"] += 1
        upcoming = (sig.active and sig.scheduled_auction_date is not None and today <= sig.scheduled_auction_date
                    <= horizon and not CLOSED.search(status))
        if not (sold or upcoming):
            continue
        if not address_ok(session, prop):
            stats["suppressed"] += 1
            continue
        payoff, liens = payoff_of(sig), prior_liens_of(sig)
        base = dict(signal_id=sig.id, lead_id=prop.lead.id if prop.lead else None, county=county_of(sig, prop),
                    docket=clean(sig.case_number), sheriff_no=sheriff_no(sig), sale_date=sig.scheduled_auction_date,
                    status=status or "-", adjournments=adjournment_line(meta),
                    street=postal_title(prop.street_address) or "-", town=town_of(prop),
                    plaintiff=plaintiff_label(sig.plaintiff_or_lienholder), payoff=payoff, prior_liens=liens)
        vd = value_data(prop, ratios, float(lim["min_confidence"]))
        if sold:
            stats["third_party_sold"] += 1
            price, source = sale_price_of(meta), "sale notice"
            if not price and deed is not None and deed.sale_price and \
                    third_party_grantee(deed.grantee, sig.plaintiff_or_lienholder):
                price, source = float(deed.sale_price), "sheriff's deed"
            if price:
                surplus = surplus_estimate(price, payoff, liens)
                if surplus is None or surplus < float(lim["min_surplus"]):
                    stats["sold_no_surplus"] += 1        # the bid didn't clear the payoff: nothing to claim
                    continue
                rows.append(SurplusRow(kind="sold", value=vd, amount=surplus, sale_price=price, price_source=source,
                                       **base))
            else:
                stats["sold_without_price"] += 1
                rows.append(SurplusRow(kind="sold", value=vd, amount=equity_estimate(vd.value, payoff and payoff + liens),
                                       **base))
            continue
        if prop.id in seen_watch:
            continue
        if junior_plaintiff(sig.plaintiff_or_lienholder):
            stats["junior_lien_skipped"] += 1
            continue
        debt = _signal_debt(prop, sig)
        if not debt:
            stats["no_debt"] += 1
            continue
        if not vd.value:
            stats["no_value"] += 1
            continue
        if debt < float(lim["partial_debt_share"]) * vd.value:
            stats["partial_debt_skipped"] += 1           # an upset this low is usually a partial / junior figure
            continue
        eq = equity_estimate(vd.value, debt)
        if eq is None or eq < float(lim["min_equity"]):
            stats["below_equity"] += 1
            continue
        seen_watch.add(prop.id)
        rows.append(SurplusRow(kind="watch", value=vd, amount=eq, **base))
    rows.sort(key=lambda r: r.rank)
    return rows, stats


# -- the product --------------------------------------------------------------------------------------------------
HEAD = ["#", "Docket / sheriff #", "Sale date / status", "Property", "Plaintiff", "Upset / payoff", "Prior liens",
        "Value data*", "Estimate**"]
WIDTHS = [0.3, 1.0, 1.7, 1.5, 1.8, 0.7, 0.6, 1.15, 1.35]


def page_count(path: Path) -> int:
    from pypdf import PdfReader
    return len(PdfReader(str(path)).pages)


def render_county(path: Path, county: str, rows: list[SurplusRow], ctx: StreamContext, lim: dict[str, Any]
                  ) -> tuple[Path, int]:
    """The county's one-page dossier; returns the path and how many rows fit on the page."""
    today = ctx.now.date()
    sold = [r for r in rows if r.kind == "sold"]
    watch = [r for r in rows if r.kind == "watch"]
    sub = (f"For attorneys only · Week of {today:%B %-d, %Y} · {len(sold)} sale(s) to a third party · {len(watch)} "
           f"upcoming sale(s) in {int(lim['window_days'])} days with estimated equity of "
           f"{kmoney(float(lim['min_equity']))}+ · Sources: sheriff sale notices (CivilView), NJ MOD-IV, recorded deeds.")
    notes = [
        "**Surplus = bid - payoff (the higher of upset and judgment) - liens the sale was subject to, before sheriff's "
        f"fees and interest; shown only when a bid is on record ({BID_SOURCE}). Equity = value figure - payoff - liens: "
        "a third-party bid above the payoff creates surplus.",
        "*Data, not an appraisal: as-is = formula estimate from recorded comparable sales; eq. = assessed value x the "
        "town's recorded sale-to-assessment ratio. Association / municipal foreclosures and partial upsets are left out "
        "(a senior mortgage may survive). Defendants are not named. Verify each docket on eCourts: sales are often "
        "adjourned (N.J.S.A. 2A:17-36: 2 by the debtor, 2 by the lender, 1 by agreement, more by court order), stayed "
        "or cancelled.",
        "Law: surplus is paid into court (N.J.S.A. 2A:50-37) and held in the Superior Court Trust Fund; claims by motion "
        "(R. 4:64-3, R. 4:57-2), junior lienholders before the former owner; unclaimed funds escheat after 10 years "
        "(N.J.S.A. 46:30B-41). Finder agreements: N.J.S.A. 46:30B-106; Haven Savings Bank v. Zanolini (App. Div. 2010). "
        "We are not finders and give no legal advice: flat-price research for attorneys, never a percentage, no contact "
        "with owners (Joint Opinion 741, 2021).",
    ]
    box = (f"<b>Subscription:</b> this report every week for each county you choose, flat "
           f"${float(lim['price_monthly']):,.0f}/month, or ${float(lim['price_report']):,.0f} for a single county "
           "report. Public-record research only: your office verifies every case on eCourts and decides whom, if "
           "anyone, to contact. The price never depends on any case, claim or contact.")
    kept = list(rows)
    while True:
        cells = [r.cells(i, today) for i, r in enumerate(kept, 1)]
        render_table_pdf(path, title=f"NJ Sheriff-Sale Surplus Research: {county} County", subtitle=sub, head=HEAD,
                         rows=cells, notes=notes, box=box, footer=operator_footer(ctx.operator), col_widths=WIDTHS)
        if len(kept) <= 1 or page_count(path) <= 1:
            if len(kept) <= SAMPLE_FULL_ROWS:
                sample_path(path).unlink(missing_ok=True)   # 1-2 rows: no masked sample, never a stale longer one
            return path, len(kept)
        kept = kept[:-1]                                 # one page, always: the lowest-ranked row goes first


def week_tag(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def _norm_county(c: str | None) -> str:
    return re.sub(r"(?i)\s+county$", "", clean(c)).lower()


@dataclass
class CountyReport:
    """One county's dossier this week: the full PDF, what Touch 2 attaches (the masked sample when one was written,
    else the 1-2 row report itself) and the rows on the page."""
    county: str
    path: Path
    rows: list[SurplusRow]

    @property
    def masked(self) -> bool:
        return sample_path(self.path).exists()

    @property
    def sample(self) -> Path:
        return sample_path(self.path) if self.masked else self.path

    @property
    def watch(self) -> int:
        return sum(1 for r in self.rows if r.kind == "watch")

    @property
    def sold(self) -> int:
        return sum(1 for r in self.rows if r.kind == "sold")


def _where(counties: list[str]) -> str:
    if len(counties) == 1:
        return f"{counties[0]} County"
    return ", ".join(counties[:-1]) + f" and {counties[-1]} counties"


def case_ref(row: SurplusRow) -> str:
    """"12 Test St (sheriff # 26-1234)": the street and the public case number, never a defendant."""
    ref = f" (sheriff # {row.sheriff_no})" if row.sheriff_no else f" (docket {row.docket})" if row.docket else ""
    return f"{row.street}{ref}"


def touch1(reports: list[CountyReport], first: str) -> tuple[str, str]:
    """(subject, body) of the cold first touch: two plain sentences, a true count and one case, no attachment, no
    link, no price. "upcoming" / "recent" say only what the rows are (watchlist sales / sales already made). The
    street is the first row of the first report: it passed `address_ok` in `collect` and the sample shows it in full."""
    counties = [r.county for r in reports]
    n = sum(len(r.rows) for r in reports)
    watch, sold = sum(r.watch for r in reports), sum(r.sold for r in reports)
    when = "upcoming and recent" if watch and sold else "upcoming" if watch else "recent"
    sales = "sheriff sale" if n == 1 else "sheriff sales"
    what = (f"{n} {when} {counties[0]} County {sales}" if len(counties) == 1
            else f"{n} {when} {sales} in {_where(counties)}")
    row = reports[0].rows[0]
    body = (f"Hello,\n\nThis week we compiled the upset-vs-equity file for {what}, including {case_ref(row)}. Would "
            "the case breakdown be useful for your practice if I e-mailed it over?" + (f"\n\n{first}" if first else ""))
    subject = f"{row.street} + {n - 1} more in {_where(counties)}" if n > 1 else f"{row.street}, {_where(counties)}"
    return subject, body


def _sheets(reports: list[CountyReport]) -> str:
    """"this week's Essex County sheet" / "this week's sheets for Essex and Union counties"."""
    where = _where([r.county for r in reports])
    return f"this week's {where} sheet" if len(reports) == 1 else f"this week's sheets for {where}"


def followup_body(reports: list[CountyReport], first: str) -> str:
    """The second cold touch: one sentence in the first touch's thread, no attachment."""
    return f"Hello,\n\nShould I send {_sheets(reports)} over?" + (f"\n\n{first}" if first else "")


def sample_body(reports: list[CountyReport], lim: dict[str, Any]) -> str:
    """Touch 2, sent only after their yes: what is attached, the flat prices, never a percentage or per-case pay."""
    here = f"Here's {_sheets(reports)}, attached" if len(reports) == 1 else f"Here are {_sheets(reports)}, attached"
    lines = [
        (f"{here}: the first {SAMPLE_FULL_ROWS} cases{' of each' if len(reports) > 1 else ''} in full, the rest "
         "with the street and docket hidden." if any(r.masked for r in reports) else f"{here}."),
        "Your office verifies every case on eCourts and decides whom, if anyone, to contact.",
        f"The weekly report is ${float(lim['price_monthly']):,.0f} a month flat, or "
        f"${float(lim['price_report']):,.0f} for one county report, never a percentage of any recovery or tied to "
        "a case.",
        'Reply "start" and I\'ll set it up.',
    ]
    return " ".join(lines)


def last_touch(session: Session, target_id: int) -> FirmOutbox | None:
    """Our newest s3 cold touch to this target that actually went out (the follow-up threads onto it)."""
    return session.scalar(select(FirmOutbox).where(FirmOutbox.target_id == target_id, FirmOutbox.stream == "s3",
                                                   FirmOutbox.kind.in_(("pitch", "followup")),
                                                   FirmOutbox.status == "sent", FirmOutbox.message_id.is_not(None))
                          .order_by(FirmOutbox.sent_at.desc(), FirmOutbox.id.desc()).limit(1))


def pitches(session: Session, ctx: StreamContext, lim: dict[str, Any], reports: dict[str, CountyReport]
            ) -> tuple[list[Outbound], int]:
    """At most `pitches_per_run` e-mails to surplus attorneys (NEW / CONTACTED) whose county has a report; statewide
    targets get the largest reports. One key per target per ISO week, so a re-run never proposes twice. A target our
    first touch reached gets the one-sentence follow-up in that thread; nobody gets more than `max_touches`, and
    nobody is proposed inside the pitch gap (the week's key stays free for a later run)."""
    from engine.firm_outbox import cfg as firm_cfg
    if not reports or not ctx.send:
        return [], 0
    tag = week_tag(ctx.now)
    st = firm_cfg()
    max_touches = int(st.get("max_touches", 2))
    gap = timedelta(days=float(st.get("pitch_gap_days", 6)))
    by_size = sorted(reports, key=lambda c: (-len(reports[c].rows), c))
    targets = list(session.scalars(select(FirmTarget).where(FirmTarget.kind == TARGET_KIND,
                                                            FirmTarget.stage.in_(("NEW", "CONTACTED")),
                                                            FirmTarget.email.is_not(None))
                                   .order_by(FirmTarget.score.desc(), FirmTarget.id)))
    first = (ctx.operator or {}).get("first", "")
    out: list[Outbound] = []
    matched = 0
    for t in targets:
        county = _norm_county(t.county)
        if county in STATEWIDE:
            chosen = by_size[:max(1, int(lim["statewide_attachments"]))]
        else:
            chosen = [c for c in reports if c.lower() == county]
        if not chosen:
            continue
        matched += 1
        if len(out) >= int(lim["pitches_per_run"]) or int(t.touches or 0) >= max_touches:
            continue
        if t.last_contact_at and t.last_contact_at > ctx.now - gap:
            continue
        key = f"s3:pitch:{t.id}:{tag}"
        if session.scalar(select(FirmOutbox.id).where(FirmOutbox.key == key)):
            continue
        mine = [reports[c] for c in chosen]
        subject, body = touch1(mine, first)
        kind, in_reply_to = "pitch", None
        prev = last_touch(session, t.id) if int(t.touches or 0) > 0 else None
        if prev is not None:
            kind, in_reply_to, body = "followup", prev.message_id, followup_body(mine, first)
            subject = "Re: " + re.sub(r"(?i)^\s*re:\s*", "", prev.subject or subject)
        out.append(Outbound(
            stream="s3", key=key, to=t.email, priority=PRIORITY_SPECIALTY, kind=kind, target_id=t.id,
            subject=subject, body=body, in_reply_to=in_reply_to, attachments=[],
            sample_body=sample_body(mine, lim), sample_attachments=[r.sample for r in mine]))
    return out, matched


class SurplusResearchStream(Stream):
    key = "s3"
    name = "Surplus research"
    priority = PRIORITY_SPECIALTY
    cadence_hours = 24.0

    def run(self, session: Session, ctx: StreamContext) -> StreamResult:
        lim = {**DEFAULTS, **{k: v for k, v in (ctx.limits or {}).items() if v is not None}}
        res = StreamResult(stream=self.key)
        rows, stats = collect(session, ctx.now.date(), lim)
        res.processed = stats["signals_examined"]
        res.found = len(rows)
        by_county: dict[str, list[SurplusRow]] = {}
        for r in rows:
            by_county.setdefault(r.county, []).append(r)
        pdfs: dict[str, CountyReport] = {}
        shown_total = 0
        for county in sorted(by_county):
            shown = by_county[county][:int(lim["max_rows_per_pdf"])]
            path = ctx.out_dir / f"s3_surplus_{slug(county.lower())}_{week_tag(ctx.now)}.pdf"
            _, n = render_county(path, county, shown, ctx, lim)
            shown = shown[:n]
            shown_total += len(shown)
            pdfs[county] = CountyReport(county=county, path=path, rows=shown)
        res.products = len(pdfs)
        res.outbound, matched = pitches(session, ctx, lim, pdfs)
        res.metrics.update(stats)
        res.metrics.update({"sold_third_party_rows": sum(1 for r in rows if r.kind == "sold"),
                            "surplus_priced": sum(1 for r in rows if r.kind == "sold" and r.sale_price),
                            "watchlist": sum(1 for r in rows if r.kind == "watch"), "rows_shown": shown_total,
                            "counties": len(pdfs), "pdfs": len(pdfs), "targets_matched": matched,
                            "pitches": len(res.outbound),
                            "followups": sum(1 for o in res.outbound if o.kind == "followup"),
                            "samples": sum(1 for r in pdfs.values() if r.masked)})
        if stats["sold_without_price"]:
            res.notes.append(f"{stats['sold_without_price']} third-party sales carry no bid on record: the dossier "
                             "points to the sheriff's deed / OPRA")
        return res


STREAM = SurplusResearchStream()
