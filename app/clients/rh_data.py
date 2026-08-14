"""Indexed Robinhood Chain data through Alchemy's official APIs.

Robinhood's connection guide recommends Alchemy and its Robinhood API overview
lists the Token and Transfers APIs. They are JSON-RPC methods on the Alchemy
chain URL; there is no documented generic ``/tokens`` or ``/holders`` REST
route. Discovery therefore lives in OKX hot-token plus ``eth_getLogs`` and
holder concentration comes from OKX/Blockscout.
"""

from __future__ import annotations

from typing import Any

from app.clients.base import BaseHTTPClient, ClientError


class RobinhoodDataClient(BaseHTTPClient):
    name = "rh_data"

    def __init__(
        self,
        rpc_url: str = "",
        api_key: str = "",
        *,
        portfolio_base_url: str = "https://api.g.alchemy.com",
        network: str = "robinhood-mainnet",
        **kw: Any,
    ) -> None:
        url = rpc_url.strip()
        if not url and api_key:
            url = f"https://robinhood-mainnet.g.alchemy.com/v2/{api_key}"
        super().__init__(url, headers={"Accept": "application/json"}, **kw)
        self.api_key = api_key
        self.portfolio_base_url = portfolio_base_url.rstrip("/")
        self.network = network
        self.enabled = bool(url)
        self._id = 0

    async def rpc(self, method: str, params: list[Any]) -> Any:
        if not self.enabled:
            raise ClientError("rh_data: RH_DATA_RPC_URL or RH_DATA_API_KEY is required")
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        data = await self.request("POST", "", json_body=payload)
        if not isinstance(data, dict):
            raise ClientError(f"rh_data {method}: unexpected response {type(data)}")
        if data.get("error"):
            raise ClientError(f"rh_data {method}: {data['error']}")
        return data.get("result")

    async def token_metadata(self, token_address: str) -> dict[str, Any] | None:
        """Official ``alchemy_getTokenMetadata``."""
        result = await self.rpc("alchemy_getTokenMetadata", [token_address])
        return result if isinstance(result, dict) else None

    async def token_meta(self, address: str) -> dict[str, Any] | None:
        """Compatibility alias used by diagnostics."""
        return await self.token_metadata(address)

    async def token_balances(
        self, owner_address: str, token_addresses: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """Official ``alchemy_getTokenBalances`` (wallet-centric, not holders)."""
        selector: Any = token_addresses if token_addresses else "erc20"
        result = await self.rpc("alchemy_getTokenBalances", [owner_address, selector])
        if not isinstance(result, dict):
            return []
        rows = result.get("tokenBalances")
        return rows if isinstance(rows, list) else []

    async def asset_transfers(
        self,
        *,
        from_block: str = "0x0",
        to_block: str = "latest",
        contract_addresses: list[str] | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
        max_count: int = 1000,
        page_key: str | None = None,
    ) -> dict[str, Any]:
        """Official ``alchemy_getAssetTransfers`` for ERC-20 activity."""
        params: dict[str, Any] = {
            "fromBlock": from_block,
            "toBlock": to_block,
            "category": ["erc20"],
            "withMetadata": True,
            "excludeZeroValue": True,
            "maxCount": hex(min(max(max_count, 1), 1000)),
        }
        if contract_addresses:
            params["contractAddresses"] = contract_addresses
        if from_address:
            params["fromAddress"] = from_address
        if to_address:
            params["toAddress"] = to_address
        if page_key:
            params["pageKey"] = page_key
        result = await self.rpc("alchemy_getAssetTransfers", [params])
        return result if isinstance(result, dict) else {"transfers": []}

    async def token_transfers(self, address: str, limit: int = 200) -> list[dict[str, Any]]:
        result = await self.asset_transfers(contract_addresses=[address], max_count=limit)
        rows = result.get("transfers")
        return rows if isinstance(rows, list) else []

    async def portfolio_tokens(self, addresses: list[str]) -> dict[str, Any]:
        """Optional Alchemy Portfolio API ``POST /assets/tokens/by-address``."""
        if not self.api_key:
            raise ClientError("rh_data: RH_DATA_API_KEY is required for Portfolio API")
        body = {
            "addresses": [{"address": a, "networks": [self.network]} for a in addresses],
            "withMetadata": True,
            "withPrices": False,
        }
        url = f"{self.portfolio_base_url}/data/v1/{self.api_key}/assets/tokens/by-address"
        data = await self.request("POST", url, json_body=body)
        return data if isinstance(data, dict) else {}

    async def list_tokens(self, limit: int = 100, cursor: str | None = None) -> list[dict[str, Any]]:
        """No documented Alchemy global token-list endpoint exists."""
        return []

    async def token_holders(self, address: str, limit: int = 100) -> dict[str, Any]:
        """Alchemy's documented Token API is wallet-centric, not a holder list."""
        return {"holders": [], "total": None, "unsupported": True}

    async def probe(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        try:
            chain_id = await self.rpc("eth_chainId", [])
            return {"enabled": True, "ok": True, "chain_id_hex": chain_id}
        except ClientError as exc:
            return {"enabled": True, "ok": False, "error": str(exc)}
