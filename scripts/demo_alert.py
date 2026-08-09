#!/usr/bin/env python3
"""Render sample alerts for three tokens without touching the network.

Useful for tuning thresholds and for seeing exactly what an alert looks like
before wiring up Telegram.

    python scripts/demo_alert.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.alerts.formatter import build_body  # noqa: E402
from app.config import Settings  # noqa: E402
from app.pipeline.decision import DecisionContext, decide  # noqa: E402
from app.pipeline.risk import evaluate_gates  # noqa: E402
from app.pipeline.scoring import score_snapshot  # noqa: E402
from app.schemas import NormalizedSnapshot, TokenRef  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)


def snap(**kw) -> NormalizedSnapshot:
    base = dict(
        token=TokenRef(address="0x" + "ab" * 20, symbol="GOOD", name="Good Token", decimals=18),
        captured_at=NOW,
        price_usd=0.05, price_sources={"okx_market": 0.05, "dex_pool": 0.0501},
        price_divergence_pct=0.1,
        liquidity_usd=1_500_000.0, market_cap_usd=9_000_000.0, fdv_usd=12_000_000.0,
        total_supply=120_000_000.0, circulating_supply=90_000_000.0,
        volume_5m=9_000.0, volume_1h=95_000.0, volume_24h=2_200_000.0,
        tx_count_24h=4_100, buy_ratio_24h=0.51,
        unique_holders=6_500, top1_holder_pct=4.0, top10_holder_pct=18.0,
        whale_concentration=0.015, holder_growth_24h_pct=12.0,
        spread_bps=20.0, slippage_bps=30.0, slippage_notional_usd=25.0,
        token_age_hours=24 * 12, price_change_1h_pct=0.8, price_change_24h_pct=4.0,
        price_vs_7d_base_pct=8.0, drawdown_from_ath_pct=22.0,
        contract_verified=True, sniper_wallet_pct=3.0, bundled_buy_pct=4.0,
        days_to_major_unlock=90.0, okx_available=True, okx_inst_id="GOOD-USDT",
    )
    base.update(kw)
    return NormalizedSnapshot(**base)


CASES = {
    "1. HEALTHY CANDIDATE (paper buy)": snap(),
    "2. CONCENTRATED + WASH TRADED (reject)": snap(
        token=TokenRef(address="0x" + "cd" * 20, symbol="RUGY", name="Rug Token", decimals=18),
        top1_holder_pct=41.0, top10_holder_pct=88.0, volume_24h=30_000_000.0,
        volume_1h=22_000_000.0, buy_ratio_24h=0.94, contract_verified=False,
        contract_flags=["OWNER_CAN_MINT", "MINT_FUNCTION", "BLACKLIST"],
        price_change_24h_pct=340.0, token_age_hours=6.0, holder_growth_24h_pct=3_000.0,
        days_to_major_unlock=2.0, sniper_wallet_pct=52.0, bundled_buy_pct=61.0,
    ),
    "3. THIN DATA (watch)": snap(
        token=TokenRef(address="0x" + "ef" * 20, symbol="NEWT", name="New Token", decimals=18),
        unique_holders=None, top1_holder_pct=None, top10_holder_pct=None,
        whale_concentration=None, holder_growth_24h_pct=None, tx_count_24h=None,
        contract_verified=None, okx_available=False, okx_inst_id=None, spread_bps=None,
        price_sources={"dex_pool": 0.05}, price_divergence_pct=None,
    ),
}


def main() -> None:
    c = Settings(_env_file=None)
    for title, s in CASES.items():
        gates = evaluate_gates(s, c)
        score = score_snapshot(s, c)
        ctx = DecisionContext(consecutive_passes=3, recent_scores=[score.total] * 3,
                              run_mode=c.run_mode, okx_available=s.okx_available)
        d = decide(s, gates, score, ctx, c)
        print(f"\n\n########## {title} ##########\n")
        print(build_body(s, d))


if __name__ == "__main__":
    main()
