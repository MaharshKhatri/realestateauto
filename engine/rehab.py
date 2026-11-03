"""NJ rehab cost model.

Tiers ($/sqft, configurable in settings.rehab): Light cosmetic $30, Medium $55, Heavy gut $110 +
roof/HVAC buffer (2025-2026 NJ figures, 2026-11-03 research), plus 5% / 10% extra contingency on Medium / Heavy. Older NJ housing stock gets per-sqft bumps (knob & tube wiring, lead paint,
buried oil tanks, galvanized plumbing are common pre-1980). The tier is inferred from age and
distress evidence; the operator overrides it after a walkthrough.
"""
from __future__ import annotations

import re
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


# What the listing itself says about condition (learned 2026-11-03: the same 1,400 sqft 1955 house priced at $43,400
# LIGHT or $142,400 HEAVY on two age/ownership flags while the MLS text said "bring your vision" or "fully renovated").
HEAVY_WORDS = re.compile(r"\b(gut(ted)?|fire damage|water damage|flood(ed)? damage|mold|mould|foundation (issues?|"
                         r"problems?|repair)|structural|tear ?down|total rehab|full rehab|uninhabitable|needs everything|"
                         r"203k|shell)\b", re.I)
MEDIUM_WORDS = re.compile(r"\b(as[- ]is|needs? (some |a little |lots of )?(work|updating|updates|renovation|rehab|"
                          r"repairs?|tlc)|tlc|handy ?man|fixer|bring your (vision|imagination|ideas|contractor)|"
                          r"investor special|contractor special|cash only|estate sale|executor|dated|original condition|"
                          r"in need of)\b", re.I)
LIGHT_WORDS = re.compile(r"\b(move[- ]in ready|fully renovated|newly renovated|completely renovated|recently renovated|"
                         r"totally renovated|renovated|updated|remodeled|brand new|turn[- ]?key|pristine|immaculate)\b",
                         re.I)
_ORDER = {RehabTier.LIGHT: 0, RehabTier.MEDIUM: 1, RehabTier.HEAVY: 2}


def description_tier(text: str | None) -> tuple[str | None, str | None]:
    """The tier the listing's own words point to (None when it says nothing about condition)."""
    if not text:
        return None, None
    if m := HEAVY_WORDS.search(text):
        return RehabTier.HEAVY, f'the listing says "{m.group(0)}"'
    if m := MEDIUM_WORDS.search(text):
        return RehabTier.MEDIUM, f'the listing says "{m.group(0)}"'
    if m := LIGHT_WORDS.search(text):
        return RehabTier.LIGHT, f'the listing says "{m.group(0)}"'
    return None, None


def apply_description(tier: str, text: str | None) -> tuple[str, str | None]:
    """Listing words win over age guesses: heavy / needs-work words raise the tier, renovated words lower it."""
    said, why = description_tier(text)
    if said is None:
        return tier, None
    if said == RehabTier.LIGHT or _ORDER[RehabTier(said)] > _ORDER[RehabTier(tier)]:
        return said, why
    return tier, None


def priced_cost(tier: str, sqft: int, year_built: int | None, cfg: dict | None = None) -> float:
    """The tier's cost plus its extra contingency (`rehab.extra_contingency`, on top of the buyer model's 10%): heavier
    work overruns more, so a final offer carries 15% (MEDIUM) / 20% (HEAVY) in all."""
    cfg = cfg or _cfg()
    extra = float((cfg.get("extra_contingency") or {}).get(str(RehabTier(tier).value), 0))
    return round(tier_cost(tier, sqft, year_built, cfg) * (1 + extra), -2)


def estimate(sqft: int, year_built: int | None, *, listing_text: str | None = None, **tier_inputs) -> RehabEstimate:
    cfg = _cfg()
    tier, reasons = choose_tier(year_built=year_built, **tier_inputs)
    tier, why = apply_description(tier, listing_text)
    if why:
        reasons = [*reasons, why]
    by_tier = {t.value: priced_cost(t.value, sqft, year_built, cfg) for t in RehabTier}
    cost = by_tier[RehabTier(tier).value]
    return RehabEstimate(tier=tier, cost=cost, per_sqft=round(cost / sqft, 2) if sqft else 0.0, sqft=sqft,
                         by_tier=by_tier, reasons=reasons)
