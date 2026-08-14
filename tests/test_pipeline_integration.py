"""End-to-end pipeline test with a mocked transport.

Everything below the socket is real: `BaseHTTPClient.request`, retry handling,
JSON decoding, the provider parsers, the normalizer, all 23 gates, the scorer
and the decision engine. Only the network is faked, via `httpx.MockTransport`.

This is what demonstrates the market-data providers are wired in rather than
declared: the assertions are about fields that had **no source at all** before
them — `buy_ratio_24h`, per-window volume, and a second price for the agreement
gate.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from app.clients.dexscreener import DexScreenerClient
from app.clients.geckoterminal import GeckoTerminalClient
from app.config import Settings
from app.pipeline.decision import DecisionContext, decide
from app.pipeline.normalize import RawBundle, normalize
from app.pipeline.risk import evaluate_gates, summarize
from app.pipeline.scoring import score_snapshot
from app.schemas import DecisionState, Severity, TokenRef

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "0xTOKEN0000000000000000000000000000000001"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def make_transport(*, ds_status: int = 200, gt_status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "dexscreener" in url or "/latest/dex/tokens/" in url:
            if ds_status != 200:
                return httpx.Response(ds_status, json={"error": "boom"})
            return httpx.Response(200, json=_fixture("dexscreener_token.json"))
        if "geckoterminal" in url or "/api/v2/networks/" in url:
            if gt_status != 200:
                return httpx.Response(gt_status, json={"errors": [{"status": str(gt_status)}]})
            return httpx.Response(200, json=_fixture("geckoterminal_token.json"))
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


async def build_bundle(transport: httpx.MockTransport) -> RawBundle:
    """Runs the real client code against the mock socket."""
    ref = TokenRef(address=TOKEN)
    b = RawBundle(ref)

    ds = DexScreenerClient(chain_id="base", max_retries=0)
    gt = GeckoTerminalClient(network="base", max_retries=0)
    ds._client = httpx.AsyncClient(base_url=ds.base_url, transport=transport)
    gt._client = httpx.AsyncClient(base_url=gt.base_url, transport=transport)

    try:
        try:
            m = await ds.token_market(TOKEN)
        except Exception:  # noqa: BLE001 - mirrors collect()'s per-source guard
            m = None
        if m:
            b.dexscreener = m
            b.dex_pool_price = m.get("price_usd")
            b.dex_pool_liquidity_usd = m.get("liquidity_usd")
            b.buys_24h = m.get("buys_24h")
            b.sells_24h = m.get("sells_24h")
            ref.symbol = ref.symbol or m.get("symbol")
            ref.name = ref.name or m.get("name")
        try:
            b.geckoterminal = await gt.token_market(TOKEN)
        except Exception:  # noqa: BLE001
            b.geckoterminal = None
    finally:
        await ds.aclose()
        await gt.aclose()
    return b


def enrich_onchain(b: RawBundle) -> RawBundle:
    """Fill the on-chain half a real node would supply, so the gates that need it
    can be exercised too."""
    b.onchain_supply = 120_000_000.0
    b.onchain_decimals = 18
    b.contract_verified = True
    b.contract_flags = []
    b.contract_detail = {"is_proxy": False}
    b.okx_advanced_info = {
        "top10HoldPercent": "18", "sniperHoldingPercent": "3",
        "bundleHoldingPercent": "4", "suspiciousHoldingPercent": "1",
        "riskControlLevel": "1", "devRugPullTokenCount": "0", "tokenTags": [],
    }
    b.okx_trades = [
        {
            "userAddress": f"0x{i:040x}", "volume": "100",
            "type": "buy" if i % 2 else "sell", "isFiltered": "0",
        }
        for i in range(100)
    ]
    b.deployed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)
    b.holder_balances = [4_800_000.0, 3_600_000.0] + [1_200_000.0] * 8 + [50_000.0] * 40
    # Holder COUNT comes from the explorer, not from the DEX aggregators —
    # neither DexScreener nor GeckoTerminal reports it.
    b.explorer_counters = {"token_holders_count": "7200"}
    b.prev_holders = 6_000
    b.prev_holders_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    b.price_history_7d = [0.040, 0.0405, 0.041, 0.0403, 0.0412, 0.0408, 0.0411]
    return b


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        position_usd=25.0,
        tokenomics_overrides_json=json.dumps({
            TOKEN.lower(): {
                "next_unlock_at": "2027-12-01T00:00:00Z",
                "unlock_pct": 8.0,
                "source_url": "https://project.example/tokenomics",
            }
        }),
    )


# ===========================================================================
@pytest.mark.asyncio
async def test_providers_populate_metrics_through_the_real_client_stack(settings):
    b = await build_bundle(make_transport())
    snap = normalize(b, settings)

    # Fields that previously had no source anywhere in the project.
    assert snap.buy_ratio_24h is not None, "buy/sell flow is still unwired"
    assert snap.buy_ratio_24h == pytest.approx(1150 / 2200)
    assert snap.volume_5m == pytest.approx(9_000.0)
    assert snap.volume_1h == pytest.approx(95_000.0)
    assert snap.volume_24h == pytest.approx(2_200_000.0)
    assert snap.tx_count_24h == 2200

    # Provenance must name the real provider, not "derived from nothing".
    assert snap.sources["volume_24h"] == "dexscreener"
    assert snap.sources["buy_ratio_24h"] == "derived"


@pytest.mark.asyncio
async def test_two_independent_price_sources_arrive(settings):
    b = await build_bundle(make_transport())
    snap = normalize(b, settings)

    assert set(snap.price_sources) == {"dex_pool", "geckoterminal"}
    assert snap.price_divergence_pct is not None
    assert snap.price_divergence_pct < 1.0
    assert snap.sources["price_usd"].startswith("reconciled")


@pytest.mark.asyncio
async def test_liquidity_takes_the_minimum_across_providers(settings):
    b = await build_bundle(make_transport())
    snap = normalize(b, settings)
    assert snap.liquidity_usd == pytest.approx(1_500_000.0)  # DexScreener < GeckoTerminal
    assert set(snap.raw["liquidity_sources"]) == {"dex_pool", "geckoterminal"}


@pytest.mark.asyncio
async def test_full_pipeline_reaches_a_buy_state(settings):
    """The real goal: with these providers a healthy token clears every gate."""
    b = enrich_onchain(await build_bundle(make_transport()))
    snap = normalize(b, settings)

    gates = evaluate_gates(snap, settings)
    buckets = summarize(gates)
    assert buckets["hard"] == [], [g.reason for g in buckets["hard"]]
    assert buckets["data"] == [], [g.reason for g in buckets["data"]]

    score = score_snapshot(snap, settings)
    ctx = DecisionContext(
        consecutive_passes=settings.stability_required_snapshots,
        recent_scores=[score.total] * 3,
        run_mode="ALERT_ONLY",
        okx_available=snap.okx_available,
    )
    decision = decide(snap, gates, score, ctx, settings)

    assert score.total >= settings.score_paper_buy_min, f"score only {score.total}"
    assert decision.state == DecisionState.ALERT


@pytest.mark.asyncio
async def test_flow_gate_is_actually_evaluated_now(settings):
    """Previously this gate always degraded to LIVE_ONLY for lack of data."""
    b = enrich_onchain(await build_bundle(make_transport()))
    snap = normalize(b, settings)
    gate = next(g for g in evaluate_gates(snap, settings) if g.name == "buy_sell_balance")
    assert gate.passed is True
    assert "buy share" in gate.reason


@pytest.mark.asyncio
async def test_one_provider_down_degrades_but_does_not_crash(settings):
    """GeckoTerminal 500: the cycle continues, but the price cross-check is lost
    and live trading is blocked rather than silently allowed."""
    b = enrich_onchain(await build_bundle(make_transport(gt_status=500)))
    snap = normalize(b, settings)

    assert snap.price_usd is not None, "DexScreener alone must still price it"
    assert set(snap.price_sources) == {"dex_pool"}

    gate = next(g for g in evaluate_gates(snap, settings) if g.name == "price_agreement")
    assert gate.passed is False
    assert gate.severity == Severity.LIVE_ONLY


@pytest.mark.asyncio
async def test_both_providers_down_routes_to_watch_never_a_buy(settings):
    b = await build_bundle(make_transport(ds_status=500, gt_status=500))
    snap = normalize(b, settings)

    assert snap.price_usd is None
    assert snap.liquidity_usd is None
    assert "liquidity_usd" in snap.missing_fields

    gates = evaluate_gates(snap, settings)
    score = score_snapshot(snap, settings)
    decision = decide(snap, gates, score, DecisionContext(run_mode="LIVE"), settings)
    assert decision.state in (DecisionState.WATCH, DecisionState.REJECT)
    assert decision.state.rank < DecisionState.PAPER_BUY.rank


@pytest.mark.asyncio
async def test_wrong_chain_pool_is_never_used_end_to_end(settings):
    """The ethereum pool in the fixture is deeper and prices the token at $999.
    If chain filtering regressed, this test catches it at pipeline level."""
    ref = TokenRef(address=TOKEN)
    b = RawBundle(ref)
    ds = DexScreenerClient(chain_id="base", max_retries=0)
    ds._client = httpx.AsyncClient(base_url=ds.base_url, transport=make_transport())
    try:
        m = await ds.token_market(TOKEN)
    finally:
        await ds.aclose()

    b.dex_pool_price = m["price_usd"]
    snap = normalize(b, settings)
    assert snap.price_usd == pytest.approx(0.04110)
    assert snap.price_usd < 1.0
