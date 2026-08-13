"""Central configuration.

Every threshold that can reject a token or size an order lives here so it is
auditable in one place and overridable from `.env` without touching code.

Design rule: defaults are *conservative*. A fresh clone with an empty `.env`
runs in ALERT_ONLY mode, never places an order, and rejects anything it cannot
measure.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RunMode = Literal["ALERT_ONLY", "PAPER", "LIVE"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ------------------------------------------------------------------ core
    run_mode: RunMode = "ALERT_ONLY"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./data/screener.db"
    timezone: str = "UTC"

    # Kill switch. Any of the three trips it: env flag, sentinel file, DB flag.
    kill_switch: bool = False
    kill_switch_file: str = "./KILL_SWITCH"

    # --------------------------------------------------- Robinhood Chain: node
    # Standard Ethereum JSON-RPC. Works with any Orbit-compatible provider.
    rh_node_rpc_url: str = ""
    rh_node_ws_url: str = ""
    rh_chain_id: int | None = None  # do not guess; probe reports the live value
    rh_node_timeout_s: float = 15.0

    # ---------------------------------------------- Robinhood Chain: data API
    # Indexed data. Base URL + path templates are config so an endpoint change
    # is a .env edit, not a code change. See docs/ENDPOINTS.md for the
    # verified/unverified matrix.
    rh_data_enabled: bool = False
    rh_data_base_url: str = ""
    rh_data_api_key: str = ""
    rh_data_path_token_list: str = "/tokens"
    rh_data_path_token_meta: str = "/tokens/{address}"
    rh_data_path_token_holders: str = "/tokens/{address}/holders"
    rh_data_path_token_transfers: str = "/tokens/{address}/transfers"

    # ------------------------------------- free market data (no API key)
    # DexScreener is the only free source here that reports buy vs sell counts,
    # which is what unblocks buy_ratio_24h. GeckoTerminal is the independent
    # second price source that makes gate_price_agreement satisfiable at all.
    # Both need their own chain slug: the same chain has a different name in
    # every provider's namespace, and a wrong slug prices a different asset.
    dexscreener_enabled: bool = True
    dexscreener_base_url: str = "https://api.dexscreener.com"
    dexscreener_chain_slug: str = ""      # e.g. "ethereum", "base", "arbitrum"
    dexscreener_timeout_s: float = 15.0

    geckoterminal_enabled: bool = True
    geckoterminal_base_url: str = "https://api.geckoterminal.com"
    geckoterminal_network: str = ""       # e.g. "eth", "base", "arbitrum"
    geckoterminal_timeout_s: float = 15.0

    # ------------------------------------------------- explorer (Blockscout)
    # Used only for source-verification status. Free, optional but recommended:
    # unverified contract => hard reject.
    explorer_enabled: bool = True
    explorer_base_url: str = "https://robinhoodchain.blockscout.com"
    explorer_timeout_s: float = 15.0

    # ------------------------------------------------------------- OKX market
    okx_market_enabled: bool = True
    okx_web3_base_url: str = "https://web3.okx.com"
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_api_passphrase: str = ""
    okx_project_id: str = ""  # OK-ACCESS-PROJECT, web3/DEX endpoints only
    okx_market_timeout_s: float = 20.0

    # ------------------------------------------------------------ OKX trading
    okx_cex_base_url: str = "https://www.okx.com"
    okx_trade_api_key: str = ""
    okx_trade_api_secret: str = ""
    okx_trade_api_passphrase: str = ""
    okx_simulated: bool = True  # sends x-simulated-trading: 1
    okx_trade_timeout_s: float = 20.0
    okx_quote_ccy: str = "USDT"

    # --------------------------------------------------------- oracle / price
    chainlink_enabled: bool = False
    # JSON map: {"ETH":"0xfeed...","USDC":"0xfeed..."}
    chainlink_feeds_json: str = "{}"
    # Max relative spread between independent price sources before we refuse to
    # trade (0.05 = 5%).
    price_max_source_divergence: float = 0.05

    # ------------------------------------------------------- ingestion tuning
    ingest_interval_s: int = 300
    score_interval_s: int = 300
    max_tokens_per_cycle: int = 60
    http_max_retries: int = 3
    http_backoff_base_s: float = 1.0
    http_rate_limit_rps: float = 5.0

    # =================================================================
    # HARD RISK GATES — a token failing any of these can never be bought
    # =================================================================
    min_liquidity_usd: float = 150_000.0
    min_liquidity_usd_live: float = 400_000.0
    max_slippage_bps: int = 150          # 1.5% on the intended clip size
    max_spread_bps: int = 80             # 0.8%
    min_volume_24h_usd: float = 100_000.0
    # Volume must not be a single candle. 24h volume / liquidity above this is
    # churn, not organic interest.
    max_volume_to_liquidity_ratio: float = 8.0
    min_volume_to_liquidity_ratio: float = 0.05
    # A single 5m window carrying this share of 24h volume is a spike.
    max_single_window_volume_share: float = 0.35

    max_top1_holder_pct: float = 12.0
    max_top10_holder_pct: float = 40.0
    min_unique_holders: int = 300
    # Holder count going backwards, or exploding implausibly fast, are both bad.
    min_holder_growth_24h_pct: float = -5.0
    max_holder_growth_24h_pct: float = 400.0

    min_token_age_hours: float = 24.0
    max_token_age_hours_for_early: float = 24.0 * 45
    min_tx_count_24h: int = 200

    # Anti-chase. We want base-building, not parabolic.
    max_price_change_24h_pct: float = 60.0
    max_price_vs_7d_base_pct: float = 120.0
    max_price_vs_ath_drawdown_required_pct: float = 15.0  # must be >=15% off ATH

    # Buy/sell imbalance outside this band means one-sided/manipulated flow.
    min_buy_ratio: float = 0.35
    max_buy_ratio: float = 0.72

    # Sniper / bundling detection
    max_sniper_wallet_pct: float = 15.0
    max_bundled_buy_pct: float = 20.0

    # Unlock proximity
    min_days_to_major_unlock: float = 14.0
    major_unlock_pct_of_supply: float = 5.0

    # Stability: how many consecutive passing snapshots before a buy is allowed.
    stability_required_snapshots: int = 3
    stability_max_score_stdev: float = 8.0

    # ------------------------------------------------------- score thresholds
    score_alert_min: float = 60.0
    score_paper_buy_min: float = 72.0
    score_live_buy_min: float = 78.0

    # ------------------------------------------------------- position sizing
    position_usd: float = 25.0
    max_exposure_per_token_usd: float = 50.0
    max_exposure_daily_usd: float = 200.0
    max_open_positions: int = 5
    max_orders_per_day: int = 10
    # Limit price offset below mid for post-only entries (bps).
    limit_offset_bps: int = 25
    order_ttl_s: int = 300
    # Paper-only fill assumption: how far below the bid the market is assumed to
    # trade within ORDER_TTL_S. Optimistic by design — see app/execution/paper.py.
    paper_assumed_touch_bps: float = 60.0

    # --------------------------------------------------------------- safe mode
    # If a reference asset moves more than this in 1h, stop all live buys.
    safe_mode_ref_instrument: str = "BTC-USDT"
    safe_mode_ref_move_1h_pct: float = 4.0

    # ---------------------------------------------------------------- alerting
    alert_console: bool = True
    alert_file: str = "./data/alerts.log"
    alert_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_min_state: str = "ALERT"

    @field_validator("run_mode", mode="before")
    @classmethod
    def _upper(cls, v: str) -> str:
        return str(v).strip().upper()

    @field_validator("rh_chain_id", mode="before")
    @classmethod
    def _blank_int_is_none(cls, v):
        """`RH_CHAIN_ID=` (blank) means "not known yet", not a parse error.

        The shipped .env.example leaves it empty on purpose — the chain ID is
        meant to be read from the node with `eth_chainId` rather than guessed —
        so an empty value has to be valid or the very first run crashes.
        """
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @property
    def live_enabled(self) -> bool:
        return self.run_mode == "LIVE" and not self.kill_switch

    @property
    def paper_enabled(self) -> bool:
        return self.run_mode in ("PAPER", "LIVE")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
