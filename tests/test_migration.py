"""Additive schema migration.

`create_all` creates missing tables and never alters existing ones, so shipping
a new column used to turn every query against a populated database into
"no such column". The accumulated history is the whole point of the screener,
so recreating the database is not an acceptable upgrade path.
"""

from __future__ import annotations

import sqlite3

import pytest


def _legacy_database(path, drop_columns, drop_tables):
    """Build a database as it existed before the new columns were added."""
    import app.db as dbm
    import app.models as m
    from app.config import reload_settings

    reload_settings()
    dbm.reset_engine()

    table = m.TokenSnapshot.__table__
    removed = [table.c[c] for c in drop_columns]
    for col in removed:
        table._columns.remove(col)
    dropped = []
    for name in drop_tables:
        tbl = m.Base.metadata.tables[name]
        m.Base.metadata.remove(tbl)
        dropped.append(tbl)

    dbm.init_db()

    def restore():
        for col in removed:
            table.append_column(col)
        for tbl in dropped:
            m.Base.metadata._add_table(tbl.name, tbl.schema, tbl)
        dbm.reset_engine()

    return restore


def test_new_columns_and_tables_arrive_without_losing_data(tmp_path, monkeypatch):
    import app.db as dbm
    from app.config import reload_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/legacy.db")
    new_cols = ["buys_24h", "holder_acceleration_pp", "buy_count_acceleration_pp"]
    restore = _legacy_database(tmp_path, new_cols, ["snapshot_outcome"])
    try:
        db = f"{tmp_path}/legacy.db"
        con = sqlite3.connect(db)
        required = [r[1] for r in con.execute("PRAGMA table_info(token)")
                    if r[3] and r[4] is None and r[1] != "id"]
        vals = {"chain": "robinhood", "address": "0xaa", "symbol": "OLD"}
        for r in required:
            vals.setdefault(r, "2026-08-01 00:00:00")
        con.execute(f"INSERT INTO token ({','.join(vals)}) "
                    f"VALUES ({','.join('?' * len(vals))})", list(vals.values()))
        con.execute("INSERT INTO token_snapshot (token_id, captured_at, price_usd) "
                    "VALUES (1, '2026-08-01 00:00:00', 1.5)")
        con.commit()
        cols = [r[1] for r in con.execute("PRAGMA table_info(token_snapshot)")]
        assert not any(c in cols for c in new_cols), "fixture did not build a legacy schema"
        con.close()

        restore()          # ship the new model...
        dbm.init_db()      # ...and start the app exactly as a launcher does

        con = sqlite3.connect(db)
        cols = [r[1] for r in con.execute("PRAGMA table_info(token_snapshot)")]
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        assert all(c in cols for c in new_cols), "columns were not added"
        assert "snapshot_outcome" in tables, "new table was not created"
        assert con.execute("SELECT price_usd FROM token_snapshot").fetchone()[0] == 1.5
        con.close()
    finally:
        try:
            restore()
        except Exception:
            pass
        dbm.reset_engine()
        reload_settings()


def test_migration_is_idempotent(tmp_path, monkeypatch):
    import app.db as dbm
    from app.config import reload_settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/idem.db")
    reload_settings()
    dbm.reset_engine()
    dbm.init_db()
    assert dbm._add_missing_columns(dbm.get_engine()) == []
    assert dbm._add_missing_columns(dbm.get_engine()) == []
    dbm.reset_engine()
    reload_settings()


def test_migration_only_touches_sqlite(monkeypatch):
    """Postgres deployments get a real migration tool, not ALTER guesswork."""
    import app.db as dbm

    class FakeURL:
        def get_backend_name(self):
            return "postgresql"

    class FakeEngine:
        url = FakeURL()

    assert dbm._add_missing_columns(FakeEngine()) == []
