"""Pure derived-metric functions.

Every function here is deterministic, dependency-free and returns `None` when
its inputs are insufficient. That is what makes the risk layer testable without
touching a network.
"""

from __future__ import annotations

import math
from typing import Any, Sequence


def safe_div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100.0


def holder_pcts(balances: Sequence[float], total_supply: float | None) -> dict[str, float | None]:
    """Top-1 / top-10 concentration and a whale score.

    `balances` must be sorted descending. Percentages are of `total_supply`; if
    total_supply is unknown we fall back to the sum of the observed balances and
    flag that as a partial view via `basis`.
    """
    if not balances:
        return {"top1_pct": None, "top10_pct": None, "whale_concentration": None, "basis": None}

    basis = total_supply if total_supply and total_supply > 0 else sum(balances)
    if not basis:
        return {"top1_pct": None, "top10_pct": None, "whale_concentration": None, "basis": None}

    ordered = sorted(balances, reverse=True)
    top1 = ordered[0] / basis * 100.0
    top10 = sum(ordered[:10]) / basis * 100.0

    # Herfindahl index over observed holders: 0 = perfectly dispersed, 1 = one
    # wallet holds everything. More informative than top-N alone because it
    # catches "20 wallets with 4% each".
    hhi = sum((b / basis) ** 2 for b in ordered)

    return {
        "top1_pct": round(top1, 4),
        "top10_pct": round(top10, 4),
        "whale_concentration": round(hhi, 6),
        "basis": "total_supply" if total_supply else "observed_sum",
    }


def volume_consistency(v5m: float | None, v1h: float | None, v24h: float | None) -> dict[str, Any]:
    """Detect whether 24h volume is spread out or concentrated in one burst.

    Returns `single_window_share` = share of 24h volume sitting in the most
    recent 5m/1h window, scaled to what a uniform distribution would predict.
    A uniform day gives ratio ~1.0; a single pump gives a large number.
    """
    out: dict[str, Any] = {"share_5m": None, "share_1h": None, "uniformity_1h": None, "spike": None}
    if not v24h or v24h <= 0:
        return out

    if v5m is not None:
        out["share_5m"] = v5m / v24h
    if v1h is not None:
        out["share_1h"] = v1h / v24h
        # Uniform expectation for 1h = 1/24 of the day.
        out["uniformity_1h"] = (v1h / v24h) / (1 / 24)

    shares = [s for s in (out["share_5m"], out["share_1h"]) if s is not None]
    if shares:
        out["spike"] = max(shares)
    return out


def estimate_slippage_bps(liquidity_usd: float | None, notional_usd: float) -> float | None:
    """Constant-product (x*y=k) slippage estimate for a single-sided buy.

    For a pool with `L` USD of total liquidity, one side holds ~L/2. Buying
    `n` USD moves price by roughly n/(L/2), and the average execution price is
    worse than mid by about half that at small sizes:

        slippage ≈ n / (L/2 + n)

    This is an approximation and is documented as such — it is used as a
    *conservative screen*, and the real book is re-checked against OKX depth
    before any live order.
    """
    if liquidity_usd is None or liquidity_usd <= 0 or notional_usd <= 0:
        return None
    side = liquidity_usd / 2.0
    slip = notional_usd / (side + notional_usd)
    return round(slip * 10_000, 2)


def spread_bps(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return round((ask - bid) / mid * 10_000, 2)


def buy_ratio(buys: int | None, sells: int | None) -> float | None:
    if buys is None or sells is None:
        return None
    total = buys + sells
    if total <= 0:
        return None
    return buys / total


def price_vs_base(current: float | None, history: Sequence[float]) -> float | None:
    """How extended is price versus its recent base (median of the window)?

    Median, not mean: one blow-off candle should not redefine the base.
    """
    clean = [p for p in history if p is not None and p > 0]
    if current is None or current <= 0 or len(clean) < 3:
        return None
    ordered = sorted(clean)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    if median <= 0:
        return None
    return (current - median) / median * 100.0


def drawdown_from_high(current: float | None, history: Sequence[float]) -> float | None:
    clean = [p for p in history if p is not None and p > 0]
    if current is None or current <= 0 or not clean:
        return None
    high = max(clean + [current])
    if high <= 0:
        return None
    return (high - current) / high * 100.0


def stdev(values: Sequence[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(var)


def median(values: Sequence[float]) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    n = len(clean)
    return clean[n // 2] if n % 2 else (clean[n // 2 - 1] + clean[n // 2]) / 2


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def linear_score(value: float | None, bad: float, good: float) -> float | None:
    """Map a value onto 0..1 between a 'bad' and a 'good' anchor.

    Works in both directions: `bad > good` inverts the scale (lower is better).
    """
    if value is None:
        return None
    if bad == good:
        return None
    return clamp((value - bad) / (good - bad))


def log_score(value: float | None, bad: float, good: float) -> float | None:
    """Log-scaled version of `linear_score`, for quantities whose usefulness
    grows multiplicatively rather than additively.

    Liquidity, holder count and volume all behave this way: going from $150k to
    $400k of liquidity is a far bigger improvement in tradability than going
    from $3.0M to $3.25M, even though the linear distance is similar. Scoring
    them linearly squashes every realistic candidate into the bottom third of
    the range and makes the thresholds meaningless.
    """
    if value is None or value <= 0 or bad <= 0 or good <= 0 or bad == good:
        return None
    return clamp(math.log(value / bad) / math.log(good / bad))
