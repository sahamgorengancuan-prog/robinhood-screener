"""Second derivatives.

A holder count of 1,000 says almost nothing; 100 -> 180 -> 320 -> 700 says a
great deal, and none of it is contained in a single value or a single rate.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.pipeline.acceleration import Sample, acceleration, growth_pct, pick_window

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
DAY = dt.timedelta(hours=24)


def series(*pairs):
    """(hours_before_now, value) -> Sample list."""
    return [Sample(at=BASE - dt.timedelta(hours=h), value=v) for h, v in pairs]


# ----------------------------------------------------------------- growth_pct
def test_growth_is_a_plain_percentage():
    assert growth_pct(180, 100) == pytest.approx(80.0)


def test_growth_from_zero_is_undefined_not_infinite():
    """0 -> 50 holders is the first measurement, not infinite growth."""
    assert growth_pct(50, 0) is None


@pytest.mark.parametrize("cur,prev", [(None, 100), (100, None), (None, None), (100, -5)])
def test_growth_returns_none_rather_than_a_number(cur, prev):
    assert growth_pct(cur, prev) is None


# --------------------------------------------------------------- acceleration
def test_accelerating_growth_is_positive():
    # 100 -> 180 (+80%) -> 396 (+120%)
    out = acceleration(series((48, 100), (24, 180), (0, 396)), BASE, DAY)
    assert out["prev_growth_pct"] == pytest.approx(80.0)
    assert out["growth_pct"] == pytest.approx(120.0)
    assert out["acceleration_pp"] == pytest.approx(40.0)


def test_decelerating_growth_is_negative_even_while_still_growing():
    """Still rising, but slower — the distinction the whole module exists for."""
    out = acceleration(series((48, 100), (24, 200), (0, 240)), BASE, DAY)
    assert out["growth_pct"] > 0
    assert out["acceleration_pp"] < 0


def test_two_points_give_growth_but_not_acceleration():
    """"We don't know yet" is the honest state for a young token."""
    out = acceleration(series((24, 100), (0, 150)), BASE, DAY)
    assert out["growth_pct"] == pytest.approx(50.0)
    assert out["prev_growth_pct"] is None
    assert out["acceleration_pp"] is None


def test_empty_series_yields_nulls_not_zeros():
    out = acceleration([], BASE, DAY)
    assert out == {"growth_pct": None, "prev_growth_pct": None, "acceleration_pp": None}


def test_a_single_point_cannot_produce_growth():
    assert acceleration(series((0, 100)), BASE, DAY)["growth_pct"] is None


def test_samples_too_far_from_the_window_are_rejected():
    """A "24h" rate computed from points 70 hours apart describes the gap, not
    the token."""
    out = acceleration(series((70, 100), (0, 200)), BASE, DAY)
    assert out["growth_pct"] is None


def test_slightly_off_cadence_samples_are_still_used():
    """A 15-minute scheduler drifts; demanding exact spacing would discard
    almost every real series."""
    out = acceleration(series((49, 100), (23.5, 180), (0.2, 396)), BASE, DAY)
    assert out["acceleration_pp"] is not None


def test_none_values_are_skipped_not_counted_as_zero():
    s = series((48, 100), (36, None), (24, 180), (0, 396))
    assert acceleration(s, BASE, DAY)["acceleration_pp"] == pytest.approx(40.0)


def test_acceleration_is_in_percentage_points_and_survives_negatives():
    """-20% then +30% is +50pp. A ratio would be meaningless across the sign."""
    out = acceleration(series((48, 200), (24, 160), (0, 208)), BASE, DAY)
    assert out["prev_growth_pct"] == pytest.approx(-20.0)
    assert out["growth_pct"] == pytest.approx(30.0)
    assert out["acceleration_pp"] == pytest.approx(50.0)


def test_naive_timestamps_do_not_raise():
    s = [Sample(at=dt.datetime(2025, 12, 30), value=100.0),
         Sample(at=dt.datetime(2025, 12, 31), value=200.0)]
    acceleration(s, BASE, DAY)  # must not raise


def test_pick_window_ignores_valueless_samples():
    s = [Sample(at=BASE, value=None), Sample(at=BASE - dt.timedelta(hours=5), value=7.0)]
    assert pick_window(s, BASE).value == 7.0


def test_pick_window_on_an_empty_series():
    assert pick_window([], BASE) is None


# ----------------------------------------------------------------- honest name
def test_the_metric_does_not_claim_to_count_unique_wallets():
    """Free sources report trade counts, not distinct buyers. Naming this
    "unique buyer acceleration" would overstate what was measured."""
    import app.pipeline.acceleration as mod
    import app.schemas as schemas

    assert "unique_buyer" not in schemas.NormalizedSnapshot.model_fields
    assert "buy_count_acceleration_pp" in schemas.NormalizedSnapshot.model_fields
    assert "not distinct wallets" in mod.__doc__


def test_normalize_populates_acceleration_from_history():
    """End to end: the series must actually reach the snapshot."""
    from app.config import get_settings
    from app.pipeline.normalize import RawBundle, normalize
    from app.schemas import TokenRef

    b = RawBundle(TokenRef(address="0x" + "cc" * 20, chain="robinhood", symbol="T"))
    b.holder_series = series((48, 100), (24, 180), (0, 396))
    snap = normalize(b, get_settings(), now=BASE)
    assert snap.holder_acceleration_pp == pytest.approx(40.0)
    assert snap.holder_growth_prev_pct == pytest.approx(80.0)
