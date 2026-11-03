"""One firm offer per lead: the single number every surface states (learned 2026-11-03).

The operator asked for offers that are "final and on the record", as high as the numbers allow (they accept a
$5,000-$10,000 fee), and never insulting. An audit found the e-mail, the LOI, the call script, the battle card and the
negotiator could each state a different number for the same house, and that a "final" number could rest on an
estimated living area, two-year-old comps or an ARV above every comp. This module is now the only place that decides:

* buyer_max: what a local rehabber can pay and still clear 15% / the profit floor (engine/quant_pricing.py, unchanged);
* final  = buyer_max - fees.min_fee ($5,000): our best and final, never above the ask;
* offer  = buyer_max - fees.target_fee ($10,000), at most 90% of the ask: the one number in writing (no anchor
  discount, no ranges). The only move left is one step to the final;
* FRFPA-covered leads never go below the 82% floor; a floor above the final means no offer;
* a listed foreclosure opens at the payoff when that sits between offer and final; a payoff above the final is a
  lender short sale (no firm number from us).

The tier decides what goes out:

* "firm"      the final reaches `outreach.min_list_to_mao_75` (80%) of the ask AND every certainty check passes:
              a priced e-mail / LOI / call with the one number;
* "ask_first" the final reaches `outreach.ask_first_min_reach` (65%) of the ask, or a certainty check failed: no
              number from us; we ask what the seller needs and accept anything at or under the final;
* "skip"      too far from the ask, a no-offer verdict, or a broken valuation: no outreach.

Certainty checks (a price is stated only when all hold): pre-flight not HOLD; living area on record; the median comp
sold within `firm.max_comp_age_months`; the ARV no more than 5% above the highest comp; not a renovated / move-in-ready
retail listing; unit count confirmed for 2-4 families; no presumed owner-occupied foreclosure (FRFPA) without
confirmed occupancy.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select

from core.config import settings

NO_OFFER_VERDICTS = {"EQUITY_RESCUE_REFERRAL", "FRFPA_BLOCKED", "PASS", "SHORT_SALE_ONLY"}
RETAIL = re.compile(r"\b(move[- ]in ready|fully renovated|newly renovated|completely renovated|recently renovated|"
                    r"totally renovated|gut renovated|renovated|updated|remodeled|brand new|turn[- ]?key|pristine|"
                    r"immaculate|meticulously)\b", re.I)
DISTRESS = re.compile(r"\b(as[- ]is|cash only|cash buyers?|investors?|handyman|fixer|tlc|needs? (some |a little |lots of )?"
                      r"(work|updating|updates|renovation|rehab|repairs?|tlc)|bring your (vision|imagination|ideas|"
                      r"contractor)|contractor special|estate sale|executor|fire damage|water damage|mold|foundation|"
                      r"gut|rehab|tear ?down|no fha|203k|in need of|sold as[- ]is|short sale|bank[- ]owned|reo)\b", re.I)
FOLLOW_TERMS = (
    "Cash, as-is: no mortgage or appraisal contingency, no repairs or credits asked of the seller.",
    "3-business-day attorney review, then a 10-business-day inspection period that includes an oil-tank sweep.",
    "The price assumes no underground oil tank and no open permits or code violations. If the inspection finds one, "
    "the seller removes or closes it (NJDEP closure) or the removal quote is held in escrow; the price itself stays.",
    "Closing on the seller's timeline, as soon as 21 days after attorney review.",
)


def _cfg() -> dict[str, Any]:
    return dict(settings().get("firm") or {})


def _floor100(x: float) -> float:
    return math.floor(x / 100.0) * 100.0


def _ceil100(x: float) -> float:
    return math.ceil(x / 100.0) * 100.0


def fees() -> tuple[float, float]:
    from engine.quant_pricing import costs
    f = costs().get("fees") or {}
    min_fee = float(f.get("min_fee", 5000))
    return min_fee, max(float(f.get("target_fee", 10000)), min_fee)


def listing_for(session: Any, lead: Any) -> Any:
    """The MLS listing behind a lead: the one its STALE_LISTING / ACTIVE_LISTING signal names (a wrong-parcel link
    included), else the property's newest listing."""
    from db.models import Listing
    p = lead.property
    refs = [sg.source_ref for sg in p.signals if sg.signal_type in ("STALE_LISTING", "ACTIVE_LISTING") and sg.source_ref]
    if session is not None and refs:
        lst = session.scalar(select(Listing).where(Listing.source_ref.in_(refs)).limit(1))
        if lst is not None:
            return lst
    return max(p.listings, key=lambda x: x.last_seen_at or datetime.min, default=None)


def listing_text(session: Any, lead: Any) -> str:
    lst = listing_for(session, lead)
    return str(getattr(lst, "description", "") or "") if lst is not None else ""


def _months(d: Any, today: date) -> float | None:
    try:
        return (today - date.fromisoformat(str(d)[:10])).days / 30.4
    except (TypeError, ValueError):
        return None


@dataclass
class FirmOffer:
    tier: str                                  # firm / ask_first / skip
    offer: float | None = None                 # the one number in writing (firm only)
    final: float | None = None                 # best and final: the most we pay (firm and ask_first)
    buyer_max: float | None = None
    ask: float | None = None
    reasons: list[str] = field(default_factory=list)    # why it isn't firm (or why skip)
    terms: tuple[str, ...] = FOLLOW_TERMS

    @property
    def priced(self) -> bool:
        return self.tier == "firm" and bool(self.offer)

    @property
    def reach(self) -> float | None:
        return round(self.final / self.ask, 3) if self.final and self.ask else None

    def fee_at(self, price: float | None) -> float | None:
        return _floor100(self.buyer_max - price) if self.buyer_max and price else None

    def to_json(self) -> dict[str, Any]:
        return {"tier": self.tier, "offer": self.offer, "final": self.final, "buyer_max": self.buyer_max,
                "ask": self.ask, "reach": self.reach, "fee_at_offer": self.fee_at(self.offer),
                "fee_at_final": self.fee_at(self.final), "reasons": list(self.reasons)}


def certainty_issues(lead: Any, session: Any = None, today: date | None = None) -> list[str]:
    """Why a stated price could turn out wrong. Empty = the number can go out as final."""
    from engine import preflight
    today = today or date.today()
    p = lead.property
    v = p.current_valuation
    a = (v.assumptions_json or {}) if v else {}
    out: list[str] = []
    pf = preflight.check(lead, today)
    if pf.status == "HOLD":
        out.extend(f"HOLD: {i}" for i in pf.issues)
    if a.get("sqft_source") != "record":
        out.append("the living area isn't on record")
    comps = list(v.comps_json or []) if v else []
    ages = sorted(m for m in (_months(c.get("sold_date"), today) for c in comps) if m is not None)
    max_age = float(_cfg().get("max_comp_age_months", 12))
    if ages and ages[len(ages) // 2] > max_age:
        out.append(f"the comparable sales are old (median {ages[len(ages) // 2]:.0f} months)")
    prices = [float(c.get("time_adjusted_price") or c.get("sold_price") or 0) for c in comps]
    top = max(prices, default=0)
    if v and v.arv_estimate and top and v.arv_estimate > 1.05 * top:
        out.append("the after-repair value is above every comparable sale")
    text = listing_text(session, lead)
    if text and RETAIL.search(text) and not DISTRESS.search(text):
        out.append("the listing describes a renovated, move-in-ready house (retail buyers pay more than an investor can)")
    units = int(p.dwellings or 1)
    mls_multi = str(getattr(listing_for(session, lead), "property_type", "") or "").startswith("multi")
    if mls_multi and units <= 1:
        out.append("the MLS says multi-family but the parcel record shows one unit: confirm the units and rents")
    if any("different unit count" in i for i in pf.issues):
        out.append("comps have a different unit count: confirm the units and the rent roll")
    if any("condo / townhouse unit valued" in i for i in pf.issues):
        out.append("condo unit without same-size unit sales: get those and the HOA first")
    track = str(lead.frfpa_track or a.get("frfpa_track") or "")
    if (a.get("frfpa_applies") or track in ("OWNER_OCCUPIED", "ESTATE_UNKNOWN_OCCUPANCY")) and \
            not lead.occupancy_confirmed_at:
        out.append("possible owner-occupied foreclosure: confirm who lives there before any price (FRFPA)")
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def firm_offer(lead: Any, session: Any = None, *, today: date | None = None,
               check_certainty: bool = True) -> FirmOffer:
    from engine.underwriter import asking_price, known_debt
    p = lead.property
    v = p.current_valuation
    if v is None or not v.arv_estimate:
        return FirmOffer("skip", reasons=["not underwritten"])
    a = v.assumptions_json or {}
    q = a.get("quant") or {}
    min_fee, target_fee = fees()
    buyer_max = float(q.get("buyer_max") or 0) or (float(v.mao_aggressive) + min_fee if v.mao_aggressive else 0.0)
    ask = asking_price(p) or (a.get("listing") or {}).get("list_price")
    ask = float(ask) if ask else None
    if v.verdict in NO_OFFER_VERDICTS:
        return FirmOffer("skip", buyer_max=buyer_max or None, ask=ask, reasons=[f"verdict {v.verdict}"])
    if buyer_max <= min_fee:
        return FirmOffer("skip", ask=ask, reasons=["no price an investor buyer could pay"])
    if ask and buyer_max <= 0.10 * ask:
        return FirmOffer("skip", buyer_max=buyer_max, ask=ask,
                         reasons=["the valuation looks broken (buyer price under 10% of the ask)"])
    final = _floor100(buyer_max - min_fee)
    offer = _floor100(buyer_max - target_fee)
    if ask:
        final = min(final, _floor100(ask))
        offer = min(offer, _floor100(0.90 * ask))
    offer = min(offer, final)
    reasons: list[str] = []
    floor = a.get("frfpa_floor") if a.get("frfpa_applies") else None
    if floor:
        if float(floor) > final:
            return FirmOffer("skip", buyer_max=buyer_max, ask=ask, final=None,
                             reasons=["the FRFPA 82% floor is above what a buyer can pay: refer, don't offer"])
        offer = min(max(offer, _ceil100(float(floor))), final)
    debt = known_debt(p)
    if debt:
        if debt > final:
            return FirmOffer("skip", buyer_max=buyer_max, ask=ask, final=final,
                             reasons=["the payoff is above our final: only a lender short sale could work"])
        if debt > offer:
            offer = min(final, math.ceil(debt / 1000.0) * 1000.0)
    out = FirmOffer("skip", offer=offer, final=final, buyer_max=buyer_max, ask=ask)
    o = dict(settings().get("outreach") or {})
    firm_gate, ask_gate = float(o.get("min_list_to_mao_75", 0.80)), float(o.get("ask_first_min_reach", 0.65))
    reach = out.reach if ask else None
    issues = certainty_issues(lead, session, today) if check_certainty and _cfg().get("enforce", True) else []
    held = [i[len("HOLD: "):] for i in issues if i.startswith("HOLD: ")]
    if held:
        # the numbers themselves are in doubt (wrong parcel, mismatched comps, capped valuation): no cold outreach on them
        out.offer, out.reasons = None, held
        return out
    if ask and reach is not None and reach < ask_gate:
        out.reasons = [f"our final is {reach:.0%} of the ask (under {ask_gate:.0%}): too far apart to call or write"]
        out.offer = None
        return out
    if issues or (ask and reach is not None and reach < firm_gate):
        out.tier, out.offer = "ask_first", None
        out.reasons = issues + ([f"our final is {reach:.0%} of the ask: ask what the seller needs first"]
                                if ask and reach is not None and reach < firm_gate else [])
        return out
    out.tier = "firm"
    return out


def store(session: Any, lead: Any, f: FirmOffer | None = None) -> FirmOffer:
    """Keep the firm numbers on the valuation (assumptions_json['firm']) so every surface and report reads the same."""
    f = f or firm_offer(lead, session)
    v = lead.property.current_valuation
    if v is not None:
        v.assumptions_json = {**(v.assumptions_json or {}), "firm": f.to_json()}
    return f


def locked(lead: Any, f: FirmOffer) -> FirmOffer:
    """No re-trading (2026-11-03: "when we give the offers, they are final and on the record"): once a number went
    out in writing, nothing later states a lower one, and nothing ever goes above the final."""
    sent = float(lead.offer_amount or 0)
    if sent and f.final:
        if f.offer is not None and f.offer < sent:
            f.offer = min(sent, f.final)
        if sent > f.final:
            f.reasons = [*f.reasons, f"a re-check puts our final ({f.final:,.0f}) under the {sent:,.0f} already "
                                     "offered: honor it only after the operator confirms"]
    return f
