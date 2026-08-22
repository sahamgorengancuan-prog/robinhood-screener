"""SQLite engine + session management.

SQLite is the right call for the MVP: one writer (the scheduler), a handful of
readers (FastAPI), tens of thousands of rows per day. WAL mode keeps the API
readable while ingestion writes. Swap `DATABASE_URL` to Postgres when either
(a) you need concurrent writers, or (b) the snapshot table passes ~10M rows.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base

log = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _sqlite_path(url: str) -> str | None:
    if not url.startswith("sqlite"):
        return None
    return url.split("///")[-1]


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    url = get_settings().database_url
    path = _sqlite_path(url)
    if path and path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    _engine = create_engine(url, future=True, connect_args=connect_args, pool_pre_ping=True)

    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _set_pragmas(dbapi_conn, _record):  # pragma: no cover - driver level
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    return _engine


def _add_missing_columns(engine: Engine) -> list[str]:
    """Additive schema migration for SQLite.

    `create_all` creates missing *tables* but never alters existing ones, so a
    new column silently turns every query against an already-populated database
    into "no such column". The screener's whole value is the history it has
    accumulated, so dropping and recreating is not an option.

    Only nullable, default-less columns are added — that is what ALTER TABLE ADD
    COLUMN can do safely on SQLite without rewriting the table. Anything else
    (renames, type changes, NOT NULL) needs a real migration and is deliberately
    not attempted here; it would fail loudly rather than corrupt data.
    """
    added: list[str] = []
    if not engine.url.get_backend_name().startswith("sqlite"):
        return added  # Postgres deployments get a real migration tool.

    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import CreateColumn

    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all just made it, or will
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have or not column.nullable or column.primary_key:
                    continue
                ddl = CreateColumn(column).compile(engine).string
                conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {ddl}")
                added.append(f"{table.name}.{column.name}")

    if added:
        log.info("schema migration added %d column(s): %s", len(added), ", ".join(added))
    return added


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    get_engine()
    assert _SessionLocal is not None
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s


def reset_engine() -> None:
    """Test helper — drops cached engine so a new DATABASE_URL takes effect."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
