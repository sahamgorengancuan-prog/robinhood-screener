"""Risk gate layer.

A gate is a pure function `(snapshot, settings) -> GateResult`. Four severities:

  HARD       — token is rejected outright, no score can rescue it.
  LIVE_ONLY  — may still ALERT or PAPER_BUY, but never LIVE_BUY.
  SOFT       — informational; the scoring layer applies the penalty.
  DATA       — a non-critical metric is unavailable, so the token goes to WATCH.

The missing-data distinction is load-bearing. Explicitly required metrics such
as liquidity, holder concentration, verification, contract scan, supply, and
unlock coverage fail as HARD; other unavailable metrics fail as DATA. A
screener that treats "unknown liquidity" the same as "adequate liquidity" will
eventually buy a honeypot. Here
an unknown value can never satisfy a gate.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings
from app.schemas import GateResult, NormalizedSnapshot, Severity

Gate = Callable[[NormalizedSnapshot, Settings], GateResult]

REGISTRY: list[Gate] = []


def gate(fn: Gate) -> Gate:
    REGISTRY.append(fn)
    return fn


def _missing(name: str, field: str) -> GateResult:
    return GateResult(
        name=name,
        passed=False,
        severity=Severity.DATA,
        reason=f"{field} unavailable — cannot evaluate (unknown is never treated as safe)",
    )


def _critical_missing(name: str, field: str) -> GateResult:
    return GateResult(
        name=name,
        passed=False,
        severity=Severity.HARD,
        reason=f"{field} unavailable — explicit fail-closed requirement",
    )


def _ok(name: str, reason: str, value=None, threshold=None) -> GateResult:
    return GateResult(name=name, passed=True, severity=Severity.SOFT, reason=reason,
                      value=value, threshold=threshold)


def _fail(name: str, sev: Severity, reason: str, value=None, threshold=None) -> GateResult:
    return GateResult(name=name, passed=False, severity=sev, reason=reason,
                      value=value, threshold=threshold)


# --------------------------------------------------------------------- liquidity
@gate
def gate_liquidity(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.liquidity_usd is None:
        return _critical_missing("liquidity_min", "liquidity_usd")
    if s.liquidity_usd < c.min_liquidity_usd:
        return _fail("liquidity_min", Severity.HARD,
                     f"liquidity ${s.liquidity_usd:,.0f} below floor ${c.min_liquidity_usd:,.0f}",
                     s.liquidity_usd, c.min_liquidity_usd)
    if s.liquidity_usd < c.min_liquidity_usd_live:
        return _fail("liquidity_live_min", Severity.LIVE_ONLY,
                     f"liquidity ${s.liquidity_usd:,.0f} below live-trade floor "
                     f"${c.min_liquidity_usd_live:,.0f} — alert/paper only",
                     s.liquidity_usd, c.min_liquidity_usd_live)
    return _ok("liquidity_min", f"liquidity ${s.liquidity_usd:,.0f}", s.liquidity_usd, c.min_liquidity_usd)


@gate
def gate_slippage(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.slippage_bps is None:
        return _missing("slippage_max", "slippage_bps")
    if s.slippage_bps > c.max_slippage_bps:
        return _fail("slippage_max", Severity.HARD,
                     f"estimated slippage {s.slippage_bps:.0f}bps on ${s.slippage_notional_usd or 0:,.0f} "
                     f"exceeds {c.max_slippage_bps}bps",
                     s.slippage_bps, c.max_slippage_bps)
    return _ok("slippage_max", f"slippage {s.slippage_bps:.0f}bps", s.slippage_bps, c.max_slippage_bps)


@gate
def gate_spread(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.spread_bps is None:
        # Spread is only measurable on a CEX book. Its absence blocks live
        # trading but should not reject an on-chain-only token outright.
        return _fail("spread_max", Severity.LIVE_ONLY,
                     "spread unavailable (no OKX book) — on-chain only / manual review")
    if s.spread_bps > c.max_spread_bps:
        return _fail("spread_max", Severity.LIVE_ONLY,
                     f"spread {s.spread_bps:.0f}bps exceeds {c.max_spread_bps}bps",
                     s.spread_bps, c.max_spread_bps)
    return _ok("spread_max", f"spread {s.spread_bps:.0f}bps", s.spread_bps, c.max_spread_bps)


# ------------------------------------------------------------------------ volume
@gate
def gate_volume_floor(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.volume_24h is None:
        return _missing("volume_min", "volume_24h")
    if s.volume_24h < c.min_volume_24h_usd:
        return _fail("volume_min", Severity.HARD,
                     f"24h volume ${s.volume_24h:,.0f} below floor ${c.min_volume_24h_usd:,.0f}",
                     s.volume_24h, c.min_volume_24h_usd)
    return _ok("volume_min", f"24h volume ${s.volume_24h:,.0f}", s.volume_24h, c.min_volume_24h_usd)


@gate
def gate_volume_organic(s: NormalizedSnapshot, c: Settings) -> GateResult:
    """Wash-trading screen: volume/liquidity turnover outside a plausible band."""
    if s.volume_24h is None or s.liquidity_usd is None or s.liquidity_usd <= 0:
        return _missing("volume_organic", "volume_24h/liquidity_usd")
    ratio = s.volume_24h / s.liquidity_usd
    if ratio > c.max_volume_to_liquidity_ratio:
        return _fail("volume_organic", Severity.HARD,
                     f"turnover {ratio:.1f}x liquidity in 24h — wash-trading / churn pattern",
                     round(ratio, 2), c.max_volume_to_liquidity_ratio)
    if ratio < c.min_volume_to_liquidity_ratio:
        return _fail("volume_organic", Severity.HARD,
                     f"turnover {ratio:.3f}x liquidity — effectively dead book",
                     round(ratio, 3), c.min_volume_to_liquidity_ratio)
    return _ok("volume_organic", f"turnover {ratio:.2f}x liquidity", round(ratio, 2))


@gate
def gate_volume_spike(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.volume_24h is None or s.volume_24h <= 0:
        return _missing("volume_spike", "volume_24h")
    window = s.volume_1h if s.volume_1h is not None else s.volume_5m
    if window is None:
        return _fail("volume_spike", Severity.LIVE_ONLY,
                     "no intraday volume window — cannot rule out a single-candle spike")
    share = window / s.volume_24h
    if share > c.max_single_window_volume_share:
        return _fail("volume_spike", Severity.HARD,
                     f"{share*100:.0f}% of 24h volume sits in one recent window — spike, not accumulation",
                     round(share, 3), c.max_single_window_volume_share)
    return _ok("volume_spike", f"recent window is {share*100:.0f}% of 24h volume", round(share, 3))


@gate
def gate_tx_count(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.tx_count_24h is None:
        return _missing("tx_count_min", "tx_count_24h")
    if s.tx_count_24h < c.min_tx_count_24h:
        return _fail("tx_count_min", Severity.HARD,
                     f"only {s.tx_count_24h} transfers in 24h — too few to call the volume real",
                     s.tx_count_24h, c.min_tx_count_24h)
    return _ok("tx_count_min", f"{s.tx_count_24h} transfers in 24h", s.tx_count_24h)


@gate
def gate_buy_sell_balance(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.buy_ratio_24h is None:
        return _fail("buy_sell_balance", Severity.LIVE_ONLY, "buy/sell split unavailable")
    if s.buy_ratio_24h < c.min_buy_ratio:
        return _fail("buy_sell_balance", Severity.HARD,
                     f"buy share {s.buy_ratio_24h*100:.0f}% — persistent distribution",
                     round(s.buy_ratio_24h, 3), c.min_buy_ratio)
    if s.buy_ratio_24h > c.max_buy_ratio:
        return _fail("buy_sell_balance", Severity.HARD,
                     f"buy share {s.buy_ratio_24h*100:.0f}% is implausibly one-sided — likely manufactured flow",
                     round(s.buy_ratio_24h, 3), c.max_buy_ratio)
    return _ok("buy_sell_balance", f"buy share {s.buy_ratio_24h*100:.0f}%", round(s.buy_ratio_24h, 3))


@gate
def gate_wash_sample(s: NormalizedSnapshot, c: Settings) -> GateResult:
    """Recent-trade sample screen; not a claim about the full 24h population."""
    if not s.trade_sample_size or s.trade_sample_size < 30:
        return _missing("wash_sample", "at least 30 recent OKX trades")
    if s.filtered_trade_pct is None or s.unique_trader_ratio is None or s.top_trader_volume_pct is None:
        return _missing("wash_sample", "trade quality fields")
    if s.filtered_trade_pct > c.max_filtered_trade_pct:
        return _fail("wash_sample", Severity.HARD,
                     f"{s.filtered_trade_pct:.1f}% of sampled trades filtered by OKX",
                     s.filtered_trade_pct, c.max_filtered_trade_pct)
    if s.unique_trader_ratio < c.min_unique_trader_ratio:
        return _fail("wash_sample", Severity.HARD,
                     f"unique-trader/trade ratio {s.unique_trader_ratio:.3f} is too low",
                     s.unique_trader_ratio, c.min_unique_trader_ratio)
    if s.top_trader_volume_pct > c.max_top_trader_volume_pct:
        return _fail("wash_sample", Severity.HARD,
                     f"one wallet generated {s.top_trader_volume_pct:.1f}% of sampled USD volume",
                     s.top_trader_volume_pct, c.max_top_trader_volume_pct)
    return _ok("wash_sample", f"{s.trade_sample_size} trades; wallet mix is plausible")


# ----------------------------------------------------------------------- holders
@gate
def gate_holder_count(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.unique_holders is None:
        return _critical_missing("holders_min", "unique_holders")
    if s.unique_holders < c.min_unique_holders:
        return _fail("holders_min", Severity.HARD,
                     f"{s.unique_holders} holders below floor {c.min_unique_holders}",
                     s.unique_holders, c.min_unique_holders)
    return _ok("holders_min", f"{s.unique_holders} holders", s.unique_holders)


@gate
def gate_top1(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.top1_holder_pct is None:
        return _critical_missing("top1_concentration", "top1_holder_pct")
    if s.top1_holder_pct > c.max_top1_holder_pct:
        return _fail("top1_concentration", Severity.HARD,
                     f"top holder controls {s.top1_holder_pct:.1f}% (max {c.max_top1_holder_pct}%)",
                     s.top1_holder_pct, c.max_top1_holder_pct)
    return _ok("top1_concentration", f"top holder {s.top1_holder_pct:.1f}%", s.top1_holder_pct)


@gate
def gate_top10(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.top10_holder_pct is None:
        return _critical_missing("top10_concentration", "top10_holder_pct")
    if s.top10_holder_pct > c.max_top10_holder_pct:
        return _fail("top10_concentration", Severity.HARD,
                     f"top-10 control {s.top10_holder_pct:.1f}% (max {c.max_top10_holder_pct}%)",
                     s.top10_holder_pct, c.max_top10_holder_pct)
    return _ok("top10_concentration", f"top-10 {s.top10_holder_pct:.1f}%", s.top10_holder_pct)


@gate
def gate_holder_growth(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.holder_growth_24h_pct is None:
        return _fail("holder_growth", Severity.DATA,
                     "holder growth unavailable — needs at least two snapshots 24h apart")
    if s.holder_growth_24h_pct < c.min_holder_growth_24h_pct:
        return _fail("holder_growth", Severity.HARD,
                     f"holders shrinking {s.holder_growth_24h_pct:.1f}% in 24h",
                     s.holder_growth_24h_pct, c.min_holder_growth_24h_pct)
    if s.holder_growth_24h_pct > c.max_holder_growth_24h_pct:
        return _fail("holder_growth", Severity.HARD,
                     f"holders +{s.holder_growth_24h_pct:.0f}% in 24h — airdrop farming or sybil inflation",
                     s.holder_growth_24h_pct, c.max_holder_growth_24h_pct)
    return _ok("holder_growth", f"holder growth {s.holder_growth_24h_pct:+.1f}%/24h", s.holder_growth_24h_pct)


@gate
def gate_sniper(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.sniper_wallet_pct is None:
        return _fail("sniper_domination", Severity.LIVE_ONLY, "sniper share unmeasured")
    if s.sniper_wallet_pct > c.max_sniper_wallet_pct:
        return _fail("sniper_domination", Severity.HARD,
                     f"first-block snipers still hold {s.sniper_wallet_pct:.1f}%",
                     s.sniper_wallet_pct, c.max_sniper_wallet_pct)
    return _ok("sniper_domination", f"sniper holdings {s.sniper_wallet_pct:.1f}%", s.sniper_wallet_pct)


@gate
def gate_bundling(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.bundled_buy_pct is None:
        return _fail("bundling", Severity.LIVE_ONLY, "bundled-buy share unmeasured")
    if s.bundled_buy_pct > c.max_bundled_buy_pct:
        return _fail("bundling", Severity.HARD,
                     f"{s.bundled_buy_pct:.1f}% of supply acquired in bundled same-block buys",
                     s.bundled_buy_pct, c.max_bundled_buy_pct)
    return _ok("bundling", f"bundled buys {s.bundled_buy_pct:.1f}%", s.bundled_buy_pct)


@gate
def gate_suspicious_holders(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.suspicious_holder_pct is None:
        return _fail("suspicious_holders", Severity.LIVE_ONLY, "suspicious-holder share unmeasured")
    if s.suspicious_holder_pct > c.max_suspicious_holder_pct:
        return _fail("suspicious_holders", Severity.HARD,
                     f"suspicious wallets hold {s.suspicious_holder_pct:.1f}%",
                     s.suspicious_holder_pct, c.max_suspicious_holder_pct)
    return _ok("suspicious_holders", f"suspicious wallets {s.suspicious_holder_pct:.1f}%")


# -------------------------------------------------------------------- contract
@gate
def gate_contract_verified(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.contract_verified is None:
        return _critical_missing("contract_verified", "contract source verification")
    if not s.contract_verified:
        return _fail("contract_verified", Severity.HARD, "contract source is not verified")
    return _ok("contract_verified", "contract source verified")


CRITICAL_CONTRACT_FLAGS = {
    "NOT_A_CONTRACT",
    "OWNER_CAN_MINT",
    "BLACKLIST",
    "SUSPICIOUSLY_SMALL_BYTECODE",
    "HONEYPOT",
    "DEVELOPER_RUG_HISTORY",
    "OKX_HIGH_RISK",
}
LIVE_BLOCKING_CONTRACT_FLAGS = {"UPGRADEABLE_PROXY", "PAUSABLE", "MUTABLE_FEES", "MUTABLE_LIMITS"}


@gate
def gate_tradeable_token(s: NormalizedSnapshot, c: Settings) -> GateResult:
    """Reject contracts that are not tokens anyone trades.

    Discovery walks mint events, which surfaces liquidity-pool shares (UNI-V2),
    ERC-4626 vault receipts and bridge wrappers alongside real tokens. Screening
    those produces confident nonsense: a pair token legitimately has one holder
    at 100% of supply and no pool of its own, so every holder and liquidity gate
    fires for reasons that have nothing to do with risk.

    The kind is read off the runtime bytecode, so this costs no extra RPC call
    and is a fact about the contract rather than a guess from its symbol.
    """
    if not s.contract_flags and s.is_proxy is None:
        # Same fail-closed condition as gate_contract_flags: an empty flag list
        # means "scan produced nothing", which is not the same as "scan passed".
        return _critical_missing("tradeable_token", "contract bytecode scan")
    kinds = [f for f in s.contract_flags if f.startswith("NOT_A_TRADEABLE_TOKEN:")]
    if kinds:
        kind = kinds[0].split(":", 1)[1]
        label = {
            "LP_SHARE": "a liquidity-pool share (answers token0()/token1())",
            "VAULT_SHARE": "an ERC-4626 vault receipt (answers asset())",
        }.get(kind, kind)
        return _fail("tradeable_token", Severity.HARD,
                     f"not a tradeable token — this contract is {label}", kind)
    return _ok("tradeable_token", "contract looks like a plain token")


@gate
def gate_contract_flags(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if not s.contract_flags and s.is_proxy is None:
        return _critical_missing("contract_flags", "contract bytecode scan")
    critical = sorted(set(s.contract_flags) & CRITICAL_CONTRACT_FLAGS)
    if critical:
        return _fail("contract_flags", Severity.HARD,
                     f"dangerous contract privileges: {', '.join(critical)}", critical)
    blocking = sorted(set(s.contract_flags) & LIVE_BLOCKING_CONTRACT_FLAGS)
    if blocking:
        return _fail("contract_flags", Severity.LIVE_ONLY,
                     f"mutable contract surface: {', '.join(blocking)} — alert only", blocking)
    return _ok("contract_flags", "no dangerous contract privileges detected", s.contract_flags)


@gate
def gate_tokenomics(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.total_supply is None:
        return _critical_missing("tokenomics", "total_supply")
    critical = [f for f in s.tokenomics_flags if f.startswith("CRITICAL_")]
    if critical:
        return _fail("tokenomics", Severity.HARD, f"tokenomics red flags: {', '.join(critical)}", critical)
    if s.tokenomics_flags:
        return _fail("tokenomics", Severity.SOFT,
                     f"tokenomics concerns: {', '.join(s.tokenomics_flags)}", s.tokenomics_flags)
    return _ok("tokenomics", "no tokenomics red flags")


@gate
def gate_unlock(s: NormalizedSnapshot, c: Settings) -> GateResult:
    """Unlock schedules are not on-chain-discoverable in the general case.

    We only act on this when a schedule is actually known. Unknown means we do
    not claim a token is safe from unlocks — it blocks live buying but does not
    fabricate a rejection reason.
    """
    if s.days_to_major_unlock is None:
        return _critical_missing("unlock_proximity", "reviewed major-unlock schedule")
    if s.days_to_major_unlock < c.min_days_to_major_unlock:
        return _fail("unlock_proximity", Severity.HARD,
                     f"major unlock in {s.days_to_major_unlock:.1f} days "
                     f"(min {c.min_days_to_major_unlock})",
                     s.days_to_major_unlock, c.min_days_to_major_unlock)
    return _ok("unlock_proximity", f"next major unlock in {s.days_to_major_unlock:.0f} days")


# ---------------------------------------------------------------- age/momentum
@gate
def gate_age(s: NormalizedSnapshot, c: Settings) -> GateResult:
    if s.token_age_hours is None:
        return _missing("token_age", "token_age_hours")
    if s.token_age_hours < c.min_token_age_hours:
        return _fail("token_age", Severity.HARD,
                     f"token is {s.token_age_hours:.1f}h old — below {c.min_token_age_hours:.0f}h minimum",
                     s.token_age_hours, c.min_token_age_hours)
    return _ok("token_age", f"token age {s.token_age_hours/24:.1f} days", s.token_age_hours)


@gate
def gate_not_extended(s: NormalizedSnapshot, c: Settings) -> GateResult:
    """Anti-chase. This is the gate that keeps the bot out of parabolas."""
    if s.price_change_24h_pct is None:
        return _missing("not_extended", "price_change_24h_pct")
    if s.price_change_24h_pct > c.max_price_change_24h_pct:
        return _fail("not_extended", Severity.HARD,
                     f"price +{s.price_change_24h_pct:.0f}% in 24h — already extended, no chasing",
                     s.price_change_24h_pct, c.max_price_change_24h_pct)
    if s.price_vs_7d_base_pct is not None and s.price_vs_7d_base_pct > c.max_price_vs_7d_base_pct:
        return _fail("not_extended", Severity.HARD,
                     f"price {s.price_vs_7d_base_pct:.0f}% above its 7d base — not base-building",
                     s.price_vs_7d_base_pct, c.max_price_vs_7d_base_pct)
    return _ok("not_extended", f"24h change {s.price_change_24h_pct:+.1f}%", s.price_change_24h_pct)


@gate
def gate_price_agreement(s: NormalizedSnapshot, c: Settings) -> GateResult:
    """Independent sources must agree before we spend money at a given price."""
    if len(s.price_sources) < 2:
        return _fail("price_agreement", Severity.LIVE_ONLY,
                     f"only {len(s.price_sources)} price source(s) — no cross-check possible")
    if s.price_divergence_pct is None:
        return _missing("price_agreement", "price_divergence_pct")
    limit = c.price_max_source_divergence * 100
    if s.price_divergence_pct > limit:
        return _fail("price_agreement", Severity.HARD,
                     f"price sources disagree by {s.price_divergence_pct:.1f}% (max {limit:.1f}%) — "
                     f"stale feed or manipulated pool",
                     s.price_divergence_pct, limit)
    return _ok("price_agreement", f"sources agree within {s.price_divergence_pct:.2f}%",
               s.price_divergence_pct)


# --------------------------------------------------------------------- runner
def evaluate_gates(snapshot: NormalizedSnapshot, settings: Settings) -> list[GateResult]:
    results: list[GateResult] = []
    for g in REGISTRY:
        try:
            results.append(g(snapshot, settings))
        except Exception as e:  # noqa: BLE001 - a broken gate must not open the door
            results.append(
                GateResult(name=getattr(g, "__name__", "unknown"), passed=False, severity=Severity.HARD,
                           reason=f"gate raised {type(e).__name__}: {e} — failing closed")
            )
    return results


def summarize(results: list[GateResult]) -> dict[str, list[GateResult]]:
    return {
        "hard": [r for r in results if not r.passed and r.severity == Severity.HARD],
        "live_only": [r for r in results if not r.passed and r.severity == Severity.LIVE_ONLY],
        "data": [r for r in results if not r.passed and r.severity == Severity.DATA],
        "soft": [r for r in results if not r.passed and r.severity == Severity.SOFT],
        "passed": [r for r in results if r.passed],
    }
