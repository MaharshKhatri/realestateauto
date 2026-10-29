"""Pre-flight check for a deal card: is the number we are about to put in front of a listing agent defensible?

Every card in the autopilot deck carries a verdict:

  GO      the valuation rests on enough clean comps of the same kind of house, the math leaves the buyer his margin
  VERIFY  sendable, with the named thing to confirm on the call (estimated living area, old comps, FRFPA occupancy...)
  HOLD    do not send a number: too few clean comps, comps flagged for refresh, low confidence, an ARV far above the
          ask or the equalized assessment. The deck leaves HOLD cards out until the lead is re-underwritten.

It also writes the comp talking points for the call: the nearest renovated comps of the same type, and the one
sentence that explains our number (repairs, hold costs, what a buyer can pay).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import re

from engine.comps import SubjectKind, attached_addresses, kind_mismatch
from engine.scripts import lead_facts, money

HOLD_CONFIDENCE = 45
VERIFY_CONFIDENCE = 65
MAX_ARV_TO_ASK = 1.5
MAX_ARV_TO_EQUALIZED = 1.8
OLD_COMP_MONTHS = 30


@dataclass
class Preflight:
    status: str                                   # GO / VERIFY / HOLD
    issues: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    comps: list[dict[str, Any]] = field(default_factory=list)
    talking_point: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _months(d: str | None, today: date) -> float | None:
    try:
        return (today - date.fromisoformat(str(d)[:10])).days / 30.4
    except (TypeError, ValueError):
        return None


def _comp_line(c: dict[str, Any]) -> dict[str, Any]:
    price = c.get("sold_price")
    return {"address": c.get("address"), "town": c.get("municipality"), "price": price, "date": c.get("sold_date"),
            "distance_mi": c.get("distance_mi"), "type": c.get("property_type"), "sqft": c.get("sqft"),
            "ppsf": round(price / c["sqft"]) if price and c.get("sqft") else None, "bucket": c.get("bucket"),
            "time_adjusted": c.get("time_adjusted_price")}


def check(lead: Any, today: date | None = None) -> Preflight:
    from engine.listing_comps import listing_type
    from engine.scoring import valuation_suspect
    from engine.underwriter import asking_price
    today = today or date.today()
    p = lead.property
    v = p.current_valuation
    if v is None or not v.arv_estimate:
        return Preflight("HOLD", ["not underwritten"])
    a = v.assumptions_json or {}
    q = a.get("quant") or {}
    f = lead_facts(lead)
    issues: list[str] = []
    checks: list[str] = []
    hold = False
    if valuation_suspect(v):
        hold = True
        issues.append("valuation was outlier-capped or the parcel is not a 1-4 family home")
    if a.get("comps_need_refresh"):
        hold = True
        issues.append(f"{a['comps_need_refresh']} stored comps failed hygiene: re-underwrite before quoting")
    comps = list(v.comps_json or [])
    kind = SubjectKind.of(building_desc=p.building_desc, qual=p.qual, units=p.dwellings, year_built=p.year_built,
                          mls_type=listing_type(p))
    cluster = attached_addresses([c for c in comps if "listing" not in str(c.get("source") or "")]) \
        if not kind.attached else set()
    bad = [c for c in comps if kind_mismatch(kind, c.get("property_type"), c.get("year_built"), today=today,
                                             attached=re.sub(r"[^A-Z0-9 ]", "", str(c.get("address") or "").upper()).strip()
                                             in cluster)]
    if bad:
        hold = True
        issues.append(f"{len(bad)} comps are a different kind of property (condo / townhouse / new construction)")
    if len(comps) < 3 or (v.comp_count or 0) < 3:
        hold = True
        issues.append(f"only {len(comps)} comps")
    conf = float(v.confidence_score or 0)
    if conf < HOLD_CONFIDENCE:
        hold = True
        issues.append(f"valuation confidence {conf:.0f}/100")
    ask = asking_price(p)
    eq = a.get("subject_equalized")
    if ask and v.arv_estimate > MAX_ARV_TO_ASK * ask:
        hold = True
        issues.append(f"ARV {money(v.arv_estimate)} is {v.arv_estimate / ask:.2f}x the {money(ask)} ask: the comps or the "
                      "listing are wrong")
    if eq and v.arv_estimate > MAX_ARV_TO_EQUALIZED * float(eq):
        issues.append(f"ARV is {v.arv_estimate / float(eq):.2f}x the equalized assessment ({money(eq)}): confirm condition")
    # softer checks: sendable, confirm on the call
    if a.get("sqft_source") != "record":
        issues.append(f"living area estimated ({v.sqft_used:,} sqft): confirm it with the agent; repairs scale with it"
                      if v.sqft_used else "living area unknown: confirm it with the agent")
    ages = [m for m in (_months(c.get("sold_date"), today) for c in comps) if m is not None]
    if ages and sorted(ages)[len(ages) // 2] > OLD_COMP_MONTHS:
        issues.append(f"median comp sale is {sorted(ages)[len(ages) // 2]:.0f} months old (MOD-IV lag): prices are "
                      "time-adjusted, ask the agent what renovated houses closed at this year")
    if kind.attached and any(not c.get("sqft") for c in comps):
        issues.append("condo / townhouse unit valued against other units' recorded sales without living area: ask the "
                      "agent for recent sales of same-size units in the building before quoting")
    units = max(int(p.dwellings or 1), 1)
    other = [c for c in comps if c.get("units") and int(c["units"]) != units]
    if other and len(other) >= max(2, len(comps) // 4):
        issues.append(f"{len(other)} of {len(comps)} comps have a different unit count than this {units}-unit house: "
                      "confirm the unit count and lean on the same-size sales")
    if str(v.comp_source or "").startswith("blended") or v.comp_source == "public_record_over_thin_listing":
        issues.append("listing comps were thin: the ARV leans on recorded sales")
    if conf < VERIFY_CONFIDENCE:
        issues.append(f"confidence {conf:.0f}/100")
    if a.get("frfpa_applies") or str(lead.frfpa_track or "") in ("OWNER_OCCUPIED", "ESTATE_UNKNOWN_OCCUPANCY"):
        issues.append("owner-occupied foreclosure presumed (FRFPA): no price until the agent confirms who lives there"
                      + (f"; any offer >= {money(a.get('frfpa_floor'))}" if a.get("frfpa_floor") else ""))
    if f.get("upset") and q.get("mao_walkaway") and f["upset"] > q["mao_walkaway"]:
        issues.append(f"payoff {money(f['upset'])} is above our walk-away {money(q['mao_walkaway'])}: short sale or no deal")
    elif f.get("upset") and q.get("mao_target") and f["upset"] > 0.95 * q["mao_target"]:
        issues.append(f"payoff {money(f['upset'])} is within 5% of our target: the seller nets little; lead with the walk-away")
    if f.get("days_to_sale") is not None and 0 <= f["days_to_sale"] <= 10:
        issues.append(f"sheriff sale in {f['days_to_sale']} days: call the agent today, a written offer must reach the "
                      "lender before the sale")
    checks.append(f"{len(comps)} comps ({v.comp_source}), confidence {conf:.0f}")
    # talking points: the top sales among the nearest comps of the same kind (what renovated houses bring)
    same = [c for c in comps if c not in bad]
    same.sort(key=lambda c: (c.get("distance_mi") if c.get("distance_mi") is not None else 9))
    near = same[:10]
    near.sort(key=lambda c: -float(c.get("time_adjusted_price") or c.get("sold_price") or 0))
    lines = [_comp_line(c) for c in near[:5]]
    top = max((float(c.get("time_adjusted_price") or c.get("sold_price") or 0) for c in same), default=0)
    if top and v.arv_estimate > 1.05 * top:
        issues.append(f"ARV {money(v.arv_estimate)} is above every comp (highest {money(top)} time-adjusted): ask the agent "
                      "what renovated houses on this street sold for before quoting")
    prices = [c["price"] for c in lines if c.get("price")]
    if prices and q.get("buyer_max"):
        b = q.get("buyer") or {}
        rng = f"{money(min(prices))}-{money(max(prices))}" if len(prices) > 1 else money(prices[0])
        tp = (f"The best comparable sales near it closed at {rng}. With about {money(b.get('rehab'))} of work plus a 10% "
              f"cushion, {money(b.get('taxes_held'))} of taxes and {money((b.get('carry') or 0) + (b.get('capital_cost') or 0))} "
              f"to carry and finance it for {b.get('holding_months') or 6} months, a cash buyer tops out near "
              f"{money(q['buyer_max'])}, so my number is {money(q.get('mao_target'))}.")
    else:
        tp = ""
    status = "HOLD" if hold else ("VERIFY" if issues else "GO")
    return Preflight(status, issues, checks, lines, tp)
