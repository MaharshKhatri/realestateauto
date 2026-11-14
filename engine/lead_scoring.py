"""The clone score (2026-11-14, the operator's CEO directive): which listings look like the deals that actually said
yes (1102 4th Ave, Asbury Park: $475,900 accepted at 89% of the ask, a tenant-occupied 2-family; 1534 Asbury Ave: a
2-family whose agent took $511,800 to the seller and asked for proof of funds; 1708 Heck Ave, Neptune: $397,400 at 88%
of the ask), so the daily cold e-mail goes to them first and to nothing that can't sign.

The price never changes here: the offer is still the desk's one firm number (`offer_math`, buyer max - $10K, never
above 90% of the ask). This only reads where that number lands and what the house is:

- the corridor: our offer (buyer max - the target fee) at 78-90% of the ask is where sellers engaged; under
  `clone.min_offer_ratio` (78%) a cold opener is a lowball the seller won't answer, so it isn't sent;
- the house: 2-4 units, tenants (retail buyers can't get an owner-occupant loan with a lease in place), estate /
  probate words, a listed foreclosure, 75+ / 120+ days on the market, a price cut, fixer words, a town where investors
  buy (`clone.investor_towns`), the living area on record (a firm number can go out on the first reply);
- out: retail / renovated listings, off-market listings, no valuation or no ask.

Every score carries its parts (`CloneScore.parts`), like every other score in the engine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.config import settings
from db.models import DistressSignal, Lead, Property, SignalType, Valuation

TENANTS = re.compile(r"(?i)\b(tenants?|tenant[- ]occupied|occupied|leases?d?|rented|income[- ]producing|rent roll|"
                     r"month[- ]to[- ]month|section 8|cash flow)\b")
ESTATE = re.compile(r"(?i)\b(estate sale|estate|probate|executor|heirs?|court[- ]ordered)\b(?! agent)")
DEFAULT_TOWNS = ("Asbury Park", "Neptune", "Neptune City", "Long Branch", "Keansburg", "New Brunswick", "Edison",
                 "Plainfield", "Rahway", "Linden", "Elizabeth", "Garfield", "Paterson", "Passaic", "Irvington",
                 "Newark", "East Orange", "Orange", "Jersey City", "Bayonne", "Perth Amboy", "Trenton", "Union",
                 "Hillside", "Roselle", "Woodbridge", "Carteret", "Sayreville")


def cfg() -> dict[str, Any]:
    return dict(settings().get("clone") or {})


@dataclass
class CloneScore:
    lead_id: int
    score: float
    ratio: float | None                 # our offer (buyer max - target fee) / the ask
    eligible: bool                      # may get a cold opener
    why_not: str = ""
    parts: dict[str, float] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)


def offer_ratio(v: Valuation | None, ask: float | None) -> float | None:
    """The desk's offer before the 90%-of-ask cap, as a share of the ask (buyer max - the target fee)."""
    from engine.offer_math import fees
    if v is None or not ask:
        return None
    q = (v.assumptions_json or {}).get("quant") or {}
    bmax = q.get("buyer_max")
    if bmax:
        return round((float(bmax) - fees()[0]) / float(ask), 3)
    return round(float(v.mao_aggressive) / float(ask), 3) if v.mao_aggressive else None


def _town(p: Property) -> str:
    return (p.city or re.sub(r"(?i)\s+(city|twp|township|boro|borough|town)$", "", p.municipality or "")).strip().title()


def score(session: Session, lead: Lead) -> CloneScore:
    from engine import listing_facts
    from engine import offer_math as om
    from engine.fast_track import is_listed_foreclosure
    from engine.scoring import listing_off_market
    from engine.underwriter import asking_price
    c = cfg()
    p = lead.property
    v = p.current_valuation if p is not None else None
    lst = om.listing_for(session, lead) if p is not None else None
    ask = (asking_price(p) if p is not None else None) or (float(lst.list_price) if lst is not None and lst.list_price
                                                          else None)
    r = offer_ratio(v, ask)
    out = CloneScore(lead_id=lead.id, score=0.0, ratio=r, eligible=False)
    if p is None or v is None or not ask:
        out.why_not = "no valuation or no asking price"
        return out
    text = om.listing_text(session, lead) or ""
    cond = om.condition(session, lead)
    units = int(p.dwellings or 1)
    rents = listing_facts.parsed(lst).get("rent_total") if lst is not None else None
    dom = int(lst.days_on_market) if lst is not None and lst.days_on_market is not None else None
    cut = bool(lst is not None and lst.price_reduced)
    towns = {t.lower() for t in (c.get("investor_towns") or DEFAULT_TOWNS)}
    w = {"corridor": 30, "high": 35, "near": 12, "multi": 15, "big": 5, "tenants": 10, "estate": 10,
         "foreclosure": 10, "dom120": 12, "dom75": 8, "cut": 8, "fixer": 10, "town": 8, "sqft": 4,
         **dict(c.get("weights") or {})}
    lo, hi = (float(x) for x in (c.get("corridor") or [0.78, 0.90]))
    parts: dict[str, float] = {}
    if r is not None:
        if r >= 0.98:
            # our number at or above the ask usually means the comps are off (2026-11-14 audit: the 90%+ band had 3
            # houses and 0 replies); the desk checks before stating a number, so it ranks like "just under"
            parts[f"offer at {r:.0%} of ask (check the comps)"] = w["near"]
        elif r >= hi:
            parts["offer at 90%+ of ask"] = w["high"]
        elif r >= lo:
            parts[f"offer at {r:.0%} of ask (corridor)"] = w["corridor"] + 5 * (r - lo) / max(hi - lo, 0.01)
        elif r >= lo - 0.06:
            parts[f"offer at {r:.0%} of ask (just under)"] = w["near"]
    if 2 <= units <= 4:
        parts[f"{units} units"] = w["multi"]
    elif units >= 5:
        parts[f"{units} units"] = w["big"]
    if TENANTS.search(text) or rents:
        parts["tenants / rents"] = w["tenants"]
    if ESTATE.search(text) or p.is_estate_owned:
        parts["estate / probate"] = w["estate"]
    if is_listed_foreclosure(p):
        parts["listed foreclosure"] = w["foreclosure"]
    if dom is not None and dom >= 120:
        parts[f"{dom} days on market"] = w["dom120"]
    elif dom is not None and dom >= 75:
        parts[f"{dom} days on market"] = w["dom75"]
    if cut:
        parts["price cut"] = w["cut"]
    if cond == "fixer":
        parts["fixer words"] = w["fixer"]
    if _town(p).lower() in towns:
        parts[f"investor town ({_town(p)})"] = w["town"]
    if (v.assumptions_json or {}).get("sqft_source") == "record":
        parts["living area on record"] = w["sqft"]
    out.parts = {k: round(float(x), 1) for k, x in parts.items()}
    out.score = round(sum(out.parts.values()), 1)
    out.facts = {"street": (p.street_address or "").title(), "town": _town(p), "county": (p.county or "").title(),
                 "units": units, "ask": ask, "dom": dom, "cut": cut, "condition": cond, "rent_total": rents,
                 "offer": round(r * ask, -2) if r else None}
    floor = float(c.get("min_offer_ratio", lo))
    if cond == "retail":
        out.why_not = "retail / renovated listing"
    elif listing_off_market(p):
        out.why_not = "off the market"
    elif r is None or r < floor:
        out.why_not = f"our offer would be {r:.0%} of the ask (under {floor:.0%})" if r else "no offer number"
    else:
        out.eligible = True
    return out


def listed_leads(session: Session) -> list[Lead]:
    stmt = (select(Lead).join(Property, Lead.property_id == Property.id)
            .join(DistressSignal, (DistressSignal.property_id == Property.id)
                  & DistressSignal.signal_type.in_((SignalType.STALE_LISTING, SignalType.ACTIVE_LISTING))
                  & DistressSignal.active.is_(True))
            .join(Valuation, (Valuation.property_id == Property.id) & Valuation.is_current.is_(True))
            .where(Lead.status_stage.notin_(["DEAD", "DO_NOT_CONTACT", "CLOSED"])))
    return list(dict.fromkeys(session.scalars(stmt)))


def rank(session: Session, limit: int = 15, *, uncontacted_only: bool = True) -> list[CloneScore]:
    """The best clone deals, eligible ones only, highest score first (never e-mailed yet, by default)."""
    out = []
    for lead in listed_leads(session):
        if uncontacted_only and lead.last_contact_at:
            continue
        try:
            s = score(session, lead)
        except Exception:                                   # noqa: BLE001 - one odd record never stops the ranking
            continue
        if s.eligible:
            out.append(s)
    out.sort(key=lambda s: (-s.score, -(s.ratio or 0), s.lead_id))
    return out[:limit]
