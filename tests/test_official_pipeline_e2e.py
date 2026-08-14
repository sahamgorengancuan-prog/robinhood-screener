"""End-to-end proof of the official-first pipeline.

`test_pipeline_integration.py` proves the free no-key providers. This file proves
the path the blueprint actually calls core: **OKX OnchainOS + the node + the
explorer**, driven through the real client stack with `httpx.MockTransport`.

Everything below the socket is real — `BaseHTTPClient.request`, OKX response
unwrapping, the parsers, `derive_trade_quality`, `apply_advanced_risk`, all
gates, the scorer and the decision engine. Only the network is faked.

The scenarios are chosen to be the ones that cost money when they regress:

  * a clean token clears every gate and reaches a buy state;
  * OKX's own honeypot / dev-rug / high-risk tags hard-reject, rather than
    merely denting the score;
  * wash-trading visible only in the trade sample is caught;
  * Premium being disabled degrades to "unmeasured" instead of "fine";
  * CEX identity that does not match on contract suffix can never reach
    LIVE_BUY, no matter how good the token looks.
"""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.clients.okx_market import OKXMarketClient
from app.clients.okx_trade import OKXTradeClient
from app.config import Settings
from app.pipeline.decision import DecisionContext, decide
from app.pipeline.normalize import (
    RawBundle,
    apply_advanced_risk,
    derive_trade_quality,
    normalize,
)
from app.pipeline.risk import evaluate_gates, summarize
from app.pipeline.scoring import score_snapshot
from app.schemas import DecisionState, Severity, TokenRef

CHAIN = "4663"
TOKEN = "0x1111111111111111111111111111111111abcdef"


# ---------------------------------------------------------------- fixtures
def price_info_row() -> dict:
    return {
        "chainIndex": CHAIN,
        "tokenContractAddress": TOKEN,
        "price": "0.04110",
        "marketCap": "9000000",
        "circulatingSupply": "90000000",
        "holders": "6500",
        "liquidity": "1500000",
        "volume24H": "2200000",
        "volume1H": "95000",
        "volume5M": "9000",
        "txs24H": "4100",
        "priceChange24H": "4.0",
        "priceChange1H": "0.8",
    }


def advanced_info(**over) -> dict:
    base = {
        "riskControlLevel": "1",
        "tokenTags": [],
        "top10HoldPercent": "18.0",
        "sniperHoldingPercent": "3.0",
        "bundleHoldingPercent": "4.0",
        "suspiciousHoldingPercent": "1.0",
        "devRugPullTokenCount": "0",
        "lpBurnedPercent": "100",
    }
    base.update(over)
    return base


def trades(n: int = 400, *, wallets: int = 160, filtered: int = 2,
           whale_share: float = 0.05) -> list[dict]:
    """A realistic trade sample. `whale_share` is the fraction of USD volume the
    single busiest wallet accounts for."""
    out: list[dict] = []
    whale_usd = 1000.0 * n * whale_share
    out.append({"userAddress": "0xwhale", "volume": str(whale_usd), "type": "buy", "isFiltered": "0"})
    remaining = 1000.0 * n * (1 - whale_share)
    per = remaining / max(n - 1, 1)
    for i in range(n - 1):
        out.append({
            "userAddress": f"0xw{i % max(wallets - 1, 1)}",
            "volume": str(per),
            "type": "buy" if i % 2 else "sell",
            "isFiltered": "1" if i < filtered else "0",
        })
    return out


def okx_envelope(data) -> dict:
    return {"code": "0", "msg": "", "data": data}


def make_transport(
    *,
    advanced: dict | None = None,
    trade_rows: list[dict] | None = None,
    okx_status: int = 200,
    currencies_ct_addr: str | None = None,
    chain_label: str = "Robinhood",
) -> httpx.MockTransport:
    adv = advanced if advanced is not None else advanced_info()
    tr = trade_rows if trade_rows is not None else trades()
    ct = currencies_ct_addr if currencies_ct_addr is not None else TOKEN

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if okx_status != 200 and "/api/v6/" in path:
            return httpx.Response(okx_status, json={"code": "50011", "msg": "rate limited"})

        if path.endswith("/price-info"):
            return httpx.Response(200, json=okx_envelope([price_info_row()]))
        if path.endswith("/advanced-info"):
            return httpx.Response(200, json=okx_envelope([adv]))
        if path.endswith("/holder"):
            return httpx.Response(200, json=okx_envelope(
                [{"holdAmountPercentage": "4.0"}, {"holdAmountPercentage": "3.5"}]))
        if path.endswith("/trades"):
            return httpx.Response(200, json=okx_envelope(tr))
        if path.endswith("/top-liquidity"):
            return httpx.Response(200, json=okx_envelope(
                [{"liquidity": "1500000", "poolAddress": "0xpool"}]))

        # ---- CEX ----
        if path.endswith("/asset/currencies"):
            return httpx.Response(200, json=okx_envelope(
                [{"ccy": "GOOD", "chain": f"GOOD-{chain_label}", "ctAddr": ct}]))
        if path.endswith("/public/instruments"):
            return httpx.Response(200, json=okx_envelope(
                [{"instId": "GOOD-USDT", "state": "live", "lotSz": "0.1", "minSz": "1"}]))
        if path.endswith("/market/ticker"):
            return httpx.Response(200, json=okx_envelope(
                [{"instId": "GOOD-USDT", "bidPx": "0.0411", "askPx": "0.0412", "last": "0.0411"}]))
        if path.endswith("/market/books"):
            return httpx.Response(200, json=okx_envelope(
                [{"asks": [["0.0412", "100000", "0", "1"]], "bids": [["0.0411", "100000", "0", "1"]]}]))
        return httpx.Response(404, json={"code": "1", "msg": "not found"})

    return httpx.MockTransport(handler)


def market_client(transport, *, premium: bool = True) -> OKXMarketClient:
    cli = OKXMarketClient("https://web3.okx.com", "k", "s", "p", "proj", max_retries=0)
    cli.premium_enabled = premium
    cli._client = httpx.AsyncClient(base_url=cli.base_url, transport=transport)
    return cli


def trade_client(transport) -> OKXTradeClient:
    cli = OKXTradeClient("https://www.okx.com", "k", "s", "p", simulated=True, max_retries=0)
    cli._client = httpx.AsyncClient(base_url=cli.base_url, transport=transport)
    return cli


async def build_bundle(transport, *, premium: bool = True) -> RawBundle:
    """Drive the real OKX clients, exactly as `collect()` does."""
    ref = TokenRef(address=TOKEN, symbol="GOOD", name="Good Token")
    b = RawBundle(ref)
    m = market_client(transport, premium=premium)
    try:
        # Each source is guarded independently, exactly as collect() does: one
        # endpoint failing must not lose the others.
        try:
            info = await m.price_info(CHAIN, [TOKEN])
            b.okx_price_info = info.get(TOKEN.lower())
        except Exception:  # noqa: BLE001
            pass
        try:
            b.okx_advanced_info = await m.advanced_info(CHAIN, TOKEN)
        except Exception:  # noqa: BLE001
            pass
        try:
            b.okx_trades = await m.trades(CHAIN, TOKEN)
        except Exception:  # noqa: BLE001
            pass
    finally:
        await m.aclose()
    return b


def enrich_onchain(b: RawBundle) -> RawBundle:
    b.onchain_supply = 120_000_000.0
    b.onchain_decimals = 18
    b.contract_verified = True
    # A completed scan that found nothing is NOT the same as no scan: the gate
    # fails closed unless is_proxy proves the bytecode was actually read.
    b.contract_detail = {"is_proxy": False, "code_size_bytes": 12000}
    b.deployed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)
    b.holder_balances = [4_800_000.0, 3_600_000.0] + [1_200_000.0] * 8 + [50_000.0] * 40
    b.explorer_counters = {"token_holders_count": "6500"}
    b.prev_holders = 6_000
    b.prev_holders_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    b.price_history_7d = [0.040, 0.0405, 0.041, 0.0403, 0.0412, 0.0408, 0.0411]
    b.okx_available = True
    b.okx_inst_id = "GOOD-USDT"
    b.okx_book = (0.0411, 0.0412)
    return b


UNLOCK_OVERRIDE = json.dumps({
    TOKEN.lower(): {
        "unlock_pct": 8.0,
        "next_unlock_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90)).isoformat(),
        "source_url": "https://example.invalid/vesting-schedule",
    }
})


@pytest.fixture
def settings():
    """Includes an operator-reviewed unlock row. Without one the unlock gate
    fails closed, which is the intended behaviour — this fixture represents a
    token someone has actually done the tokenomics homework on."""
    return Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        position_usd=25.0,
        tokenomics_overrides_json=UNLOCK_OVERRIDE,
    )


def run_decision(snap, settings, run_mode="ALERT_ONLY"):
    gates = evaluate_gates(snap, settings)
    score = score_snapshot(snap, settings)
    ctx = DecisionContext(
        consecutive_passes=settings.stability_required_snapshots,
        recent_scores=[score.total] * 3,
        run_mode=run_mode,
        okx_available=snap.okx_available,
    )
    return gates, score, decide(snap, gates, score, ctx, settings)


# ===========================================================================
# Happy path
# ===========================================================================
@pytest.mark.asyncio
async def test_official_stack_populates_every_core_metric(settings):
    b = await build_bundle(make_transport())
    snap = normalize(b, settings)

    assert snap.price_usd == pytest.approx(0.04110)
    assert snap.liquidity_usd == pytest.approx(1_500_000.0)
    assert snap.volume_24h == pytest.approx(2_200_000.0)
    assert snap.tx_count_24h == 4100
    assert snap.unique_holders == 6500
    # From advanced-info, the Premium risk endpoint.
    assert snap.sniper_wallet_pct == pytest.approx(3.0)
    assert snap.bundled_buy_pct == pytest.approx(4.0)
    assert snap.suspicious_holder_pct == pytest.approx(1.0)
    # From the trade sample.
    assert snap.trade_sample_size == 400
    assert snap.unique_trader_ratio is not None
    assert snap.filtered_trade_pct is not None


@pytest.mark.asyncio
async def test_clean_token_clears_every_gate(settings):
    snap = normalize(enrich_onchain(await build_bundle(make_transport())), settings)
    gates, score, decision = run_decision(snap, settings)
    buckets = summarize(gates)

    assert buckets["hard"] == [], [g.reason for g in buckets["hard"]]
    assert buckets["data"] == [], [g.reason for g in buckets["data"]]
    assert score.total >= settings.score_alert_min
    assert decision.state.rank >= DecisionState.ALERT.rank


# ===========================================================================
# OKX risk tags must disqualify, not merely dent the score
# ===========================================================================
@pytest.mark.asyncio
@pytest.mark.parametrize("override,flag", [
    ({"tokenTags": ["honeypot"]}, "HONEYPOT"),
    ({"devRugPullTokenCount": "3"}, "DEVELOPER_RUG_HISTORY"),
    ({"riskControlLevel": "5"}, "OKX_HIGH_RISK"),
])
async def test_okx_risk_tags_hard_reject(settings, override, flag):
    b = enrich_onchain(await build_bundle(make_transport(advanced=advanced_info(**override))))
    snap = normalize(b, settings)

    assert flag in snap.contract_flags
    gates, _, decision = run_decision(snap, settings)
    gate = next(g for g in gates if g.name == "contract_flags")
    assert gate.severity == Severity.HARD and not gate.passed
    assert decision.state == DecisionState.REJECT


@pytest.mark.asyncio
async def test_okx_concentration_fields_reach_their_gates(settings):
    b = enrich_onchain(await build_bundle(make_transport(
        advanced=advanced_info(sniperHoldingPercent="55.0", suspiciousHoldingPercent="40.0"))))
    snap = normalize(b, settings)
    gates, _, decision = run_decision(snap, settings)

    failed = {g.name for g in gates if not g.passed and g.severity == Severity.HARD}
    assert "sniper_domination" in failed
    assert "suspicious_holders" in failed
    assert decision.state == DecisionState.REJECT


# ===========================================================================
# Wash trading visible only in the trade sample
# ===========================================================================
@pytest.mark.asyncio
async def test_single_wallet_dominating_volume_is_rejected(settings):
    """Volume and liquidity look fine; one wallet made most of the volume."""
    b = enrich_onchain(await build_bundle(
        make_transport(trade_rows=trades(400, whale_share=0.85))))
    snap = normalize(b, settings)

    assert snap.top_trader_volume_pct > 80
    gates, _, decision = run_decision(snap, settings)
    gate = next(g for g in gates if g.name == "wash_sample")
    assert not gate.passed and gate.severity == Severity.HARD
    assert decision.state == DecisionState.REJECT


@pytest.mark.asyncio
async def test_recycled_wallets_are_rejected(settings):
    b = enrich_onchain(await build_bundle(
        make_transport(trade_rows=trades(400, wallets=4))))
    snap = normalize(b, settings)

    assert snap.unique_trader_ratio < settings.min_unique_trader_ratio
    gates, _, _ = run_decision(snap, settings)
    assert not next(g for g in gates if g.name == "wash_sample").passed


@pytest.mark.asyncio
async def test_okx_filtered_trades_are_rejected(settings):
    b = enrich_onchain(await build_bundle(
        make_transport(trade_rows=trades(400, filtered=200))))
    snap = normalize(b, settings)
    assert snap.filtered_trade_pct > 40
    gates, _, _ = run_decision(snap, settings)
    assert not next(g for g in gates if g.name == "wash_sample").passed


@pytest.mark.asyncio
async def test_small_trade_sample_is_unmeasured_not_passed(settings):
    """Fewer than 30 trades cannot support a wash verdict either way."""
    b = enrich_onchain(await build_bundle(make_transport(trade_rows=trades(10))))
    snap = normalize(b, settings)
    gates, _, decision = run_decision(snap, settings)
    gate = next(g for g in gates if g.name == "wash_sample")
    assert not gate.passed and gate.severity == Severity.DATA
    assert decision.state == DecisionState.WATCH


# ===========================================================================
# Degradation
# ===========================================================================
@pytest.mark.asyncio
async def test_premium_disabled_leaves_risk_fields_unmeasured(settings):
    """Turning Premium off to stay inside quota must not look like 'safe'."""
    b = enrich_onchain(await build_bundle(make_transport(), premium=False))
    snap = normalize(b, settings)

    # Premium-only risk fields go unmeasured...
    assert snap.sniper_wallet_pct is None
    assert snap.suspicious_holder_pct is None
    # ...but `trades` is a Basic-tier endpoint, so wash detection survives being
    # switched to quota-saving mode. That is the point of splitting the tiers.
    assert snap.trade_sample_size == 400
    assert snap.unique_trader_ratio is not None

    gates, _, decision = run_decision(snap, settings, run_mode="LIVE")
    for name in ("sniper_domination", "suspicious_holders"):
        gate = next(g for g in gates if g.name == name)
        assert not gate.passed, f"{name} must not pass on missing data"
    assert decision.state != DecisionState.LIVE_BUY


@pytest.mark.asyncio
async def test_okx_outage_routes_to_watch_never_a_buy(settings):
    b = await build_bundle(make_transport(okx_status=429))
    snap = normalize(b, settings)

    assert snap.price_usd is None and snap.liquidity_usd is None
    _, _, decision = run_decision(snap, settings, run_mode="LIVE")
    assert decision.state.rank < DecisionState.PAPER_BUY.rank


# ===========================================================================
# CEX identity — the check that stops us buying a same-symbol impostor
# ===========================================================================
@pytest.mark.asyncio
async def test_identity_matches_on_chain_label_and_contract_suffix():
    t = trade_client(make_transport())
    try:
        inst, ok, reason = await t.resolve_contract_spot(
            symbol="GOOD", contract_address=TOKEN, chain_hint="Robinhood", quote_ccy="USDT")
    finally:
        await t.aclose()
    assert ok is True and inst == "GOOD-USDT", reason


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ct,label", [
    ("0x9999999999999999999999999999999999999999", "Robinhood"),   # wrong contract
    (TOKEN, "Ethereum"),                                           # wrong chain
    ("", "Robinhood"),                                             # no contract published
])
async def test_identity_refuses_on_any_mismatch(bad_ct, label):
    """A same-symbol token on another chain is the most expensive possible
    confusion, so anything short of an exact match must refuse."""
    t = trade_client(make_transport(currencies_ct_addr=bad_ct, chain_label=label))
    try:
        inst, ok, reason = await t.resolve_contract_spot(
            symbol="GOOD", contract_address=TOKEN, chain_hint="Robinhood", quote_ccy="USDT")
    finally:
        await t.aclose()
    assert ok is not True and inst is None, "anything short of an exact match must block LIVE"
    assert reason


# ===========================================================================
# Unit-level edge cases for the new derivations
# ===========================================================================
def test_trade_quality_on_empty_sample_is_all_none():
    q = derive_trade_quality([])
    assert all(v is None for v in q.values())


def test_trade_quality_ignores_trades_without_a_wallet():
    q = derive_trade_quality([{"volume": "10", "type": "buy"}] * 5)
    assert q["trade_sample_size"] == 5
    assert q["unique_trader_ratio"] == 0.0, "no identifiable wallets is maximal suspicion"


def test_trade_quality_handles_zero_volume_without_dividing_by_zero():
    q = derive_trade_quality([{"userAddress": "0xa", "volume": "0", "type": "buy"}] * 3)
    assert q["top_trader_volume_pct"] is None
    assert q["trade_sample_size"] == 3


def test_trade_quality_sample_buy_ratio_only_counts_known_sides():
    q = derive_trade_quality([
        {"userAddress": "0xa", "volume": "1", "type": "buy"},
        {"userAddress": "0xb", "volume": "1", "type": "sell"},
        {"userAddress": "0xc", "volume": "1", "type": "unknown"},
    ])
    assert q["sample_buy_ratio"] == pytest.approx(0.5)


def test_advanced_risk_is_a_noop_when_absent(now):
    from app.schemas import NormalizedSnapshot

    snap = NormalizedSnapshot(token=TokenRef(address=TOKEN), captured_at=now)
    apply_advanced_risk(snap, None)
    assert snap.contract_flags == []
    assert snap.sniper_wallet_pct is None


def test_advanced_risk_does_not_duplicate_flags(now):
    """Called twice (retry, re-normalise) it must not stack duplicates."""
    from app.schemas import NormalizedSnapshot

    snap = NormalizedSnapshot(token=TokenRef(address=TOKEN), captured_at=now)
    adv = advanced_info(tokenTags=["honeypot"])
    apply_advanced_risk(snap, adv)
    apply_advanced_risk(snap, adv)
    assert snap.contract_flags.count("HONEYPOT") == 1
