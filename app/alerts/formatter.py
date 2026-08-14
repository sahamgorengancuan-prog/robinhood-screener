"""Alert rendering.

An alert has to answer, in order: what is it, how good is it, why, and what
should I do. Failure reasons are shown as prominently as passes — an alert that
only lists positives is a sales pitch, not a screen.
"""

from __future__ import annotations

from typing import Any

from app.schemas import Decision, DecisionState, NormalizedSnapshot, Severity

STATE_ICON = {
    DecisionState.LIVE_BUY: "[LIVE BUY]",
    DecisionState.PAPER_BUY: "[PAPER BUY]",
    DecisionState.ALERT: "[ALERT]",
    DecisionState.WATCH: "[WATCH]",
    DecisionState.REJECT: "[REJECT]",
}

ACTION = {
    DecisionState.LIVE_BUY: "Small live buy submitted (post-only). Monitor fill and liquidity.",
    DecisionState.PAPER_BUY: "Simulated only. Review manually before any real capital.",
    DecisionState.ALERT: "Watchlist candidate. Do not buy yet — see blockers below.",
    DecisionState.WATCH: "Insufficient data. Keep collecting snapshots.",
    DecisionState.REJECT: "No action. Rejected.",
}


def _fmt_usd(v: float | None) -> str:
    return f"${v:,.0f}" if v is not None else "n/a"


def _fmt_pct(v: float | None, digits: int = 1) -> str:
    return f"{v:.{digits}f}%" if v is not None else "n/a"


def build_title(snap: NormalizedSnapshot, decision: Decision) -> str:
    sym = snap.token.symbol or snap.token.address[:10]
    return f"{STATE_ICON[decision.state]} {sym} — score {decision.score.total:.1f}/100"


def build_body(snap: NormalizedSnapshot, decision: Decision) -> str:
    t = snap.token
    lines: list[str] = []

    lines.append(build_title(snap, decision))
    lines.append("=" * 62)
    lines.append(f"Token      : {t.name or 'unknown'} ({t.symbol or '?'})")
    lines.append(f"Contract   : {t.address}")
    lines.append(f"Chain      : {t.chain}")
    lines.append(f"Captured   : {snap.captured_at.isoformat()}")
    lines.append("")

    lines.append("SCORE BREAKDOWN")
    for comp in decision.score.components:
        bar_len = int(round(comp.ratio * 20))
        bar = "#" * bar_len + "." * (20 - bar_len)
        lines.append(f"  {comp.name:<11} {comp.points:5.1f}/{comp.weight:<4.0f} [{bar}]")
    lines.append(f"  {'TOTAL':<11} {decision.score.total:5.1f}/100")
    lines.append("")

    lines.append("KEY METRICS")
    lines.append(f"  price            : {snap.price_usd if snap.price_usd is not None else 'n/a'}"
                 f"   (sources: {', '.join(snap.price_sources) or 'none'}"
                 f"{f', divergence {snap.price_divergence_pct:.2f}%' if snap.price_divergence_pct is not None else ''})")
    lines.append(f"  liquidity        : {_fmt_usd(snap.liquidity_usd)}")
    lines.append(f"  volume 5m/1h/24h : {_fmt_usd(snap.volume_5m)} / {_fmt_usd(snap.volume_1h)} / {_fmt_usd(snap.volume_24h)}")
    lines.append(f"  tx count 24h     : {snap.tx_count_24h if snap.tx_count_24h is not None else 'n/a'}")
    lines.append(f"  buy share 24h    : {_fmt_pct(snap.buy_ratio_24h * 100 if snap.buy_ratio_24h is not None else None)}")
    lines.append(
        f"  wash sample      : {snap.trade_sample_size or 'n/a'} trades · "
        f"unique ratio {snap.unique_trader_ratio if snap.unique_trader_ratio is not None else 'n/a'} · "
        f"top wallet {_fmt_pct(snap.top_trader_volume_pct)} · "
        f"filtered {_fmt_pct(snap.filtered_trade_pct)}"
    )
    lines.append(f"  holders          : {snap.unique_holders if snap.unique_holders is not None else 'n/a'}"
                 f"  (growth {_fmt_pct(snap.holder_growth_24h_pct)}/24h)")
    lines.append(f"  top1 / top10     : {_fmt_pct(snap.top1_holder_pct)} / {_fmt_pct(snap.top10_holder_pct)}")
    lines.append(f"  whale HHI        : {snap.whale_concentration if snap.whale_concentration is not None else 'n/a'}")
    lines.append(f"  spread / slippage: {_fmt_pct(snap.spread_bps / 100 if snap.spread_bps is not None else None, 2)}"
                 f" / {_fmt_pct(snap.slippage_bps / 100 if snap.slippage_bps is not None else None, 2)}"
                 f" (on {_fmt_usd(snap.slippage_notional_usd)})")
    lines.append(f"  token age        : {f'{snap.token_age_hours/24:.1f} days' if snap.token_age_hours is not None else 'n/a'}")
    lines.append(f"  price 1h / 24h   : {_fmt_pct(snap.price_change_1h_pct)} / {_fmt_pct(snap.price_change_24h_pct)}")
    lines.append(f"  vs 7d base       : {_fmt_pct(snap.price_vs_7d_base_pct)}")
    lines.append(f"  off local high   : {_fmt_pct(snap.drawdown_from_ath_pct)}")
    lines.append("")

    lines.append("RISK FLAGS")
    lines.append(f"  contract verified: {snap.contract_verified if snap.contract_verified is not None else 'UNKNOWN'}")
    lines.append(f"  contract flags   : {', '.join(snap.contract_flags) or 'none detected'}")
    lines.append(f"  tokenomics flags : {', '.join(snap.tokenomics_flags) or 'none detected'}")
    lines.append(f"  sniper / bundled : {_fmt_pct(snap.sniper_wallet_pct)} / {_fmt_pct(snap.bundled_buy_pct)}")
    lines.append(f"  suspicious share : {_fmt_pct(snap.suspicious_holder_pct)}")
    lines.append(f"  next unlock      : {f'{snap.days_to_major_unlock:.0f} days' if snap.days_to_major_unlock is not None else 'unknown'}")
    lines.append("")

    hard = [g for g in decision.gates if not g.passed and g.severity == Severity.HARD]
    live_only = [g for g in decision.gates if not g.passed and g.severity == Severity.LIVE_ONLY]
    data = [g for g in decision.gates if not g.passed and g.severity == Severity.DATA]

    if hard:
        lines.append("HARD FAILURES (disqualifying)")
        lines.extend(f"  x {g.name}: {g.reason}" for g in hard)
        lines.append("")
    if live_only:
        lines.append("LIVE-TRADE BLOCKERS (manual review required)")
        lines.extend(f"  ! {g.name}: {g.reason}" for g in live_only)
        lines.append("")
    if data:
        lines.append("MISSING DATA")
        lines.extend(f"  ? {g.name}: {g.reason}" for g in data)
        lines.append("")

    passed = [g for g in decision.gates if g.passed]
    if passed:
        lines.append(f"PASSED GATES ({len(passed)})")
        lines.extend(f"  + {g.name}: {g.reason}" for g in passed)
        lines.append("")

    if snap.missing_fields:
        lines.append(f"UNAVAILABLE FIELDS: {', '.join(snap.missing_fields)}")
        lines.append("")

    lines.append("VENUE")
    if snap.okx_available is True:
        lines.append(f"  OKX: available as {snap.okx_inst_id or 'DEX-only listing'}")
    elif snap.okx_available is False:
        lines.append("  OKX: NOT LISTED — on-chain only / manual review (no auto-buy)")
    else:
        lines.append("  OKX: availability unresolved")
    if snap.okx_identity_reason:
        lines.append(f"  Identity: {snap.okx_identity_reason}")
    lines.append("")

    lines.append(f"DECISION : {decision.state.value}")
    lines.append(f"REASON   : {decision.reason}")
    lines.append(f"ACTION   : {ACTION[decision.state]}")

    return "\n".join(lines)


def build_payload(snap: NormalizedSnapshot, decision: Decision) -> dict[str, Any]:
    """Machine-readable form for webhooks and the dashboard."""
    return {
        "state": decision.state.value,
        "score": decision.score.total,
        "score_components": {c.name: c.points for c in decision.score.components},
        "token": {
            "chain": snap.token.chain,
            "address": snap.token.address,
            "symbol": snap.token.symbol,
            "name": snap.token.name,
        },
        "metrics": {
            "price_usd": snap.price_usd,
            "price_sources": snap.price_sources,
            "price_divergence_pct": snap.price_divergence_pct,
            "liquidity_usd": snap.liquidity_usd,
            "volume_5m": snap.volume_5m,
            "volume_1h": snap.volume_1h,
            "volume_24h": snap.volume_24h,
            "tx_count_24h": snap.tx_count_24h,
            "buy_ratio_24h": snap.buy_ratio_24h,
            "trade_sample_size": snap.trade_sample_size,
            "unique_trader_ratio": snap.unique_trader_ratio,
            "top_trader_volume_pct": snap.top_trader_volume_pct,
            "filtered_trade_pct": snap.filtered_trade_pct,
            "unique_holders": snap.unique_holders,
            "top1_holder_pct": snap.top1_holder_pct,
            "top10_holder_pct": snap.top10_holder_pct,
            "whale_concentration": snap.whale_concentration,
            "holder_growth_24h_pct": snap.holder_growth_24h_pct,
            "spread_bps": snap.spread_bps,
            "slippage_bps": snap.slippage_bps,
            "token_age_hours": snap.token_age_hours,
            "price_change_24h_pct": snap.price_change_24h_pct,
        },
        "risk": {
            "contract_verified": snap.contract_verified,
            "contract_flags": snap.contract_flags,
            "tokenomics_flags": snap.tokenomics_flags,
            "days_to_major_unlock": snap.days_to_major_unlock,
            "sniper_wallet_pct": snap.sniper_wallet_pct,
            "bundled_buy_pct": snap.bundled_buy_pct,
            "suspicious_holder_pct": snap.suspicious_holder_pct,
        },
        "okx": {
            "available": snap.okx_available,
            "inst_id": snap.okx_inst_id,
            "identity_reason": snap.okx_identity_reason,
        },
        "gates": [g.as_dict() for g in decision.gates],
        "missing_fields": snap.missing_fields,
        "reason": decision.reason,
        "action": ACTION[decision.state],
        "captured_at": snap.captured_at.isoformat(),
    }


def dedupe_key(snap: NormalizedSnapshot, decision: Decision) -> str:
    """One alert per token per state per hour — enough to notice a change,
    not enough to train yourself to ignore the channel."""
    hour = snap.captured_at.strftime("%Y%m%d%H")
    return f"{snap.token.key}:{decision.state.value}:{hour}"
