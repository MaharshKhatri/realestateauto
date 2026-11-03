"""The Mac runs the desk by itself (learned 2026-11-03: the operator wants to handle calls only).

The autopilot daemon (manage.py autopilot-daemon, launchd) already reads replies every 5 minutes and sends the safe
answers. This module adds the two things that still needed a person at the keyboard:

* self_update(): fixes pushed from a cloud Claude session reach the Mac with no `git pull`. Every half hour the
  daemon fetches its branch; when there are new commits it rebases onto them (local commits are kept), reinstalls
  when pyproject.toml changed, and restarts itself on the new code. Uncommitted edits are parked in a named stash,
  never merged half-way into running code. Any conflict leaves the Mac on the code it had.
* run_agent(): a headless Claude Code run (`claude -p`, the operator's own sign-in) with the desk-agent prompt
  (.claude/commands/autopilot.md) while replies wait on the operator. It may run only manage.py inbox / gmail / lead
  / script (no file edits, no other shell). The price rules are enforced in code too: approve() keeps the drafted
  number, and `gmail --reply --price` refuses anything above the walk-away.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from db.models import SwarmState

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
AGENT_KEY = "desk_agent"
PROMPT_FILE = ROOT / ".claude" / "commands" / "autopilot.md"
# What the headless agent may run. Anything else is refused by Claude Code without asking (print mode).
AGENT_TOOLS = [
    "Bash(.venv/bin/python manage.py inbox:*)",
    "Bash(.venv/bin/python manage.py gmail:*)",
    "Bash(.venv/bin/python manage.py lead:*)",
    "Bash(.venv/bin/python manage.py script:*)",
    "Read",
]
CLAUDE_PLACES = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")


def _git(root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)


def self_update(root: Path = ROOT, *, pip: bool = True) -> dict[str, Any]:
    """Bring the checkout up to its remote branch. {"updated": bool, "commits": n, "reason": ...}."""
    out: dict[str, Any] = {"updated": False, "commits": 0, "reason": ""}
    try:
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not branch or branch == "HEAD":
            out["reason"] = "not on a branch"
            return out
        if _git(root, "fetch", "-q", "origin", branch).returncode:
            out["reason"] = "fetch failed (offline?)"
            return out
        behind = int(_git(root, "rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip() or 0)
        if not behind:
            out["reason"] = "up to date"
            return out
        before = _git(root, "rev-parse", "HEAD").stdout.strip()
        if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            _git(root, "stash", "push", "-m", f"autopilot self-update {stamp}: local edits parked")
            out["stashed"] = True
        r = _git(root, "pull", "--rebase", "-q", "origin", branch)
        if r.returncode:
            _git(root, "rebase", "--abort")
            out["reason"] = "conflict with local commits: kept the current code (" + (r.stderr or r.stdout)[-200:] + ")"
            return out
        changed = _git(root, "diff", "--name-only", before, "HEAD").stdout.split()
        if pip and "pyproject.toml" in changed:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", str(root)], cwd=root, timeout=600)
        out.update(updated=True, commits=behind, changed=changed)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        out["reason"] = f"{type(exc).__name__}: {exc}"
    return out


def find_claude(explicit: str | None = None) -> str | None:
    """The Claude Code CLI: the path install-daemon recorded, else PATH, else the usual install places."""
    for c in (explicit, shutil.which("claude"), *(os.path.expanduser(p) for p in CLAUDE_PLACES)):
        if c and Path(c).exists() and os.access(c, os.X_OK):
            return str(c)
    return None


def _state(session: Session) -> dict[str, Any]:
    row = session.get(SwarmState, AGENT_KEY)
    return dict(row.value_json or {}) if row else {}


def _save(session: Session, **values: Any) -> None:
    row = session.get(SwarmState, AGENT_KEY)
    if row is None:
        row = SwarmState(key=AGENT_KEY, value_json={})
        session.add(row)
    row.value_json = {**(row.value_json or {}), **values}
    session.flush()


def agent_due(session: Session, now: datetime, *, waiting: int, every_minutes: int = 90,
              hours: tuple[int, int] = (9, 19)) -> bool:
    """Run the agent when replies wait on the operator, inside working hours, at most every `every_minutes`."""
    if waiting <= 0 or every_minutes <= 0 or not hours[0] <= now.hour < hours[1]:
        return False
    last = _state(session).get("last_run")
    return not last or datetime.fromisoformat(last) <= now - timedelta(minutes=every_minutes)


def agent_prompt(path: Path = PROMPT_FILE) -> str:
    """The desk-agent instructions without the slash-command header (print mode gets the text itself)."""
    text = path.read_text()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def run_agent(session: Session, claude: str, now: datetime, *, root: Path = ROOT, timeout: int = 900) -> dict[str, Any]:
    """One unattended desk-agent run; its report goes to logs/agent.log."""
    _save(session, last_run=now.isoformat(timespec="seconds"))
    session.commit()                                          # no lock held while the agent works
    cmd = [claude, "-p", agent_prompt(), "--allowedTools", *AGENT_TOOLS]
    log_path = root / "logs" / "agent.log"
    log_path.parent.mkdir(exist_ok=True)
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
        report, ok = (r.stdout or "").strip(), r.returncode == 0
        err = (r.stderr or "").strip()[-500:]
    except subprocess.TimeoutExpired:
        report, ok, err = "", False, f"timed out after {timeout} s"
    except OSError as exc:
        report, ok, err = "", False, str(exc)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n=== {now:%Y-%m-%d %H:%M} desk agent ({'ok' if ok else 'failed'}) ===\n{report}\n")
        if err and not ok:
            f.write(f"[error] {err}\n")
    _save(session, last_ok=ok, last_report=report[-1500:], last_error=None if ok else err)
    session.commit()
    return {"ok": ok, "report": report, "error": None if ok else err}
