"""Swarm orchestrator: registry, pipeline order, parallel scrape phase, cron daemon.

    python -m swarm.orchestrator list
    python -m swarm.orchestrator run-all [--sequential] [--skip stale_listings] [--top 25]
    python -m swarm.orchestrator run sheriff_sales [--opt limit=50]
    python -m swarm.orchestrator job data_refresh
    python -m swarm.orchestrator daemon           # APScheduler, cron schedules from config/settings.yaml
    python -m swarm.orchestrator status

Jobs (each composed of agents; every agent logs its own agent_runs row):
    SheriffSaleMonitorAgent      sheriff_sales ∥ lis_pendens (county-clerk foreclosure filings)
    DataRefreshAgent             tax_assessment + cash_buyers + stale_listings + tax_sales → owner_enrichment
    ContactEnrichmentAgent       municipal_owners → listing_agents → owner_resolver → signal_stacker
                                 (names, agents, heirs, mailing matches; listed distress + portfolio owners)
    CompUnderwriterAgent         lead_scoring → underwriter(top N) → outlier_guard → lead_scoring → fast_track
                                 → buyer_matcher
    PipelineSelfImprovementAgent closing_alerts → self_improvement
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select

from agents.base import BaseAgent
from agents.scrapers.nj_tax_assessment_agent import CashBuyerAgent, TaxAssessmentAgent
from agents.scrapers.sheriff_sale_agent import SheriffSaleAgent
from core.config import LOG_DIR, settings
from db.database import init_db, session_scope
from db.models import AgentRun
from engine.scoring import LeadScoringAgent
from engine.underwriter import UnderwriterAgent
from swarm.self_improvement import PipelineSelfImprovementAgent

log = logging.getLogger("swarm")


def _stale_listing_agent(**opts: Any) -> BaseAgent:
    from agents.scrapers.stale_listing_distress_agent import StaleListingDistressAgent
    return StaleListingDistressAgent(**opts)


def _owner_enrichment_agent(**opts: Any) -> BaseAgent:
    from agents.scrapers.owner_records_agent import OwnerEnrichmentAgent
    return OwnerEnrichmentAgent(**opts)


def _listing_agent_enricher(**opts: Any) -> BaseAgent:
    from agents.enrichment.listing_agent_enricher import ListingAgentEnricher
    return ListingAgentEnricher(**opts)


def _municipal_owners(**opts: Any) -> BaseAgent:
    from agents.enrichment.municipal_owner_layers import MunicipalOwnerLayersAgent
    return MunicipalOwnerLayersAgent(**opts)


def _owner_resolver(**opts: Any) -> BaseAgent:
    from agents.enrichment.public_owner_resolver import PublicOwnerResolver
    return PublicOwnerResolver(**opts)


def _buyer_matcher(**opts: Any) -> BaseAgent:
    from engine.buyer_matcher import BuyerMatcherAgent
    return BuyerMatcherAgent(**opts)


def _signal_stacker(**opts: Any) -> BaseAgent:
    from agents.enrichment.signal_stacker import SignalStackerAgent
    return SignalStackerAgent(**opts)


def _outlier_guard(**opts: Any) -> BaseAgent:
    from engine.valuation_guard import OutlierGuardAgent
    return OutlierGuardAgent(**opts)


def _fast_track(**opts: Any) -> BaseAgent:
    from engine.fast_track import FastTrackAgent
    return FastTrackAgent(**opts)


def _closing_alerts(**opts: Any) -> BaseAgent:
    from engine.closing_coordinator import ClosingAlertsAgent
    return ClosingAlertsAgent(**opts)


def _lis_pendens(**opts: Any) -> BaseAgent:
    from agents.scrapers.lis_pendens_agent import LisPendensAgent
    return LisPendensAgent(**opts)


def _tax_sale_agent(**opts: Any) -> BaseAgent:
    from agents.scrapers.tax_sale_agent import TaxSaleAgent
    return TaxSaleAgent(**opts)


@dataclass
class AgentSpec:
    name: str
    factory: Callable[..., BaseAgent | None]
    description: str
    phase: str                 # scrape | enrich | analyze | improve
    defaults: dict[str, Any] = field(default_factory=dict)


REGISTRY: dict[str, AgentSpec] = {spec.name: spec for spec in [
    AgentSpec("sheriff_sales", SheriffSaleAgent, SheriffSaleAgent.description, "scrape"),
    AgentSpec("tax_assessment", TaxAssessmentAgent, TaxAssessmentAgent.description, "scrape"),
    AgentSpec("cash_buyers", CashBuyerAgent, CashBuyerAgent.description, "scrape"),
    AgentSpec("stale_listings", _stale_listing_agent, "Active listings with 90+ DOM, price cuts, distress language.", "scrape"),
    AgentSpec("tax_sales", _tax_sale_agent, "Municipal tax lien sale lists: delinquent owners, sold certificates.", "scrape"),
    AgentSpec("lis_pendens", _lis_pendens, "County-clerk lis pendens / foreclosure filings (Union + Essex live, other "
              "counties from saved pages) → LIS_PENDENS signals.", "scrape"),
    AgentSpec("owner_enrichment", _owner_enrichment_agent, "Owner names/sqft from county exports + Newark snapshot; name buyers, find flips.", "enrich"),
    AgentSpec("listing_agents", _listing_agent_enricher, "Listing-agent contacts (name, brokerage, phones) for stale MLS leads.", "enrich"),
    AgentSpec("municipal_owners", _municipal_owners, "Owner names from Jersey City / New Brunswick / Plainfield parcel layers.", "enrich"),
    AgentSpec("owner_resolver", _owner_resolver, "Defendants/heirs/executors, mailing cross-reference, entity lookups, municipal owner layers.", "enrich"),
    AgentSpec("signal_stacker", _signal_stacker, "Stacked distress: listed foreclosures/absentee/tax leads, portfolio owners.", "enrich"),
    AgentSpec("lead_scoring", LeadScoringAgent, LeadScoringAgent.description, "analyze"),
    AgentSpec("underwriter", UnderwriterAgent, UnderwriterAgent.description, "analyze", {"top": 25}),
    AgentSpec("outlier_guard", _outlier_guard, "Comp hygiene + ARV guard on every valuation (package deeds, ask, "
              "assessment, $/sqft, non-residential); re-underwrites thin comp sets.", "analyze", {"max_live": 25}),
    AgentSpec("fast_track", _fast_track, "Listed-foreclosure sniper queue + portfolio owners (counts, call-today notes).",
              "analyze"),
    AgentSpec("closing_alerts", _closing_alerts, "Closing milestones: attorney review, deadlines into the call queue.",
              "improve"),
    AgentSpec("buyer_matcher", _buyer_matcher, "Top-5 recorded cash buyers for each top underwritten deal.", "analyze", {"top": 50}),
    AgentSpec("self_improvement", PipelineSelfImprovementAgent, PipelineSelfImprovementAgent.description, "improve"),
]}

JOBS: dict[str, list[str]] = {
    "SheriffSaleMonitorAgent": ["sheriff_sales", "lis_pendens"],
    "DataRefreshAgent": ["tax_assessment", "cash_buyers", "stale_listings", "tax_sales", "owner_enrichment"],
    "ContactEnrichmentAgent": ["municipal_owners", "listing_agents", "owner_resolver", "signal_stacker"],
    "CompUnderwriterAgent": ["lead_scoring", "underwriter", "outlier_guard", "lead_scoring", "fast_track",
                             "buyer_matcher"],
    "PipelineSelfImprovementAgent": ["closing_alerts", "self_improvement"],
}
JOB_ALIASES = {"sheriff": "SheriffSaleMonitorAgent", "data_refresh": "DataRefreshAgent",
               "contacts": "ContactEnrichmentAgent", "underwrite": "CompUnderwriterAgent",
               "self_improvement": "PipelineSelfImprovementAgent"}
PIPELINE = ["SheriffSaleMonitorAgent", "DataRefreshAgent", "ContactEnrichmentAgent", "CompUnderwriterAgent",
            "PipelineSelfImprovementAgent"]


def run_agent(name: str, **options: Any) -> AgentRun | None:
    """Run one agent. Never raises: a crash is logged and the swarm moves on."""
    spec = REGISTRY[name]
    t0 = time.monotonic()
    try:
        agent = spec.factory(**{**spec.defaults, **options})
        if agent is None:
            log.info("agent %s unavailable (optional dependency/source missing); skipped", name)
            return None
        log.info("▶ %s", name)
        run = agent.execute()
    except Exception:  # noqa: BLE001
        log.exception("agent %s failed before it could record its run", name)
        return None
    log.info("■ %s: %s processed=%s new_leads=%s errors=%s (%.0fs)", name, run.status, run.records_processed,
             run.new_leads_found, run.error_count, time.monotonic() - t0)
    return run


def run_job(job: str, *, parallel: bool = True, skip: set[str] | None = None,
            overrides: dict[str, dict[str, Any]] | None = None) -> list[AgentRun]:
    job = JOB_ALIASES.get(job, job)
    names = [n for n in JOBS[job] if n not in (skip or set())]
    overrides = overrides or {}
    runs: list[AgentRun] = []
    scrape = [n for n in names if REGISTRY[n].phase == "scrape"]
    if parallel and len(scrape) > 1:
        # Scrapers hit different hosts; the shared per-host throttle keeps each source polite.
        with ThreadPoolExecutor(max_workers=len(scrape)) as pool:
            futures = {pool.submit(run_agent, n, **overrides.get(n, {})): n for n in scrape}
            for fut in as_completed(futures):
                run = fut.result()
                if run:
                    runs.append(run)
        names = [n for n in names if n not in scrape]
    for n in names:
        run = run_agent(n, **overrides.get(n, {}))
        if run:
            runs.append(run)
    return runs


def run_all(*, parallel: bool = True, skip: set[str] | None = None,
            overrides: dict[str, dict[str, Any]] | None = None) -> list[AgentRun]:
    init_db()
    runs: list[AgentRun] = []
    if parallel:
        # Sheriff monitor and data refresh are independent: run them side by side.
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(run_job, j, parallel=True, skip=skip, overrides=overrides)
                    for j in ("SheriffSaleMonitorAgent", "DataRefreshAgent")]
            for f in futs:
                runs.extend(f.result())
    else:
        for j in ("SheriffSaleMonitorAgent", "DataRefreshAgent"):
            runs.extend(run_job(j, parallel=False, skip=skip, overrides=overrides))
    for j in ("ContactEnrichmentAgent", "CompUnderwriterAgent", "PipelineSelfImprovementAgent"):
        runs.extend(run_job(j, parallel=False, skip=skip, overrides=overrides))
    return runs


def status(limit: int = 20) -> list[AgentRun]:
    init_db()
    with session_scope() as s:
        runs = s.scalars(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)).all()
        for r in runs:
            s.expunge(r)
    return runs


def daemon() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    cfg = settings()
    tz = cfg.operator.get("timezone", "America/New_York")
    sched = BlockingScheduler(timezone=tz)
    schedules = cfg.agents.get("schedules", {})
    for name, cron in schedules.items():
        if name not in REGISTRY:
            log.warning("schedule for unknown agent %s ignored", name)
            continue
        sched.add_job(run_agent, CronTrigger.from_crontab(cron, timezone=tz), args=[name], id=name,
                      max_instances=1, coalesce=True, misfire_grace_time=3600)
        log.info("scheduled %-18s %s", name, cron)
    log.info("swarm daemon started (Ctrl+C to stop)")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("swarm daemon stopped")


LIST_OPTIONS = {"counties", "towns", "zips", "municipalities"}


def _parse_opts(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        if key in LIST_OPTIONS or "," in value:
            out[key] = [v.strip() for v in value.split(",") if v.strip()]
        elif value.isdigit():
            out[key] = int(value)
        elif value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        else:
            out[key] = value
    return out


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_DIR / "swarm.log")]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt, handlers=handlers)
    for noisy in ("urllib3", "httpx", "homeharvest", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m swarm.orchestrator", description=__doc__.split("\n\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list agents and jobs")
    p_all = sub.add_parser("run-all", help="full pipeline once")
    p_all.add_argument("--sequential", action="store_true")
    p_all.add_argument("--skip", action="append", default=[])
    p_all.add_argument("--top", type=int, help="how many leads the underwriter values")
    p_run = sub.add_parser("run", help="run one agent")
    p_run.add_argument("agent", choices=sorted(REGISTRY))
    p_run.add_argument("--opt", action="append", help="agent option key=value (repeatable)")
    p_job = sub.add_parser("job", help="run one job")
    p_job.add_argument("job", choices=sorted(set(JOBS) | set(JOB_ALIASES)))
    sub.add_parser("daemon", help="run on cron schedules")
    sub.add_parser("status", help="recent agent runs")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if args.cmd == "list":
        for name, spec in REGISTRY.items():
            print(f"{name:18s} [{spec.phase}] {spec.description}")
        print()
        for job, names in JOBS.items():
            print(f"{job:30s} → {' → '.join(names)}")
    elif args.cmd == "run-all":
        overrides = {"underwriter": {"top": args.top}} if args.top else {}
        runs = run_all(parallel=not args.sequential, skip=set(args.skip), overrides=overrides)
        print(f"\npipeline finished: {len(runs)} agent runs")
        for r in runs:
            print(f"  {r.agent_name:18s} {r.status:8s} processed={r.records_processed:<6} new_leads={r.new_leads_found:<5} errors={r.error_count}")
    elif args.cmd == "run":
        run = run_agent(args.agent, **_parse_opts(args.opt))
        if run:
            print(f"{run.agent_name}: {run.status} processed={run.records_processed} new_leads={run.new_leads_found} errors={run.error_count}")
    elif args.cmd == "job":
        for r in run_job(args.job):
            print(f"  {r.agent_name:18s} {r.status:8s} processed={r.records_processed} errors={r.error_count}")
    elif args.cmd == "daemon":
        daemon()
    elif args.cmd == "status":
        for r in status():
            dur = (r.completed_at - r.started_at).total_seconds() if r.completed_at else None
            print(f"{r.started_at:%Y-%m-%d %H:%M} {r.agent_name:18s} {r.status:8s} processed={r.records_processed:<6} "
                  f"new={r.new_leads_found:<5} err={r.error_count:<3} {f'{dur:.0f}s' if dur else 'running'}")


if __name__ == "__main__":
    main()
