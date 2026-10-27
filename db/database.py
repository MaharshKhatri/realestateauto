"""Engine/session factory, schema creation and lightweight additive migrations.

SQLite by default (wholesaling.db at the repo root). Point WHOLESALING_DATABASE_URL at Postgres
(e.g. a free Supabase/Neon instance) to scale; the models are dialect-neutral.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import database_url
from db.models import Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None
_init_lock = threading.RLock()
_initialized_urls: set[str] = set()


def get_engine(url: str | None = None) -> Engine:
    global _engine, _SessionFactory
    if _engine is None or url is not None:
        url = url or database_url()
        kwargs = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 120}
        engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA busy_timeout=120000")
                cur.close()
        _engine = engine
        _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return _engine


def init_db(url: str | None = None) -> Engine:
    """Create tables and add any columns that were added to the models since the DB was created."""
    with _init_lock:  # agents in parallel threads all call init_db(); create tables once per process
        engine = get_engine(url)
        key = str(engine.url)
        if key not in _initialized_urls:
            Base.metadata.create_all(engine)
            _add_missing_columns(engine)
            _initialized_urls.add(key)
        return engine


def _add_missing_columns(engine: Engine) -> None:
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                coltype = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {coltype}'))
                log.info("migrated: added %s.%s", table.name, column.name)
            # Columns added by earlier migrations have NULL on old rows; give them the model's scalar default.
            for column in table.columns:
                default = column.default.arg if column.default is not None and column.default.is_scalar else None
                if default is None or column.primary_key:
                    continue
                conn.execute(text(f'UPDATE {table.name} SET "{column.name}" = :d WHERE "{column.name}" IS NULL'),
                             {"d": default.value if hasattr(default, "value") else default})


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    if _SessionFactory is None or url is not None:
        init_db(url)
    elif str(_engine.url) not in _initialized_urls:
        init_db()
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
