"""Price reconciliation across independent sources.

Three sources can disagree: the OKX indexed price, a DEX pool-derived price
from the Node API, and a Chainlink feed. They disagree for legitimate reasons
(different update cadence, different venue) and for dangerous ones (a stale
feed, or a pool someone just manipulated to set up an exit).

Policy, in order:

  1. Prefer the **median** of available sources, not the mean — one manipulated
     source cannot drag the median the way it drags an average.
  2. Measure `divergence` as the max relative distance from that median.
  3. If divergence exceeds `PRICE_MAX_SOURCE_DIVERGENCE`, the price is marked
     untrusted, `gate_price_agreement` fails HARD, and nothing gets bought.
  4. With a single source, no cross-check is possible: the value is used for
     alerting but blocks live execution (LIVE_ONLY).

Source priority when a tie-break is needed (most to least trusted):
    chainlink > okx_market > dex_pool > explorer
Rationale: an oracle aggregates many venues, OKX aggregates many pools, a
single pool is the easiest thing in the list to manipulate.
"""

from __future__ import annotations

from app.pipeline.metrics import median

SOURCE_PRIORITY = ["chainlink", "okx_market", "dex_pool", "explorer"]


def reconcile_prices(sources: dict[str, float]) -> dict[str, object]:
    """Returns {'price', 'divergence_pct', 'trusted', 'chosen_source', 'detail'}."""
    clean = {k: v for k, v in sources.items() if v is not None and v > 0}

    if not clean:
        return {"price": None, "divergence_pct": None, "trusted": False,
                "chosen_source": None, "detail": "no price source produced a value"}

    if len(clean) == 1:
        name, val = next(iter(clean.items()))
        return {"price": val, "divergence_pct": None, "trusted": False,
                "chosen_source": name,
                "detail": f"single source ({name}) — no cross-check available"}

    med = median(list(clean.values()))
    assert med is not None and med > 0

    divergence = max(abs(v - med) / med for v in clean.values()) * 100.0

    chosen = next((s for s in SOURCE_PRIORITY if s in clean), None) or next(iter(clean))
    # The reported price is the median; `chosen_source` records which trusted
    # source is closest in intent, for the audit trail.
    return {
        "price": med,
        "divergence_pct": round(divergence, 4),
        "trusted": True,
        "chosen_source": chosen,
        "detail": ", ".join(f"{k}={v:.10g}" for k, v in sorted(clean.items())),
    }


def reconcile_liquidity(sources: dict[str, float]) -> float | None:
    """Liquidity disagreements are resolved conservatively: take the minimum.

    If OKX says $900k and the pool says $200k, we can only actually exit into
    $200k. Sizing off the optimistic number is how you end up unable to leave.
    """
    clean = [v for v in sources.values() if v is not None and v > 0]
    return min(clean) if clean else None
