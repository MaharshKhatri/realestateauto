"""One-click swarm refresh from the dashboard: run the pipeline in a background thread and report live progress.

Two modes:
  quick  offline re-analysis of what is already in wholesaling.db (stack, score, guard valuations, fast track,
         buyer matches, closing alerts). A few minutes on ~8,000 leads (the stacker dominates); no scraping.
  full   the daily pipeline: today's sheriff sales and listings + lis pendens imports, owner/contact enrichment,
         then the quick steps with live underwriting and the self-improvement audit. 10-30 minutes; every source
         keeps its own polite rate limit.

The weekly heavy harvests (MOD-IV absentee owners, mailing-cluster cash buyers, tax-sale pages) stay on the cron
daemon (`python -m swarm.orchestrator daemon`). Only one refresh runs at a time; each agent still writes its own
agent_runs row, so `python -m swarm.orchestrator status` shows the same history.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable

QUICK = ["signal_stacker", "lead_scoring", "outlier_guard", "lead_scoring", "fast_track", "buyer_matcher",
         "closing_alerts", "calendar_sync"]
FULL = ["sheriff_sales", "stale_listings", "lis_pendens", "owner_enrichment", "municipal_owners", "listing_agents",
        "owner_resolver", "signal_stacker", "lead_scoring", "underwriter", "outlier_guard", "lead_scoring",
        "fast_track", "buyer_matcher", "closing_alerts", "calendar_sync", "self_improvement"]
MODES = {"quick": QUICK, "full": FULL}
QUICK_OPTIONS = {"outlier_guard": {"offline": True}}

_lock = threading.Lock()
_state: dict[str, Any] = {"running": False, "mode": None, "started_at": None, "finished_at": None, "current": None,
                          "steps": [], "error": None}


def status() -> dict[str, Any]:
    with _lock:
        return {**_state, "steps": [dict(s) for s in _state["steps"]]}


def _set(**kw: Any) -> None:
    with _lock:
        _state.update(kw)


def _step(i: int, **kw: Any) -> None:
    with _lock:
        _state["steps"][i].update(kw)


def _work(mode: str, steps: list[str], runner: Callable[..., Any]) -> None:
    from swarm.orchestrator import REGISTRY
    try:
        for i, name in enumerate(steps):
            _set(current=name)
            if name not in REGISTRY:
                _step(i, status="skipped", note="agent not installed")
                continue
            _step(i, status="running", started_at=datetime.now().isoformat(timespec="seconds"))
            t0 = time.monotonic()
            opts = QUICK_OPTIONS.get(name, {}) if mode == "quick" else {}
            run = runner(name, **opts)
            _step(i, status=(getattr(run, "status", None) or "skipped"), seconds=round(time.monotonic() - t0, 1),
                  records=getattr(run, "records_processed", None), new_leads=getattr(run, "new_leads_found", None),
                  errors=getattr(run, "error_count", None))
    except Exception as exc:  # noqa: BLE001  — report, never crash the web server
        _set(error=f"{type(exc).__name__}: {exc}")
    finally:
        _set(running=False, current=None, finished_at=datetime.now().isoformat(timespec="seconds"))


def trigger(mode: str = "quick", *, runner: Callable[..., Any] | None = None, wait: bool = False) -> dict[str, Any]:
    """Start a refresh. Returns the new status, or raises RuntimeError if one is already running."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if runner is None:
        from swarm.orchestrator import run_agent as runner
    steps = MODES[mode]
    with _lock:
        if _state["running"]:
            raise RuntimeError(f"a {_state['mode']} refresh is already running ({_state['current'] or 'starting'})")
        _state.update(running=True, mode=mode, started_at=datetime.now().isoformat(timespec="seconds"),
                      finished_at=None, current=None, error=None,
                      steps=[{"agent": n, "status": "pending"} for n in steps])
    from db.database import init_db
    init_db()
    t = threading.Thread(target=_work, args=(mode, steps, runner), name=f"swarm-{mode}", daemon=True)
    t.start()
    if wait:
        t.join()
    return status()
