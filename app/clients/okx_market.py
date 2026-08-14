"""Official OKX OnchainOS Market API v6 client.

Basic endpoints are the cheapest core: supported chains, search, basic info,
hot tokens, top liquidity and recent trades. Price info, advanced risk info and
top holders are Premium endpoints, but OKX's Free plan currently includes a
monthly Premium allowance; they can be disabled with one setting.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
from typing import Any

from app.clients.base import BaseHTTPClient, ClientError, pick, to_float, to_int

PATH_SUPPORTED_CHAINS = "/api/v6/dex/market/supported/chain"
PATH_TOKEN_SEARCH = "/api/v6/dex/market/token/search"
PATH_TOKEN_BASIC_INFO = "/api/v6/dex/market/token/basic-info"
PATH_PRICE_INFO = "/api/v6/dex/market/price-info"
PATH_HOT_TOKEN = "/api/v6/dex/market/token/hot-token"
PATH_TOP_LIQUIDITY = "/api/v6/dex/market/token/top-liquidity"
PATH_TRADES = "/api/v6/dex/market/trades"
PATH_ADVANCED_INFO = "/api/v6/dex/market/token/advanced-info"
PATH_HOLDERS = "/api/v6/dex/market/token/holder"


def okx_timestamp() -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def okx_sign(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    msg = f"{timestamp}{method.upper()}{request_path}{body}"
    mac = hmac.new(secret.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


class OKXAuthMixin:
    api_key: str
    api_secret: str
    passphrase: str
    project_id: str = ""
    simulated: bool = False

    def auth_headers(self, method: str, path: str, body: str) -> dict[str, str]:
        if not (self.api_key and self.api_secret and self.passphrase):
            return {"Content-Type": "application/json"}
        ts = okx_timestamp()
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": okx_sign(self.api_secret, ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.project_id:
            headers["OK-ACCESS-PROJECT"] = self.project_id
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        return headers


class OKXMarketClient(OKXAuthMixin, BaseHTTPClient):
    name = "okx_market"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        project_id: str = "",
        *,
        premium_enabled: bool = True,
        **kw: Any,
    ) -> None:
        super().__init__(base_url, **kw)
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.project_id = project_id
        self.premium_enabled = premium_enabled
        self.enabled = bool(base_url)

    @staticmethod
    def _unwrap(data: Any) -> Any:
        if isinstance(data, dict) and "code" in data:
            if str(data.get("code")) not in ("0", "00000"):
                raise ClientError(f"okx_market code={data.get('code')} msg={data.get('msg')}")
            return data.get("data")
        return data

    async def supported_chains(self) -> list[dict[str, Any]]:
        data = self._unwrap(await self.request("GET", PATH_SUPPORTED_CHAINS, cache_key="chains"))
        return data if isinstance(data, list) else []

    async def token_search(
        self, query: str, chain_index: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Official params are ``chains`` and ``search`` (not keyword)."""
        params: dict[str, Any] = {"search": query, "limit": str(min(max(limit, 1), 100))}
        if chain_index:
            params["chains"] = chain_index
        data = self._unwrap(
            await self.request("GET", PATH_TOKEN_SEARCH, params=params, cache_key=f"search:{chain_index}:{query}")
        )
        if isinstance(data, dict):
            data = pick(data, "tokens", "list", default=[])
        return data if isinstance(data, list) else []

    async def token_basic_info(self, chain_index: str, token_address: str) -> dict[str, Any] | None:
        body = [{"chainIndex": chain_index, "tokenContractAddress": token_address}]
        data = self._unwrap(await self.request("POST", PATH_TOKEN_BASIC_INFO, json_body=body))
        return data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)

    async def price_info(self, chain_index: str, token_addresses: list[str]) -> dict[str, dict[str, Any]]:
        if not self.premium_enabled or not token_addresses:
            return {}
        body = [
            {"chainIndex": chain_index, "tokenContractAddress": address}
            for address in token_addresses[:100]
        ]
        data = self._unwrap(await self.request("POST", PATH_PRICE_INFO, json_body=body))
        return self._by_address(data)

    async def hot_tokens(
        self,
        chain_index: str,
        *,
        time_frame: int = 4,
        limit: int = 100,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "rankingType": "4",  # trending
            "chainIndex": chain_index,
            "rankingTimeFrame": str(time_frame),
            "riskFilter": "true",
            "limit": str(min(max(limit, 1), 100)),
        }
        for key, value in (filters or {}).items():
            if value is not None:
                params[key] = str(value).lower() if isinstance(value, bool) else str(value)
        data = self._unwrap(await self.request("GET", PATH_HOT_TOKEN, params=params))
        if isinstance(data, dict):
            data = pick(data, "tokens", "list", default=[])
        return data if isinstance(data, list) else []

    async def top_liquidity(self, chain_index: str, token_address: str) -> list[dict[str, Any]]:
        params = {"chainIndex": chain_index, "tokenContractAddress": token_address}
        data = self._unwrap(await self.request("GET", PATH_TOP_LIQUIDITY, params=params))
        if isinstance(data, dict):
            data = pick(data, "liquidityList", "list", default=[])
        return data if isinstance(data, list) else []

    async def trades(self, chain_index: str, token_address: str, limit: int = 500) -> list[dict[str, Any]]:
        params = {
            "chainIndex": chain_index,
            "tokenContractAddress": token_address,
            "limit": str(min(max(limit, 1), 500)),
        }
        data = self._unwrap(await self.request("GET", PATH_TRADES, params=params))
        if isinstance(data, dict):
            data = pick(data, "trades", "list", default=[])
        return data if isinstance(data, list) else []

    async def advanced_info(self, chain_index: str, token_address: str) -> dict[str, Any] | None:
        if not self.premium_enabled:
            return None
        params = {"chainIndex": chain_index, "tokenContractAddress": token_address}
        data = self._unwrap(await self.request("GET", PATH_ADVANCED_INFO, params=params))
        return data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)

    async def holders(self, chain_index: str, token_address: str, limit: int = 100) -> list[dict[str, Any]]:
        if not self.premium_enabled:
            return []
        params = {
            "chainIndex": chain_index,
            "tokenContractAddress": token_address,
            "limit": str(min(max(limit, 1), 100)),
        }
        data = self._unwrap(await self.request("GET", PATH_HOLDERS, params=params))
        if isinstance(data, dict):
            data = pick(data, "holders", "list", default=[])
        return data if isinstance(data, list) else []

    @staticmethod
    def _by_address(data: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                address = pick(item, "tokenContractAddress", "tokenAddress")
                if address:
                    out[str(address).lower()] = item
        return out

    @staticmethod
    def extract_market(item: dict[str, Any]) -> dict[str, Any]:
        """Exact documented keys first; safe legacy aliases second."""
        return {
            "price_usd": to_float(pick(item, "price", "priceUsd")),
            "liquidity_usd": to_float(pick(item, "liquidity", "liquidityUsd")),
            "volume_5m": to_float(pick(item, "volume5M", "volume5m")),
            "volume_1h": to_float(pick(item, "volume1H", "volume1h")),
            "volume_24h": to_float(pick(item, "volume24H", "volume24h")),
            "market_cap_usd": to_float(pick(item, "marketCap", "marketCapUsd")),
            "circulating_supply": to_float(pick(item, "circSupply", "circulatingSupply")),
            "unique_holders": to_int(pick(item, "holders", "holderCount")),
            "price_change_1h_pct": to_float(pick(item, "priceChange1H", "priceChange1h")),
            "price_change_24h_pct": to_float(pick(item, "priceChange24H", "priceChange24h")),
            "tx_count_24h": to_int(pick(item, "txs24H", "txCount24H")),
        }

    async def probe(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        try:
            chains = await self.supported_chains()
            robinhood = [c for c in chains if str(c.get("chainIndex")) == "4663"]
            return {"enabled": True, "ok": bool(robinhood), "robinhood": robinhood[:1]}
        except ClientError as exc:
            return {"enabled": True, "ok": False, "error": str(exc)}
