"""Order safety tests — the last line of defence before real money."""

from __future__ import annotations

import pytest

from app.execution.order_safety import (
    ExposureState,
    build_order_plan,
    check_execution_conditions,
    check_exposure,
    realized_slippage_bps,
)


# ------------------------------------------------------------------ exposure
def test_fresh_account_can_place_one_clip(settings):
    v = check_exposure(ExposureState(), settings.position_usd, settings)
    assert v.allowed


def test_per_token_cap_is_enforced(settings):
    state = ExposureState(token_exposure_usd=settings.max_exposure_per_token_usd - 1)
    v = check_exposure(state, settings.position_usd, settings)
    assert not v.allowed
    assert any("per-token cap" in r for r in v.reasons)


def test_daily_cap_is_enforced(settings):
    state = ExposureState(daily_exposure_usd=settings.max_exposure_daily_usd)
    v = check_exposure(state, settings.position_usd, settings)
    assert not v.allowed
    assert any("daily cap" in r for r in v.reasons)


def test_max_open_positions_enforced(settings):
    state = ExposureState(open_positions=settings.max_open_positions)
    v = check_exposure(state, settings.position_usd, settings)
    assert not v.allowed
    assert any("open positions" in r for r in v.reasons)


def test_daily_order_count_enforced(settings):
    state = ExposureState(orders_today=settings.max_orders_per_day)
    v = check_exposure(state, settings.position_usd, settings)
    assert not v.allowed


def test_oversized_notional_rejected(settings):
    v = check_exposure(ExposureState(), settings.position_usd * 10, settings)
    assert not v.allowed


@pytest.mark.parametrize("notional", [0.0, -1.0, -1_000_000.0])
def test_non_positive_notional_rejected(settings, notional):
    assert not check_exposure(ExposureState(), notional, settings).allowed


# ---------------------------------------------------------------- order plan
def test_limit_price_is_below_the_bid(settings):
    plan, v = build_order_plan("TKN-USDT", bid=1.0, ask=1.002, reference_price=1.001,
                               notional_usd=25.0, c=settings)
    assert v.allowed and plan is not None
    assert plan.limit_price < 1.0
    expected = 1.0 * (1 - settings.limit_offset_bps / 10_000)
    assert plan.limit_price == pytest.approx(expected)


def test_notional_never_exceeds_the_configured_clip(settings):
    plan, v = build_order_plan("TKN-USDT", bid=1.0, ask=1.002, reference_price=1.0,
                               notional_usd=25.0, c=settings)
    assert plan.notional_usd <= 25.0 * 1.01


@pytest.mark.parametrize("bid,ask", [(None, 1.0), (1.0, None), (0, 1.0), (-1, 1.0), (None, None)])
def test_missing_or_absurd_book_refuses_to_build(settings, bid, ask):
    plan, v = build_order_plan("TKN-USDT", bid, ask, 1.0, 25.0, settings)
    assert plan is None and not v.allowed


def test_crossed_book_refused(settings):
    plan, v = build_order_plan("TKN-USDT", bid=1.05, ask=1.00, reference_price=1.0,
                               notional_usd=25.0, c=settings)
    assert plan is None and not v.allowed
    assert any("crossed book" in r for r in v.reasons)


def test_wide_spread_refused_at_execution_time(settings):
    # 5% spread, far above the 80bps limit.
    plan, v = build_order_plan("TKN-USDT", bid=1.00, ask=1.05, reference_price=1.02,
                               notional_usd=25.0, c=settings)
    assert plan is None and not v.allowed
    assert any("spread" in r for r in v.reasons)


def test_book_diverging_from_reference_price_refused(settings):
    """The book must agree with the price we scored — otherwise we are trading
    an asset that moved out from under the analysis."""
    plan, v = build_order_plan("TKN-USDT", bid=2.00, ask=2.001, reference_price=1.00,
                               notional_usd=25.0, c=settings)
    assert plan is None and not v.allowed
    assert any("diverges" in r for r in v.reasons)


def test_lot_size_rounds_down_never_up(settings):
    plan, v = build_order_plan("TKN-USDT", bid=1.0, ask=1.001, reference_price=1.0,
                               notional_usd=25.0, c=settings, lot_size=10.0)
    assert plan is not None
    assert plan.size_base % 10.0 == 0
    assert plan.notional_usd <= 25.0


def test_notional_smaller_than_one_lot_refused(settings):
    plan, v = build_order_plan("TKN-USDT", bid=1.0, ask=1.001, reference_price=1.0,
                               notional_usd=25.0, c=settings, lot_size=1_000.0)
    assert plan is None and not v.allowed


def test_below_instrument_minimum_refused(settings):
    plan, v = build_order_plan("TKN-USDT", bid=1.0, ask=1.001, reference_price=1.0,
                               notional_usd=25.0, c=settings, min_size=10_000.0)
    assert plan is None and not v.allowed
    assert any("minimum" in r for r in v.reasons)


# -------------------------------------------------------- execution conditions
def test_unknown_current_liquidity_blocks_execution(settings):
    v = check_execution_conditions(None, 500_000.0, 30.0, settings)
    assert not v.allowed


def test_liquidity_drop_since_decision_aborts(settings):
    v = check_execution_conditions(500_000.0, 1_000_000.0, 30.0, settings)
    assert not v.allowed
    assert any("dropped" in r for r in v.reasons)


def test_widened_slippage_aborts(settings):
    v = check_execution_conditions(900_000.0, 900_000.0, 900.0, settings)
    assert not v.allowed
    assert any("slippage widened" in r for r in v.reasons)


def test_missing_slippage_aborts(settings):
    v = check_execution_conditions(900_000.0, 900_000.0, None, settings)
    assert not v.allowed


def test_stable_conditions_pass(settings):
    v = check_execution_conditions(900_000.0, 950_000.0, 30.0, settings)
    assert v.allowed


# ------------------------------------------------------------------- slippage
def test_realized_slippage_positive_when_filled_worse():
    assert realized_slippage_bps(1.0, 1.01) == pytest.approx(100.0)


def test_realized_slippage_negative_when_filled_better():
    assert realized_slippage_bps(1.0, 0.99) == pytest.approx(-100.0)


@pytest.mark.parametrize("expected,avg", [(0.0, 1.0), (1.0, 0.0), (-1.0, 1.0)])
def test_realized_slippage_guards_bad_inputs(expected, avg):
    assert realized_slippage_bps(expected, avg) is None
