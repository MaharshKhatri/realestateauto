"""Test configuration: every test gets an isolated SQLite database."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = Path(__file__).parent / "fixtures"
# No test may reach a real Google Calendar, even on a laptop where setup-calendar has run: the OAuth files point
# at paths that never exist (tests/test_gcal.py swaps in its own token and a fake Google service).
os.environ["WHOLESALING_GCAL_TOKEN"] = str(ROOT / "tests" / ".no-calendar-token.json")
os.environ["WHOLESALING_GCAL_CREDENTIALS"] = str(ROOT / "tests" / ".no-calendar-credentials.json")
# ... and a Mac that handed the desk to the cloud (data/runtime_cloud.json) still runs the calendar tests.
os.environ["WHOLESALING_HANDOFF_FLAG"] = str(ROOT / "tests" / ".no-handoff-flag.json")
# ... and no test may open the operator's Gmail: IMAP goes to a closed local port (tests/test_gmail_autopilot.py
# serves its messages from a fake connection instead).
os.environ["WHOLESALING_IMAP_HOST"] = "127.0.0.1"
os.environ["WHOLESALING_IMAP_PORT"] = "9"


@pytest.fixture(autouse=True)
def _classic_offer_gates(monkeypatch, request):
    """The shared fixtures (a $360K listing with a $285K walk-away and a $228.8K anchor) were written for the 75%
    gate and the plain 12% anchor. Production moved to 80% / a 75%-of-ask opening floor on 2026-11-03; those rules
    have their own tests (marked `production_gates`), which skip this pin."""
    if request.node.get_closest_marker("production_gates"):
        return
    from core.config import settings
    monkeypatch.setitem(settings()["deal"], "striking_distance_pct", 0.75)
    monkeypatch.setitem(settings()["outreach"], "min_list_to_mao_75", 0.75)
    monkeypatch.setitem(settings()["outreach"], "min_open_pct_of_ask", 0)
    monkeypatch.setitem(settings()["outreach"], "ask_first_min_reach", 0.75)
    monkeypatch.setitem(settings(), "firm", {**dict(settings().get("firm") or {}), "enforce": False})
    # the shared fixtures give every listing the same agent e-mail: the one-new-e-mail-per-agent rule has its own test
    monkeypatch.setitem(settings(), "gmail_autopilot", {**dict(settings().get("gmail_autopilot") or {}),
                                                         "agent_cold_gap_hours": 0})


@pytest.fixture(autouse=True)
def _pre_ceo_engine(monkeypatch):
    """The deck, outbox and stream tests were written before the 2026-11-14 CEO re-architecture (clone-only cold
    openers, frozen subscription streams, the 14-a-day partner ramp, automatic partner notes); those have their own
    tests, which set the live values themselves."""
    from core.config import settings
    monkeypatch.setitem(settings(), "clone", {**(settings().get("clone") or {}), "enabled": False})
    monkeypatch.setitem(settings(), "partner_auto", {**(settings().get("partner_auto") or {}), "enabled": False})
    monkeypatch.setitem(settings(), "research_firm", {**settings()["research_firm"], "frozen_streams": [],
                                                      "ramp": [30]})


@pytest.fixture(autouse=True)
def _full_calendar(monkeypatch):
    """The calendar tests were written for the full event set; the 2026-11-09 clean "firm" mode has its own tests."""
    from core.config import settings
    monkeypatch.setitem(settings()["calendar"], "mode", "")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A fresh database; yields a session."""
    monkeypatch.setenv("WHOLESALING_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    import db.database as database
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_SessionFactory", None)
    database.init_db()
    with database.session_scope() as session:
        yield session


@pytest.fixture(autouse=True)
def _not_on_github(monkeypatch):
    """CI runs on GitHub Actions, where the cloud desk's behavior switches on (GITHUB_ACTIONS: no dashboard links,
    'by the cloud desk'). Tests run the same everywhere; the cloud-desk tests set it themselves."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture(autouse=True)
def _no_real_waits(monkeypatch):
    """SMTP retries and the gap between offers in one inbox run wait in real seconds on the laptop; tests don't."""
    import engine.agent_outreach as ao
    import engine.gmail_autopilot as gm
    monkeypatch.setattr(ao, "_sleep", lambda s: None)
    monkeypatch.setattr(gm, "_sleep", lambda s: None)
