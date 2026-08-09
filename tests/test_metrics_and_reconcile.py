from __future__ import annotations

import pytest

from app.pipeline.metrics import (
    drawdown_from_high,
    estimate_slippage_bps,
    holder_pcts,
    linear_score,
    price_vs_base,
    spread_bps,
    volume_consistency,
)
from app.util.reconcile import reconcile_liquidity, reconcile_prices


# ------------------------------------------------------------------- holders
def test_holder_pcts_uses_total_supply_when_known():
    d = holder_pcts([300.0, 200.0, 100.0], total_supply=1000.0)
    assert d["top1_pct"] == pytest.approx(30.0)
    assert d["top10_pct"] == pytest.approx(60.0)
    assert d["basis"] == "total_supply"


def test_holder_pcts_marks_partial_basis_when_supply_unknown():
    d = holder_pcts([300.0, 200.0, 100.0], total_supply=None)
    assert d["basis"] == "observed_sum"
    assert d["top10_pct"] == pytest.approx(100.0)


def test_holder_pcts_empty_is_none_not_zero():
    d = holder_pcts([], total_supply=1000.0)
    assert d["top1_pct"] is None and d["top10_pct"] is None


def test_hhi_detects_dispersed_whales_that_top1_misses():
    """Twenty wallets at 5% each: top1 looks fine, HHI does not."""
    spread_out = holder_pcts([50.0] * 20, total_supply=1000.0)
    concentrated = holder_pcts([1000.0], total_supply=1000.0)
    assert spread_out["top1_pct"] == pytest.approx(5.0)
    assert spread_out["whale_concentration"] < concentrated["whale_concentration"]


# ------------------------------------------------------------------ slippage
def test_slippage_shrinks_as_liquidity_grows():
    thin = estimate_slippage_bps(50_000.0, 1_000.0)
    deep = estimate_slippage_bps(5_000_000.0, 1_000.0)
    assert thin > deep


def test_slippage_none_without_liquidity():
    assert estimate_slippage_bps(None, 1_000.0) is None
    assert estimate_slippage_bps(0.0, 1_000.0) is None


def test_spread_bps_basic():
    assert spread_bps(99.0, 101.0) == pytest.approx(200.0)
    assert spread_bps(None, 101.0) is None
    assert spread_bps(101.0, 99.0) is None  # crossed


# -------------------------------------------------------------------- volume
def test_volume_consistency_flags_a_spike():
    even = volume_consistency(None, 42_000.0, 1_000_000.0)
    spike = volume_consistency(None, 800_000.0, 1_000_000.0)
    assert spike["share_1h"] > even["share_1h"]
    assert even["uniformity_1h"] == pytest.approx(1.008, abs=0.01)


def test_volume_consistency_without_24h_is_all_none():
    assert volume_consistency(1.0, 1.0, None)["share_1h"] is None


# ------------------------------------------------------------------ momentum
def test_price_vs_base_uses_median_not_mean():
    """One blow-off print must not redefine the base."""
    history = [1.0, 1.0, 1.0, 1.0, 100.0]
    assert price_vs_base(1.0, history) == pytest.approx(0.0)


def test_drawdown_from_high():
    assert drawdown_from_high(50.0, [100.0, 80.0]) == pytest.approx(50.0)
    assert drawdown_from_high(120.0, [100.0]) == pytest.approx(0.0)


def test_linear_score_handles_inverted_scales():
    # lower is better
    assert linear_score(10.0, bad=100.0, good=0.0) == pytest.approx(0.9)
    # higher is better
    assert linear_score(90.0, bad=0.0, good=100.0) == pytest.approx(0.9)
    assert linear_score(None, 0.0, 1.0) is None
    assert linear_score(500.0, bad=0.0, good=100.0) == 1.0  # clamped


# -------------------------------------------------------------- reconciliation
def test_reconcile_prefers_median_of_three():
    r = reconcile_prices({"okx_market": 1.00, "dex_pool": 1.01, "chainlink": 1.005})
    assert r["price"] == pytest.approx(1.005)
    assert r["trusted"] is True
    assert r["divergence_pct"] < 1.0


def test_reconcile_flags_large_divergence():
    r = reconcile_prices({"okx_market": 1.00, "dex_pool": 2.00})
    assert r["divergence_pct"] > 30.0


def test_single_source_is_untrusted():
    r = reconcile_prices({"okx_market": 1.00})
    assert r["trusted"] is False
    assert r["price"] == 1.00
    assert "no cross-check" in r["detail"]


def test_no_source_yields_none():
    r = reconcile_prices({})
    assert r["price"] is None and r["trusted"] is False


def test_zero_and_negative_prices_are_discarded():
    r = reconcile_prices({"okx_market": 0.0, "dex_pool": -5.0, "chainlink": 1.0})
    assert r["price"] == 1.0
    assert r["trusted"] is False  # only one usable source survived


def test_manipulated_outlier_does_not_move_the_median_much():
    honest = reconcile_prices({"a": 1.0, "b": 1.0, "c": 1.0})
    attacked = reconcile_prices({"a": 1.0, "b": 1.0, "c": 50.0})
    assert attacked["price"] == honest["price"]
    assert attacked["divergence_pct"] > 1000  # but the divergence gate still fires


def test_liquidity_reconciliation_is_pessimistic():
    assert reconcile_liquidity({"okx_market": 900_000.0, "dex_pool": 200_000.0}) == 200_000.0
    assert reconcile_liquidity({}) is None
