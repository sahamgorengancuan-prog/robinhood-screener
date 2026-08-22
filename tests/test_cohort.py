"""Observed distributions used to argue about thresholds."""

from __future__ import annotations

import pytest

from app.pipeline.cohort import (
    AGE_BUCKETS,
    MIN_SAMPLE,
    MetricStats,
    bucket_for,
    percentile,
    percentile_of,
)


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)


def test_percentile_endpoints():
    v = [10, 20, 30]
    assert percentile(v, 0) == 10
    assert percentile(v, 100) == 30


def test_percentile_of_an_empty_population_is_none():
    assert percentile([], 50) is None
    assert percentile_of(5, []) is None


def test_percentile_of_a_single_value():
    assert percentile([7], 90) == 7.0


def test_nones_are_dropped_not_counted_as_zero():
    assert percentile([None, 10, None, 20], 50) == pytest.approx(15.0)


def test_percentile_of_places_a_value_in_a_population():
    pop = list(range(100))
    assert percentile_of(50, pop) == pytest.approx(50.5, abs=1.0)
    assert percentile_of(-1, pop) == 0.0


def test_percentile_of_handles_ties_without_claiming_the_top():
    """Ten identical values: being one of them is not the 100th percentile."""
    assert percentile_of(5, [5] * 10) == pytest.approx(50.0)


def test_percentile_of_a_missing_value_is_none():
    assert percentile_of(None, [1, 2, 3]) is None


@pytest.mark.parametrize("hours,expected", [
    (0.5, "<6h"), (5.9, "<6h"), (6.0, "6-24h"), (23.9, "6-24h"),
    (24.0, "1-7d"), (167.0, "1-7d"), (168.0, ">7d"), (10_000.0, ">7d"),
])
def test_age_buckets_partition_without_gaps_or_overlap(hours, expected):
    assert bucket_for(hours) == expected


def test_unknown_age_belongs_to_no_cohort():
    """Pooling unknown-age tokens into a cohort describes neither population."""
    assert bucket_for(None) is None


def test_buckets_are_contiguous():
    for (_, _, high), (_, low, _) in zip(AGE_BUCKETS, AGE_BUCKETS[1:]):
        assert high == low


def test_a_thin_sample_is_marked_untrustworthy():
    assert not MetricStats("liquidity_usd", n=MIN_SAMPLE - 1).trustworthy
    assert MetricStats("liquidity_usd", n=MIN_SAMPLE).trustworthy


def test_render_says_so_when_there_is_nothing_to_report():
    from app.pipeline.cohort import render

    assert "no snapshots" in render({})


def test_cohort_module_refuses_to_set_thresholds():
    """It reports; it does not decide. A percentile is not a safety standard."""
    import app.pipeline.cohort as mod

    assert "does **not** set thresholds" in mod.__doc__
