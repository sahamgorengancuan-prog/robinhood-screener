"""Scoring engine — 0..100 across five weighted components.

    Liquidity quality      30
    Holder distribution    25
    Volume quality         20
    Tokenomics safety      15
    Early momentum quality 10

Two rules keep the score honest:

1. **Unknown is not free.** A component built from missing inputs scores its
   `unknown_ratio` (0.35 by default), not 1.0. Silence is mildly bad, not
   neutral, and never good.
2. **Score cannot override a gate.** A 95/100 token with one HARD gate failure
   is still REJECT. The score ranks survivors; it does not grant exceptions.
"""

from __future__ import annotations

from app.config import Settings
from app.pipeline.metrics import clamp, linear_score, log_score
from app.schemas import NormalizedSnapshot, ScoreComponent, ScoreResult

UNKNOWN_RATIO = 0.35

W_LIQUIDITY = 30.0
W_HOLDERS = 25.0
W_VOLUME = 20.0
W_TOKENOMICS = 15.0
W_MOMENTUM = 10.0


def _blend(parts: list[tuple[float | None, float]], reasons: list[str]) -> float:
    """Weighted mean of sub-scores; missing parts contribute UNKNOWN_RATIO."""
    total_w = sum(w for _, w in parts)
    if total_w <= 0:
        return UNKNOWN_RATIO
    acc = 0.0
    for value, w in parts:
        acc += (UNKNOWN_RATIO if value is None else value) * w
    return clamp(acc / total_w)


def score_liquidity(s: NormalizedSnapshot, c: Settings) -> ScoreComponent:
    reasons: list[str] = []

    if s.liquidity_usd is None:
        depth = None
        reasons.append("liquidity unknown")
    else:
        # Log-scaled: zero at the hard floor, full marks at 10x the live floor.
        depth = log_score(s.liquidity_usd, c.min_liquidity_usd, c.min_liquidity_usd_live * 10)
        reasons.append(f"liquidity ${s.liquidity_usd:,.0f}")

    if s.slippage_bps is None:
        slip = None
        reasons.append("slippage unknown")
    else:
        slip = linear_score(s.slippage_bps, c.max_slippage_bps, 10.0)
        reasons.append(f"slippage {s.slippage_bps:.0f}bps")

    if s.spread_bps is None:
        spr = None
        reasons.append("spread unknown (no CEX book)")
    else:
        spr = linear_score(s.spread_bps, c.max_spread_bps, 5.0)
        reasons.append(f"spread {s.spread_bps:.0f}bps")

    # Deep liquidity relative to market cap means the float is genuinely tradable.
    if s.liquidity_usd and s.market_cap_usd and s.market_cap_usd > 0:
        ratio = s.liquidity_usd / s.market_cap_usd
        backing = linear_score(ratio, 0.01, 0.20)
        reasons.append(f"liquidity/mcap {ratio*100:.1f}%")
    else:
        backing = None

    ratio_score = _blend([(depth, 0.40), (slip, 0.25), (spr, 0.15), (backing, 0.20)], reasons)
    return ScoreComponent(name="liquidity", weight=W_LIQUIDITY, ratio=ratio_score,
                          points=round(ratio_score * W_LIQUIDITY, 2), reasons=reasons)


def score_holders(s: NormalizedSnapshot, c: Settings) -> ScoreComponent:
    reasons: list[str] = []

    if s.top1_holder_pct is None:
        top1 = None
        reasons.append("top1 unknown")
    else:
        top1 = linear_score(s.top1_holder_pct, c.max_top1_holder_pct, 1.0)
        reasons.append(f"top1 {s.top1_holder_pct:.1f}%")

    if s.top10_holder_pct is None:
        top10 = None
        reasons.append("top10 unknown")
    else:
        top10 = linear_score(s.top10_holder_pct, c.max_top10_holder_pct, 10.0)
        if s.top10_holder_pct > c.max_top10_holder_pct * 0.8:
            reasons.append(f"top10 {s.top10_holder_pct:.1f}% — heavy concentration penalty")
        else:
            reasons.append(f"top10 {s.top10_holder_pct:.1f}%")

    if s.unique_holders is None:
        count = None
        reasons.append("holder count unknown")
    else:
        count = log_score(float(s.unique_holders), float(c.min_unique_holders), 10_000.0)
        reasons.append(f"{s.unique_holders} holders")

    if s.holder_growth_24h_pct is None:
        growth = None
        reasons.append("holder growth unknown")
    else:
        # Best around steady +5..+40%/24h. Both stagnation and explosions score low.
        g = s.holder_growth_24h_pct
        growth = clamp(g / 20.0) if g <= 20 else clamp(1.0 - (g - 20.0) / 180.0)
        reasons.append(f"holder growth {g:+.1f}%/24h")

    if s.whale_concentration is None:
        hhi = None
    else:
        hhi = linear_score(s.whale_concentration, 0.25, 0.01)
        reasons.append(f"HHI {s.whale_concentration:.4f}")

    ratio = _blend([(top1, 0.25), (top10, 0.30), (count, 0.20), (growth, 0.15), (hhi, 0.10)], reasons)
    return ScoreComponent(name="holders", weight=W_HOLDERS, ratio=ratio,
                          points=round(ratio * W_HOLDERS, 2), reasons=reasons)


def score_volume(s: NormalizedSnapshot, c: Settings) -> ScoreComponent:
    reasons: list[str] = []

    if s.volume_24h is None:
        vol = None
        reasons.append("24h volume unknown")
    else:
        vol = log_score(s.volume_24h, c.min_volume_24h_usd, c.min_volume_24h_usd * 20)
        reasons.append(f"24h volume ${s.volume_24h:,.0f}")

    # Consistency across windows: reward volume that is spread out, punish bursts.
    consistency: float | None = None
    if s.volume_24h and s.volume_24h > 0 and s.volume_1h is not None:
        share_1h = s.volume_1h / s.volume_24h
        uniform = 1 / 24
        # 1.0 when the last hour is ~1/24 of the day; decays as it dominates.
        consistency = clamp(1.0 - abs(share_1h - uniform) / (c.max_single_window_volume_share - uniform))
        if share_1h > c.max_single_window_volume_share * 0.6:
            reasons.append(f"volume spike: last 1h = {share_1h*100:.0f}% of 24h — penalized")
        else:
            reasons.append(f"volume spread evenly ({share_1h*100:.1f}% in last 1h)")
    else:
        reasons.append("intraday volume windows unavailable")

    if s.tx_count_24h is None:
        txs = None
    else:
        txs = log_score(float(s.tx_count_24h), float(c.min_tx_count_24h), 5000.0)
        reasons.append(f"{s.tx_count_24h} txs/24h")

    # Average trade size sanity: huge volume from few txs is a wash pattern.
    if s.volume_24h and s.tx_count_24h and s.tx_count_24h > 0:
        avg = s.volume_24h / s.tx_count_24h
        # Organic retail flow clusters well under $5k average.
        granularity = clamp(1.0 - (avg - 200.0) / 5000.0) if avg > 200 else 1.0
        reasons.append(f"avg trade ${avg:,.0f}")
    else:
        granularity = None

    if s.buy_ratio_24h is None:
        balance = None
    else:
        # Best at ~0.5; falls off symmetrically toward the gate edges.
        balance = clamp(1.0 - abs(s.buy_ratio_24h - 0.5) / 0.25)
        reasons.append(f"buy share {s.buy_ratio_24h*100:.0f}%")

    ratio = _blend([(vol, 0.30), (consistency, 0.30), (txs, 0.15), (granularity, 0.15), (balance, 0.10)], reasons)
    return ScoreComponent(name="volume", weight=W_VOLUME, ratio=ratio,
                          points=round(ratio * W_VOLUME, 2), reasons=reasons)


def score_tokenomics(s: NormalizedSnapshot, c: Settings) -> ScoreComponent:
    reasons: list[str] = []
    base = 1.0

    if s.contract_verified is True:
        reasons.append("contract verified")
    elif s.contract_verified is False:
        base -= 0.60
        reasons.append("contract NOT verified (-0.60)")
    else:
        base -= 0.30
        reasons.append("verification status unknown (-0.30)")

    penalties = {
        "OWNER_CAN_MINT": 0.50,
        "MINT_FUNCTION": 0.20,
        "BLACKLIST": 0.35,
        "PAUSABLE": 0.15,
        "UPGRADEABLE_PROXY": 0.20,
        "MUTABLE_FEES": 0.15,
        "MUTABLE_LIMITS": 0.10,
        "OWNER_PRIVILEGE_ACTIVE": 0.10,
        "SUSPICIOUSLY_SMALL_BYTECODE": 0.30,
    }
    for flag in s.contract_flags:
        p = penalties.get(flag)
        if p:
            base -= p
            reasons.append(f"{flag} (-{p:.2f})")

    for flag in s.tokenomics_flags:
        base -= 0.20
        reasons.append(f"{flag} (-0.20)")

    if s.days_to_major_unlock is not None:
        if s.days_to_major_unlock < 30:
            base -= 0.20
            reasons.append(f"unlock in {s.days_to_major_unlock:.0f}d (-0.20)")
        else:
            reasons.append(f"next unlock {s.days_to_major_unlock:.0f}d away")
    else:
        base -= 0.10
        reasons.append("unlock schedule unknown (-0.10)")

    ratio = clamp(base)
    return ScoreComponent(name="tokenomics", weight=W_TOKENOMICS, ratio=ratio,
                          points=round(ratio * W_TOKENOMICS, 2), reasons=reasons)


def score_momentum(s: NormalizedSnapshot, c: Settings) -> ScoreComponent:
    """Rewards *quiet* strength. Explicitly punishes parabolic moves."""
    reasons: list[str] = []

    if s.price_change_24h_pct is None:
        move = None
        reasons.append("24h price change unknown")
    else:
        ch = s.price_change_24h_pct
        if ch < -25:
            move = 0.2
            reasons.append(f"24h {ch:+.1f}% — falling knife")
        elif ch <= 15:
            # The sweet spot: flat to mildly positive = base-building.
            move = 1.0 - abs(ch - 5.0) / 40.0
            reasons.append(f"24h {ch:+.1f}% — base-building range")
        else:
            move = clamp(1.0 - (ch - 15.0) / (c.max_price_change_24h_pct - 15.0))
            reasons.append(f"24h {ch:+.1f}% — extended, chase penalty")

    if s.price_vs_7d_base_pct is None:
        base_pos = None
    else:
        b = s.price_vs_7d_base_pct
        base_pos = clamp(1.0 - abs(b) / c.max_price_vs_7d_base_pct)
        reasons.append(f"{b:+.0f}% vs 7d base")

    if s.drawdown_from_ath_pct is None:
        dd = None
    else:
        # Being off the highs is *good* here — we want accumulation, not tops.
        dd = clamp(s.drawdown_from_ath_pct / 50.0)
        reasons.append(f"{s.drawdown_from_ath_pct:.0f}% off local high")

    if s.token_age_hours is None:
        age = None
    else:
        days = s.token_age_hours / 24.0
        if days < 1:
            age = 0.0
            reasons.append(f"{days:.1f}d old — no track record")
        elif days <= c.max_token_age_hours_for_early / 24.0:
            age = clamp(days / 14.0)
            reasons.append(f"{days:.1f}d old — early window")
        else:
            age = 0.5
            reasons.append(f"{days:.0f}d old — past the early window")

    ratio = _blend([(move, 0.40), (base_pos, 0.20), (dd, 0.20), (age, 0.20)], reasons)
    return ScoreComponent(name="momentum", weight=W_MOMENTUM, ratio=ratio,
                          points=round(ratio * W_MOMENTUM, 2), reasons=reasons)


def score_snapshot(s: NormalizedSnapshot, c: Settings) -> ScoreResult:
    components = [
        score_liquidity(s, c),
        score_holders(s, c),
        score_volume(s, c),
        score_tokenomics(s, c),
        score_momentum(s, c),
    ]
    total = round(sum(x.points for x in components), 2)
    reasons = [f"{x.name}: {x.points:.1f}/{x.weight:.0f}" for x in components]
    return ScoreResult(total=total, components=components, reasons=reasons)
