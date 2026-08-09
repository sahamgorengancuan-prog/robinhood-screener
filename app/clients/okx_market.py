"""OKX Market (Web3 / DEX) API client.

Used for four things:
  1. Token Search       — does this contract exist on any OKX-indexed venue?
  2. Token Basic Info   — metadata cross-check against on-chain values.
  3. Token Price Info   — price, volume, supply, holders, liquidity.
  4. Liquidity / price WS channel — real-time refresh before an order.

Endpoint paths are configurable constants at the top of the file. The two that
appeared in OKX's public docs index are marked VERIFIED-PATH; the others are
marked UNVERIFIED and are read defensively. See docs/ENDPOINTS.md.

Auth is OKX's standard scheme: HMAC-SHA256 over
`timestamp + METHOD + requestPath + body`, base64 encoded, with the Web3 API
additionally requiring `OK-ACCESS-PROJECT`.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import logging
from typing import Any

from app.clients.base import BaseHTTPClient, ClientError, pick, to_float, to_int

log = logging.getLogger(__name__)

# VERIFIED-PATH (present in OKX public API reference index)
PATH_TOKEN_SEARCH = "/api/v6/dex/market/token/search"
PATH_PRICE_INFO = "/api/v6/dex/market/price-info"
# UNVERIFIED — confirm against your account's docs before relying on it.
PATH_TOKEN_BASIC_INFO = "/api/v6/dex/market/token/basic-info"


def okx_timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.datetime.now().microsecond // 1000:03d}Z"


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
        **kw: Any,
    ) -> None:
        super().__init__(base_url, **kw)
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.project_id = project_id
        self.enabled = bool(base_url)

    @staticmethod
    def _unwrap(data: Any) -> Any:
        """OKX wraps payloads as {'code':'0','msg':'','data':[...]}."""
        if isinstance(data, dict) and "code" in data:
            code = str(data.get("code"))
            if code not in ("0", "00000"):
                raise ClientError(f"okx_market error code={code} msg={data.get('msg')}")
            return data.get("data")
        return data

    # ------------------------------------------------------------------ search
    async def token_search(self, query: str, chain_index: str | None = None) -> list[dict[str, Any]]:
        """Search by symbol, name or contract address. Empty list = not listed."""
        if not self.enabled:
            return []
        params: dict[str, Any] = {"keyword": query, "search": query}
        if chain_index:
            params["chainIndex"] = chain_index
        try:
            data = self._unwrap(
                await self.request("GET", PATH_TOKEN_SEARCH, params=params, cache_key=f"search:{query}")
            )
        except ClientError as e:
            log.warning("okx token_search failed for %s: %s", query, e)
            raise
        if isinstance(data, dict):
            data = pick(data, "list", "tokens", "items", default=[])
        return data if isinstance(data, list) else []

    async def token_basic_info(self, chain_index: str, token_address: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        body = [{"chainIndex": chain_index, "tokenContractAddress": token_address}]
        try:
            data = self._unwrap(await self.request("POST", PATH_TOKEN_BASIC_INFO, json_body=body))
        except ClientError as e:
            log.info("okx token_basic_info unavailable (%s)", e)
            return None
        if isinstance(data, list):
            return data[0] if data else None
        return data if isinstance(data, dict) else None

    # -------------------------------------------------------------- price info
    async def price_info(self, chain_index: str, token_addresses: list[str]) -> dict[str, dict[str, Any]]:
        """Batch price/volume/liquidity/holders lookup, keyed by lowercase address."""
        if not self.enabled or not token_addresses:
            return {}
        body = [
            {"chainIndex": chain_index, "tokenContractAddress": a} for a in token_addresses[:100]
        ]
        data = self._unwrap(await self.request("POST", PATH_PRICE_INFO, json_body=body))
        out: dict[str, dict[str, Any]] = {}
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                addr = pick(item, "tokenContractAddress", "tokenAddress", "address", "contractAddress")
                if addr:
                    out[str(addr).lower()] = item
        return out

    # ------------------------------------------------------------- extraction
    @staticmethod
    def extract_market(item: dict[str, Any]) -> dict[str, Any]:
        """Map an OKX price-info item onto our normalized names.

        Every value is `None` when absent — never coerced to 0.
        """
        return {
            "price_usd": to_float(pick(item, "price", "priceUsd", "lastPrice", "usdPrice")),
            "liquidity_usd": to_float(pick(item, "liquidity", "liquidityUsd", "liquidityInUsd", "poolLiquidity")),
            "volume_1h": to_float(pick(item, "volume1H", "volume1h", "vol1H", "volumeH1")),
            "volume_24h": to_float(pick(item, "volume24H", "volume24h", "vol24H", "volumeH24")),
            "volume_5m": to_float(pick(item, "volume5M", "volume5m", "vol5M", "volumeM5")),
            "market_cap_usd": to_float(pick(item, "marketCap", "marketCapUsd", "mktCap")),
            "fdv_usd": to_float(pick(item, "fdv", "fullyDilutedValuation", "fdvUsd")),
            "total_supply": to_float(pick(item, "totalSupply", "supply", "maxSupply")),
            "circulating_supply": to_float(pick(item, "circulatingSupply", "circulatingAmount")),
            "unique_holders": to_int(pick(item, "holders", "holderCount", "holdersCount", "uniqueHolders")),
            "price_change_1h_pct": to_float(pick(item, "priceChange1H", "change1H", "priceChangePercent1H")),
            "price_change_24h_pct": to_float(pick(item, "priceChange24H", "change24H", "priceChangePercent24H")),
            "tx_count_24h": to_int(pick(item, "txs24H", "txCount24H", "tradeCount24H", "txns24H")),
        }

    async def probe(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        try:
            res = await self.token_search("USDC")
            return {"enabled": True, "ok": True, "results": len(res),
                    "sample_keys": sorted(res[0].keys()) if res else []}
        except ClientError as e:
            return {"enabled": True, "ok": False, "error": str(e)}
