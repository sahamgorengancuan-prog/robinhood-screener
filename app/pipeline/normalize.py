"""Normalization layer.

Takes the heterogeneous payloads every client returns and produces exactly one
`NormalizedSnapshot`. Nothing downstream ever sees a client-specific shape.

Two invariants:
  * a field is only set when a source actually produced it (`set_field` ignores
    None), and every set is recorded in `sources` for the audit trail;
  * anything still unset at the end is appended to `missing_fields`, which is
    what drives the DATA gates.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from app.config import Settings
from app.pipeline import metrics
from app.pipeline.acceleration import acceleration
from app.schemas import NormalizedSnapshot, TokenRef
from app.util.reconcile import reconcile_liquidity, reconcile_prices

log = logging.getLogger(__name__)

# Burn / router / pool addresses must not be counted as "holders" — they are not
# people, and including them wrecks concentration metrics.
NON_HOLDER_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

TRACKED_FIELDS = [
    "price_usd", "liquidity_usd", "volume_24h", "volume_1h", "unique_holders",
    "top1_holder_pct", "top10_holder_pct", "holder_growth_24h_pct", "tx_count_24h",
    "token_age_hours", "price_change_24h_pct", "slippage_bps", "total_supply",
    "unique_trader_ratio", "top_trader_volume_pct", "filtered_trade_pct",
]


class RawBundle:
    """Everything the ingestion layer collected for one token, pre-normalization."""

    def __init__(self, token: TokenRef) -> None:
        self.token = token
        self.okx_price_info: dict[str, Any] | None = None
        self.okx_hot: dict[str, Any] | None = None
        self.dexscreener: dict[str, Any] | None = None
        self.geckoterminal: dict[str, Any] | None = None
        self.okx_basic_info: dict[str, Any] | None = None
        self.okx_advanced_info: dict[str, Any] | None = None
        self.okx_holders: list[dict[str, Any]] = []
        self.okx_trades: list[dict[str, Any]] = []
        self.okx_liquidity: list[dict[str, Any]] = []
        self.okx_inst_id: str | None = None
        self.okx_available: bool | None = None
        self.okx_identity_reason: str | None = None
        self.okx_book: tuple[float | None, float | None] = (None, None)
        self.okx_book_slippage_bps: float | None = None
        self.rh_meta: dict[str, Any] | None = None
        self.rh_transfers: list[dict[str, Any]] = []
        self.latest_block: int | None = None
        self.rh_holders: dict[str, Any] | None = None
        self.explorer_holders: list[dict[str, Any]] | None = None
        self.explorer_counters: dict[str, Any] | None = None
        self.contract_verified: bool | None = None
        self.contract_flags: list[str] = []
        self.contract_detail: dict[str, Any] = {}
        self.onchain_supply: float | None = None
        self.onchain_decimals: int | None = None
        self.chainlink_price: float | None = None
        self.dex_pool_price: float | None = None
        self.dex_pool_liquidity_usd: float | None = None
        self.deployed_at: dt.datetime | None = None
        self.holder_balances: list[float] = []
        self.holder_basis_is_partial: bool = True
        self.buys_24h: int | None = None
        self.holder_series: list = []
        self.buy_count_series: list = []
        self.sells_24h: int | None = None
        # History injected by the caller from previous snapshots.
        self.prev_holders: int | None = None
        self.prev_holders_at: dt.datetime | None = None
        self.price_history_7d: list[float] = []


def normalize(bundle: RawBundle, c: Settings, now: dt.datetime | None = None) -> NormalizedSnapshot:
    now = now or dt.datetime.now(dt.timezone.utc)
    snap = NormalizedSnapshot(token=bundle.token, captured_at=now)

    # ------------------------------------------------------------ market data
    #
    # Precedence is deliberate. `set_field` ignores None and does NOT overwrite,
    # so whichever source is applied FIRST wins for a given field. DexScreener
    # goes first because it is the only source reporting per-window volume and
    # buy/sell counts, and mixing windows from different providers would make
    # the spike and turnover gates compare unlike numbers.
    from app.clients.base import to_float, to_int

    hot: dict[str, Any] = bundle.okx_hot or {}
    hot_price = to_float(hot.get("price")) if hot else None
    hot_map: dict[str, float | int | None] = {
        "liquidity_usd": to_float(hot.get("liquidity")),
        "volume_24h": to_float(hot.get("volume")),
        "tx_count_24h": to_int(hot.get("txs")),
        "unique_holders": to_int(hot.get("holders")),
        "market_cap_usd": to_float(hot.get("marketCap")),
        "price_change_24h_pct": to_float(hot.get("change")),
    } if hot else {}

    ds: dict[str, Any] = bundle.dexscreener or {}
    if ds:
        for field in (
            "volume_5m", "volume_1h", "volume_24h", "tx_count_24h",
            "price_change_1h_pct", "price_change_24h_pct", "market_cap_usd", "fdv_usd",
        ):
            snap.set_field(field, ds.get(field), "dexscreener")

    gt: dict[str, Any] = bundle.geckoterminal or {}
    if gt:
        for field in ("volume_24h", "market_cap_usd", "fdv_usd"):
            snap.set_field(field, gt.get(field), "geckoterminal")

    okx: dict[str, Any] = {}
    if bundle.okx_price_info:
        from app.clients.okx_market import OKXMarketClient

        okx = OKXMarketClient.extract_market(bundle.okx_price_info)
        for field in (
            "liquidity_usd", "volume_5m", "volume_1h", "volume_24h", "market_cap_usd",
            "fdv_usd", "circulating_supply", "unique_holders", "price_change_1h_pct",
            "price_change_24h_pct", "tx_count_24h",
        ):
            snap.set_field(field, okx.get(field), "okx_market")

    # The hot-token response is a discovery payload, not the preferred market
    # snapshot. Apply it last as a Basic-tier fallback so it cannot splice its
    # 24h values into DexScreener/price-info's shorter time windows.
    for field, value in hot_map.items():
        if field != "liquidity_usd":
            snap.set_field(field, value, "okx_hot_token")
    if hot:
        snap.set_field("top10_holder_pct", to_float(hot.get("top10HoldPercent")), "okx_hot_token")
        snap.set_field("bundled_buy_pct", to_float(hot.get("bundleHoldPercent")), "okx_hot_token")

    # -------------------------------------------------------- price consensus
    #
    # Three independent venues can contribute. Two are enough for the median and
    # the divergence check that `gate_price_agreement` requires; with one, that
    # gate degrades to LIVE_ONLY and no amount of score reaches LIVE_BUY.
    price_sources: dict[str, float] = {}
    if okx.get("price_usd"):
        price_sources["okx_market"] = float(okx["price_usd"])
    elif hot_price:
        price_sources["okx_market"] = float(hot_price)
    if bundle.dex_pool_price:
        price_sources["dex_pool"] = float(bundle.dex_pool_price)
    if gt.get("price_usd"):
        price_sources["geckoterminal"] = float(gt["price_usd"])
    if bundle.chainlink_price:
        price_sources["chainlink"] = float(bundle.chainlink_price)
    book_bid, book_ask = bundle.okx_book
    if bundle.okx_available and book_bid and book_ask:
        price_sources["okx_cex_book"] = (book_bid + book_ask) / 2.0

    rec = reconcile_prices(price_sources)
    snap.price_sources = price_sources
    if rec["price"] is not None:
        snap.set_field("price_usd", rec["price"], f"reconciled({rec['chosen_source']})")
    if rec["divergence_pct"] is not None:
        snap.price_divergence_pct = float(rec["divergence_pct"])  # type: ignore[arg-type]
    snap.raw["price_reconciliation"] = rec

    # ---------------------------------------------------------- liquidity mix
    liq_sources: dict[str, float] = {}
    okx_liquidity = okx.get("liquidity_usd") or hot_map.get("liquidity_usd")
    if okx_liquidity:
        liq_sources["okx_market"] = float(okx_liquidity)
    if bundle.dex_pool_liquidity_usd:
        liq_sources["dex_pool"] = float(bundle.dex_pool_liquidity_usd)
    if gt.get("liquidity_usd"):
        liq_sources["geckoterminal"] = float(gt["liquidity_usd"])
    merged_liq = reconcile_liquidity(liq_sources)
    if merged_liq is not None:
        snap.set_field("liquidity_usd", merged_liq, "reconciled(min)")
        snap.raw["liquidity_sources"] = liq_sources

    # ------------------------------------------------------------ supply/meta
    # On-chain totalSupply() wins: it is the only one that cannot be wrong.
    supply = bundle.onchain_supply
    supply_src = "rh_node"
    if supply is None and gt.get("total_supply"):
        supply, supply_src = float(gt["total_supply"]), "geckoterminal"
    if supply is None and okx.get("total_supply"):
        supply, supply_src = float(okx["total_supply"]), "okx_market"
    snap.set_field("total_supply", supply, supply_src)
    if bundle.onchain_decimals is not None:
        snap.token.decimals = bundle.onchain_decimals

    # ---------------------------------------------------------------- holders
    # OKX's holdPercent is already percentage points (e.g. 76.75 means
    # 76.75%), not a 0..1 ratio. Pool and burn addresses are excluded.
    pool_addresses = {
        str(row.get("poolAddress") or "").lower() for row in bundle.okx_liquidity
        if row.get("poolAddress")
    }
    okx_holder_pcts = []
    if bundle.okx_holders:
        from app.clients.base import to_float

        for holder in bundle.okx_holders:
            address = str(holder.get("holderWalletAddress") or "").lower()
            pct = to_float(holder.get("holdPercent"))
            if pct is not None and address not in NON_HOLDER_ADDRESSES and address not in pool_addresses:
                okx_holder_pcts.append(pct)
        okx_holder_pcts.sort(reverse=True)
        if okx_holder_pcts:
            snap.set_field("top1_holder_pct", okx_holder_pcts[0], "okx_holder")
            snap.set_field("top10_holder_pct", sum(okx_holder_pcts[:10]), "okx_holder")
            snap.set_field("whale_concentration", sum(p for p in okx_holder_pcts if p >= 1.0), "okx_holder")

    balances = [b for b in bundle.holder_balances if b and b > 0]
    if balances:
        dist = metrics.holder_pcts(balances, snap.total_supply)
        # A partial holder list yields a lower bound on concentration. Reporting
        # it as if complete would understate risk, so we only publish it when the
        # basis is the real total supply.
        if dist["basis"] == "total_supply":
            snap.set_field("top1_holder_pct", dist["top1_pct"], "holders")
            snap.set_field("top10_holder_pct", dist["top10_pct"], "holders")
            snap.set_field("whale_concentration", dist["whale_concentration"], "holders")
        else:
            snap.raw["holder_distribution_partial"] = dist

    if bundle.rh_holders and bundle.rh_holders.get("total") is not None:
        snap.set_field("unique_holders", bundle.rh_holders["total"], "rh_data")
    if snap.unique_holders is None and bundle.explorer_counters:
        from app.clients.base import to_int

        snap.set_field("unique_holders", to_int(bundle.explorer_counters.get("token_holders_count")), "explorer")

    # holder growth needs two observations separated in time
    if (
        snap.unique_holders is not None
        and bundle.prev_holders
        and bundle.prev_holders > 0
        and bundle.prev_holders_at is not None
    ):
        hours = (now - bundle.prev_holders_at).total_seconds() / 3600.0
        if hours >= 1.0:
            raw_growth = (snap.unique_holders - bundle.prev_holders) / bundle.prev_holders * 100.0
            snap.set_field("holder_growth_24h_pct", raw_growth * (24.0 / hours), "derived")

    # ---------------------------------------------------------- acceleration
    # Compared over two equal 24h windows. Equal length is the point: a 1h rate
    # measured against a 24h rate describes the windows, not the token.
    window = dt.timedelta(hours=24)
    holder_accel = acceleration(bundle.holder_series, now, window)
    snap.set_field("holder_growth_prev_pct", holder_accel["prev_growth_pct"], "derived")
    snap.set_field("holder_acceleration_pp", holder_accel["acceleration_pp"], "derived")

    buy_accel = acceleration(bundle.buy_count_series, now, window)
    snap.set_field("buy_count_growth_pct", buy_accel["growth_pct"], "derived")
    snap.set_field("buy_count_growth_prev_pct", buy_accel["prev_growth_pct"], "derived")
    snap.set_field("buy_count_acceleration_pp", buy_accel["acceleration_pp"], "derived")

    # ------------------------------------------------------------- flow split
    snap.set_field("buys_24h", bundle.buys_24h, "derived")
    snap.set_field("sells_24h", bundle.sells_24h, "derived")
    br = metrics.buy_ratio(bundle.buys_24h, bundle.sells_24h)
    snap.set_field("buy_ratio_24h", br, "derived")
    trade_quality = derive_trade_quality(bundle.okx_trades)
    for field in ("trade_sample_size", "unique_trader_ratio", "top_trader_volume_pct", "filtered_trade_pct"):
        snap.set_field(field, trade_quality.get(field), "okx_trades")
    if snap.buy_ratio_24h is None and trade_quality.get("sample_buy_ratio") is not None:
        snap.set_field("buy_ratio_24h", trade_quality["sample_buy_ratio"], "okx_trades_sample")

    # --------------------------------------------------------- microstructure
    bid, ask = bundle.okx_book
    snap.set_field("spread_bps", metrics.spread_bps(bid, ask), "okx_trade")
    snap.set_field("slippage_bps", bundle.okx_book_slippage_bps, "okx_trade_book_vwap")
    if snap.slippage_bps is None and snap.liquidity_usd is not None:
        snap.set_field(
            "slippage_bps",
            metrics.estimate_slippage_bps(snap.liquidity_usd, c.position_usd),
            "derived(xyk)",
        )
    if snap.slippage_bps is not None:
        snap.slippage_notional_usd = c.position_usd

    # -------------------------------------------------------------------- age
    if bundle.deployed_at:
        snap.set_field("token_age_hours", (now - bundle.deployed_at).total_seconds() / 3600.0, "rh_node")

    # -------------------------------------------------------------- momentum
    if bundle.price_history_7d and snap.price_usd:
        snap.set_field(
            "price_vs_7d_base_pct",
            metrics.price_vs_base(snap.price_usd, bundle.price_history_7d),
            "derived",
        )
        snap.set_field(
            "drawdown_from_ath_pct",
            metrics.drawdown_from_high(snap.price_usd, bundle.price_history_7d),
            "derived",
        )

    # ------------------------------------------------------------------ risk
    snap.contract_flags = list(bundle.contract_flags)
    snap.contract_verified = bundle.contract_verified
    snap.is_proxy = bundle.contract_detail.get("is_proxy")
    snap.tokenomics_flags = derive_tokenomics_flags(snap, bundle)
    apply_advanced_risk(snap, bundle.okx_advanced_info)
    apply_tokenomics_override(snap, c)

    # ----------------------------------------------------------------- venue
    snap.okx_available = bundle.okx_available
    snap.okx_inst_id = bundle.okx_inst_id
    snap.okx_identity_reason = bundle.okx_identity_reason

    # ------------------------------------------------------------- provenance
    for f in TRACKED_FIELDS:
        if getattr(snap, f, None) is None:
            snap.note_missing(f)

    snap.raw["contract_detail"] = bundle.contract_detail
    snap.raw["okx_identity_reason"] = bundle.okx_identity_reason
    snap.raw["trade_sample_size"] = len(bundle.okx_trades)
    snap.raw["alchemy_transfer_sample_size"] = len(bundle.rh_transfers)
    return snap


def derive_trade_quality(trades: list[dict[str, Any]]) -> dict[str, float | int | None]:
    """Sample-based wash indicators from up to 500 official OKX trades."""
    from app.clients.base import to_float

    if not trades:
        return {
            "trade_sample_size": None, "unique_trader_ratio": None,
            "top_trader_volume_pct": None, "filtered_trade_pct": None,
            "sample_buy_ratio": None,
        }
    wallet_volume: dict[str, float] = {}
    total_usd = 0.0
    buys = sells = filtered = 0
    for trade in trades:
        wallet = str(trade.get("userAddress") or "").lower()
        usd = to_float(trade.get("volume")) or 0.0
        if wallet:
            wallet_volume[wallet] = wallet_volume.get(wallet, 0.0) + usd
        total_usd += usd
        side = str(trade.get("type") or "").lower()
        buys += side == "buy"
        sells += side == "sell"
        filtered += str(trade.get("isFiltered") or "0") == "1"
    n = len(trades)
    return {
        "trade_sample_size": n,
        "unique_trader_ratio": len(wallet_volume) / n if n else None,
        "top_trader_volume_pct": (
            max(wallet_volume.values(), default=0.0) / total_usd * 100.0 if total_usd > 0 else None
        ),
        "filtered_trade_pct": filtered / n * 100.0 if n else None,
        "sample_buy_ratio": buys / (buys + sells) if buys + sells else None,
    }


def apply_advanced_risk(snap: NormalizedSnapshot, advanced: dict[str, Any] | None) -> None:
    if not advanced:
        return
    from app.clients.base import to_float, to_int

    snap.set_field("top10_holder_pct", to_float(advanced.get("top10HoldPercent")), "okx_advanced")
    snap.set_field("sniper_wallet_pct", to_float(advanced.get("sniperHoldingPercent")), "okx_advanced")
    snap.set_field("bundled_buy_pct", to_float(advanced.get("bundleHoldingPercent")), "okx_advanced")
    snap.set_field("suspicious_holder_pct", to_float(advanced.get("suspiciousHoldingPercent")), "okx_advanced")
    def add_contract_flag(flag: str) -> None:
        # Idempotent: this may run more than once for a snapshot (retry, or a
        # re-normalise during inspection), and "HONEYPOT, HONEYPOT" in an alert
        # reads like two findings instead of one.
        if flag not in snap.contract_flags:
            snap.contract_flags.append(flag)

    tags = {str(tag) for tag in (advanced.get("tokenTags") or [])}
    if "honeypot" in tags:
        add_contract_flag("HONEYPOT")
    if to_int(advanced.get("devRugPullTokenCount")) not in (None, 0):
        add_contract_flag("DEVELOPER_RUG_HISTORY")
    if (to_int(advanced.get("riskControlLevel")) or 0) >= 4:
        add_contract_flag("OKX_HIGH_RISK")
    if "lowLiquidity" in tags and "CRITICAL_OKX_LOW_LIQUIDITY" not in snap.tokenomics_flags:
        snap.tokenomics_flags.append("CRITICAL_OKX_LOW_LIQUIDITY")
    snap.raw["okx_advanced"] = advanced


def apply_tokenomics_override(snap: NormalizedSnapshot, c: Settings) -> None:
    """Apply only operator-reviewed, source-backed unlock data."""
    try:
        overrides = json.loads(c.tokenomics_overrides_json or "{}")
    except json.JSONDecodeError:
        snap.tokenomics_flags.append("CRITICAL_TOKENOMICS_OVERRIDE_INVALID")
        return
    item = overrides.get(snap.token.address.lower()) if isinstance(overrides, dict) else None
    if not isinstance(item, dict) or not item.get("source_url"):
        return
    from app.clients.base import to_float

    unlock_pct = to_float(item.get("unlock_pct"))
    if unlock_pct is None:
        snap.tokenomics_flags.append("CRITICAL_UNLOCK_PERCENT_UNKNOWN")
        return
    snap.raw["next_unlock_pct"] = unlock_pct
    if unlock_pct < c.major_unlock_pct_of_supply:
        # The row describes a minor unlock, not the next major one. It cannot
        # prove that no larger cliff occurs sooner, so leave the gate unresolved.
        return
    raw_at = item.get("next_unlock_at")
    if raw_at:
        try:
            when = dt.datetime.fromisoformat(str(raw_at).replace("Z", "+00:00"))
            snap.days_to_major_unlock = (when - snap.captured_at).total_seconds() / 86400.0
            snap.sources["days_to_major_unlock"] = f"manual_review:{item['source_url']}"
        except (TypeError, ValueError):
            snap.tokenomics_flags.append("CRITICAL_UNLOCK_OVERRIDE_INVALID")


def derive_tokenomics_flags(snap: NormalizedSnapshot, bundle: RawBundle) -> list[str]:
    """Tokenomics red flags derivable from data we actually have.

    Deliberately narrow. Vesting schedules, team allocations and treasury splits
    are not on-chain-discoverable in the general case, so this function does not
    pretend to evaluate them — `days_to_major_unlock` stays None and the unlock
    gate degrades to LIVE_ONLY rather than inventing a verdict.
    """
    flags: list[str] = []

    if snap.total_supply is None:
        flags.append("CRITICAL_SUPPLY_UNKNOWN")
        return flags

    if snap.total_supply <= 0:
        flags.append("CRITICAL_SUPPLY_ZERO")

    # Circulating far below total means a large locked overhang exists somewhere.
    if snap.circulating_supply and snap.total_supply:
        circ_ratio = snap.circulating_supply / snap.total_supply
        if circ_ratio < 0.15:
            flags.append("LOW_CIRCULATING_FLOAT")

    # FDV wildly above market cap is the same problem expressed in dollars.
    if snap.fdv_usd and snap.market_cap_usd and snap.market_cap_usd > 0:
        if snap.fdv_usd / snap.market_cap_usd > 6.0:
            flags.append("FDV_OVERHANG")

    # Liquidity that is a rounding error against market cap cannot support exits.
    if snap.liquidity_usd and snap.market_cap_usd and snap.market_cap_usd > 0:
        if snap.liquidity_usd / snap.market_cap_usd < 0.01:
            flags.append("CRITICAL_LIQUIDITY_TO_MCAP")

    return flags
