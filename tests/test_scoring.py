from __future__ import annotations

from app.pipeline.scoring import (
    UNKNOWN_RATIO,
    score_holders,
    score_liquidity,
    score_momentum,
    score_snapshot,
    score_tokenomics,
    score_volume,
)
from app.schemas import NormalizedSnapshot, TokenRef
from tests.conftest import make_snapshot


def test_weights_sum_to_100(good_snapshot, settings):
    res = score_snapshot(good_snapshot, settings)
    assert sum(c.weight for c in res.components) == 100.0
    assert 0.0 <= res.total <= 100.0


def test_component_weights_match_spec(good_snapshot, settings):
    res = score_snapshot(good_snapshot, settings)
    weights = {c.name: c.weight for c in res.components}
    assert weights == {
        "liquidity": 30.0, "holders": 25.0, "volume": 20.0,
        "tokenomics": 15.0, "momentum": 10.0,
    }


def test_healthy_token_scores_well(good_snapshot, settings):
    assert score_snapshot(good_snapshot, settings).total >= settings.score_alert_min


def test_score_never_exceeds_component_weight(good_snapshot, settings):
    for c in score_snapshot(good_snapshot, settings).components:
        assert 0.0 <= c.points <= c.weight


# ------------------------------------------------------- unknown is penalized
def test_empty_snapshot_scores_low_not_zero_and_not_high(now, settings):
    empty = NormalizedSnapshot(token=TokenRef(address="0x" + "00" * 20), captured_at=now)
    res = score_snapshot(empty, settings)
    # Unknown data must land well below the alert threshold.
    assert res.total < settings.score_alert_min
    assert res.total > 0  # but it is a real number, not a silent zero


def test_missing_metric_scores_worse_than_present_good_one(good_snapshot, settings, now):
    full = score_liquidity(good_snapshot, settings)
    blind = score_liquidity(make_snapshot(now, liquidity_usd=None, slippage_bps=None,
                                          spread_bps=None, market_cap_usd=None), settings)
    assert blind.ratio < full.ratio
    assert abs(blind.ratio - UNKNOWN_RATIO) < 1e-9


# ------------------------------------------------------------------- holders
def test_heavy_top10_cuts_holder_score_sharply(now, settings):
    healthy = score_holders(make_snapshot(now, top10_holder_pct=15.0), settings)
    heavy = score_holders(make_snapshot(now, top10_holder_pct=38.0), settings)
    assert heavy.points < healthy.points * 0.75


def test_more_holders_scores_higher(now, settings):
    few = score_holders(make_snapshot(now, unique_holders=400), settings)
    many = score_holders(make_snapshot(now, unique_holders=9_000), settings)
    assert many.points > few.points


# -------------------------------------------------------------------- volume
def test_volume_spike_is_penalized_versus_even_distribution(now, settings):
    even = score_volume(make_snapshot(now, volume_24h=1_000_000.0, volume_1h=42_000.0), settings)
    spike = score_volume(make_snapshot(now, volume_24h=1_000_000.0, volume_1h=300_000.0), settings)
    assert spike.points < even.points
    assert any("spike" in r for r in spike.reasons)


def test_few_huge_trades_score_worse_than_many_small_ones(now, settings):
    retail = score_volume(make_snapshot(now, volume_24h=1_000_000.0, tx_count_24h=5_000), settings)
    chunky = score_volume(make_snapshot(now, volume_24h=1_000_000.0, tx_count_24h=210), settings)
    assert chunky.points < retail.points


def test_balanced_flow_beats_lopsided_flow(now, settings):
    balanced = score_volume(make_snapshot(now, buy_ratio_24h=0.50), settings)
    lopsided = score_volume(make_snapshot(now, buy_ratio_24h=0.70), settings)
    assert lopsided.points < balanced.points


# ---------------------------------------------------------------- tokenomics
def test_unverified_contract_craters_tokenomics_score(now, settings):
    ok = score_tokenomics(make_snapshot(now, contract_verified=True), settings)
    bad = score_tokenomics(make_snapshot(now, contract_verified=False), settings)
    assert bad.points < ok.points * 0.5


def test_owner_can_mint_is_the_heaviest_single_penalty(now, settings):
    base = score_tokenomics(make_snapshot(now), settings)
    mintable = score_tokenomics(make_snapshot(now, contract_flags=["OWNER_CAN_MINT"]), settings)
    pausable = score_tokenomics(make_snapshot(now, contract_flags=["PAUSABLE"]), settings)
    assert mintable.points < pausable.points < base.points


def test_tokenomics_score_floors_at_zero(now, settings):
    awful = score_tokenomics(
        make_snapshot(
            now,
            contract_verified=False,
            contract_flags=["OWNER_CAN_MINT", "MINT_FUNCTION", "BLACKLIST", "PAUSABLE",
                            "UPGRADEABLE_PROXY", "MUTABLE_FEES"],
            tokenomics_flags=["LOW_CIRCULATING_FLOAT", "FDV_OVERHANG"],
            days_to_major_unlock=2.0,
        ),
        settings,
    )
    assert awful.points == 0.0


# ------------------------------------------------------------------ momentum
def test_parabolic_move_scores_below_quiet_base(now, settings):
    quiet = score_momentum(make_snapshot(now, price_change_24h_pct=3.0), settings)
    parabolic = score_momentum(make_snapshot(now, price_change_24h_pct=55.0), settings)
    assert parabolic.points < quiet.points
    assert any("chase" in r for r in parabolic.reasons)


def test_falling_knife_scores_low(now, settings):
    knife = score_momentum(make_snapshot(now, price_change_24h_pct=-60.0), settings)
    quiet = score_momentum(make_snapshot(now, price_change_24h_pct=3.0), settings)
    assert knife.points < quiet.points


def test_being_off_the_high_is_rewarded(now, settings):
    at_high = score_momentum(make_snapshot(now, drawdown_from_ath_pct=0.5), settings)
    off_high = score_momentum(make_snapshot(now, drawdown_from_ath_pct=35.0), settings)
    assert off_high.points > at_high.points


def test_brand_new_token_gets_no_momentum_credit(now, settings):
    newborn = score_momentum(make_snapshot(now, token_age_hours=6.0), settings)
    seasoned = score_momentum(make_snapshot(now, token_age_hours=24 * 20), settings)
    assert newborn.points < seasoned.points


# --------------------------------------------------------------- determinism
def test_scoring_is_deterministic(good_snapshot, settings):
    a = score_snapshot(good_snapshot, settings)
    b = score_snapshot(good_snapshot, settings)
    assert a.total == b.total
