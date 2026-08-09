"""Blockscout explorer client — contract source verification status.

Blockscout's v2 API is open source and stable across deployments, which is why
it is used here instead of a paid contract-audit provider. One rule depends on
it: an unverified contract can never reach LIVE_BUY.

If the explorer is unreachable, `verified` stays `None` — which is treated as
"cannot verify" (reject for live), not as "verified".
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.base import BaseHTTPClient, ClientError, pick

log = logging.getLogger(__name__)

PATH_CONTRACT = "/api/v2/smart-contracts/{address}"
PATH_TOKEN = "/api/v2/tokens/{address}"
PATH_TOKEN_HOLDERS = "/api/v2/tokens/{address}/holders"
PATH_TOKEN_COUNTERS = "/api/v2/tokens/{address}/counters"


class ExplorerClient(BaseHTTPClient):
    name = "explorer"

    def __init__(self, base_url: str, **kw: Any) -> None:
        super().__init__(base_url, headers={"Accept": "application/json"}, **kw)
        self.enabled = bool(base_url)

    async def contract_info(self, address: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            return await self.request(
                "GET", PATH_CONTRACT.format(address=address), cache_key=f"contract:{address}"
            )
        except ClientError as e:
            log.info("explorer contract_info miss for %s: %s", address, e)
            return None

    async def is_verified(self, address: str) -> bool | None:
        info = await self.contract_info(address)
        if info is None:
            return None
        v = pick(info, "is_verified", "isVerified", "verified")
        if isinstance(v, bool):
            return v
        # A non-empty source listing is equivalent evidence.
        if pick(info, "source_code", "sourceCode"):
            return True
        return None

    async def token_info(self, address: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            return await self.request("GET", PATH_TOKEN.format(address=address), cache_key=f"tok:{address}")
        except ClientError:
            return None

    async def token_holders(self, address: str) -> list[dict[str, Any]]:
        """Top holders. Blockscout returns them balance-descending."""
        if not self.enabled:
            return []
        try:
            data = await self.request("GET", PATH_TOKEN_HOLDERS.format(address=address))
        except ClientError:
            return []
        items = pick(data, "items", "result", default=[])
        return items if isinstance(items, list) else []

    async def token_counters(self, address: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            return await self.request("GET", PATH_TOKEN_COUNTERS.format(address=address))
        except ClientError:
            return None
