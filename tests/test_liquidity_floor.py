"""The liquidity floor must be derived, and must argue its own case.

150_000 was a number with no derivation. On a chain whose deepest observed pool
was $62,865 it rejected every token forever while explaining nothing.
"""

from __future__ import annotations

import pytest

from app.pipeline.liquidity_floor import (
    ABSOLUTE_MINIMUM_USD,
    constant_product_depth_usd,
    effective_min_liquidity,
    required_liquidity_usd,
)


def test_constant_product_depth_matches_the_closed_form():
    """slippage = 2d/L, so L = 20000*d/bps. A $25 clip at 150bps needs $3,333."""
    assert constant_product_depth_usd(25, 150) == pytest.approx(3_333.33, rel=1e-3)


def test_depth_scales_linearly_with_position():
    assert constant_product_depth_usd(50, 150) == pytest.approx(
        2 * constant_product_depth_usd(25, 150))


def test_depth_scales_inversely_with_slippage_budget():
    """Halving the tolerated slippage doubles the depth required."""
    assert constant_product_depth_usd(25, 75) == pytest.approx(
        2 * constant_product_depth_usd(25, 150))


@pytest.mark.parametrize("position,bps", [(0, 150), (-5, 150), (25, 0), (25, -1)])
def test_nonsense_inputs_return_none_rather_than_a_number(position, bps):
    assert constant_product_depth_usd(position, bps) is None
    assert required_liquidity_usd(position, bps, 10) is None


def test_a_floor_under_the_floor():
    """A microscopic clip must not derive a floor of a few dollars — fees and one
    competing trade dominate long before the arithmetic stops applying."""
    assert required_liquidity_usd(0.01, 5_000, 1) == ABSOLUTE_MINIMUM_USD


def test_safety_multiple_below_one_is_ignored():
    """A multiple under 1 would ask for less depth than the trade needs."""
    assert required_liquidity_usd(25, 150, 0.1) == required_liquidity_usd(25, 150, 1.0)


def test_derived_floor_is_far_below_the_old_constant(settings):
    """The old floor was ~45x stricter than the trade it protected."""
    floor, why = effective_min_liquidity(settings)
    assert floor < 150_000
    assert "derived" in why and "150" not in why.split("safety")[0][:20]


def test_the_reason_states_the_derivation(settings):
    floor, why = effective_min_liquidity(settings)
    assert "clip" in why and "bps" in why and "safety" in why


def test_live_floor_is_stricter_than_the_entry_floor(settings):
    assert effective_min_liquidity(settings, live=True)[0] > effective_min_liquidity(settings)[0]


def test_absolute_mode_uses_the_configured_constant(settings):
    c = settings.model_copy(update={"liquidity_floor_mode": "absolute"})
    floor, why = effective_min_liquidity(c)
    assert floor == c.min_liquidity_usd
    assert "absolute" in why


def test_combined_mode_takes_the_stricter_of_the_two(settings):
    c = settings.model_copy(update={"liquidity_floor_mode": "derived_or_absolute"})
    floor, _ = effective_min_liquidity(c)
    assert floor == max(c.min_liquidity_usd, effective_min_liquidity(
        settings.model_copy(update={"liquidity_floor_mode": "derived"}))[0])


def test_floor_tracks_position_size(settings):
    """Doubling the clip must double the depth demanded, or the floor is
    protecting a trade that is no longer the one being made."""
    small = effective_min_liquidity(settings.model_copy(update={"position_usd": 25.0}))[0]
    big = effective_min_liquidity(settings.model_copy(update={"position_usd": 250.0}))[0]
    assert big == pytest.approx(10 * small)


def test_gate_reason_carries_the_derivation(now, settings):
    from app.pipeline.risk import evaluate_gates

    from tests.conftest import make_snapshot

    snap = make_snapshot(now, liquidity_usd=100.0)
    g = next(x for x in evaluate_gates(snap, settings) if x.name == "liquidity_min")
    assert not g.passed
    assert "derived" in g.reason and "clip" in g.reason
