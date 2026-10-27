"""Lead scoring: motivation (will they sell?), viability (is there a deal?), priority (call order).

Every score is explainable: `score_breakdown_json` stores each contributing factor and the
human-readable reasons the briefing prints. Weights are versioned in `scoring_weights`; the
self-improvement agent writes new versions (outcome-based lift once outcomes exist, and a
learned viability prior per lead source from underwriting results).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.base import AgentContext, BaseAgent
from agents.scrapers.njgis_client import years_owned
from core.config import settings
from db.models import Lead, Property, ScoringWeights, SignalType, utcnow

log = logging.getLogger(__name__)

TERMINAL = {"DEAD", "DO_NOT_CONTACT", "CLOSED"}
NON_DEAL_PRIORITY_FACTOR = {"PASS": 0.5, "FRFPA_BLOCKED": 0.6, "EQUITY_RESCUE_REFERRAL": 0.65, "SHORT_SALE_ONLY": 0.75}
VERDICT_POINTS = {"STRONG": 45, "WORKABLE": 33, "WORKABLE_82_PCT_FLOOR": 28, "TIGHT": 18, "SHORT_SALE_ONLY": 8,
                  "EQUITY_RESCUE_REFERRAL": 8, "FRFPA_BLOCKED": 5, "PASS": 0}
NO_OFFER_VERDICTS = {"FRFPA_BLOCKED", "PASS", "EQUITY_RESCUE_REFERRAL"}


@dataclass
class Score:
    motivation: float
    viability: float
    priority: float
    factors: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    viability_basis: str = "proxy"


def default_weights() -> dict[str, Any]:
    s = settings().get("scoring", {})
    return {"motivation": dict(s.get("motivation", {})),
            "priority_mix": dict(s.get("priority_mix", {"motivation": 0.55, "viability": 0.45})),
            "listing_striking_distance": dict(s.get("listing_striking_distance", {})),
            "source_viability_prior": {}}


def active_weights(session: Session) -> tuple[int, dict[str, Any]]:
    row = session.scalar(select(ScoringWeights).where(ScoringWeights.active.is_(True))
                         .order_by(ScoringWeights.version.desc()))
    if row is None:
        row = ScoringWeights(version=1, weights_json=default_weights(), active=True,
                             reason="seed weights from config/settings.yaml")
        session.add(row)
        session.flush()
    weights = default_weights()
    weights.update(row.weights_json or {})
    weights["motivation"] = {**default_weights()["motivation"], **(row.weights_json or {}).get("motivation", {})}
    return row.version, weights


def _fmt_money(v: float | None) -> str:
    return f"${v:,.0f}" if v is not None else "?"


def days_phrase(days: int) -> str:
    return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"


SIGNAL_FAMILIES = {
    SignalType.SHERIFF_SALE: "foreclosure", SignalType.LIS_PENDENS: "foreclosure",
    SignalType.STALE_LISTING: "listing", SignalType.FAILED_LISTING: "listing", SignalType.ACTIVE_LISTING: "listing",
    SignalType.LONG_TERM_ABSENTEE: "absentee", SignalType.OUT_OF_STATE_OWNER: "absentee",
    SignalType.TAX_DELINQUENT: "tax", SignalType.TAX_SALE_CERTIFICATE: "tax",
    SignalType.ESTATE_OWNER: "estate", SignalType.PROBATE: "estate",
    SignalType.VACANT: "vacant", SignalType.CODE_VIOLATION: "code", SignalType.PORTFOLIO_OWNER: "portfolio",
}


def signal_families(prop: Property) -> list[str]:
    """Distinct kinds of distress on the property (several signals of one kind count once)."""
    fams = {SIGNAL_FAMILIES[s.signal_type] for s in prop.active_signals if s.signal_type in SIGNAL_FAMILIES}
    if prop.is_estate_owned:
        fams.add("estate")
    return sorted(fams)


def stack_badge(prop: Property) -> str | None:
    n = len(signal_families(prop))
    return f"[{n}x STACKED]" if n >= 2 else None


def motivation_factors(prop: Property, today: date | None = None) -> tuple[dict[str, float], list[str]]:
    """Feature strengths in [0, 1] plus reason strings. Weights are applied separately."""
    today = today or date.today()
    f: dict[str, float] = {}
    reasons: list[str] = []
    sigs = prop.active_signals
    by_type: dict[str, list] = {}
    for s in sigs:
        by_type.setdefault(s.signal_type, []).append(s)

    sheriff = sorted(by_type.get(SignalType.SHERIFF_SALE, []), key=lambda s: s.scheduled_auction_date or date.max)
    if sheriff:
        s = sheriff[0]
        meta = s.raw_metadata_json or {}
        f["sheriff_sale_scheduled"] = 0.6 if meta.get("on_hold") else 1.0
        days = (s.scheduled_auction_date - today).days if s.scheduled_auction_date else None
        txt = f"Sheriff sale {s.scheduled_auction_date:%b %d}" if s.scheduled_auction_date else "Sheriff sale scheduled"
        if days is not None:
            txt += f" ({days_phrase(days)})" if days >= 0 else " (date passed — check adjournment)"
            if 0 <= days <= 30:
                # A wholesale closing takes weeks: a sale in the next couple of days only helps if it is
                # adjourned again, so urgency peaks 1–4 weeks out.
                f["sale_within_30_days"] = 0.5 if days <= 2 else 0.8 if days <= 7 else 1.0
            elif 0 <= days <= 60:
                f["sale_within_60_days"] = 1.0
        if s.amount_owed:
            txt += f", upset {_fmt_money(s.amount_owed)}"
        if meta.get("on_hold"):
            txt += f" [{s.status}]"
        reasons.append(txt)
        if meta.get("adjournments_total"):
            f["adjourned_sale"] = 1.0
            reasons.append(f"{meta['adjournments_total']} adjournment(s) so far"
                           + (f", {meta.get('defendant_adjournments')} by homeowner" if meta.get("defendant_adjournments") else ""))
    if by_type.get(SignalType.LIS_PENDENS):
        f["lis_pendens"] = 1.0
        reasons.append("Lis pendens filed (early foreclosure)")
    if by_type.get(SignalType.TAX_SALE_CERTIFICATE):
        c = by_type[SignalType.TAX_SALE_CERTIFICATE][0]
        f["tax_sale_certificate"] = 1.0
        txt = f"Tax sale certificate ({_fmt_money(c.amount_owed)})"
        if c.filing_date:
            txt += f" sold {c.filing_date:%m/%Y}; holder may file foreclosure from {(c.raw_metadata_json or {}).get('foreclosure_filing_earliest', '?')}"
        reasons.append(txt)
    if by_type.get(SignalType.TAX_DELINQUENT):
        d = by_type[SignalType.TAX_DELINQUENT][0]
        meta = d.raw_metadata_json or {}
        f["tax_delinquent"] = 1.0 if "taxes" in (meta.get("charges") or ["taxes"]) else 0.6
        txt = f"On municipal tax sale list: {_fmt_money(d.amount_owed)} owed ({', '.join(meta.get('charges') or ['taxes'])})"
        if d.scheduled_auction_date:
            days = (d.scheduled_auction_date - today).days
            txt += f" — lien sale {d.scheduled_auction_date:%b %d} ({days_phrase(days)})" if days >= 0 else ""
        reasons.append(txt)
    if by_type.get(SignalType.PROBATE) or by_type.get(SignalType.ESTATE_OWNER) or prop.is_estate_owned:
        f["probate_estate"] = 1.0
        reasons.append("Estate / heirs involved (probate)")
    if by_type.get(SignalType.CODE_VIOLATION):
        f["code_violation"] = 1.0
        reasons.append("Code violations")
    if by_type.get(SignalType.VACANT):
        f["vacant"] = 1.0
        reasons.append("Vacant")

    tenure = years_owned(prop.last_sale_date)
    if prop.is_out_of_state or by_type.get(SignalType.OUT_OF_STATE_OWNER):
        f["out_of_state"] = 1.0
        reasons.append(f"Out-of-state owner ({prop.owner_state or '?'})")
    if prop.is_absentee or by_type.get(SignalType.LONG_TERM_ABSENTEE):
        f["absentee"] = 1.0
        if "out_of_state" not in f:
            reasons.append("Absentee owner (mails elsewhere)")
    if tenure is not None and tenure >= 10:
        f["long_tenure"] = min(tenure / 30, 1.0)
        reasons.append(f"Owned {tenure:.0f} yrs (since {prop.last_sale_date:%Y})")
    if prop.year_built and prop.year_built < 1980:
        f["old_stock"] = 1.0 if prop.year_built < 1950 else 0.6
    stale = by_type.get(SignalType.STALE_LISTING, [])
    if stale:
        meta = stale[0].raw_metadata_json or {}
        dom = meta.get("days_on_market") or 0
        f["stale_listing"] = min(dom / 180, 1.0) if dom >= 90 else 0.4
        txt = f"Listed {dom} days at {_fmt_money(meta.get('list_price'))}"
        if meta.get("price_reduced"):
            f["price_drops"] = 1.0
            txt += ", price reduced"
        if meta.get("keywords"):
            f["distress_keywords"] = min(len(meta["keywords"]) / 3, 1.0)
            txt += f" — \"{', '.join(meta['keywords'])}\""
        reasons.append(txt)
    portfolio = by_type.get(SignalType.PORTFOLIO_OWNER, [])
    if portfolio:
        meta = portfolio[0].raw_metadata_json or {}
        f["portfolio_owner"] = 1.0
        reasons.append(f"Portfolio owner: same owner/mailing on {meta.get('properties', '?')} distressed/absentee "
                       "leads — one conversation can cover several houses")
    if prop.is_corporate_owned:
        f["corporate_owner"] = 1.0
    fams = signal_families(prop)
    if len(fams) >= 2:
        f["stack_4x" if len(fams) >= 4 else "stack_3x" if len(fams) == 3 else "stack_2x"] = 1.0
        reasons.insert(0, f"{len(fams)}x stacked distress: {' + '.join(fams)}")
    return f, reasons


def listing_terms(prop: Property) -> dict[str, Any]:
    """Asking-price gap (list_price, list_to_mao_70/75, striking_distance, valuation_conflict), recomputed from the
    current valuation so older valuations get the ARV-vs-ask sanity check too."""
    from engine.underwriter import asking_price, listing_gap
    val = prop.current_valuation
    if val is None:
        return {}
    ask = asking_price(prop) or ((val.assumptions_json or {}).get("listing") or {}).get("list_price")
    return listing_gap(ask, val.mao_conservative, val.mao_aggressive, val.arv_estimate) if ask else {}


def has_agent_contact(prop: Property) -> bool:
    lead = prop.lead
    return bool(lead and any(c.contact_type == "LISTING_AGENT" and (c.phone or c.email) and c.dnc_status != "dnc"
                             for c in lead.contacts))


def viability(prop: Property, weights: dict[str, Any], primary_source: str | None) -> tuple[float, list[str], str]:
    cfg = settings()
    lo, hi = cfg.markets.arv_sweet_spot["min"], cfg.markets.arv_sweet_spot["max"]
    reasons: list[str] = []
    val = prop.current_valuation
    if val and val.arv_estimate:
        score = VERDICT_POINTS.get(val.verdict or "", 15)
        if val.arv_estimate and val.mao_conservative:
            score += max(0.0, min(val.mao_conservative / val.arv_estimate / 0.5, 1.0)) * 20
        if val.deal_spread is not None and val.arv_estimate:
            score += max(-10.0, min(val.deal_spread / val.arv_estimate * 50, 15))
        score += (val.confidence_score or 0) * 0.12
        score += 8 if lo <= val.arv_estimate <= hi else 0
        reasons.append(f"Underwritten {val.verdict}: ARV {_fmt_money(val.arv_estimate)}, MAO "
                       f"{_fmt_money(val.mao_conservative)}, fee {_fmt_money(val.target_assignment_fee)}")
        gap = listing_terms(prop)
        if gap.get("striking_distance") and has_agent_contact(prop):
            cfg = weights.get("listing_striking_distance") or settings().get("scoring", {}).get(
                "listing_striking_distance", {})
            close = (gap.get("list_to_mao_75") or 0) >= 0.90
            score += float(cfg.get("viability_close" if close else "viability", 22 if close else 15))
            reasons.append(f"Within striking distance of ask: our 75% ceiling is {gap['list_to_mao_75']:.0%} of the "
                           f"{_fmt_money(gap['list_price'])} list price (70% MAO = {gap.get('list_to_mao_70', 0):.0%}); "
                           "listing agent reachable")
        return max(0.0, min(score, 100.0)), reasons, "valuation"

    # Not underwritten yet: equity proxies + learned prior for this lead source.
    score = 0.0
    value = prop.estimated_value
    sheriff = next((s for s in prop.active_signals if s.signal_type == SignalType.SHERIFF_SALE), None)
    debt = sheriff.amount_owed if sheriff else None
    if value and debt:
        equity_ratio = 1 - debt / value
        score += max(-10.0, min(equity_ratio, 0.6)) / 0.6 * 40
        reasons.append(f"Est. equity {equity_ratio:.0%} (value {_fmt_money(value)} vs upset {_fmt_money(debt)})")
    elif prop.estimated_equity is not None and value:
        ratio = prop.estimated_equity / value
        score += max(0.0, min(ratio, 1.0)) * 40
        if ratio >= 0.7:
            reasons.append(f"Likely high equity (~{ratio:.0%}, est. value {_fmt_money(value)})")
    elif prop.assessed_total:
        score += 15
    if value and lo <= value <= hi * 1.2:
        score += 12
    if prop.property_class == "2":
        score += 8
    tenure = years_owned(prop.last_sale_date)
    if tenure is not None and tenure < 3:
        score -= 15
    if prop.parcel_id and "UNMATCHED" not in prop.parcel_id:
        score += 5
    prior = (weights.get("source_viability_prior") or {}).get(primary_source or "")
    if prior is not None:
        score = 0.7 * score + 0.3 * prior * 100
    return max(0.0, min(score, 100.0)), reasons, "proxy"


def score_property(prop: Property, weights: dict[str, Any], primary_source: str | None = None) -> Score:
    factors, reasons = motivation_factors(prop)
    w = weights["motivation"]
    contributions = {k: round(w.get(k, 0) * v, 2) for k, v in factors.items()}
    raw = sum(contributions.values())
    motivation = max(0.0, min(raw, 100.0))
    via, via_reasons, basis = viability(prop, weights, primary_source)
    mix = weights.get("priority_mix", {"motivation": 0.55, "viability": 0.45})
    priority = mix["motivation"] * motivation + mix["viability"] * via
    # Urgency: a near auction with real equity is the most valuable call of the day.
    if factors.get("sale_within_30_days") and via >= 40:
        priority += 5
    gap = listing_terms(prop)
    if gap.get("striking_distance") and has_agent_contact(prop):
        cfg = weights.get("listing_striking_distance") or settings().get("scoring", {}).get(
            "listing_striking_distance", {})
        priority += float(cfg.get("priority", 8))
    # A motivated seller with no compliant/profitable wholesale offer is a referral, not a deal.
    verdict = prop.current_valuation.verdict if prop.current_valuation else None
    priority *= NON_DEAL_PRIORITY_FACTOR.get(verdict, 1.0)
    if prop.owner_name:
        priority += 2   # someone to ask for
    priority = round(max(0.0, min(priority, 100.0)), 1)
    return Score(motivation=round(motivation, 1), viability=round(via, 1), priority=priority,
                 factors=contributions, reasons=reasons + via_reasons, viability_basis=basis)


class LeadScoringAgent(BaseAgent):
    name = "lead_scoring"
    description = "Recalculate motivation, viability and priority for every open lead."

    def run(self, ctx: AgentContext) -> None:
        version, weights = active_weights(ctx.session)
        leads = ctx.session.scalars(select(Lead).where(Lead.status_stage.notin_(TERMINAL))).all()
        self._fill_value_proxies(ctx, [l.property for l in leads])
        ctx.records_processed += score_leads(leads, version, weights)
        ctx.checkpoint()
        ctx.metrics["scoring_version"] = version
        ctx.metrics["leads_scored"] = len(leads)

    @staticmethod
    def _fill_value_proxies(ctx: AgentContext, props: list[Property]) -> None:
        """Assessment x town sale/assessment ratio for parcels that have no estimate yet."""
        from agents.scrapers.njgis_client import NJGISClient
        from engine.equity import estimate_equity
        from engine.market import muni_stats

        todo = [p for p in props if p.estimated_value is None and p.assessed_total and p.mun_code]
        if not todo:
            return
        gis = NJGISClient()
        for p in todo:
            try:
                stats = muni_stats(gis, p.mun_code, p.municipality or "")
            except Exception as exc:  # noqa: BLE001
                ctx.error(f"muni stats {p.mun_code}: {exc}")
                continue
            value = stats.value_from_assessment(p.assessed_total)
            if value:
                p.estimated_value = value
                debt = next((s.amount_owed for s in p.active_signals
                             if s.signal_type == SignalType.SHERIFF_SALE and s.amount_owed), None)
                p.estimated_equity = estimate_equity(value, p.last_sale_price, p.last_sale_date, known_debt=debt)
                ctx.bump("value_proxies_filled")
        ctx.checkpoint()


# Pipeline stage → priority points: a seller who engaged is worth more than any cold record.
STAGE_BONUS = {"CONTACTED": 4, "INTERESTED": 12, "APPOINTMENT": 15, "OFFER_SENT_TO_AGENT": 16, "OFFER_MADE": 18,
               "NEGOTIATING": 20,
               "UNDER_CONTRACT": 25, "ASSIGNED": 25}


def engagement_adjustment(lead: Lead) -> tuple[float, list[str]]:
    """Priority points from what the operator has learned (CRM stage, reachable phone, dead-end dialing)."""
    if lead.status_stage in TERMINAL:
        return 0.0, [f"pipeline: {lead.status_stage.replace('_', ' ').lower()}"]
    bonus, why = float(STAGE_BONUS.get(lead.status_stage, 0)), []
    if bonus:
        why.append(f"pipeline: {lead.status_stage.replace('_', ' ').lower()} (+{bonus:.0f})")
    if any(c.phone and c.dnc_status != "dnc" and (c.confidence_score or 0) > 0 for c in lead.contacts):
        bonus += 4
        why.append("callable phone on file (+4)")
    if lead.contact_attempts >= 6 and lead.status_stage in ("NEW", "SKIP_TRACE", "CONTACT_ATTEMPTED"):
        bonus -= 6
        why.append(f"{lead.contact_attempts} attempts without a conversation (-6): switch to a letter / another contact")
    return bonus, why


READY_VERDICTS = {"STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT"}


def ready_to_call(lead: Lead) -> tuple[bool, str | None]:
    """An underwritten deal with an offer path AND someone reachable right now (phone or listing-agent e-mail)."""
    if lead.status_stage in TERMINAL:
        return False, None
    val = lead.property.current_valuation
    if val is None:
        return False, None
    gap = listing_terms(lead.property)
    if gap.get("valuation_conflict"):
        return False, None          # ARV far above ask: re-check comps before anyone calls with a number
    if val.verdict in NO_OFFER_VERDICTS or val.verdict == "SHORT_SALE_ONLY":
        return False, None
    if gap.get("list_price"):
        # Listed: the asking price is the obstacle, not the spread — only listings our 75% ceiling can reach.
        if not gap.get("striking_distance"):
            return False, None
    elif val.verdict not in READY_VERDICTS:
        return False, None
    for c in sorted(lead.contacts, key=lambda c: (c.contact_type != "LISTING_AGENT", -(c.confidence_score or 0))):
        if c.dnc_status == "dnc" or (c.confidence_score or 0) <= 0:
            continue
        if c.phone:
            return True, ("listing agent phone" if c.contact_type == "LISTING_AGENT" else
                          f"{c.contact_type.lower().replace('_', ' ')} phone")
        if c.email and c.contact_type == "LISTING_AGENT":
            return True, "listing agent e-mail"
    return False, None


def apply_score(lead: Lead, version: int, weights: dict[str, Any]) -> Score:
    from engine.underwriter import frfpa_decision   # local import: underwriter imports this module
    if any(s.signal_type in (SignalType.SHERIFF_SALE, SignalType.LIS_PENDENS) for s in lead.property.active_signals):
        lead.frfpa_track = frfpa_decision(lead.property).track
    sc = score_property(lead.property, weights, lead.primary_source)
    bonus, why = engagement_adjustment(lead)
    if bonus or why:
        sc.priority = round(max(0.0, min(sc.priority + bonus, 100.0)), 1) if lead.status_stage not in TERMINAL else 0.0
        sc.factors["engagement"] = bonus
        sc.reasons.extend(why)
    lead.motivation_score, lead.deal_viability_score, lead.priority_rank_score = sc.motivation, sc.viability, sc.priority
    ready, via = ready_to_call(lead)
    lead.ready_to_call = ready
    lead.score_breakdown_json = {"factors": sc.factors, "reasons": sc.reasons,
                                 "viability_basis": sc.viability_basis, "scored_at": utcnow().isoformat(),
                                 "stack": signal_families(lead.property), "ready_via": via}
    lead.scoring_version = version
    lead.ai_strategy_brief = strategy_brief(lead, sc)
    return sc


def score_leads(leads: list[Lead], version: int, weights: dict[str, Any]) -> int:
    for lead in leads:
        apply_score(lead, version, weights)
    return len(leads)


def strategy_brief(lead: Lead, sc: Score) -> str:
    """One-paragraph plan of attack, derived from the dominant motivation factor."""
    p = lead.property
    f = sc.factors
    val = p.current_valuation
    parts: list[str] = []
    if "sheriff_sale_scheduled" in f:
        s = next((x for x in p.active_signals if x.signal_type == SignalType.SHERIFF_SALE), None)
        meta = (s.raw_metadata_json or {}) if s else {}
        days = (s.scheduled_auction_date - date.today()).days if s and s.scheduled_auction_date else None
        txt = "Pre-auction: at sheriff sale the owner loses the house and must chase any surplus through the court. "
        if days is not None and days >= 0:
            txt += f"Sale {days_phrase(days)} — call now and mail an urgent letter today. "
        if (meta.get("defendant_adjournments") or 0) >= 2:
            txt += ("Homeowner has used both statutory adjournments; check the current status before investing time. ")
        txt += ("Don't advise on postponing the sale yourself (a regulated 'distressed property service' under "
                "FRFPA) — point them to the sheriff's office, their attorney or a free HUD-approved counselor. ")
        parts.append(txt.strip())
        if val and val.verdict == "SHORT_SALE_ONLY":
            parts.append("Debt exceeds MAO: pitch a short sale (lender approval) or pass; don't promise a number above MAO.")
        a = (val.assumptions_json or {}) if val else {}
        if a.get("frfpa_applies"):
            parts.append(f"FRFPA ({a.get('frfpa_track')}): {a.get('frfpa_basis')} Any offer ≥ 82% of FMV "
                         f"({_fmt_money(a.get('frfpa_floor'))}); attorney-drafted FRFPA contract with 10-business-day "
                         "cancellation; close at an attorney/title office.")
            if a.get("verify_before_contract"):
                parts.append("Verify: " + "; ".join(a["verify_before_contract"]) + ".")
            ex = a.get("exempt_scenario") or {}
            if ex and a.get("frfpa_track") == "ESTATE_VERIFY_OCCUPANCY":
                parts.append(f"If nobody lives there it is exempt: {ex.get('verdict')} at MAO {_fmt_money(ex.get('mao_conservative'))}.")
        elif a.get("verify_before_contract"):
            parts.append(f"FRFPA exempt ({a.get('frfpa_track')}, {a.get('frfpa_confidence')}): {a.get('frfpa_basis')} "
                         "Before a contract: " + "; ".join(a["verify_before_contract"]) + ".")
    elif "stale_listing" in f:
        parts.append("Listed property: call the listing agent with an as-is cash offer near the anchor; "
                     "ask about seller motivation, days on market and price history.")
    elif "probate_estate" in f:
        parts.append("Estate: find the executor/administrator; lead with empathy and a fast, as-is close that "
                     "spares the heirs repairs and carrying costs.")
    elif "out_of_state" in f:
        parts.append("Out-of-state owner: managing a NJ rental from afar (taxes, tenants, repairs) — offer a "
                     "hassle-free sale; owner name pending lookup, use the mailing address for a letter.")
    elif "absentee" in f:
        parts.append("Absentee landlord with long tenure: likely high equity and tenant fatigue; mail + call.")
    if val and val.verdict == "WORKABLE_82_PCT_FLOOR":
        a = val.assumptions_json or {}
        parts.append(f"Compliant offer {_fmt_money(a.get('compliant_offer'))} (82% of FMV); light-rehab/wholetail buyer "
                     f"at {_fmt_money(a.get('track_buyer_price'))} leaves ~{_fmt_money(a.get('track_fee'))}.")
    elif val and val.verdict == "EQUITY_RESCUE_REFERRAL":
        a = val.assumptions_json or {}
        parts.append(f"Equity at risk ~{_fmt_money(a.get('equity_at_risk'))}: the owner's best outcome is a market sale "
                     "with a listing agent before the auction. Unlicensed, you can't take a referral fee (N.J.S.A. "
                     "45:15-3) — refer for goodwill or get licensed.")
    elif val and val.verdict in ("FRFPA_BLOCKED", "PASS"):
        parts.append("No compliant wholesale offer at today's numbers — offer a listing-agent referral, or revisit "
                     "if the repair estimate drops after a walkthrough.")
    elif val and val.mao_conservative:
        parts.append(f"Open at {_fmt_money(val.anchor_offer)}, walk-away {_fmt_money(val.mao_conservative)} "
                     f"(aggressive ceiling {_fmt_money(val.mao_aggressive)}).")
    parts.append("Disclose: investor, not an agent; may assign the contract; seller should have an attorney review.")
    return " ".join(parts)
