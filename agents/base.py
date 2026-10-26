"""BaseAgent: run bookkeeping (agent_runs), checkpoints, and fault isolation."""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from core.http import PoliteSession
from db.database import init_db, session_scope
from db.models import AgentRun, utcnow

log = logging.getLogger(__name__)


@dataclass
class AgentContext:
    session: Session
    http: PoliteSession
    options: dict[str, Any] = field(default_factory=dict)
    records_processed: int = 0
    new_leads_found: int = 0
    records_updated: int = 0
    errors: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        log.warning(message)
        self.errors.append(message[:500])

    def note(self, message: str) -> None:
        log.info(message)
        self.notes.append(message)

    def bump(self, metric: str, n: int = 1) -> None:
        self.metrics[metric] = self.metrics.get(metric, 0) + n

    def checkpoint(self) -> None:
        """Commit work so far; a crash later in the run keeps what was already collected."""
        self.session.commit()


class BaseAgent:
    name: str = "base"
    description: str = ""
    # Error ratio above which a run is marked 'partial' rather than 'success'.
    partial_threshold: float = 0.0

    def __init__(self, **options: Any):
        self.options = options

    def run(self, ctx: AgentContext) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def execute(self) -> AgentRun:
        """Run the agent with full bookkeeping. Never raises; failures are recorded on the run."""
        init_db()
        with session_scope() as s:
            run = AgentRun(agent_name=self.name, status="running", started_at=utcnow())
            s.add(run)
            s.flush()
            run_id = run.id

        http = PoliteSession()
        started = time.monotonic()
        status = "success"
        crash: str | None = None
        ctx: AgentContext | None = None
        try:
            with session_scope() as session:
                ctx = AgentContext(session=session, http=http, options=self.options)
                self.run(ctx)
        except Exception as exc:  # noqa: BLE001 - one agent must never kill the swarm
            status = "failed"
            crash = f"{type(exc).__name__}: {exc}"
            log.error("agent %s crashed: %s\n%s", self.name, crash, traceback.format_exc())

        with session_scope() as s:
            run = s.get(AgentRun, run_id)
            run.completed_at = utcnow()
            if ctx is not None:
                run.records_processed = ctx.records_processed
                run.new_leads_found = ctx.new_leads_found
                run.records_updated = ctx.records_updated
                run.errors = ctx.errors + ([crash] if crash else [])
                run.error_count = len(run.errors)
                run.metrics_json = {**ctx.metrics, "http": http.stats_summary(),
                                    "duration_s": round(time.monotonic() - started, 1)}
                run.self_improvement_notes = "\n".join(ctx.notes) or None
                if status == "success" and ctx.errors:
                    status = "partial"
                if status == "failed" and ctx.records_processed:
                    status = "partial"
            else:
                run.errors = [crash] if crash else []
                run.error_count = len(run.errors)
            run.status = status
            s.flush()
            s.expunge(run)
        return run
