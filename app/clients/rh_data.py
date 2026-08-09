"""Robinhood Chain Data API client (indexed data).

IMPORTANT — READ BEFORE TRUSTING THIS FILE
------------------------------------------
The exact request/response contract of the Robinhood Chain Data API could not
be verified from this build environment (`docs.robinhood.com` is blocked by the
egress proxy). Rather than invent field names and have them silently parse to
wrong values, this client is built so that:

  * every path is a config template (`RH_DATA_PATH_*` in .env),
  * every field is read through `pick()` against several plausible key names,
  * anything not found stays `None` and is recorded in `missing_fields`,
  * the whole client is disabled by default (`RH_DATA_ENABLED=false`).

Bring it up with `python -m scripts.probe_endpoints`, read the dumped payload,
then set the paths and — if the key names differ from the candidates below —
extend the candidate lists. `docs/ENDPOINTS.md` tracks what is verified.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.base import BaseHTTPClient, ClientError, pick, to_float, to_int

log = logging.getLogger(__name__)


class RobinhoodDataClient(BaseHTTPClient):
    name = "rh_data"

    def __init__(self, base_url: str, api_key: str = "", *, paths: dict[str, str] | None = None, **kw: Any):
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key
        super().__init__(base_url, headers=headers, **kw)
        self.paths = paths or {}
        self.enabled = bool(base_url)

    def _path(self, key: str, **fmt: Any) -> str:
        tpl = self.paths.get(key)
        if not tpl:
            raise ClientError(f"rh_data: no path configured for '{key}'")
        return tpl.format(**fmt)

    async def list_tokens(self, limit: int = 100, cursor: str | None = None) -> list[dict[str, Any]]:
        """Discovery feed: new / trending / active tokens."""
        if not self.enabled:
            return []
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = await self.request("GET", self._path("token_list"), params=params)
        items = pick(data, "data", "items", "tokens", "result", "results", default=data)
        if isinstance(items, dict):
            items = pick(items, "items", "tokens", "list", default=[])
        return items if isinstance(items, list) else []

    async def token_meta(self, address: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        data = await self.request(
            "GET", self._path("token_meta", address=address), cache_key=f"meta:{address}"
        )
        return pick(data, "data", "token", "result", default=data)

    async def token_holders(self, address: str, limit: int = 100) -> dict[str, Any]:
        """Returns {'holders': [{'address':..,'balance':..,'pct':..}], 'total': int|None}."""
        if not self.enabled:
            return {"holders": [], "total": None}
        data = await self.request(
            "GET", self._path("token_holders", address=address), params={"limit": limit}
        )
        container = pick(data, "data", "result", default=data)
        raw = pick(container, "holders", "items", "list", default=container)
        total = to_int(pick(container, "total", "holder_count", "holdersCount", "count"))

        holders: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for h in raw:
                if not isinstance(h, dict):
                    continue
                holders.append(
                    {
                        "address": pick(h, "address", "holder", "owner", "wallet"),
                        "balance": to_float(pick(h, "balance", "amount", "value", "quantity")),
                        "pct": to_float(pick(h, "percentage", "pct", "share", "percent")),
                    }
                )
        return {"holders": holders, "total": total}

    async def token_transfers(self, address: str, limit: int = 200) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        data = await self.request(
            "GET", self._path("token_transfers", address=address), params={"limit": limit}
        )
        container = pick(data, "data", "result", default=data)
        items = pick(container, "transfers", "items", "list", default=container)
        return items if isinstance(items, list) else []

    async def probe(self) -> dict[str, Any]:
        """Connectivity check used by scripts/probe_endpoints.py."""
        if not self.enabled:
            return {"enabled": False}
        try:
            sample = await self.list_tokens(limit=1)
            return {"enabled": True, "ok": True, "sample_keys": sorted(sample[0].keys()) if sample else []}
        except ClientError as e:
            return {"enabled": True, "ok": False, "error": str(e)}
