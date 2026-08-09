"""Pre-trade safety checks — pure functions, no I/O.

This module is the last thing between a decision and real money, and it is
deliberately independent of the risk layer: the gates decide *whether* a token
is acceptable, these checks decide whether *this specific order* is sane. Both
must pass. Duplicating a couple of checks here is intentional defence in depth —
a bug in the gate registry should not be able to place an order.

Everything here is synchronous and pure so the tests can enumerate the edge
cases exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings


@dataclass
class ExposureState:
    open_positions: int = 0
    token_exposure_usd: float = 0.0
    daily_exposure_usd: float = 0.0
    orders_today: int = 0


@dataclass
class OrderPlan:
    inst_id: str
    limit_price: float
    size_base: float
    notional_usd: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class SafetyVerdict:
    allowed: bool
    reasons: list[str] = field(default_factory=list)

    def block(self, reason: str) -> "SafetyVerdict":
        self.allowed = False
        self.reasons.append(reason)
        return self


def check_exposure(state: ExposureState, notional_usd: float, c: Settings) -> SafetyVerdict:
    v = SafetyVerdict(allowed=True)

    if notional_usd <= 0:
        v.block(f"non-positive notional {notional_usd}")
    if notional_usd > c.position_usd * 1.5:
        v.block(f"notional ${notional_usd:,.2f} exceeds 1.5x configured clip ${c.position_usd:,.2f}")
    if state.token_exposure_usd + notional_usd > c.max_exposure_per_token_usd:
        v.block(
            f"per-token cap: ${state.token_exposure_usd:,.2f} + ${notional_usd:,.2f} "
            f"> ${c.max_exposure_per_token_usd:,.2f}"
        )
    if state.daily_exposure_usd + notional_usd > c.max_exposure_daily_usd:
        v.block(
            f"daily cap: ${state.daily_exposure_usd:,.2f} + ${notional_usd:,.2f} "
            f"> ${c.max_exposure_daily_usd:,.2f}"
        )
    if state.open_positions >= c.max_open_positions:
        v.block(f"max open positions reached ({state.open_positions}/{c.max_open_positions})")
    if state.orders_today >= c.max_orders_per_day:
        v.block(f"daily order count reached ({state.orders_today}/{c.max_orders_per_day})")

    if v.allowed:
        v.reasons.append(
            f"exposure ok: token ${state.token_exposure_usd:,.2f}/"
            f"${c.max_exposure_per_token_usd:,.0f}, day ${state.daily_exposure_usd:,.2f}/"
            f"${c.max_exposure_daily_usd:,.0f}"
        )
    return v


def build_order_plan(
    inst_id: str,
    bid: float | None,
    ask: float | None,
    reference_price: float | None,
    notional_usd: float,
    c: Settings,
    lot_size: float | None = None,
    min_size: float | None = None,
) -> tuple[OrderPlan | None, SafetyVerdict]:
    """Turn a book snapshot into a concrete post-only limit buy, or refuse.

    Price: bid minus `limit_offset_bps`. Post-only below the bid can never take
    liquidity, so the worst case is no fill — never a surprise fill at the ask.
    """
    v = SafetyVerdict(allowed=True)

    if bid is None or ask is None or bid <= 0 or ask <= 0:
        v.block("no valid order book (missing bid/ask)")
        return None, v
    if ask < bid:
        v.block(f"crossed book bid={bid} ask={ask}")
        return None, v

    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10_000
    if spread_bps > c.max_spread_bps:
        v.block(f"spread {spread_bps:.0f}bps exceeds {c.max_spread_bps}bps at execution time")
        return None, v

    # Independent price cross-check: the book must agree with what we scored.
    if reference_price and reference_price > 0:
        drift = abs(mid - reference_price) / reference_price
        if drift > c.price_max_source_divergence:
            v.block(
                f"book mid {mid:.10g} diverges {drift*100:.1f}% from reference "
                f"{reference_price:.10g} (max {c.price_max_source_divergence*100:.1f}%)"
            )
            return None, v
        v.reasons.append(f"book agrees with reference within {drift*100:.2f}%")

    limit_price = bid * (1.0 - c.limit_offset_bps / 10_000.0)
    if limit_price <= 0:
        v.block("computed limit price <= 0")
        return None, v

    size = notional_usd / limit_price

    if lot_size and lot_size > 0:
        # Round DOWN. Rounding up would spend more than the configured clip.
        steps = int(size / lot_size)
        size = steps * lot_size
        if size <= 0:
            v.block(f"notional ${notional_usd:,.2f} is smaller than one lot ({lot_size})")
            return None, v

    if min_size and size < min_size:
        v.block(f"size {size:.10g} below instrument minimum {min_size:.10g}")
        return None, v

    actual_notional = size * limit_price
    if actual_notional > notional_usd * 1.01:
        v.block(f"rounded notional ${actual_notional:,.2f} exceeds requested ${notional_usd:,.2f}")
        return None, v

    plan = OrderPlan(
        inst_id=inst_id,
        limit_price=limit_price,
        size_base=size,
        notional_usd=actual_notional,
        reasons=[
            f"post-only buy {size:.10g} @ {limit_price:.10g} ({c.limit_offset_bps}bps below bid {bid:.10g})",
            f"spread {spread_bps:.0f}bps within {c.max_spread_bps}bps",
        ],
    )
    v.reasons.extend(plan.reasons)
    return plan, v


def check_execution_conditions(
    liquidity_usd_now: float | None,
    liquidity_usd_at_decision: float | None,
    slippage_bps_now: float | None,
    c: Settings,
) -> SafetyVerdict:
    """Re-validated immediately before sending. Conditions drift between the
    scoring pass and the order; a token that thinned out in the last minute must
    not be bought on stale numbers."""
    v = SafetyVerdict(allowed=True)

    if liquidity_usd_now is None:
        return v.block("current liquidity unknown at execution time")
    if liquidity_usd_now < c.min_liquidity_usd_live:
        v.block(f"liquidity ${liquidity_usd_now:,.0f} now below live floor ${c.min_liquidity_usd_live:,.0f}")
    if liquidity_usd_at_decision and liquidity_usd_at_decision > 0:
        drop = (liquidity_usd_at_decision - liquidity_usd_now) / liquidity_usd_at_decision
        if drop > 0.25:
            v.block(f"liquidity dropped {drop*100:.0f}% since scoring — aborting")
    if slippage_bps_now is None:
        v.block("current slippage estimate unavailable")
    elif slippage_bps_now > c.max_slippage_bps:
        v.block(f"slippage widened to {slippage_bps_now:.0f}bps (max {c.max_slippage_bps})")

    if v.allowed:
        v.reasons.append(f"execution conditions stable (liquidity ${liquidity_usd_now:,.0f})")
    return v


def realized_slippage_bps(expected_price: float, avg_fill_price: float) -> float | None:
    if expected_price <= 0 or avg_fill_price <= 0:
        return None
    return round((avg_fill_price - expected_price) / expected_price * 10_000, 2)
