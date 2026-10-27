"""Pipeline Self-Improvement & Alpha Discovery agent.

Runs last in every pipeline. It:
  1. audits scraper health (run status, HTTP failure rates per host),
  2. audits data yield and coverage (owner names, upset amounts, parcel matches, sqft, valuations),
  3. audits contact readiness (top-100 leads with a callable phone or listing agent), buyer-match coverage,
     DNC data freshness and call outcomes (connect rate, overdue follow-ups),
  4. audits spread viability (verdict mix and MAO/ARV by lead source and municipality),
  5. tunes scoring:
       - learns a viability prior per lead source from underwriting verdicts (always),
       - lifts/discounts motivation weights from real outcomes once >= 20 leads are labeled,
  6. discovers alpha: ranks non-focus municipalities in the target counties by renovated-vs-
     distressed price gap ("spread potential") and auto-adds the best ones to the harvest list,
  7. writes a dated report to logs/swarm_intelligence.md (newest first, last 30 kept).
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from agents.base import AgentContext, BaseAgent
from agents.scrapers.njgis_client import NJGISClient
from core.config import LOG_DIR, settings
from db.models import AgentRun, DistressSignal, Lead, ScoringWeights, SwarmState, Valuation, utcnow
from engine.market import muni_stats
from engine.scoring import active_weights, default_weights

log = logging.getLogger(__name__)
REPORT_PATH = LOG_DIR / "swarm_intelligence.md"
REPORT_MARKER = "\n<!-- report -->\n"
POSITIVE_STAGES = {"INTERESTED", "APPOINTMENT", "OFFER_MADE", "NEGOTIATING", "UNDER_CONTRACT", "ASSIGNED", "CLOSED"}
NEGATIVE_STAGES = {"DEAD", "DO_NOT_CONTACT"}
GOOD_VERDICTS = {"STRONG", "WORKABLE"}
DEAL_VERDICTS = {"STRONG", "WORKABLE", "WORKABLE_82_PCT_FLOOR", "TIGHT"}
SPOKE_OUTCOMES = {"SPOKE_WARM_FOLLOWUP", "SPOKE_NEEDS_OFFER", "SPOKE_NOT_INTERESTED", "AGENT_WILL_PRESENT_LOI",
                  "OFFER_ACCEPTED", "DNC", "DEAD"}


def callable_contact(lead: Lead):
    """A phone we may dial: not DNC, not marked wrong (confidence 0)."""
    return next((c for c in lead.contacts if c.phone and c.dnc_status != "dnc" and (c.confidence_score or 0) > 0), None)


def contact_readiness(leads: list[Lead]) -> dict[str, float | int]:
    """Share of leads with a callable phone — owner/heir phone vs listing-agent phone — and who still needs a name."""
    owner_phone = agent_phone = name_only = nothing = 0
    for lead in leads:
        c = callable_contact(lead)
        if c is not None:
            if c.contact_type == "LISTING_AGENT" and not any(
                    x.phone and x.contact_type != "LISTING_AGENT" and x.dnc_status != "dnc" for x in lead.contacts):
                agent_phone += 1
            else:
                owner_phone += 1
        elif lead.property.owner_name or any(x.name for x in lead.contacts):
            name_only += 1
        else:
            nothing += 1
    n = len(leads) or 1
    return {"ready": (owner_phone + agent_phone) / n, "owner_phone": owner_phone, "agent_phone": agent_phone,
            "skip_traceable": name_only, "needs_name": nothing, "n": len(leads)}


def smoothed_rate(successes: int, total: int, prior: float = 0.4, k: float = 5.0) -> float:
    return (successes + prior * k) / (total + k)


def outcome_lifts(rows: list[tuple[dict, bool]], alpha: float = 1.0) -> dict[str, float]:
    """rows: (factor dict, is_positive). Returns factor -> lift = P(f|pos)/P(f|neg) (Laplace-smoothed)."""
    pos = [f for f, ok in rows if ok]
    neg = [f for f, ok in rows if not ok]
    factors = {k for f, _ in rows for k in f}
    lifts = {}
    for k in factors:
        p = (sum(1 for f in pos if k in f) + alpha) / (len(pos) + 2 * alpha)
        n = (sum(1 for f in neg if k in f) + alpha) / (len(neg) + 2 * alpha)
        lifts[k] = p / n
    return lifts


def tuned_weights(current: dict[str, float], seed: dict[str, float], lifts: dict[str, float]) -> dict[str, float]:
    out = dict(current)
    for k, lift in lifts.items():
        if k not in current or current[k] <= 0:
            continue
        step = min(max(lift ** 0.5, 0.8), 1.25)
        base = seed.get(k, current[k])
        out[k] = round(min(max(current[k] * step, base * 0.25), base * 2.0), 2)
    return out


class PipelineSelfImprovementAgent(BaseAgent):
    name = "self_improvement"
    description = "Audit yield, coverage, spreads; tune scoring weights; discover expansion towns."

    def run(self, ctx: AgentContext) -> None:
        s = ctx.session
        findings: list[str] = []
        actions: list[str] = []
        sections: dict[str, list[str]] = {}

        # 1. Scraper health ---------------------------------------------------------------------
        health: list[str] = []
        since = utcnow() - timedelta(days=7)
        names = [r[0] for r in s.execute(select(AgentRun.agent_name).distinct())]
        for name in sorted(names):
            if name == self.name:
                continue
            runs = s.scalars(select(AgentRun).where(AgentRun.agent_name == name, AgentRun.started_at >= since)
                             .order_by(AgentRun.started_at.desc())).all()
            if not runs:
                continue
            last = runs[0]
            ok = sum(1 for r in runs if r.status == "success")
            reqs = fails = 0
            for host, st in ((last.metrics_json or {}).get("http") or {}).items():
                reqs += st.get("requests", 0)
                fails += st.get("failures", 0)
            fail_rate = fails / reqs if reqs else 0.0
            health.append(f"| {name} | {last.status} | {ok}/{len(runs)} | {last.records_processed} | "
                          f"{last.new_leads_found} | {last.error_count} | {fail_rate:.0%} | "
                          f"{(last.metrics_json or {}).get('duration_s', '?')}s |")
            if last.status == "failed":
                findings.append(f"**{name} failed on its last run** — {(last.errors or ['?'])[-1][:160]}")
            elif fail_rate > 0.10:
                findings.append(f"{name}: {fail_rate:.0%} HTTP failure rate — source may be throttling or changing.")
        sections["Scraper health (7 days)"] = (
            ["| agent | last status | ok runs | processed | new leads | errors | http fail | duration |",
             "|---|---|---|---|---|---|---|---|"] + health) if health else ["No runs yet."]

        # 2. Yield & coverage -------------------------------------------------------------------
        open_leads = s.scalars(select(Lead).where(Lead.status_stage.notin_(["DEAD", "DO_NOT_CONTACT", "CLOSED"]))).all()
        by_source = Counter(l.primary_source for l in open_leads)
        total = len(open_leads) or 1
        owner_cov = sum(1 for l in open_leads if l.property.owner_name) / total
        unmatched = sum(1 for l in open_leads if "UNMATCHED" in l.property.parcel_id) / total
        sqft_cov = sum(1 for l in open_leads if l.property.sqft) / total
        sheriff = s.scalars(select(DistressSignal).where(DistressSignal.signal_type == "SHERIFF_SALE",
                                                         DistressSignal.active.is_(True))).all()
        upset_cov = sum(1 for g in sheriff if g.amount_owed) / (len(sheriff) or 1)
        top = sorted(open_leads, key=lambda l: l.priority_rank_score, reverse=True)[:50]
        val_cov = sum(1 for l in top if l.property.current_valuation) / (len(top) or 1)
        new_24h = sum(1 for l in open_leads if l.created_at >= utcnow() - timedelta(days=1))
        sections["Yield & coverage"] = [
            f"- Open leads: **{len(open_leads)}** ({new_24h} new in 24h) — by source: "
            + ", ".join(f"{k} {v}" for k, v in by_source.most_common()),
            f"- Active sheriff sales tracked: {len(sheriff)}; upset/judgment amount captured on {upset_cov:.0%}",
            f"- Owner name known: {owner_cov:.0%} · parcel matched: {1 - unmatched:.0%} · living area known: {sqft_cov:.0%}",
            f"- Top-50 leads underwritten: {val_cov:.0%}",
        ]
        if owner_cov < 0.6:
            findings.append(f"Owner names known on only {owner_cov:.0%} of open leads (NJGIN redacts names). "
                            "Biggest unlock: owner-record enrichment / skip tracing for absentee leads.")
        if upset_cov < 0.8 and sheriff:
            findings.append(f"Upset amount missing on {1 - upset_cov:.0%} of sheriff sales — extend detail parsers.")
        if unmatched > 0.10:
            findings.append(f"{unmatched:.0%} of leads didn't match a MOD-IV parcel — improve address matching.")
        if val_cov < 0.6 and top:
            findings.append(f"Only {val_cov:.0%} of the top 50 leads are underwritten — raise underwriter --top.")

        # 3. Contact readiness, buyer matching, DNC and call outcomes -------------------------------
        from engine.compliance import ftc_data_status
        from db.models import BuyerMatch, Interaction
        cr = contact_readiness(sorted(open_leads, key=lambda l: l.priority_rank_score, reverse=True)[:100])
        deals = [l for l in sorted(open_leads, key=lambda l: l.priority_rank_score, reverse=True)
                 if l.property.current_valuation and l.property.current_valuation.verdict in DEAL_VERDICTS][:50]
        matched = sum(1 for l in deals if len(l.buyer_matches) >= 5)
        match_cov = matched / (len(deals) or 1)
        ftc = ftc_data_status(s)
        week = utcnow() - timedelta(days=7)
        calls = s.scalars(select(Interaction).where(Interaction.occurred_at >= week)).all()
        outcomes = Counter(i.outcome for i in calls)
        spoke = sum(n for o, n in outcomes.items() if o in SPOKE_OUTCOMES)
        overdue = sum(1 for l in open_leads if l.next_followup_at and l.next_followup_at < utcnow() - timedelta(days=1))
        lines = [
            f"- Top-100 contact-ready: **{cr['ready']:.0%}** — owner/heir phone {cr['owner_phone']}, listing agent only "
            f"{cr['agent_phone']}; skip-traceable now (name known, no phone) {cr['skip_traceable']}; need a name first "
            f"{cr['needs_name']}",
            f"- Top deals with 5 matched cash buyers: {matched}/{len(deals)} ({match_cov:.0%}); buyer_matches rows: "
            f"{s.query(BuyerMatch).count()}",
            "- National DNC data: " + (f"{ftc['list_date']} ({'fresh' if ftc['fresh'] else 'STALE'})" if ftc["loaded"]
                                       else "not loaded"),
            f"- Calls (7 days): {len(calls)}; conversations {spoke} ({spoke / (len(calls) or 1):.0%} connect); "
            + (", ".join(f"{o.lower()} {n}" for o, n in outcomes.most_common()) or "none logged")
            + f"; overdue follow-ups {overdue}",
        ]
        from engine.scoring import signal_families
        ready_now = sum(1 for l in open_leads if l.ready_to_call)
        stacks = Counter(min(len(signal_families(l.property)), 4) for l in open_leads)
        offers_out = sum(1 for l in open_leads if l.status_stage == "OFFER_SENT_TO_AGENT")
        lines.append(f"- Ready to call now (deal + live phone / agent e-mail): **{ready_now}**; agent LOIs awaiting "
                     f"follow-up {offers_out}; stacked distress 2x {stacks.get(2, 0)}, 3x {stacks.get(3, 0)}, "
                     f"4x+ {stacks.get(4, 0)}")
        sections["Contact readiness & CRM"] = lines
        if ready_now < 50:
            findings.append(f"Only {ready_now} deals are ready to call. Underwrite more stale listings "
                            "(swarm run underwriter --opt scope=stale_listing) and refresh listing agents.")
        if cr["ready"] < 0.5:
            findings.append(f"Only {cr['ready']:.0%} of the top 100 leads have a callable number. "
                            f"{cr['skip_traceable']} can be skip-traced right now (python manage.py skiptrace "
                            f"--export-top 50); {cr['needs_name']} need an owner name first (owner exports / OPRA).")
        if not ftc["loaded"] or not ftc["fresh"]:
            findings.append("National DNC data is " + ("missing" if not ftc["loaded"] else "older than 31 days")
                            + ": download the free NJ area codes from telemarketing.donotcall.gov and run "
                            "python manage.py dnc --load <file> before dialing owners.")
        if deals and match_cov < 0.8:
            findings.append(f"Only {match_cov:.0%} of the top deals have 5 matched buyers — grow the buyer list "
                            "(county deed exports) or widen buyer geography.")
        if overdue:
            findings.append(f"{overdue} follow-up(s) are more than a day overdue — work the call queue first.")
        if len(calls) >= 20 and spoke / len(calls) < 0.1:
            findings.append(f"Connect rate {spoke / len(calls):.0%} on {len(calls)} calls: vary call times (evenings, "
                            "Saturday mornings) and add letters (manage.py mail-merge) for no-answer leads.")

        # 4. Spread viability ---------------------------------------------------------------------
        vals = s.scalars(select(Valuation).where(Valuation.is_current.is_(True))).all()
        verdicts = Counter(v.verdict for v in vals)
        src_stats: dict[str, list[str]] = defaultdict(list)
        muni_spreads: dict[str, list[float]] = defaultdict(list)
        for v in vals:
            lead = v.property.lead
            src_stats[lead.primary_source if lead else "unknown"].append(v.verdict)
            if v.arv_estimate and v.mao_conservative:
                muni_spreads[v.property.municipality or "?"].append(v.mao_conservative / v.arv_estimate)
        lines = [f"- Valuations: {len(vals)} — " + ", ".join(f"{k} {n}" for k, n in verdicts.most_common())]
        for src, vs in sorted(src_stats.items()):
            good = sum(1 for x in vs if x in GOOD_VERDICTS)
            lines.append(f"- {src}: {good}/{len(vs)} STRONG/WORKABLE")
        if muni_spreads:
            ranked = sorted(muni_spreads.items(), key=lambda kv: statistics.median(kv[1]), reverse=True)
            lines.append("- Median MAO/ARV by town (higher = more room): " + ", ".join(
                f"{m} {statistics.median(x):.0%} (n={len(x)})" for m, x in ranked[:8]))
        sections["Spread viability"] = lines
        sheriff_vs = src_stats.get("sheriff_sale", [])
        if len(sheriff_vs) >= 5 and sum(1 for x in sheriff_vs if x == "SHORT_SALE_ONLY") / len(sheriff_vs) > 0.5:
            findings.append("Most underwritten sheriff-sale leads are underwater (debt > MAO). Prioritize the ones with "
                            "equity, pitch short sales on the rest, and give absentee/high-equity leads more call time.")

        # 5. Tune scoring ---------------------------------------------------------------------------
        version, weights = active_weights(s)
        new_weights = {k: (dict(v) if isinstance(v, dict) else v) for k, v in weights.items()}
        priors = {}
        for src, vs in src_stats.items():
            if len(vs) >= 3:
                priors[src] = round(smoothed_rate(sum(1 for x in vs if x in GOOD_VERDICTS), len(vs)), 3)
        changed_reasons = []
        old_priors = weights.get("source_viability_prior") or {}
        if any(abs(priors.get(k, -1) - old_priors.get(k, -1)) > 0.03 for k in set(priors) | set(old_priors)):
            new_weights["source_viability_prior"] = priors
            changed_reasons.append("viability prior by source from underwriting verdicts: "
                                   + ", ".join(f"{k}={v:.2f}" for k, v in priors.items()))
        labeled = [l for l in s.scalars(select(Lead)).all()
                   if l.status_stage in POSITIVE_STAGES or (l.status_stage in NEGATIVE_STAGES and l.outcome)]
        if len(labeled) >= 20:
            rows = [((l.score_breakdown_json or {}).get("factors") or {}, l.status_stage in POSITIVE_STAGES) for l in labeled]
            lifts = outcome_lifts(rows)
            tuned = tuned_weights(weights["motivation"], default_weights()["motivation"], lifts)
            if tuned != weights["motivation"]:
                new_weights["motivation"] = tuned
                changed_reasons.append(f"motivation weights re-fit on {len(labeled)} outcomes")
        else:
            sections.setdefault("Learning", []).append(
                f"- Outcome learning waits for 20 labeled leads (have {len(labeled)}). Log call outcomes to feed it.")
        if changed_reasons:
            s.execute(ScoringWeights.__table__.update().values(active=False))
            s.add(ScoringWeights(version=version + 1, weights_json=new_weights, active=True,
                                 reason="; ".join(changed_reasons)))
            actions.append(f"Scoring weights v{version} → v{version + 1}: " + "; ".join(changed_reasons))

        # 6. Alpha discovery: expansion municipalities ------------------------------------------------
        if not self.options.get("skip_expansion"):
            actions.extend(self._expansion(ctx, sections))

        # 7. Report --------------------------------------------------------------------------------
        ctx.metrics.update({"open_leads": len(open_leads), "owner_coverage": round(owner_cov, 3),
                            "upset_coverage": round(upset_cov, 3), "valuations": len(vals),
                            "verdicts": dict(verdicts), "contact_readiness_top100": round(cr["ready"], 3),
                            "buyer_match_coverage": round(match_cov, 3), "calls_7d": len(calls),
                            "ready_to_call": ready_now, "stacked_2x_plus": sum(n for k, n in stacks.items() if k >= 2),
                            "connect_rate_7d": round(spoke / (len(calls) or 1), 3)})
        ctx.records_processed = len(open_leads)
        notes = findings + actions
        for n in notes:
            ctx.note(n)
        write_report(sections, findings, actions)

    def _expansion(self, ctx: AgentContext, sections: dict[str, list[str]]) -> list[str]:
        cfg = settings()
        s = ctx.session
        state = s.get(SwarmState, "auto_expansion")
        if state is None:
            state = SwarmState(key="auto_expansion", value_json={"municipalities": []})
            s.add(state)
        cached = s.get(SwarmState, "muni_spread_rank")
        fresh = cached and cached.updated_at and utcnow() - cached.updated_at < timedelta(days=7)
        focus = {(m["county"].upper(), m["name"]) for m in cfg.markets.get("focus_municipalities", [])}
        if fresh:
            ranking = cached.value_json.get("ranking", [])
        else:
            gis = NJGISClient()
            lo, hi = cfg.markets.arv_sweet_spot["min"], cfg.markets.arv_sweet_spot["max"]
            ranking = []
            for county in cfg.markets.get("counties", []):
                for mun_name, code in gis.municipalities(county).items():
                    st = muni_stats(gis, code, mun_name)
                    if not (st.median_ratio and st.p25_ratio and st.p75_ratio and st.median_sale_price):
                        continue
                    if st.sales_sampled < 60:
                        continue
                    gap = st.p75_ratio / st.p25_ratio            # renovated vs distressed spread
                    in_band = 1.0 if lo <= st.median_sale_price <= hi else 0.5
                    old = st.pct_pre_1980 or 0.0
                    score = round((gap - 1) * 100 * in_band * (0.5 + old), 2)
                    ranking.append({"county": county.upper(), "municipality": mun_name, "mun_code": code,
                                    "score": score, "gap": round(gap, 3), "median_price": st.median_sale_price,
                                    "pct_pre_1980": old, "sales": st.sales_sampled})
            ranking.sort(key=lambda r: r["score"], reverse=True)
            if cached is None:
                cached = SwarmState(key="muni_spread_rank", value_json={})
                s.add(cached)
            cached.value_json = {"ranking": ranking[:60]}
            cached.updated_at = utcnow()
        current = list(state.value_json.get("municipalities", []))   # copy: in-place edits aren't tracked
        have = {(m["county"], m["name"]) for m in current} | focus
        picks = [r for r in ranking if (r["county"], r["municipality"]) not in have][:2]
        max_auto = int(self.options.get("max_auto_expansion", 6))
        actions = []
        if picks and len(current) < max_auto:
            for r in picks[: max_auto - len(current)]:
                current.append({"county": r["county"], "name": r["municipality"], "score": r["score"],
                                "added": utcnow().date().isoformat()})
                actions.append(f"Expansion: added {r['municipality']} ({r['county'].title()}) to harvest list — "
                               f"renovated/distressed price gap {r['gap']:.2f}x, median sale ${r['median_price']:,.0f}, "
                               f"{r['pct_pre_1980']:.0%} pre-1980 stock")
            state.value_json = {"municipalities": current}
            flag_modified(state, "value_json")
            state.updated_at = utcnow()
        sections["Alpha discovery — spread potential by town (top 10)"] = [
            "| town | county | score | p75/p25 price-to-assessment gap | median sale | pre-1980 | in harvest |",
            "|---|---|---|---|---|---|---|"] + [
            f"| {r['municipality']} | {r['county'].title()} | {r['score']} | {r['gap']:.2f}x | ${r['median_price']:,.0f} | "
            f"{r['pct_pre_1980']:.0%} | {'yes' if (r['county'], r['municipality']) in have or any(c['name'] == r['municipality'] for c in current) else ''} |"
            for r in ranking[:10]]
        return actions


def write_report(sections: dict[str, list[str]], findings: list[str], actions: list[str], keep: int = 30) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    body = [f"## Swarm intelligence report — {stamp}", ""]
    body += ["### Bottlenecks & findings", *([f"- {f}" for f in findings] or ["- None detected."]), ""]
    body += ["### Actions taken automatically", *([f"- {a}" for a in actions] or ["- None this run."]), ""]
    for title, lines in sections.items():
        body += [f"### {title}", *lines, ""]
    header = ("# Swarm Intelligence Log\n\nWritten by `swarm/self_improvement.py` after every pipeline run "
              "(newest first). Contains aggregate metrics only — no personal data.\n")
    old_reports: list[str] = []
    if REPORT_PATH.exists():
        parts = REPORT_PATH.read_text().split(REPORT_MARKER)
        old_reports = [p for p in parts[1:] if p.strip()]
    reports = ["\n".join(body)] + old_reports[: keep - 1]
    REPORT_PATH.write_text(header + "".join(REPORT_MARKER + r.rstrip() + "\n" for r in reports))
