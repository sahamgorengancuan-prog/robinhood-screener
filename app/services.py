"""Client container.

One place that knows how to construct every client from settings, so jobs, the
API and scripts all share the same configuration and connection pools.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.clients.chainlink import ChainlinkClient
from app.clients.explorer import ExplorerClient
from app.clients.okx_market import OKXMarketClient
from app.clients.okx_trade import OKXTradeClient
from app.clients.rh_data import RobinhoodDataClient
from app.clients.rh_node import RobinhoodNodeClient
from app.config import Settings, get_settings


@dataclass
class Services:
    settings: Settings
    node: RobinhoodNodeClient
    data: RobinhoodDataClient
    explorer: ExplorerClient
    market: OKXMarketClient
    trade: OKXTradeClient
    chainlink: ChainlinkClient

    async def aclose(self) -> None:
        for c in (self.node, self.data, self.explorer, self.market, self.trade):
            await c.aclose()


def build_services(settings: Settings | None = None) -> Services:
    c = settings or get_settings()
    common = {
        "max_retries": c.http_max_retries,
        "backoff_base_s": c.http_backoff_base_s,
        "rps": c.http_rate_limit_rps,
    }

    node = RobinhoodNodeClient(c.rh_node_rpc_url, timeout_s=c.rh_node_timeout_s, **common)
    data = RobinhoodDataClient(
        c.rh_data_base_url if c.rh_data_enabled else "",
        c.rh_data_api_key,
        paths={
            "token_list": c.rh_data_path_token_list,
            "token_meta": c.rh_data_path_token_meta,
            "token_holders": c.rh_data_path_token_holders,
            "token_transfers": c.rh_data_path_token_transfers,
        },
        **common,
    )
    explorer = ExplorerClient(
        c.explorer_base_url if c.explorer_enabled else "", timeout_s=c.explorer_timeout_s, **common
    )
    market = OKXMarketClient(
        c.okx_web3_base_url if c.okx_market_enabled else "",
        c.okx_api_key,
        c.okx_api_secret,
        c.okx_api_passphrase,
        c.okx_project_id,
        timeout_s=c.okx_market_timeout_s,
        **common,
    )
    trade = OKXTradeClient(
        c.okx_cex_base_url,
        c.okx_trade_api_key,
        c.okx_trade_api_secret,
        c.okx_trade_api_passphrase,
        simulated=c.okx_simulated,
        timeout_s=c.okx_trade_timeout_s,
        **common,
    )
    chainlink = ChainlinkClient(node, c.chainlink_feeds_json if c.chainlink_enabled else "{}")

    return Services(settings=c, node=node, data=data, explorer=explorer,
                    market=market, trade=trade, chainlink=chainlink)
