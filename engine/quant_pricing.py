"""Quantitative buyer-implied pricing: what a local end buyer can actually pay, solved from NJ flip economics and
calibrated to recorded investor purchases — the replacement for the flat 70% rule.

Three prices per deal (dollars the seller receives, before our fee):

* ``buyer_max``    — End-Buyer Max Acquisition Price: the most a rehabber can pay and still clear BOTH a 15% net ROI
                     and a $45,000 net profit after the rehab (with contingency), acquisition closing costs, six months of
                     taxes / insurance / utilities / cost of capital, the resale commission and the seller's NJ Realty
                     Transfer Fee (plus the graduated fee over $1M) on the resale — capped at what investors in that town
                     have actually paid (alpha, below).
* ``mao_walkaway`` — buyer_max minus our $10,000 minimum fee: never contract above this.
* ``mao_target``   — the price at which the buyer still clears 16% and we keep a $15K / $20K / $25K fee (by ARV band).
* ``opening_offer`` — the anchor: 12% under the target, never above 90% of a live asking price.

alpha, the Empirical Investor ARV Multiplier, is (buyer price + rehab) / ARV. The classic rule fixes it at 0.70; here it
floats in [0.68, 0.80] with the town. ``manage.py calibrate`` measures, for every recorded investor purchase we hold
(cash_buyers.recent_purchases_json), the price paid divided by the parcel's equalized market value (MOD-IV assessment x
the town's sale/assessment ratio, both from NJGIN) and stores the lower quartile per town / county / state in
config/market_calibration.yaml — aggregates only, no parcels and no names. A town whose investors pay a larger share of
market value (dense rehabber demand, fast resale) gets a higher alpha; a thin market a lower one. buyer_max is the LOWER
of the alpha ceiling and the ROI / profit solve, so the end buyer's margin is always preserved.

Costs and thresholds live in config/nj_deal_costs.yaml (statutory sources inside).
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from core.config import CONFIG_DIR, settings

log = logging.getLogger(__name__)

COSTS_FILE = CONFIG_DIR / "nj_deal_costs.yaml"
CALIBRATION_FILE = CONFIG_DIR / "market_calibration.yaml"
BINDING_LABELS = {"min_profit": "the $ profit floor", "min_roi": "the ROI floor", "local_alpha": "what local investors pay"}


@lru_cache(maxsize=1)
def costs() -> dict[str, Any]:
    return yaml.safe_load(COSTS_FILE.read_text()) or {}


@lru_cache(maxsize=1)
def calibration() -> dict[str, Any]:
    if not CALIBRATION_FILE.exists():
        return {}
    return yaml.safe_load(CALIBRATION_FILE.read_text()) or {}


def reload() -> None:
    costs.cache_clear()
    calibration.cache_clear()


def pricing_model(cfg: dict[str, Any] | None = None) -> str:
    """'quant' (default) or 'fixed_pct' (the classic 70% / 75% rule) — settings.yaml deal.pricing_model."""
    model = str((cfg or settings().deal).get("pricing_model", "quant")).lower()
    return model if model in ("quant", "fixed_pct") else "quant"


# -- NJ transfer fees (seller pays) -------------------------------------------------------------------
def realty_transfer_fee(consideration: float, cfg: dict[str, Any] | None = None) -> float:
    """N.J.S.A. 46:15-7: fee per $500 (or fraction) of the consideration, bracketed; two schedules split at $350K.

    Verified against the Division of Taxation schedules: $150,500 -> $603.35, $350,000 -> $2,105, $600,000 -> $5,185,
    $1,000,500 -> $9,581.05. Consideration under $100 records without a fee.
    """
    rtf = (cfg or costs())["realty_transfer_fee"]
    if consideration < 100:
        return 0.0
    schedule = rtf["schedules"]["not_over_350k" if consideration <= float(rtf["threshold"]) else "over_350k"]
    units = math.ceil(consideration / 500)
    fee, done = 0.0, 0
    for band in schedule:
        top_units = math.ceil(float(band["up_to"]) / 500) if band.get("up_to") else units
        n = max(0, min(units, top_units) - done)
        fee += n * float(band["per_500"])
        done += n
        if done >= units:
            break
    return round(fee, 2)


def graduated_percent_fee(consideration: float, cfg: dict[str, Any] | None = None) -> float:
    """P.L.2025, c.69 (deeds recorded on/after 2025-07-10): 1% - 3.5% of the ENTIRE consideration over $1M, seller pays."""
    tiers = (cfg or costs())["realty_transfer_fee"]["graduated_percent_fee"]["tiers"]
    for t in tiers:
        if consideration > float(t["over"]) and (t.get("up_to") is None or consideration <= float(t["up_to"])):
            return round(consideration * float(t["pct"]), 2)
    return 0.0


@dataclass
class ResaleCosts:
    price: float
    commission: float
    seller_closing: float
    rtf: float
    graduated_fee: float

    @property
    def total(self) -> float:
        return round(self.commission + self.seller_closing + self.rtf + self.graduated_fee, 2)

    @property
    def net(self) -> float:
        return round(self.price - self.total, 2)

    def to_json(self) -> dict[str, float]:
        return {**asdict(self), "total": self.total, "net": self.net}


def resale_costs(price: float, cfg: dict[str, Any] | None = None) -> ResaleCosts:
    """What the end buyer nets when they resell at `price` (commission, seller closing, RTF, graduated fee)."""
    cfg = cfg or costs()
    eb = cfg["end_buyer"]
    return ResaleCosts(price=round(price, 2), commission=round(price * float(eb["resale_commission_pct"]), 2),
                       seller_closing=round(price * float(eb["seller_closing_pct"]), 2),
                       rtf=realty_transfer_fee(price, cfg), graduated_fee=graduated_percent_fee(price, cfg))


# -- end-buyer (flipper) economics ---------------------------------------------------------------------
@dataclass
class BuyerEconomics:
    purchase_price: float
    rehab: float
    rehab_contingency: float
    buyer_closing: float
    taxes_held: float
    carry: float            # insurance + utilities over the hold
    capital_cost: float     # interest + points on purchase + rehab
    resale_net: float
    total_investment: float
    profit: float
    roi: float
    holding_months: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _hold_inputs(arv: float, repairs: float, annual_taxes: float | None, cfg: dict[str, Any]) -> dict[str, float]:
    eb = cfg["end_buyer"]
    months = int(eb["holding_months"])
    taxes_annual = float(annual_taxes) if annual_taxes else arv * float(eb["default_effective_tax_rate"])
    interest, points = float(eb["capital_cost_annual_pct"]) * months / 12, float(eb["capital_points_pct"])
    draw = float(eb.get("rehab_draw_factor", 1.0))
    rehab_total = repairs * (1 + float(eb["rehab_contingency_pct"]))
    return {
        "months": months,
        "k": interest + points,                                   # capital cost per $ of purchase price
        "k_rehab": interest * draw + points,                      # per $ of rehab: interest only on the drawn share
        "bc": float(eb["buyer_closing_pct"]),
        "rehab_total": rehab_total,
        "contingency": repairs * float(eb["rehab_contingency_pct"]),
        "taxes": taxes_annual * months / 12,
        "carry": (float(eb["insurance_per_month"]) + float(eb["utilities_per_month"])) * months,
    }


def buyer_economics(price: float, *, arv: float, repairs: float, annual_taxes: float | None = None,
                    cfg: dict[str, Any] | None = None) -> BuyerEconomics:
    """The rehabber's full P&L if they buy at `price`, spend `repairs` (+ contingency) and resell at `arv`."""
    cfg = cfg or costs()
    h = _hold_inputs(arv, repairs, annual_taxes, cfg)
    capital = price * h["k"] + h["rehab_total"] * h["k_rehab"]
    closing = price * h["bc"]
    investment = price + closing + h["rehab_total"] + h["taxes"] + h["carry"] + capital
    net = resale_costs(arv, cfg).net
    profit = net - investment
    return BuyerEconomics(purchase_price=round(price, 2), rehab=round(repairs, 2), rehab_contingency=round(h["contingency"], 2),
                          buyer_closing=round(closing, 2), taxes_held=round(h["taxes"], 2), carry=round(h["carry"], 2),
                          capital_cost=round(capital, 2), resale_net=net, total_investment=round(investment, 2),
                          profit=round(profit, 2), roi=round(profit / investment, 4) if investment > 0 else 0.0,
                          holding_months=h["months"])


def solve_buyer_max(*, arv: float, repairs: float, annual_taxes: float | None = None, min_roi: float | None = None,
                    min_profit: float | None = None, cfg: dict[str, Any] | None = None) -> tuple[float, str]:
    """Largest purchase price P with profit(P) >= min_profit AND profit(P) / investment(P) >= min_roi.

    profit is linear in P: profit = F - g*P with g = 1 + closing% + capital% and F = resale net - C, where
    C = rehab*(1 + rehab capital%) + taxes + carry is the cash in that does not depend on P; investment = g*P + C.
    Both constraints are closed-form, the tighter one wins ("binding").
    """
    cfg = cfg or costs()
    eb = cfg["end_buyer"]
    r = float(eb["min_roi"] if min_roi is None else min_roi)
    floor_profit = float(eb["min_profit"] if min_profit is None else min_profit)
    h = _hold_inputs(arv, repairs, annual_taxes, cfg)
    g = 1 + h["bc"] + h["k"]
    c = h["rehab_total"] * (1 + h["k_rehab"]) + h["taxes"] + h["carry"]
    f = resale_costs(arv, cfg).net - c
    p_profit = (f - floor_profit) / g
    p_roi = (f - r * c) / (g * (1 + r))
    if p_profit <= p_roi:
        return max(p_profit, 0.0), "min_profit"
    return max(p_roi, 0.0), "min_roi"


# -- empirical alpha -------------------------------------------------------------------------------------
@dataclass
class LocalAlpha:
    alpha: float
    level: str              # town / county / state / default
    name: str | None
    n: int
    q25: float | None
    q50: float | None
    share: float | None     # investor purchases / class-2 sales sampled in the town
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def local_alpha(mun_code: str | None = None, county: str | None = None, cfg: dict[str, Any] | None = None,
                cal: dict[str, Any] | None = None) -> LocalAlpha:
    """alpha for a town: baseline + slope x (local q25 - statewide q25), nudged by demand density, clamped."""
    a = (cfg or costs())["alpha"]
    cal = calibration() if cal is None else cal
    floor, ceiling, base, slope = (float(a["floor"]), float(a["ceiling"]), float(a["baseline"]), float(a["slope"]))
    state = cal.get("statewide") or {}
    state_q25 = state.get("q25")
    if not state_q25:
        return LocalAlpha(base, "default", None, 0, None, None, None,
                          ["no market calibration on file (run `manage.py calibrate`): classic 70% all-in multiple"])
    town = (cal.get("towns") or {}).get(str(mun_code)) if mun_code else None
    cty = (cal.get("counties") or {}).get(str(county).title()) if county else None
    notes: list[str] = []
    if town and int(town.get("n", 0)) >= int(a["min_town_sample"]):
        level, src, name = "town", town, town.get("name") or str(mun_code)
    elif cty and int(cty.get("n", 0)) >= int(a["min_county_sample"]):
        level, src, name = "county", cty, f"{str(county).title()} County"
    else:
        level, src, name = "state", state, "New Jersey (our target counties)"
    q25, q50, n = float(src["q25"]), float(src.get("q50") or 0) or None, int(src.get("n", 0))
    alpha = base + slope * (q25 - float(state_q25))
    notes.append(f"{level} evidence: {n} recorded investor purchases, lower-quartile price = {q25:.0%} of equalized value "
                 f"(statewide {float(state_q25):.0%})")
    share = src.get("share") if level == "town" else None
    if share is not None:
        if float(share) >= float(a["dense_market_share"]):
            alpha += 0.01
            notes.append(f"dense investor market ({float(share):.1%} of recent sales went to repeat buyers): +0.01")
        elif float(share) < float(a["thin_market_share"]):
            alpha -= 0.01
            notes.append(f"thin investor market ({float(share):.1%} of recent sales): -0.01")
    clamped = min(max(alpha, floor), ceiling)
    if clamped != alpha:
        notes.append(f"clamped to the {floor:.2f}-{ceiling:.2f} band")
    return LocalAlpha(round(clamped, 3), level, name, n, q25, q50, float(share) if share is not None else None, notes)


def fee_target_for(arv: float, cfg: dict[str, Any] | None = None) -> float:
    for band in (cfg or costs())["fees"]["target_by_arv"]:
        if band.get("up_to") is None or arv <= float(band["up_to"]):
            return float(band["fee"])
    return float((cfg or costs())["fees"]["target_by_arv"][-1]["fee"])


# -- the offer ------------------------------------------------------------------------------------------
@dataclass
class QuantOffer:
    arv: float
    repairs: float
    annual_taxes_used: float
    alpha: LocalAlpha
    alpha_effective: float
    buyer_max: float
    buyer_max_solve: float
    buyer_max_alpha: float
    binding: str
    mao_walkaway: float
    mao_target: float
    opening_offer: float
    fee_target: float
    min_fee: float
    buyer: BuyerEconomics          # the rehabber's P&L at buyer_max
    resale: ResaleCosts
    ask: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def buyer_profit(self) -> float:
        return self.buyer.profit

    @property
    def buyer_roi(self) -> float:
        return self.buyer.roi

    def to_json(self) -> dict[str, Any]:
        return {
            "model": "quant", "arv": self.arv, "repairs": self.repairs, "annual_taxes_used": self.annual_taxes_used,
            "alpha": self.alpha.to_json(), "alpha_effective": self.alpha_effective, "buyer_max": self.buyer_max,
            "buyer_max_solve": self.buyer_max_solve, "buyer_max_alpha": self.buyer_max_alpha, "binding": self.binding,
            "mao_walkaway": self.mao_walkaway, "mao_target": self.mao_target, "opening_offer": self.opening_offer,
            "fee_target": self.fee_target, "min_fee": self.min_fee, "buyer": self.buyer.to_json(),
            "resale": self.resale.to_json(), "ask": self.ask,
            "list_to_target": round(self.mao_target / self.ask, 3) if self.ask else None,
            "list_to_walkaway": round(self.mao_walkaway / self.ask, 3) if self.ask else None, "notes": self.notes,
        }


def quant_offer(*, arv: float, repairs: float, annual_taxes: float | None = None, ask: float | None = None,
                mun_code: str | None = None, county: str | None = None, frfpa_floor: float | None = None,
                min_roi: float | None = None, min_profit: float | None = None, target_roi: float | None = None,
                anchor_discount: float | None = None, cfg: dict[str, Any] | None = None) -> QuantOffer:
    cfg = cfg or costs()
    eb, fees = cfg["end_buyer"], cfg["fees"]
    la = local_alpha(mun_code, county, cfg)
    p_solve, binding = solve_buyer_max(arv=arv, repairs=repairs, annual_taxes=annual_taxes, min_roi=min_roi,
                                       min_profit=min_profit, cfg=cfg)
    p_alpha = la.alpha * arv - repairs
    if p_alpha < p_solve:
        buyer_max, binding = p_alpha, "local_alpha"
    else:
        buyer_max = p_solve
    buyer_max = max(math.floor(buyer_max / 100) * 100.0, 0.0)      # down to the $100: the floors always hold
    p_target, _ = solve_buyer_max(arv=arv, repairs=repairs, annual_taxes=annual_taxes,
                                  min_roi=float(eb["target_roi"] if target_roi is None else target_roi),
                                  min_profit=min_profit, cfg=cfg)
    fee_target, min_fee = fee_target_for(arv, cfg), float(fees["min_fee"])
    mao_walkaway = round(buyer_max - min_fee, -2)
    mao_target = min(math.floor((min(p_target, p_alpha) - fee_target) / 100) * 100.0, mao_walkaway)
    discount = float(settings().deal.get("anchor_discount_pct", 0.12) if anchor_discount is None else anchor_discount)
    opening = mao_target * (1 - discount)
    if ask:
        opening = min(opening, 0.90 * ask)
    if frfpa_floor:
        opening = max(opening, frfpa_floor)
    opening = round(opening, -2)
    econ = buyer_economics(buyer_max, arv=arv, repairs=repairs, annual_taxes=annual_taxes, cfg=cfg)
    h = _hold_inputs(arv, repairs, annual_taxes, cfg)
    alpha_eff = round((buyer_max + repairs) / arv, 3) if arv else 0.0
    notes = list(la.notes)
    notes.append(f"buyer max {buyer_max:,.0f} is set by {BINDING_LABELS.get(binding, binding)}: a rehabber paying it clears "
                 f"{econ.profit:,.0f} ({econ.roi:.0%} on {econ.total_investment:,.0f} invested) after {h['rehab_total']:,.0f} "
                 f"rehab incl. contingency, {h['taxes']:,.0f} taxes and {h['carry']:,.0f} carry for {h['months']} months, "
                 f"{econ.capital_cost:,.0f} cost of capital and {resale_costs(arv, cfg).total:,.0f} to resell "
                 f"(commission, transfer fee)")
    if alpha_eff != la.alpha:
        notes.append(f"all-in multiple {alpha_eff:.0%} of ARV (local investors accept up to {la.alpha:.0%})")
    return QuantOffer(arv=round(arv, -2), repairs=round(repairs, -2), annual_taxes_used=round(h["taxes"] * 12 / h["months"]),
                      alpha=la, alpha_effective=alpha_eff, buyer_max=buyer_max, buyer_max_solve=round(p_solve, -2),
                      buyer_max_alpha=round(p_alpha, -2), binding=binding, mao_walkaway=mao_walkaway,
                      mao_target=mao_target, opening_offer=opening, fee_target=fee_target, min_fee=min_fee, buyer=econ,
                      resale=resale_costs(arv, cfg), ask=ask, notes=notes)


def counter_economics(counter: float, *, arv: float, repairs: float, annual_taxes: float | None, buyer_max: float,
                      cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a seller's counter leaves everyone: the buyer's P&L if they paid buyer_max and our fee = buyer_max - counter."""
    econ = buyer_economics(buyer_max, arv=arv, repairs=repairs, annual_taxes=annual_taxes, cfg=cfg)
    at_counter = buyer_economics(counter, arv=arv, repairs=repairs, annual_taxes=annual_taxes, cfg=cfg)
    return {"counter": round(counter, -2), "fee_at_counter": round(buyer_max - counter, -2),
            "buyer_profit_at_max": econ.profit, "buyer_roi_at_max": econ.roi,
            "buyer_profit_if_paid_counter": at_counter.profit, "buyer_roi_if_paid_counter": at_counter.roi}


# -- calibration from recorded investor purchases -------------------------------------------------------------
def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def harvest_investor_ratios(session: Any, gis: Any, *, progress: Any = None) -> list[dict[str, Any]]:
    """For every priced purchase in cash_buyers.recent_purchases_json with a parcel key: the parcel's current MOD-IV
    assessment (NJGIN, block-batched) and the town's sale/assessment ratio -> q = price / equalized value."""
    from db.models import CashBuyer
    from db.repository import normalize_block_lot
    from engine.market import muni_stats
    purchases: list[dict[str, Any]] = []
    for b in session.query(CashBuyer).all():
        for x in (b.recent_purchases_json or []):
            if isinstance(x, dict) and (x.get("price") or 0) > 0 and x.get("mun_code") and x.get("block") and x.get("lot"):
                purchases.append({**x, "buyer_id": b.id, "buyer_type": b.buyer_type, "flips": b.flips_count or 0})
    by_mun: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in purchases:
        by_mun[str(p["mun_code"])].append(p)
    done = 0
    for mun, items in by_mun.items():
        blocks = sorted({str(p["block"]) for p in items})
        index: dict[tuple[str, str, str], dict[str, Any]] = {}
        for i in range(0, len(blocks), 40):
            chunk = ",".join("'" + b.replace("'", "''") + "'" for b in blocks[i:i + 40])
            try:
                for f in gis.query(f"PCL_MUN='{mun}' AND PCLBLOCK IN ({chunk})",
                                   out_fields=["PCLBLOCK", "PCLLOT", "PCLQCODE", "NET_VALUE", "PROP_CLASS", "COUNTY",
                                               "MUN_NAME"], centroid=False):
                    a = f["attributes"]
                    index[(normalize_block_lot(a.get("PCLBLOCK")), normalize_block_lot(a.get("PCLLOT")),
                           (a.get("PCLQCODE") or "").strip().upper())] = a
            except Exception as exc:  # noqa: BLE001 - one town's parcels missing only thins that town's sample
                log.warning("calibration: parcel query failed for %s: %s", mun, exc)
        try:
            st = muni_stats(gis, mun, items[0].get("municipality") or "")
            ratio, sampled = st.median_ratio, st.sales_sampled
        except Exception as exc:  # noqa: BLE001
            log.warning("calibration: muni_stats failed for %s: %s", mun, exc)
            ratio, sampled = None, 0
        for p in items:
            key = (normalize_block_lot(str(p["block"])), normalize_block_lot(str(p["lot"])), "")
            a = index.get(key)
            if a is None:
                cands = [v for k, v in index.items() if k[0] == key[0] and k[1] == key[1]]
                a = cands[0] if len(cands) == 1 else None
            p["net_value"] = a.get("NET_VALUE") if a else None
            p["prop_class"] = a.get("PROP_CLASS") if a else None
            p["county_gis"] = a.get("COUNTY") if a else None
            p["town_ratio"], p["town_sales_sampled"] = ratio, sampled
            if a and a.get("NET_VALUE") and ratio:
                p["equalized"] = round(a["NET_VALUE"] * ratio)
                p["q"] = round(p["price"] / p["equalized"], 4)
        done += len(items)
        if progress:
            progress(f"{mun} {items[0].get('municipality')}: {len(items)} purchases, ratio {ratio} [{done}/{len(purchases)}]")
    return purchases


def clean_purchase(p: dict[str, Any], cfg: dict[str, Any] | None = None) -> bool:
    """Arm's-length house purchases only: class 2, sane price, sane ratio, a town ratio measured on >= 20 sales."""
    guard = settings().deal.get("outlier_guard", {})
    lo, hi = float(guard.get("comp_min_price", 40000)), float(guard.get("comp_max_price", 2500000))
    return bool(p.get("q") and p.get("prop_class") == "2" and lo <= float(p["price"]) <= hi
                and 0.15 <= float(p["q"]) <= 2.5 and int(p.get("town_sales_sampled") or 0) >= 20)


def aggregate(purchases: list[dict[str, Any]], cfg: dict[str, Any] | None = None, *, as_of: date | None = None
              ) -> dict[str, Any]:
    """Town / county / statewide lower-quartile and median of q, plus the town's investor share of recent sales."""
    clean = [p for p in purchases if clean_purchase(p, cfg)]
    towns: dict[str, list[dict[str, Any]]] = defaultdict(list)
    counties: dict[str, list[float]] = defaultdict(list)
    for p in clean:
        towns[str(p["mun_code"])].append(p)
        county = (p.get("county") or p.get("county_gis") or "").strip().title()
        if county:
            counties[county].append(float(p["q"]))
    qs = [float(p["q"]) for p in clean]
    out: dict[str, Any] = {
        "generated_on": (as_of or date.today()).isoformat(),
        "method": "q = recorded investor purchase price / (MOD-IV assessment x town sale-assessment ratio); "
                  "lower quartile (q25) per town (>= 8 purchases), county (>= 20), statewide",
        "purchases_total": len(purchases), "purchases_clean": len(clean),
        "statewide": {"n": len(qs), "q25": round(_pct(qs, 0.25) or 0, 4), "q50": round(_pct(qs, 0.5) or 0, 4)},
        "counties": {c: {"n": len(v), "q25": round(_pct(v, 0.25) or 0, 4), "q50": round(_pct(v, 0.5) or 0, 4)}
                     for c, v in sorted(counties.items())},
        "towns": {},
    }
    for code, items in sorted(towns.items()):
        v = [float(p["q"]) for p in items]
        sampled = max((int(p.get("town_sales_sampled") or 0) for p in items), default=0)
        out["towns"][code] = {"name": items[0].get("municipality"),
                              "county": (items[0].get("county") or items[0].get("county_gis") or "").title() or None,
                              "n": len(v), "q25": round(_pct(v, 0.25) or 0, 4), "q50": round(_pct(v, 0.5) or 0, 4),
                              "sales_sampled": sampled, "share": round(len(v) / sampled, 4) if sampled else None}
    return out


def calibrate(session: Any, gis: Any, *, out: Path = CALIBRATION_FILE, progress: Any = None) -> dict[str, Any]:
    """Harvest + aggregate + write config/market_calibration.yaml (aggregates only; safe to commit)."""
    data = aggregate(harvest_investor_ratios(session, gis, progress=progress))
    write_calibration(data, out)
    return data


def write_calibration(data: dict[str, Any], out: Path = CALIBRATION_FILE) -> None:
    header = ("# Empirical investor pricing calibration — written by `manage.py calibrate` (engine/quant_pricing.py).\n"
              "# Aggregates only: per-town lower-quartile / median of (investor purchase price / equalized market value)\n"
              "# and the share of recent class-2 sales bought by repeat investors. No parcels, no names.\n")
    out.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    calibration.cache_clear()


# -- recalibrating the stored valuations -------------------------------------------------------------------
def striking_metrics(session: Any, *, gate: float = 0.75, report: float = 0.78) -> dict[str, Any]:
    """How many listed leads our ceiling reaches at `gate` and at `report` of the ask (MLS fixers vs listed foreclosures)."""
    from sqlalchemy import select

    from db.models import Lead, Property, SignalType, Valuation
    from engine.underwriter import asking_price
    rows = session.execute(select(Valuation, Property).join(Property, Valuation.property_id == Property.id)
                           .join(Lead, Lead.property_id == Property.id).where(Valuation.is_current.is_(True))).all()
    m: dict[str, Any] = {"valuations": len(rows), "listed": 0, "fixers": 0, "foreclosures": 0,
                         f"strike_{int(gate * 100)}": 0, f"strike_{int(report * 100)}": 0,
                         f"fixers_{int(gate * 100)}": 0, f"fixers_{int(report * 100)}": 0,
                         f"foreclosures_{int(gate * 100)}": 0, f"foreclosures_{int(report * 100)}": 0,
                         "buyer_roi_below_min": 0, "quant": 0, "conflicts": 0}
    rois: list[float] = []
    min_roi = float(costs()["end_buyer"]["min_roi"])
    for val, prop in rows:
        a = val.assumptions_json or {}
        q = a.get("quant") or {}
        if q:
            m["quant"] += 1
            roi = (q.get("buyer") or {}).get("roi")
            if roi is not None and (q.get("buyer_max") or 0) > 0:      # buyer_max 0 = PASS, no buyer price to test
                rois.append(float(roi))
                if q.get("binding") != "local_alpha" and float(roi) < min_roi - 0.005:
                    m["buyer_roi_below_min"] += 1
        ask = asking_price(prop) or (a.get("listing") or {}).get("list_price")
        if not ask or not val.mao_aggressive:
            continue
        m["listed"] += 1
        kinds = {s.signal_type for s in prop.active_signals}
        fc = bool(kinds & {SignalType.SHERIFF_SALE, SignalType.LIS_PENDENS})
        m["foreclosures" if fc else "fixers"] += 1
        if val.arv_estimate and val.arv_estimate > 1.6 * ask:
            m["conflicts"] += 1
            continue
        ratio = val.mao_aggressive / ask
        for th in (gate, report):
            if ratio >= th:
                m[f"strike_{int(th * 100)}"] += 1
                m[f"{'foreclosures' if fc else 'fixers'}_{int(th * 100)}"] += 1
    if rois:
        m["buyer_roi_median"] = round(_pct(rois, 0.5) or 0, 3)
        m["buyer_roi_min"] = round(min(rois), 3)
    return m


def recalibrate_all(session: Any, *, limit: int | None = None, progress: Any = None) -> dict[str, Any]:
    """Re-price every current valuation with the configured model (no new comps) and report before / after."""
    from sqlalchemy import select

    from db.models import Property, Valuation
    from engine.underwriter import reprice
    before = striking_metrics(session)
    ids = [r[0] for r in session.execute(select(Property.id).join(Valuation, Valuation.property_id == Property.id)
                                         .where(Valuation.is_current.is_(True)).order_by(Property.id))]
    if limit:
        ids = ids[:limit]
    done = errors = 0
    for i, pid in enumerate(ids, 1):
        prop = session.get(Property, pid)
        try:
            if reprice(session, prop) is not None:
                done += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sweep
            errors += 1
            log.warning("reprice failed for property %s: %s", pid, exc)
        if i % 200 == 0:
            session.commit()
            if progress:
                progress(f"repriced {i}/{len(ids)}")
    session.commit()
    after = striking_metrics(session)
    return {"repriced": done, "errors": errors, "before": before, "after": after,
            "delta": {k: (after.get(k, 0) - before.get(k, 0)) for k in before if isinstance(before.get(k), (int, float))}}
