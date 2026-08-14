from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.clients.okx_market import OKXMarketClient
from app.clients.okx_trade import OKXTradeClient
from app.clients.rh_node import RobinhoodNodeClient, TRANSFER_TOPIC, ZERO_ADDRESS_TOPIC
from app.config import Settings
from app.pipeline.normalize import RawBundle, normalize
from app.schemas import TokenRef


@pytest.mark.asyncio
async def test_okx_search_uses_official_chains_and_search_params():
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed.update(dict(request.url.params))
        return httpx.Response(200, json={"code": "0", "data": [{
            "chainIndex": "4663", "tokenContractAddress": "0xabc",
        }]})

    client = OKXMarketClient("https://web3.okx.com", max_retries=0)
    client._client = httpx.AsyncClient(base_url=client.base_url, transport=httpx.MockTransport(handler))
    try:
        rows = await client.token_search("0xabc", "4663")
    finally:
        await client.aclose()
    assert observed["chains"] == "4663"
    assert observed["search"] == "0xabc"
    assert "keyword" not in observed and rows[0]["tokenContractAddress"] == "0xabc"


def test_okx_price_info_maps_documented_circ_supply_key():
    mapped = OKXMarketClient.extract_market({
        "price": "0.25", "circSupply": "1234", "liquidity": "500000",
        "volume5M": "10", "volume1H": "100", "volume24H": "1000",
        "txs24H": "300", "holders": "400",
    })
    assert mapped["circulating_supply"] == pytest.approx(1234)
    assert mapped["tx_count_24h"] == 300


@pytest.mark.asyncio
async def test_cex_identity_requires_chain_and_contract_suffix(monkeypatch):
    client = OKXTradeClient(
        "https://www.okx.com", api_key="k", api_secret="s", passphrase="p"
    )

    async def currencies(_ccy=None):
        return [{"ccy": "ABC", "chain": "ABC-Robinhood", "ctAddr": "...12abcd"}]

    async def instrument(_base, _quote="USDT"):
        return "ABC-USDT"

    monkeypatch.setattr(client, "currencies", currencies)
    monkeypatch.setattr(client, "find_spot_instrument", instrument)
    inst, available, reason = await client.resolve_contract_spot(
        symbol="ABC", contract_address="0x0000000000000000000000000000000012abcd",
        chain_hint="Robinhood",
    )
    assert inst == "ABC-USDT" and available is True and "matched" in reason

    inst, available, _ = await client.resolve_contract_spot(
        symbol="ABC", contract_address="0x00000000000000000000000000000000ffffff",
        chain_hint="Robinhood",
    )
    assert inst is None and available is False


@pytest.mark.asyncio
async def test_node_mint_discovery_rejects_erc721_shape(monkeypatch):
    node = RobinhoodNodeClient("https://rpc.example", max_retries=0)
    erc20 = {
        "address": "0x" + "11" * 20,
        "topics": [TRANSFER_TOPIC, ZERO_ADDRESS_TOPIC, "0x" + "22" * 32],
        "data": "0x64",
    }
    erc721 = {
        "address": "0x" + "33" * 20,
        "topics": [TRANSFER_TOPIC, ZERO_ADDRESS_TOPIC, "0x" + "44" * 32, "0x1"],
        "data": "0x",
    }

    async def rpc(_method, _params):
        return [erc20, erc721]

    monkeypatch.setattr(node, "rpc", rpc)
    assert await node.discover_minted_contracts(1, 2) == [erc20["address"]]


def test_okx_holder_percent_and_trade_sample_feed_normalizer():
    bundle = RawBundle(TokenRef(address="0x" + "ab" * 20, symbol="ABC"))
    bundle.okx_holders = [
        {"holderWalletAddress": "0x" + "01" * 20, "holdPercent": "4.5"},
        {"holderWalletAddress": "0x" + "02" * 20, "holdPercent": "3.5"},
    ]
    bundle.okx_trades = [
        {
            "userAddress": f"0x{i:040x}", "volume": "100",
            "type": "buy" if i % 2 else "sell", "isFiltered": "0",
        }
        for i in range(40)
    ]
    snap = normalize(
        bundle,
        Settings(_env_file=None),
        now=dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc),
    )
    assert snap.top1_holder_pct == pytest.approx(4.5)
    assert snap.top10_holder_pct == pytest.approx(8.0)
    assert snap.trade_sample_size == 40
    assert snap.unique_trader_ratio == pytest.approx(1.0)
