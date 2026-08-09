"""Risk gate tests.

The single most important property: **a missing metric must never pass a gate.**
`test_every_gate_rejects_an_empty_snapshot` enforces that across the whole
registry, so a gate added later cannot quietly introduce a permissive default.
"""

from __future__ import annotations

import pytest

from app.pipeline.risk import REGISTRY, evaluate_gates, summarize
from app.schemas import NormalizedSnapshot, Severity, TokenRef
from tests.conftest import make_snapshot


def result_for(name: str, snap, settings):
    return next(g for g in evaluate_gates(snap, settings) if g.name == name)


def test_good_snapshot_passes_every_gate(good_snapshot, settings):
    results = evaluate_gates(good_snapshot, settings)
    buckets = summarize(results)
    assert buckets["hard"] == [], [g.reason for g in buckets["hard"]]
    assert buckets["data"] == [], [g.reason for g in buckets["data"]]
    assert buckets["live_only"] == [], [g.reason for g in buckets["live_only"]]


def test_every_gate_rejects_an_empty_snapshot(now, settings):
    """An all-None snapshot must not pass a single gate."""
    empty = NormalizedSnapshot(token=TokenRef(address="0x" + "00" * 20), captured_at=now)
    results = evaluate_gates(empty, settings)
    passing = [g.name for g in results if g.passed]
    assert passing == [], f"gates passed on empty data: {passing}"
    assert len(results) == len(REGISTRY)


# ------------------------------------------------------------------ liquidity
def test_liquidity_below_floor_is_hard_reject(now, settings):
    snap = make_snapshot(now, liquidity_usd=1_000.0)
    g = result_for("liquidity_min", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_liquidity_between_floors_blocks_live_only(now, settings):
    # Volume scaled down with liquidity so only the liquidity dimension varies.
    snap = make_snapshot(now, liquidity_usd=200_000.0, volume_24h=300_000.0, volume_1h=12_000.0)
    results = evaluate_gates(snap, settings)
    g = next(x for x in results if x.name == "liquidity_live_min")
    assert not g.passed and g.severity == Severity.LIVE_ONLY
    assert summarize(results)["hard"] == []


def test_missing_liquidity_is_data_not_pass(now, settings):
    snap = make_snapshot(now, liquidity_usd=None, slippage_bps=None)
    g = result_for("liquidity_min", snap, settings)
    assert not g.passed and g.severity == Severity.DATA


def test_slippage_above_limit_is_hard(now, settings):
    snap = make_snapshot(now, slippage_bps=400.0)
    g = result_for("slippage_max", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


# --------------------------------------------------------------------- volume
def test_wash_trading_turnover_rejected(now, settings):
    # $20M of volume on $800k of liquidity = 25x turnover.
    snap = make_snapshot(now, volume_24h=20_000_000.0)
    g = result_for("volume_organic", snap, settings)
    assert not g.passed and g.severity == Severity.HARD
    assert "wash" in g.reason.lower()


def test_dead_book_rejected(now, settings):
    snap = make_snapshot(now, volume_24h=120_000.0, liquidity_usd=8_000_000.0)
    g = result_for("volume_organic", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_single_candle_spike_rejected(now, settings):
    # 70% of the day's volume in the last hour.
    snap = make_snapshot(now, volume_24h=1_000_000.0, volume_1h=700_000.0)
    g = result_for("volume_spike", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_one_sided_buying_rejected(now, settings):
    snap = make_snapshot(now, buy_ratio_24h=0.95)
    g = result_for("buy_sell_balance", snap, settings)
    assert not g.passed and g.severity == Severity.HARD
    assert "one-sided" in g.reason


def test_persistent_distribution_rejected(now, settings):
    snap = make_snapshot(now, buy_ratio_24h=0.20)
    g = result_for("buy_sell_balance", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


# -------------------------------------------------------------------- holders
def test_top1_concentration_rejected(now, settings):
    snap = make_snapshot(now, top1_holder_pct=30.0)
    g = result_for("top1_concentration", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_top10_concentration_rejected(now, settings):
    snap = make_snapshot(now, top10_holder_pct=75.0)
    g = result_for("top10_concentration", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_shrinking_holders_rejected(now, settings):
    snap = make_snapshot(now, holder_growth_24h_pct=-30.0)
    g = result_for("holder_growth", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_implausible_holder_explosion_rejected(now, settings):
    snap = make_snapshot(now, holder_growth_24h_pct=2_000.0)
    g = result_for("holder_growth", snap, settings)
    assert not g.passed and g.severity == Severity.HARD
    assert "sybil" in g.reason or "airdrop" in g.reason


def test_sniper_domination_rejected(now, settings):
    snap = make_snapshot(now, sniper_wallet_pct=40.0)
    g = result_for("sniper_domination", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_bundled_buys_rejected(now, settings):
    snap = make_snapshot(now, bundled_buy_pct=55.0)
    g = result_for("bundling", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


# ------------------------------------------------------------------- contract
def test_unverified_contract_is_hard_reject(now, settings):
    snap = make_snapshot(now, contract_verified=False)
    g = result_for("contract_verified", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_unknown_verification_blocks_live_but_not_alert(now, settings):
    snap = make_snapshot(now, contract_verified=None)
    g = result_for("contract_verified", snap, settings)
    assert not g.passed and g.severity == Severity.LIVE_ONLY


@pytest.mark.parametrize("flag", ["OWNER_CAN_MINT", "BLACKLIST", "NOT_A_CONTRACT"])
def test_critical_contract_flags_are_hard(now, settings, flag):
    snap = make_snapshot(now, contract_flags=[flag])
    g = result_for("contract_flags", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


@pytest.mark.parametrize("flag", ["UPGRADEABLE_PROXY", "PAUSABLE", "MUTABLE_FEES"])
def test_mutable_surface_blocks_live_only(now, settings, flag):
    snap = make_snapshot(now, contract_flags=[flag])
    g = result_for("contract_flags", snap, settings)
    assert not g.passed and g.severity == Severity.LIVE_ONLY


def test_critical_tokenomics_flag_is_hard(now, settings):
    snap = make_snapshot(now, tokenomics_flags=["CRITICAL_LIQUIDITY_TO_MCAP"])
    g = result_for("tokenomics", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_imminent_unlock_rejected(now, settings):
    snap = make_snapshot(now, days_to_major_unlock=3.0)
    g = result_for("unlock_proximity", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_unknown_unlock_schedule_does_not_fabricate_a_verdict(now, settings):
    snap = make_snapshot(now, days_to_major_unlock=None)
    g = result_for("unlock_proximity", snap, settings)
    assert not g.passed and g.severity == Severity.LIVE_ONLY
    assert "cannot rule out" in g.reason


# ---------------------------------------------------------------- anti-chase
def test_parabolic_token_rejected(now, settings):
    snap = make_snapshot(now, price_change_24h_pct=180.0)
    g = result_for("not_extended", snap, settings)
    assert not g.passed and g.severity == Severity.HARD
    assert "no chasing" in g.reason


def test_price_far_above_base_rejected(now, settings):
    snap = make_snapshot(now, price_change_24h_pct=20.0, price_vs_7d_base_pct=400.0)
    g = result_for("not_extended", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_too_young_rejected(now, settings):
    snap = make_snapshot(now, token_age_hours=3.0)
    g = result_for("token_age", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


# -------------------------------------------------------- price reconciliation
def test_diverging_price_sources_is_hard_reject(now, settings):
    snap = make_snapshot(now, price_sources={"okx_market": 0.05, "dex_pool": 0.09},
                         price_divergence_pct=28.0)
    g = result_for("price_agreement", snap, settings)
    assert not g.passed and g.severity == Severity.HARD


def test_single_price_source_blocks_live(now, settings):
    snap = make_snapshot(now, price_sources={"okx_market": 0.05}, price_divergence_pct=None)
    g = result_for("price_agreement", snap, settings)
    assert not g.passed and g.severity == Severity.LIVE_ONLY


def test_a_raising_gate_fails_closed(now, settings, monkeypatch):
    """A gate that throws must produce a HARD failure, never be skipped."""
    import app.pipeline.risk as risk

    def exploding_gate(s, c):
        raise ValueError("boom")

    monkeypatch.setattr(risk, "REGISTRY", [exploding_gate])
    results = risk.evaluate_gates(make_snapshot(now), settings)
    assert len(results) == 1
    assert not results[0].passed
    assert results[0].severity == Severity.HARD
    assert "failing closed" in results[0].reason
