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
