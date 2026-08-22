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


def win(base, following, horizon="1h"):
    return Window(base=base, horizon=horizon, following=following)


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
        assert any(r.max_return_pct and r.max_return_pct > 0 for r in rows)

        # Sampling stops at t+120m, so a 1h window opened early is watched to
        # its end while a 6h window is not. Both are labelled; only one claims
        # to be complete. Previously every row claimed it.
        by_h = {}
        for r in rows:
            by_h.setdefault(r.horizon, []).append(r)
        assert any(r.window_complete for r in by_h["1h"]), "1h windows were fully observed"
        assert not any(r.window_complete for r in by_h.get("6h", [])), \
            "a 6h window cannot be complete when sampling stopped after 2h"
        assert all(0.0 <= r.coverage_pct <= 100.0 for r in rows)

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


# --------------------------------------------------- window bounds & coverage
def test_a_later_snapshots_window_is_not_truncated(tmp_path, monkeypatch):
    """Regression: the series was queried only up to `earliest + max(horizon)`,
    so a base captured later had its window cut short. A 50x that happened
    inside a 7d window was recorded as zero observations — silently destroying
    the one part of the dataset whose cost is calendar time."""
    from app import db
    from app.config import reload_settings
    from app.models import SnapshotOutcome, Token, TokenSnapshot
    from app.pipeline.outcomes import label_snapshots

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    reload_settings()
    db.reset_engine()
    db.init_db()

    start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    with db.session_scope() as s:
        tok = Token(chain="robinhood", address="0x" + "bb" * 20, symbol="LATE")
        s.add(tok)
        s.flush()
        for days, price in [(0, 1.0), (3, 1.0), (9, 50.0), (10, 50.0)]:
            s.add(TokenSnapshot(token_id=tok.id, captured_at=start + dt.timedelta(days=days),
                                price_usd=price, liquidity_usd=10_000.0))

    with db.session_scope() as s:
        label_snapshots(s, now=start + dt.timedelta(days=11))

    with db.session_scope() as s:
        base = s.query(TokenSnapshot).order_by(TokenSnapshot.captured_at).all()[1]
        row = (s.query(SnapshotOutcome)
               .filter_by(snapshot_id=base.id, horizon="7d").one())
        # base at day 3; the 50x at day 9 is inside its 7d window (ends day 10)
        assert row.observations >= 2
        assert row.max_return_pct == pytest.approx(4900.0)
        assert row.reached_10x is True

    db.reset_engine()
    reload_settings()


def test_a_token_that_stops_being_sampled_is_not_marked_complete():
    """The censoring trap: nothing observed, yet the row used to claim a fully
    watched window, so filtering on window_complete kept exactly the rows a
    model has to drop."""
    from app.pipeline.outcomes import window_coverage

    base = FakeSnap(0, price=1.0)
    coverage, complete = window_coverage(win(base, []))
    assert coverage == 0.0 and complete is False

    row = measure(win(base, []))
    assert row["observations"] == 0
    assert row["window_complete"] is False
    assert row["max_return_pct"] is None, "an unobserved window must not read as flat"


def test_coverage_tracks_how_far_observation_reached():
    from app.pipeline.outcomes import window_coverage

    base = FakeSnap(0, price=1.0)
    # 1h window; last observation at 15 minutes = 25% reach
    assert window_coverage(win(base, [FakeSnap(15, 1.0)])) == (25.0, False)
    # observed to 50 of 60 minutes -> past the tail threshold
    coverage, complete = window_coverage(win(base, [FakeSnap(15, 1.0), FakeSnap(50, 1.0)]))
    assert coverage == pytest.approx(83.33, abs=0.01)
    assert complete is True


def test_coverage_never_exceeds_one_hundred_percent():
    from app.pipeline.outcomes import window_coverage

    base = FakeSnap(0, price=1.0)
    coverage, complete = window_coverage(win(base, [FakeSnap(600, 1.0)]))
    assert coverage == 100.0 and complete is True
