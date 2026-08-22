"""Where the liquidity floor comes from.

`MIN_LIQUIDITY_USD = 150_000` was a number with no derivation behind it. On a
chain whose deepest observed pool was $62,865 it rejected everything, forever,
and told the operator nothing about why that number rather than another.

The floor a position actually needs is a function of the position, not of the
chain. For a constant-product pool holding `L` dollars of total liquidity, the
quote-side reserve is `L/2`, and buying `d` dollars moves the effective price by

    slippage ≈ d / (L/2) = 2d / L

so the depth required to keep a `d`-dollar clip inside `s` basis points is

    L = 2d / (s / 10_000) = 20_000 · d / s

For the default $25 clip at 150 bps that is $3,333 — which is why the old
constant was roughly 45× stricter than the trade it was protecting.

That bare number is still not the floor to use. It assumes the pool is the size
it was when measured, that you are the only participant, and that the exit is as
deep as the entry. None of those hold when you most need them, so the derived
requirement is multiplied by a safety factor. The point is that the factor is
now a named, arguable assumption instead of being baked invisibly into 150,000.

Concentrated-liquidity pools (Uniswap v3/v4, which Robinhood Chain uses) do not
follow constant-product globally — depth near the current tick can be far better
or far worse than this implies. Treat the result as a lower bound on what to
require, never as a promise about realised slippage; the slippage gate measures
that separately from real quotes.
"""

from __future__ import annotations

from app.config import Settings

#: Below this, a pool is too thin for the arithmetic above to mean anything —
#: fees, rounding and a single competing trade dominate. A floor under the floor.
ABSOLUTE_MINIMUM_USD = 2_000.0


def constant_product_depth_usd(position_usd: float, slippage_bps: float) -> float | None:
    """Total pool liquidity needed to keep `position_usd` inside `slippage_bps`."""
    if position_usd <= 0 or slippage_bps <= 0:
        return None
    return 20_000.0 * position_usd / slippage_bps


def required_liquidity_usd(
    position_usd: float,
    slippage_bps: float,
    safety_multiple: float,
) -> float | None:
    base = constant_product_depth_usd(position_usd, slippage_bps)
    if base is None:
        return None
    return max(base * max(safety_multiple, 1.0), ABSOLUTE_MINIMUM_USD)


def effective_min_liquidity(c: Settings, *, live: bool = False) -> tuple[float, str]:
    """The floor this configuration actually enforces, and how it was reached.

    Returns (usd, explanation) so a rejection can say *why* the bar sits where
    it does rather than quoting a constant back at the operator.
    """
    configured = c.min_liquidity_usd_live if live else c.min_liquidity_usd
    multiple = c.liquidity_safety_multiple_live if live else c.liquidity_safety_multiple

    if c.liquidity_floor_mode == "absolute":
        return configured, f"absolute floor ${configured:,.0f} from configuration"

    derived = required_liquidity_usd(c.position_usd, c.max_slippage_bps, multiple)
    if derived is None:
        # Cannot derive without a position size or a slippage budget: fail closed
        # onto the configured number rather than inventing one.
        return configured, f"absolute floor ${configured:,.0f} (position/slippage unset)"

    why = (
        f"derived: ${c.position_usd:,.0f} clip at {c.max_slippage_bps}bps needs "
        f"${constant_product_depth_usd(c.position_usd, c.max_slippage_bps):,.0f} of "
        f"constant-product depth, x{multiple:g} safety"
    )
    if c.liquidity_floor_mode == "derived_or_absolute":
        if configured > derived:
            return configured, f"absolute floor ${configured:,.0f} (higher than {why})"
        return derived, why
    return derived, why
