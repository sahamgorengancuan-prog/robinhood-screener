"""Parser tests for the free market-data providers.

These are the tests that make DexScreener and GeckoTerminal real integrations
rather than placeholders. Parsers are pinned against recorded payloads and,
more importantly, against the ways real APIs go wrong: nulls, missing branches,
numbers as strings, empty results, and pools from a *different chain* sharing an
address. Runtime probes cover connectivity without making CI depend on the
providers.

The last one matters most. If chain filtering ever breaks, the screener prices a
different asset and every downstream number is confidently wrong.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app.clients.dexscreener import DexScreenerClient
from app.clients.geckoterminal import GeckoTerminalClient

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def ds():
    return DexScreenerClient(chain_id="base")


@pytest.fixture
def gt():
    return GeckoTerminalClient(network="base")


# ===========================================================================
# DexScreener
# ===========================================================================
def test_picks_the_deepest_pool_on_the_configured_chain(ds):
    pairs = load("dexscreener_token.json")["pairs"]
    primary = ds.select_primary_pair(pairs)
    assert primary["dexId"] == "aerodrome"
    assert primary["liquidity"]["usd"] == 1_500_000.0


def test_never_selects_a_pool_from_another_chain(ds):
    """The ethereum pool has 50M liquidity — far deeper — and a $999 price.
    Selecting it would price a completely different asset."""
    pairs = load("dexscreener_token.json")["pairs"]
    primary = ds.select_primary_pair(pairs)
    assert primary["chainId"] == "base"
    assert ds.extract(primary)["price_usd"] != pytest.approx(999.99)


def test_no_pool_on_configured_chain_returns_none():
    client = DexScreenerClient(chain_id="solana")
    assert client.select_primary_pair(load("dexscreener_token.json")["pairs"]) is None


def test_blank_chain_slug_disables_network_calls():
    """Never query a same-address token from an unknown EVM chain."""
    client = DexScreenerClient(chain_id="")
    assert client.enabled is False


def test_extract_maps_every_expected_field(ds):
    pairs = load("dexscreener_token.json")["pairs"]
    m = ds.extract(ds.select_primary_pair(pairs))

    assert m["price_usd"] == pytest.approx(0.04110)
    assert m["liquidity_usd"] == pytest.approx(1_500_000.0)
    assert m["volume_24h"] == pytest.approx(2_200_000.0)
    assert m["volume_1h"] == pytest.approx(95_000.0)
    assert m["volume_5m"] == pytest.approx(9_000.0)
    assert m["buys_24h"] == 1150
    assert m["sells_24h"] == 1050
    assert m["tx_count_24h"] == 2200
    assert m["price_change_24h_pct"] == pytest.approx(4.0)
    assert m["market_cap_usd"] == pytest.approx(9_000_000.0)
    assert m["symbol"] == "EXMP"


def test_buy_sell_counts_feed_the_flow_gate(ds):
    """This is the metric that had no source at all before this provider."""
    from app.pipeline.metrics import buy_ratio

    pairs = load("dexscreener_token.json")["pairs"]
    m = ds.extract(ds.select_primary_pair(pairs))
    ratio = buy_ratio(m["buys_24h"], m["sells_24h"])
    assert ratio == pytest.approx(1150 / 2200)
    assert 0.0 < ratio < 1.0


def test_price_is_parsed_from_a_string(ds):
    """DexScreener returns priceUsd as a string; a naive float() on None crashes
    and a naive cast of "" yields 0.0, which would read as a free token."""
    assert ds.extract({"priceUsd": "0.04102"})["price_usd"] == pytest.approx(0.04102)


def test_pair_age_is_derived_from_pair_created_at(ds):
    created = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    m = ds.extract({"pairCreatedAt": created.timestamp() * 1000})
    assert m["pair_age_hours"] == pytest.approx(48, abs=0.5)


@pytest.mark.parametrize("pair", [
    {},
    {"liquidity": None, "volume": None, "txns": None, "priceChange": None},
    {"liquidity": {}, "volume": {}, "txns": {"h24": {}}},
    {"priceUsd": None, "txns": {"h24": {"buys": None, "sells": None}}},
    {"priceUsd": "", "liquidity": {"usd": ""}},
    {"priceUsd": "not-a-number"},
])
def test_missing_or_hostile_branches_yield_none_never_zero(ds, pair):
    """A zero that means 'unknown' is what lets a screener buy what it can't see."""
    m = ds.extract(pair)
    for key in ("price_usd", "liquidity_usd", "volume_24h", "buys_24h",
                "sells_24h", "tx_count_24h"):
        assert m[key] is None, f"{key} became {m[key]!r} instead of None"


def test_tx_count_is_none_when_only_one_side_is_known(ds):
    m = ds.extract({"txns": {"h24": {"buys": 10}}})
    assert m["buys_24h"] == 10
    assert m["sells_24h"] is None
    assert m["tx_count_24h"] is None, "a half-known total would understate activity"


def test_zero_counts_are_preserved_not_treated_as_missing(ds):
    """Genuine zero activity is information; it must survive as 0, not None."""
    m = ds.extract({"txns": {"h24": {"buys": 0, "sells": 0}}})
    assert m["buys_24h"] == 0 and m["sells_24h"] == 0
    assert m["tx_count_24h"] == 0


def test_selection_ignores_non_dict_entries(ds):
    pairs = [None, "garbage", 42, {"chainId": "base", "liquidity": {"usd": 5.0}}]
    assert ds.select_primary_pair(pairs)["liquidity"]["usd"] == 5.0


def test_pool_with_no_liquidity_key_does_not_crash_selection(ds):
    pairs = [{"chainId": "base"}, {"chainId": "base", "liquidity": {"usd": 10.0}}]
    assert ds.select_primary_pair(pairs)["liquidity"]["usd"] == 10.0


# ===========================================================================
# GeckoTerminal
# ===========================================================================
def test_unwraps_the_jsonapi_envelope(gt):
    attrs = gt._attributes(load("geckoterminal_token.json"))
    assert attrs["symbol"] == "EXMP"


def test_extract_maps_expected_fields(gt):
    attrs = gt._attributes(load("geckoterminal_token.json"))
    m = gt.extract(attrs)
    assert m["price_usd"] == pytest.approx(0.041150)
    assert m["liquidity_usd"] == pytest.approx(1_512_340.75)
    assert m["volume_24h"] == pytest.approx(2_185_000.0)
    assert m["market_cap_usd"] == pytest.approx(9_010_000.0)


def test_total_supply_is_scaled_by_decimals(gt):
    """Raw base units would be off by 1e18 and make every supply ratio nonsense."""
    import asyncio

    async def fake_token(_addr):
        return gt._attributes(load("geckoterminal_token.json"))

    gt.token = fake_token  # type: ignore[assignment]
    m = asyncio.run(gt.token_market("0xabc"))
    assert m["total_supply"] == pytest.approx(120_000_000.0)
    assert "decimals" not in m


@pytest.mark.parametrize("payload", [
    None, {}, {"data": None}, {"data": []}, {"data": {"attributes": None}},
    {"data": "garbage"}, [1, 2, 3],
])
def test_broken_envelopes_return_none(gt, payload):
    assert gt._attributes(payload) is None


def test_data_list_form_takes_the_first_entry(gt):
    payload = {"data": [{"attributes": {"symbol": "AAA"}}, {"attributes": {"symbol": "BBB"}}]}
    assert gt._attributes(payload)["symbol"] == "AAA"


@pytest.mark.parametrize("attrs", [
    {},
    {"price_usd": None, "total_reserve_in_usd": None, "volume_usd": None},
    {"price_usd": "", "volume_usd": {}},
    {"volume_usd": "not-a-dict-or-number"},
])
def test_missing_attributes_yield_none(gt, attrs):
    m = gt.extract(attrs)
    assert m["price_usd"] is None
    assert m["liquidity_usd"] is None
    assert m["volume_24h"] is None


def test_volume_accepts_both_scalar_and_nested_forms(gt):
    assert gt.extract({"volume_usd": {"h24": "123.5"}})["volume_24h"] == pytest.approx(123.5)
    assert gt.extract({"volume_usd": "99.5"})["volume_24h"] == pytest.approx(99.5)


def test_client_disabled_without_a_network_slug():
    """Guessing a slug would query the wrong chain, so it refuses to run."""
    assert GeckoTerminalClient(network="").enabled is False
    assert GeckoTerminalClient(network="base").enabled is True


# ===========================================================================
# Cross-provider: the whole point is two independent prices
# ===========================================================================
def test_two_providers_give_reconcilable_prices(ds, gt):
    from app.util.reconcile import reconcile_prices

    ds_price = ds.extract(ds.select_primary_pair(load("dexscreener_token.json")["pairs"]))["price_usd"]
    gt_price = gt.extract(gt._attributes(load("geckoterminal_token.json")))["price_usd"]

    rec = reconcile_prices({"dex_pool": ds_price, "geckoterminal": gt_price})
    assert rec["trusted"] is True, "two sources must enable the cross-check"
    assert rec["divergence_pct"] < 1.0


def test_price_agreement_gate_passes_with_both_providers(now, settings):
    """Regression guard for the finding that one price source makes LIVE_BUY
    unreachable regardless of score."""
    from app.pipeline.risk import evaluate_gates
    from app.schemas import Severity
    from tests.conftest import make_snapshot

    snap = make_snapshot(now, price_sources={"dex_pool": 0.04110, "geckoterminal": 0.04115},
                         price_divergence_pct=0.06)
    gate = next(g for g in evaluate_gates(snap, settings) if g.name == "price_agreement")
    assert gate.passed is True

    single = make_snapshot(now, price_sources={"dex_pool": 0.04110}, price_divergence_pct=None)
    gate_single = next(g for g in evaluate_gates(single, settings) if g.name == "price_agreement")
    assert gate_single.passed is False
    assert gate_single.severity == Severity.LIVE_ONLY


def test_liquidity_reconciliation_stays_pessimistic(ds, gt):
    from app.util.reconcile import reconcile_liquidity

    ds_liq = ds.extract(ds.select_primary_pair(load("dexscreener_token.json")["pairs"]))["liquidity_usd"]
    gt_liq = gt.extract(gt._attributes(load("geckoterminal_token.json")))["liquidity_usd"]
    assert reconcile_liquidity({"dex_pool": ds_liq, "geckoterminal": gt_liq}) == min(ds_liq, gt_liq)


# ===========================================================================
# Source precedence — first writer wins
# ===========================================================================
def test_set_field_does_not_let_a_later_source_overwrite_an_earlier_one(now):
    """Regression guard. GeckoTerminal was silently clobbering DexScreener's
    volume, mixing one provider's 24h window with another's — which makes the
    turnover and spike gates compare unlike numbers."""
    from app.schemas import NormalizedSnapshot, TokenRef

    snap = NormalizedSnapshot(token=TokenRef(address="0x" + "aa" * 20), captured_at=now)
    snap.set_field("volume_24h", 2_200_000.0, "dexscreener")
    snap.set_field("volume_24h", 2_185_000.0, "geckoterminal")

    assert snap.volume_24h == pytest.approx(2_200_000.0)
    assert snap.sources["volume_24h"] == "dexscreener"


def test_a_later_source_still_fills_a_field_left_empty(now):
    from app.schemas import NormalizedSnapshot, TokenRef

    snap = NormalizedSnapshot(token=TokenRef(address="0x" + "aa" * 20), captured_at=now)
    snap.set_field("volume_24h", None, "dexscreener")
    snap.set_field("volume_24h", 500.0, "geckoterminal")

    assert snap.volume_24h == pytest.approx(500.0)
    assert snap.sources["volume_24h"] == "geckoterminal"


def test_overwrite_is_available_for_an_authoritative_source(now):
    from app.schemas import NormalizedSnapshot, TokenRef

    snap = NormalizedSnapshot(token=TokenRef(address="0x" + "aa" * 20), captured_at=now)
    snap.set_field("total_supply", 1.0, "okx_market")
    snap.set_field("total_supply", 42.0, "rh_node", overwrite=True)
    assert snap.total_supply == pytest.approx(42.0)
    assert snap.sources["total_supply"] == "rh_node"
