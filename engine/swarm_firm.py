"""The 10-stream research firm's orchestrator (2026-11-09, the operator: "route every listing, sheriff sale, deed and
tax record through 10 revenue streams at once").

Each desk run (`cloud_desk._work` -> `tick`) does three things inside a time budget:
1. seeds B2B targets once a day (config/b2b_targets.yaml: firms verified on their own pages, with source URLs);
2. runs the streams that are due (each declares `cadence_hours` and `run_hours`; the last run of each is kept in
   swarm_state `firm_last_run`), one transaction per stream, a crash recorded by name and never stopping the rest;
   every proposed e-mail goes to `firm_outbox.enqueue` (dedupe + stop lists + pitch gaps);
3. sends what the priority caps allow (`firm_outbox.send_due`).

`status()` is the executive table for `/firm-status`, the 📊 calendar event and the 9 AM / 1 PM / 6 PM chat report:
per stream what it looked at, produced, queued and sent, replies waiting and warm prospects, and monthly recurring
revenue counted only from targets actually in TRIAL / PAYING (no projected pipeline money).
"""
from __future__ import annotations

import hashlib

import importlib
import logging
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select

from core.config import ROOT, settings
from db.database import session_scope
from db.models import FirmOutbox, FirmReply, FirmStreamRun, FirmTarget, InboundReply, SwarmState, utcnow
from engine import firm_outbox as fo
from engine.streams.base import Stream, StreamContext

log = logging.getLogger(__name__)

STREAMS: dict[str, tuple[str, str]] = {
    "s1": ("engine.streams.s1_acquisitions", "Acquisitions (listing agents)"),
    "s2": ("engine.streams.s2_deal_desk_saas", "AI Deal Desk SaaS"),
    "s3": ("engine.streams.s3_surplus_overages", "Surplus research"),
    "s4": ("engine.streams.s4_contractor_leads", "Contractor rehab triggers"),
    "s5": ("engine.streams.s5_hard_money_feed", "Hard-money borrower feed"),
    "s6": ("engine.streams.s6_tax_appeals", "Tax-appeal evidence"),
    "s7": ("engine.streams.s7_mansion_tax_cliff", "Mansion-tax cliff net sheets"),
    "s8": ("engine.streams.s8_contract_fallout", "Contract fallout"),
    "s9": ("engine.streams.s9_cpa_1031_feed", "CPA cost-seg / 1031 feed"),
    "s10": ("engine.streams.s10_pre_mls_distress", "Pre-MLS distress"),
    "s11": ("engine.streams.s11_sheriff_bid_sheets", "Sheriff-sale bid sheets"),
}
STATE_KEY = "firm_last_run"
TARGETS_FILE = ROOT / "config" / "b2b_targets.yaml"


def cfg() -> dict[str, Any]:
    return dict(settings().get("research_firm") or {})


def stream_cfg(key: str) -> dict[str, Any]:
    return dict((cfg().get("streams") or {}).get(key) or {})


def load(key: str) -> Stream | None:
    mod_name = STREAMS[key][0]
    try:
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as exc:
        if exc.name == mod_name:
            return None                              # not built yet: listed as "not installed"
        raise
    return getattr(mod, "STREAM", None)


def _state(session: Any) -> dict[str, Any]:
    row = session.get(SwarmState, STATE_KEY)
    return dict(row.value_json or {}) if row else {}


def _set_state(session: Any, **values: Any) -> None:
    row = session.get(SwarmState, STATE_KEY)
    if row is None:
        row = SwarmState(key=STATE_KEY, value_json={})
        session.add(row)
    row.value_json = {**(row.value_json or {}), **values}


def due(stream: Stream, last: str | None, now: datetime) -> bool:
    if not stream_cfg(stream.key).get("enabled", True):
        return False
    a, b = stream.run_hours
    if not (a <= now.hour < b):
        return False
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(hours=float(stream.cadence_hours))
    except ValueError:
        return True


# -- targets ------------------------------------------------------------------------------------------------------
KIND_STREAM = {
    "foreclosure_surplus_attorney": "s3", "oil_tank_contractor": "s4", "roofer": "s4", "hvac_contractor": "s4",
    "rehab_general_contractor": "s4", "dumpster_rental": "s4", "hard_money_lender": "s5", "mortgage_broker": "s5",
    "property_tax_appeal_attorney": "s6", "cost_segregation_firm": "s9", "qualified_intermediary_1031": "s9",
    "real_estate_cpa": "s9", "investor_friendly_broker": "s2", "investor_agent": "s2", "flipper": "s2",
    "sheriff_bidder": "s11",                        # cash buyers who took title by a sheriff's deed
    "builder_developer": "s13",                     # the teardown digest (s13); seeded now, pitched once s13 runs
}


def targets_digest(path: Path | None = None) -> str:
    path = path or TARGETS_FILE
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else ""


def seed_targets(session: Any, path: Path | None = None) -> int:
    """config/b2b_targets.yaml -> firm_targets (idempotent; one row per e-mail; a row's stage is never reset)."""
    path = path or TARGETS_FILE
    if not path.exists():
        return 0
    data = yaml.safe_load(path.read_text()) or {}
    n = 0
    for t in data.get("targets") or []:
        kind = str(t.get("kind") or "").strip()
        if kind not in KIND_STREAM:
            continue
        row = fo.upsert_target(session, kind=kind, stream=KIND_STREAM[kind], email=t.get("email"), org=t.get("org"),
                               phone=t.get("phone"), website=t.get("website"), town=t.get("town"),
                               county=(t.get("county") or None), source=t.get("source"),
                               data={"note": t.get("note")} if t.get("note") else None)
        n += int(row is not None)
    return n


# -- running ------------------------------------------------------------------------------------------------------
def run_streams(*, now: datetime, out_dir: Path, only: list[str] | None = None, force: bool = False,
                send: bool = True, budget_seconds: float | None = None) -> dict[str, Any]:
    """Run the due streams (or `only`, or every stream with force) one by one inside the budget."""
    import os
    from engine import human_copy as hc
    budget = float(budget_seconds if budget_seconds is not None else cfg().get("budget_seconds", 150))
    t0 = time.monotonic()
    out: dict[str, Any] = {}
    with session_scope() as s:
        last = _state(s)
    op = hc.operator()
    for key in STREAMS:
        if only and key not in only:
            continue
        if time.monotonic() - t0 > budget:
            out.setdefault("skipped_budget", []).append(key)
            continue
        stream = load(key)
        if stream is None:
            out[key] = {"status": "not installed"}
            continue
        if not force and not due(stream, last.get(key), now):
            continue
        started = time.monotonic()
        ctx = StreamContext(now=now, out_dir=out_dir / key, operator=op, send=send, limits=stream_cfg(key))
        rec: dict[str, Any] = {}
        try:
            with session_scope() as s:
                res = stream.run(s, ctx)
                queued = 0
                for ob in res.outbound if send else []:
                    queued += int(fo.enqueue(s, ob, now) is not None)
                s.add(FirmStreamRun(stream=key, finished_at=utcnow(), processed=res.processed,
                                    found=res.found, products=res.products, queued=queued,
                                    metrics_json={k: v for k, v in (res.metrics or {}).items()
                                                  if isinstance(v, (int, float, str, bool))}))
                _set_state(s, **{key: now.isoformat(timespec="seconds")})
            rec = {"processed": res.processed, "found": res.found, "products": res.products, "queued": queued}
            if send and key != "s2":
                # paying / trial clients get this run's full report(s) once a week (s2 builds its own per-client
                # digests); a delivery never waits for the cold gap or caps
                with session_scope() as s:
                    rec["delivered"] = fo.deliver(s, key, STREAMS[key][1], ctx.out_dir, now)
            if send and res.internal:
                rec["internal_mailed"] = mail_internal(key, STREAMS[key][1], res.internal, now)
        except Exception as exc:                      # noqa: BLE001 - one stream's crash never stops the others
            log.exception("firm stream %s failed", key)
            with session_scope() as s:
                s.add(FirmStreamRun(stream=key, finished_at=utcnow(), error=type(exc).__name__))
                _set_state(s, **{key: now.isoformat(timespec="seconds")})
            rec = {"error": type(exc).__name__}
        rec["seconds"] = round(time.monotonic() - started, 1)
        out[key] = rec
        if os.environ.get("FIRM_DEBUG"):
            log.info("firm %s: %s", key, rec)
    return out


def mail_internal(key: str, label: str, files: list[Path], now: datetime, smtp_factory: Any = None) -> int:
    """A stream's operator-only reports (e.g. s10's pre-MLS queue) go to the operator's own inbox as
    "[njw-<key>] <date> <name>"; nothing is mailed when Gmail isn't set up. Returns the files sent."""
    import os
    paths = [Path(f) for f in files if Path(f).exists()]
    user, pw = os.environ.get("SMTP_USER", "").strip(), os.environ.get("SMTP_APP_PASSWORD", "")
    if not paths or not user or not pw:
        return 0
    from engine.cloud_desk import _send_self_files
    try:
        _send_self_files(user, pw, f"[njw-{key}] {now:%Y-%m-%d} {label}",
                         f"{label}: internal report from the research desk (for you only; not for forwarding).",
                         paths, smtp_factory or _SMTP_FACTORY)
    except Exception:                                   # noqa: BLE001 - a report never stops the run
        log.exception("firm: internal report for %s not mailed", key)
        return 0
    return len(paths)


_SMTP_FACTORY: Any = None


def tick(*, now: datetime | None = None, smtp_factory: Any = None, send: bool = True, only: list[str] | None = None,
         force: bool = False, budget_seconds: float | None = None) -> dict[str, Any]:
    """One desk run's share of the firm: seed targets (daily), run due streams, send what the caps allow."""
    from engine.gmail_autopilot import now_eastern
    t = now_eastern(now).replace(tzinfo=None)
    out: dict[str, Any] = {}
    with session_scope() as s:
        st = _state(s)
        # re-seeded daily and whenever the target file changes; never marked done while the file is missing (2026-11-09:
        # the first live run came before config/b2b_targets.yaml was committed, marked the day seeded, and every
        # specialty stream then had nobody to pitch until the next day)
        digest = targets_digest()
        if digest and (st.get("seeded") != t.date().isoformat() or st.get("seeded_hash") != digest):
            out["targets_seeded"] = seed_targets(s)
            _set_state(s, seeded=t.date().isoformat(), seeded_hash=digest)
    global _SMTP_FACTORY
    _SMTP_FACTORY = smtp_factory
    with tempfile.TemporaryDirectory() as tmp:
        out["streams"] = run_streams(now=t, out_dir=Path(tmp), only=only, force=force, send=send,
                                     budget_seconds=budget_seconds)
    if send:
        with session_scope() as s:
            out["nudges_queued"] = fo.queue_nudges(s, now)   # the in-thread rest of each cold sequence
        out["outbox"] = fo.send_due(now=now, smtp_factory=smtp_factory)
    return out


# -- reporting ----------------------------------------------------------------------------------------------------
def _price(key: str) -> tuple[float, float]:
    c = stream_cfg(key)
    return float(c.get("price_trial", 0) or 0), float(c.get("price_monthly", 0) or 0)


def status(session: Any, now: datetime | None = None) -> dict[str, Any]:
    """Per stream: last run counts, 24-hour sends, replies waiting, warm prospects, targets by stage, MRR."""
    since = utcnow() - timedelta(hours=24)
    rows: list[dict[str, Any]] = []
    total_mrr = 0.0
    for key, (_, label) in STREAMS.items():
        last = session.scalar(select(FirmStreamRun).where(FirmStreamRun.stream == key)
                              .order_by(FirmStreamRun.id.desc()).limit(1))
        sent = int(session.scalar(select(func.count(FirmOutbox.id)).where(
            FirmOutbox.stream == key, FirmOutbox.status == "sent", FirmOutbox.sent_at >= since)) or 0)
        queued = int(session.scalar(select(func.count(FirmOutbox.id)).where(
            FirmOutbox.stream == key, FirmOutbox.status == "queued")) or 0)
        waiting = int(session.scalar(select(func.count(FirmReply.id)).where(
            FirmReply.stream == key, FirmReply.status == "new")) or 0)
        warm = int(session.scalar(select(func.count(FirmReply.id)).where(
            FirmReply.stream == key, FirmReply.status == "new", FirmReply.intent == "interested")) or 0)
        stages = dict(session.execute(select(FirmTarget.stage, func.count(FirmTarget.id))
                                      .where(FirmTarget.stream == key).group_by(FirmTarget.stage)).all())
        trial_fee, monthly = _price(key)
        mrr = stages.get("TRIAL", 0) * (trial_fee or monthly) + stages.get("PAYING", 0) * monthly
        total_mrr += mrr
        if key == "s1":
            waiting = int(session.scalar(select(func.count(InboundReply.id)).where(
                InboundReply.status == "needs_approval")) or 0)
        rows.append({"key": key, "name": label, "installed": load(key) is not None,
                     "last_run": last.started_at.isoformat(timespec="minutes") if last else None,
                     "error": last.error if last else None,
                     "processed": last.processed if last else 0, "found": last.found if last else 0,
                     "products": last.products if last else 0, "queued": queued, "sent_24h": sent,
                     "waiting": waiting, "warm": warm, "targets": sum(stages.values()), "stages": stages,
                     "mrr": mrr})
    from engine import cold_guard
    caps = fo.caps()
    ceiling = caps[fo.PRIORITY_PITCH] + caps[fo.PRIORITY_SPECIALTY]
    return {"streams": rows, "mrr": total_mrr, "sent_24h": sum(r["sent_24h"] for r in rows),
            "waiting": sum(r["waiting"] for r in rows), "warm": sum(r["warm"] for r in rows),
            "cold_paused": cold_guard.paused(session, now), "quiet_inbox": cold_guard.quiet_inbox(session, now),
            "first_touch_cap": cold_guard.first_touch_cap(session, ceiling)}


def flat(st: dict[str, Any]) -> dict[str, Any]:
    """Counts only, for the public run log: {"s3_found": 12, ...}."""
    out: dict[str, Any] = {"firm_mrr": int(st["mrr"]), "firm_sent_24h": st["sent_24h"],
                           "firm_waiting": st["waiting"], "firm_warm": st["warm"]}
    if st.get("first_touch_cap") is not None:
        out["firm_touch_cap"] = st["first_touch_cap"]
    if st.get("cold_paused"):
        out["cold_paused"] = 1                           # the reason stays off the public log (it may name a count)
    if st.get("quiet_inbox"):
        out["quiet_inbox"] = 1
    for r in st["streams"]:
        k = r["key"]
        out.update({f"{k}_found": r["found"], f"{k}_products": r["products"], f"{k}_sent": r["sent_24h"],
                    f"{k}_queued": r["queued"]})
        if r["waiting"]:
            out[f"{k}_waiting"] = r["waiting"]           # s1: agent replies waiting; others: B2B replies
        if r["error"]:
            out[f"{k}_error"] = r["error"]
        if not r["installed"]:
            out[f"{k}_missing"] = 1
    return out


def table_lines(st: dict[str, Any]) -> list[str]:
    head = f"{'stream':<32} {'looked':>7} {'found':>6} {'pdfs':>5} {'queued':>6} {'sent':>5} {'wait':>5} {'warm':>5}"
    lines = [head, "-" * len(head)]
    for r in st["streams"]:
        name = f"{r['key']} {r['name']}" + ("" if r["installed"] else " (not installed)")
        lines.append(f"{name[:32]:<32} {r['processed']:>7} {r['found']:>6} {r['products']:>5} {r['queued']:>6} "
                     f"{r['sent_24h']:>5} {r['waiting']:>5} {r['warm']:>5}")
    lines.append(f"Recurring revenue from trials / paying clients: ${st['mrr']:,.0f}/month")
    if st.get("first_touch_cap") is not None:
        lines.append(f"B2B first touches allowed today: {st['first_touch_cap']} (weekly ramp; nudges separate)")
    if st.get("cold_paused"):
        lines.append(f"COLD E-MAIL PAUSED: {st['cold_paused']}. Replies still go out.")
    if st.get("quiet_inbox"):
        lines.append(f"CHECK: {st['quiet_inbox']}")
    return lines
