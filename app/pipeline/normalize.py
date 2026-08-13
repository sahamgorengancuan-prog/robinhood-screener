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
import logging
from typing import Any

from app.config import Settings
from app.pipeline import metrics
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
]


class RawBundle:
    """Everything the ingestion layer collected for one token, pre-normalization."""

    def __init__(self, token: TokenRef) -> None:
        self.token = token
        self.okx_price_info: dict[str, Any] | None = None
        self.dexscreener: dict[str, Any] | None = None
        self.geckoterminal: dict[str, Any] | None = None
        self.okx_basic_info: dict[str, Any] | None = None
        self.okx_inst_id: str | None = None
        self.okx_available: bool | None = None
        self.okx_book: tuple[float | None, float | None] = (None, None)
        self.rh_meta: dict[str, Any] | None = None
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

    # -------------------------------------------------------- price consensus
    #
    # Three independent venues can contribute. Two are enough for the median and
    # the divergence check that `gate_price_agreement` requires; with one, that
    # gate degrades to LIVE_ONLY and no amount of score reaches LIVE_BUY.
    price_sources: dict[str, float] = {}
    if okx.get("price_usd"):
        price_sources["okx_market"] = float(okx["price_usd"])
    if bundle.dex_pool_price:
        price_sources["dex_pool"] = float(bundle.dex_pool_price)
    if gt.get("price_usd"):
        price_sources["geckoterminal"] = float(gt["price_usd"])
    if bundle.chainlink_price:
        price_sources["chainlink"] = float(bundle.chainlink_price)

    rec = reconcile_prices(price_sources)
    snap.price_sources = price_sources
    if rec["price"] is not None:
        snap.set_field("price_usd", rec["price"], f"reconciled({rec['chosen_source']})")
    if rec["divergence_pct"] is not None:
        snap.price_divergence_pct = float(rec["divergence_pct"])  # type: ignore[arg-type]
    snap.raw["price_reconciliation"] = rec

    # ---------------------------------------------------------- liquidity mix
    liq_sources: dict[str, float] = {}
    if okx.get("liquidity_usd"):
        liq_sources["okx_market"] = float(okx["liquidity_usd"])
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

    # ------------------------------------------------------------- flow split
    br = metrics.buy_ratio(bundle.buys_24h, bundle.sells_24h)
    snap.set_field("buy_ratio_24h", br, "derived")

    # --------------------------------------------------------- microstructure
    bid, ask = bundle.okx_book
    snap.set_field("spread_bps", metrics.spread_bps(bid, ask), "okx_trade")
    if snap.liquidity_usd is not None:
        snap.set_field(
            "slippage_bps",
            metrics.estimate_slippage_bps(snap.liquidity_usd, c.position_usd),
            "derived(xyk)",
        )
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

    # ----------------------------------------------------------------- venue
    snap.okx_available = bundle.okx_available
    snap.okx_inst_id = bundle.okx_inst_id

    # ------------------------------------------------------------- provenance
    for f in TRACKED_FIELDS:
        if getattr(snap, f, None) is None:
            snap.note_missing(f)

    snap.raw["contract_detail"] = bundle.contract_detail
    return snap


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
