"""NJ rehab cost model.

Tiers ($/sqft, configurable in settings.rehab): Light cosmetic $25, Medium $50, Heavy gut $85 +
roof/HVAC buffer. Older NJ housing stock gets per-sqft bumps (knob & tube wiring, lead paint,
buried oil tanks, galvanized plumbing are common pre-1980). The tier is inferred from age and
distress evidence; the operator overrides it after a walkthrough.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.config import settings
from db.models import RehabTier


@dataclass
class RehabEstimate:
    tier: str
    cost: float
    per_sqft: float
    sqft: int
    by_tier: dict[str, float]
    reasons: list[str] = field(default_factory=list)


def _cfg() -> dict:
    return dict(settings().get("rehab", {}))


def age_bump(year_built: int | None, cfg: dict | None = None) -> float:
    cfg = cfg or _cfg()
    if not year_built:
        return float(cfg.get("unknown_age_add_per_sqft", 4))
    for rule in sorted(cfg.get("age_bumps", []), key=lambda r: r["before"]):
        if year_built < rule["before"]:
            return float(rule["add_per_sqft"])
    return 0.0


def tier_cost(tier: str, sqft: int, year_built: int | None, cfg: dict | None = None) -> float:
    cfg = cfg or _cfg()
    base = {RehabTier.LIGHT: cfg.get("light_per_sqft", 25), RehabTier.MEDIUM: cfg.get("medium_per_sqft", 50),
            RehabTier.HEAVY: cfg.get("heavy_per_sqft", 85)}[RehabTier(tier)]
    cost = (base + age_bump(year_built, cfg)) * sqft
    if tier == RehabTier.HEAVY:
        cost += cfg.get("heavy_roof_hvac_buffer", 15000)
    return round(cost, -2)


def choose_tier(*, year_built: int | None, vacant: bool = False, estate: bool = False,
                foreclosure: bool = False, years_owned: float | None = None, absentee_rental: bool = False,
                as_is_to_arv: float | None = None, units: int | None = None) -> tuple[str, list[str]]:
    points, reasons = 0.0, []
    if year_built:
        if year_built < 1940:
            points += 2; reasons.append(f"built {year_built} (pre-1940 systems)")
        elif year_built < 1960:
            points += 1.5; reasons.append(f"built {year_built}")
        elif year_built < 1980:
            points += 1; reasons.append(f"built {year_built}")
    if vacant:
        points += 2; reasons.append("vacant (deferred maintenance, possible winterization damage)")
    if estate:
        points += 1.5; reasons.append("estate/inherited (often dated)")
    if foreclosure:
        points += 1; reasons.append("in foreclosure (deferred maintenance likely)")
    if years_owned and years_owned >= 25:
        points += 1; reasons.append(f"same owner {years_owned:.0f} yrs (likely original kitchens/baths)")
    if absentee_rental:
        points += 0.5; reasons.append("absentee-owned rental (tenant wear)")
    if as_is_to_arv is not None and as_is_to_arv < 0.6:
        points += 1; reasons.append("as-is comps far below renovated comps")
    if units and units >= 3:
        points += 0.5; reasons.append(f"{units} units")
    if points >= 4:
        return RehabTier.HEAVY, reasons
    if points >= 2:
        return RehabTier.MEDIUM, reasons
    return RehabTier.LIGHT, reasons


def estimate(sqft: int, year_built: int | None, **tier_inputs) -> RehabEstimate:
    cfg = _cfg()
    tier, reasons = choose_tier(year_built=year_built, **tier_inputs)
    by_tier = {t.value: tier_cost(t.value, sqft, year_built, cfg) for t in RehabTier}
    cost = by_tier[tier]
    return RehabEstimate(tier=tier, cost=cost, per_sqft=round(cost / sqft, 2) if sqft else 0.0, sqft=sqft,
                         by_tier=by_tier, reasons=reasons)
