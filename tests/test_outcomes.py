"""Forward-return labelling.

The label half of the dataset is the only part whose cost is calendar time, so
its correctness has to be pinned before months of rows accumulate under a bug.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.pipeline.outcomes import (
    HORIZONS,
    RUG_LIQUIDITY_DRAWDOWN_PCT,
    Window,
    measure,
    pending_horizons,
)


class FakeSnap:
    def __init__(self, minutes, price=None, liquidity=None, sid=0):
        self.id = sid
        self.captured_at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=minutes)
        self.price_usd = price
        self.liquidity_usd = liquidity
        self.token_id = 1


def win(base, following, complete=True):
    return Window(base=base, horizon="1h", following=following, complete=complete)


# ------------------------------------------------------------------- measure
def test_peak_and_trough_are_measured_against_the_base_price():
    base = FakeSnap(0, price=1.0)
    row = measure(win(base, [FakeSnap(15, 1.5), FakeSnap(30, 3.0), FakeSnap(45, 0.8)]))
    assert row["max_return_pct"] == pytest.approx(200.0)
    assert row["min_return_pct"] == pytest.approx(-20.0)
    assert row["end_return_pct"] == pytest.approx(-20.0)


def test_multiple_thresholds_are_flagged_from_the_peak():
    base = FakeSnap(0, price=1.0)
    row = measure(win(base, [FakeSnap(10, 5.5)]))
    assert row["reached_2x"] and row["reached_5x"]
    assert row["reached_10x"] is False


def test_time_to_2x_uses_the_first_crossing_not_the_peak():
    base = FakeSnap(0, price=1.0)
    row = measure(win(base, [FakeSnap(10, 2.1), FakeSnap(50, 9.0)]))
    assert row["seconds_to_2x"] == 600


def test_a_token_that_never_doubles_has_no_time_to_2x():
    base = FakeSnap(0, price=1.0)
    row = measure(win(base, [FakeSnap(10, 1.4)]))
    assert row["seconds_to_2x"] is None
    assert row["reached_2x"] is False


def test_missing_base_price_leaves_returns_null_rather_than_zero():
    """A flat 0% is a claim. Null is the truth."""
    row = measure(win(FakeSnap(0, price=None), [FakeSnap(10, 2.0)]))
    assert row["max_return_pct"] is None
    assert row["reached_2x"] is None


def test_no_following_snapshots_leaves_returns_null():
    row = measure(win(FakeSnap(0, price=1.0), []))
    assert row["max_return_pct"] is None
    assert row["observations"] == 0


def test_zero_and_negative_prices_are_not_treated_as_observations():
    base = FakeSnap(0, price=1.0)
    row = measure(win(base, [FakeSnap(10, 0.0), FakeSnap(20, 2.0)]))
    assert row["observations"] == 1
    assert row["max_return_pct"] == pytest.approx(100.0)


def test_observations_are_recorded_so_thin_labels_can_be_discounted():
    base = FakeSnap(0, price=1.0)
    row = measure(win(base, [FakeSnap(i, 1.0 + i / 100) for i in range(1, 13)]))
    assert row["observations"] == 12


# ---------------------------------------------------------------- rug signal
def test_a_drained_pool_is_flagged_even_when_the_price_rose():
    """Price up while the pool empties is not a gain; it is an exit that is no
    longer available."""
    base = FakeSnap(0, price=1.0, liquidity=50_000.0)
    row = measure(win(base, [FakeSnap(10, 4.0, liquidity=500.0)]))
    assert row["max_return_pct"] == pytest.approx(300.0)
    assert row["liquidity_drawdown_pct"] == pytest.approx(99.0)
    assert row["rug_suspected"] is True


def test_a_healthy_pool_is_not_flagged():
    base = FakeSnap(0, price=1.0, liquidity=50_000.0)
    row = measure(win(base, [FakeSnap(10, 1.2, liquidity=60_000.0)]))
    assert row["rug_suspected"] is False
    assert row["liquidity_drawdown_pct"] <= 0


def test_rug_uses_the_worst_point_not_the_final_one():
    """A pool pulled and partly refilled still had a window with no exit."""
    base = FakeSnap(0, price=1.0, liquidity=50_000.0)
    row = measure(win(base, [FakeSnap(10, 1.0, liquidity=100.0),
                             FakeSnap(20, 1.0, liquidity=49_000.0)]))
    assert row["rug_suspected"] is True


def test_missing_liquidity_leaves_the_rug_flag_null():
    base = FakeSnap(0, price=1.0, liquidity=None)
    row = measure(win(base, [FakeSnap(10, 1.2, liquidity=1_000.0)]))
    assert row["rug_suspected"] is None


def test_rug_threshold_is_the_documented_constant():
    base = FakeSnap(0, price=1.0, liquidity=100.0)
    just_under = measure(win(base, [FakeSnap(10, 1.0, liquidity=100.0 * (1 - (RUG_LIQUIDITY_DRAWDOWN_PCT - 1) / 100))]))
    assert just_under["rug_suspected"] is False


# ------------------------------------------------------------------ horizons
def test_a_window_is_only_labelled_once_it_has_closed():
    """Labelling early writes a row that silently means "so far"."""
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    assert pending_horizons(base, base + dt.timedelta(minutes=59), set()) == []
    assert "1h" in pending_horizons(base, base + dt.timedelta(minutes=61), set())


def test_already_labelled_horizons_are_not_repeated():
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    later = base + dt.timedelta(days=30)
    assert pending_horizons(base, later, set(HORIZONS)) == []


def test_all_horizons_open_once_enough_time_has_passed():
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    later = base + dt.timedelta(days=30)
    assert set(pending_horizons(base, later, set())) == set(HORIZONS)


def test_naive_timestamps_are_handled():
    """SQLite hands back naive datetimes; comparing them to aware ones raises."""
    naive = dt.datetime(2026, 1, 1)
    now = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
    assert "1h" in pending_horizons(naive, now, set())


# -------------------------------------------------------------- integration
def test_labelling_covers_rejected_tokens_too(tmp_path, monkeypatch):
    """A dataset of only the tokens the screener liked teaches a model to agree
    with the screener. The rejects are the negative evidence."""
    import app.db as db
    from app.config import reload_settings
    from app.models import SnapshotOutcome, Token, TokenSnapshot
    from app.pipeline.outcomes import label_snapshots

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    reload_settings()
    db.reset_engine()
    db.init_db()

    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    with db.session_scope() as s:
        tok = Token(chain="robinhood", address="0x" + "aa" * 20, symbol="REJECTED")
        s.add(tok)
        s.flush()
        for i in range(0, 130, 15):
            s.add(TokenSnapshot(token_id=tok.id, captured_at=start + dt.timedelta(minutes=i),
                                price_usd=1.0 + i / 50, liquidity_usd=10_000.0))

    with db.session_scope() as s:
        stats = label_snapshots(s, now=start + dt.timedelta(hours=8))

    assert stats["labelled"] > 0
    with db.session_scope() as s:
        rows = s.query(SnapshotOutcome).all()
        assert rows, "no outcome rows written"
        assert all(r.window_complete for r in rows)
        assert any(r.max_return_pct and r.max_return_pct > 0 for r in rows)

    db.reset_engine()
    reload_settings()


def test_labelling_is_idempotent(tmp_path, monkeypatch):
    """Cycles run every 15 minutes; re-labelling must not duplicate rows."""
    import app.db as db
    from app.config import reload_settings
    from app.models import SnapshotOutcome, Token, TokenSnapshot
    from app.pipeline.outcomes import label_snapshots

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/i.db")
    reload_settings()
    db.reset_engine()
    db.init_db()

    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    with db.session_scope() as s:
        tok = Token(chain="robinhood", address="0x" + "bb" * 20, symbol="X")
        s.add(tok); s.flush()
        for i in range(0, 130, 15):
            s.add(TokenSnapshot(token_id=tok.id, captured_at=start + dt.timedelta(minutes=i),
                                price_usd=1.0, liquidity_usd=10_000.0))

    now = start + dt.timedelta(hours=8)
    with db.session_scope() as s:
        first = label_snapshots(s, now=now)["labelled"]
    with db.session_scope() as s:
        second = label_snapshots(s, now=now)["labelled"]

    assert first > 0
    assert second == 0, "re-running the labeller wrote duplicate rows"

    db.reset_engine()
    reload_settings()
