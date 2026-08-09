from __future__ import annotations

import datetime as dt

import pytest

from app.config import Settings
from app.schemas import NormalizedSnapshot, TokenRef


@pytest.fixture
def settings() -> Settings:
    # Explicit construction, not .env, so tests never depend on local config.
    return Settings(
        _env_file=None,
        run_mode="ALERT_ONLY",
        database_url="sqlite:///:memory:",
        position_usd=25.0,
    )


@pytest.fixture
def now() -> dt.datetime:
    return dt.datetime(2026, 8, 9, 12, 0, tzinfo=dt.timezone.utc)


def make_snapshot(now: dt.datetime, **overrides) -> NormalizedSnapshot:
    """A token that passes every gate. Tests break exactly one thing at a time."""
    base = dict(
        token=TokenRef(address="0x" + "ab" * 20, symbol="GOOD", name="Good Token", decimals=18),
        captured_at=now,
        price_usd=0.05,
        price_sources={"okx_market": 0.05, "dex_pool": 0.0501},
        price_divergence_pct=0.1,
        liquidity_usd=1_500_000.0,
        market_cap_usd=9_000_000.0,
        fdv_usd=12_000_000.0,
        total_supply=120_000_000.0,
        circulating_supply=90_000_000.0,
        volume_5m=9_000.0,
        volume_1h=95_000.0,
        volume_24h=2_200_000.0,
        tx_count_24h=4_100,
        buy_ratio_24h=0.51,
        unique_holders=6_500,
        top1_holder_pct=4.0,
        top10_holder_pct=18.0,
        whale_concentration=0.015,
        holder_growth_24h_pct=12.0,
        spread_bps=20.0,
        slippage_bps=30.0,
        slippage_notional_usd=25.0,
        token_age_hours=24 * 12,
        price_change_1h_pct=0.8,
        price_change_24h_pct=4.0,
        price_vs_7d_base_pct=8.0,
        drawdown_from_ath_pct=22.0,
        tokenomics_flags=[],
        contract_flags=[],
        contract_verified=True,
        is_proxy=False,
        sniper_wallet_pct=3.0,
        bundled_buy_pct=4.0,
        days_to_major_unlock=90.0,
        okx_available=True,
        okx_inst_id="GOOD-USDT",
    )
    base.update(overrides)
    return NormalizedSnapshot(**base)


@pytest.fixture
def good_snapshot(now):
    return make_snapshot(now)
