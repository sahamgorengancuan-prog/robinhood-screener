"""GeckoTerminal client — free, no API key, no signup.

Its job here is to be the **second, independent price source**. That is not a
nice-to-have: `reconcile_prices()` needs at least two sources to take a median
and measure divergence, and `gate_price_agreement` returns LIVE_ONLY with only
one — meaning LIVE_BUY is unreachable no matter how good the score is. Adding
this provider is what makes the top of the decision ladder attainable at all.

It also cross-checks liquidity. Liquidity is reconciled by taking the
**minimum** across sources, so a second opinion can only make sizing more
conservative, never less.

Endpoints (public, unauthenticated, JSON:API envelope):

    GET /api/v2/networks/{network}/tokens/{address}
    GET /api/v2/networks/{network}/tokens/{address}/pools

Free tier is roughly 30 calls/minute, which is why `MAX_TOKENS_PER_CYCLE` and
the shared rate limiter matter. Responses are wrapped as
`{"data": {"id": ..., "type": ..., "attributes": {...}}}` — the attributes dict
is where every value lives.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.base import BaseHTTPClient, ClientError, to_float

log = logging.getLogger(__name__)

PATH_TOKEN = "/api/v2/networks/{network}/tokens/{address}"
PATH_TOKEN_POOLS = "/api/v2/networks/{network}/tokens/{address}/pools"

EXPECTED_FIELDS = ("price_usd", "liquidity_usd", "volume_24h", "market_cap_usd")


class GeckoTerminalClient(BaseHTTPClient):
    name = "geckoterminal"

    def __init__(self, base_url: str = "https://api.geckoterminal.com", network: str = "", **kw: Any):
        super().__init__(base_url, headers={"Accept": "application/json"}, **kw)
        # GeckoTerminal network slug ("eth", "base", "arbitrum", ...). Without
        # it we cannot build a URL, so the client stays disabled rather than
        # guessing and querying the wrong chain.
        self.network = (network or "").strip().lower()
        self.enabled = bool(base_url and self.network)

    @staticmethod
    def _attributes(payload: Any) -> dict[str, Any] | None:
        """Unwrap the JSON:API envelope."""
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        attrs = data.get("attributes")
        return attrs if isinstance(attrs, dict) else None

    async def token(self, address: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        data = await self.request(
            "GET",
            PATH_TOKEN.format(network=self.network, address=address),
            cache_key=f"gt:{self.network}:{address}",
        )
        return self._attributes(data)

    @staticmethod
    def extract(attrs: dict[str, Any]) -> dict[str, Any]:
        """Map GeckoTerminal token attributes onto our normalized names."""
        volume = attrs.get("volume_usd")
        volume_24h = to_float(volume.get("h24")) if isinstance(volume, dict) else to_float(volume)

        return {
            "price_usd": to_float(attrs.get("price_usd")),
            # total_reserve_in_usd is the pooled value backing the token — the
            # closest equivalent to the liquidity figure other sources report.
            "liquidity_usd": to_float(attrs.get("total_reserve_in_usd")),
            "volume_24h": volume_24h,
            "market_cap_usd": to_float(attrs.get("market_cap_usd")),
            "fdv_usd": to_float(attrs.get("fdv_usd")),
            "total_supply": to_float(attrs.get("total_supply")),
            "decimals": attrs.get("decimals"),
            "symbol": attrs.get("symbol") or None,
            "name": attrs.get("name") or None,
        }

    async def token_market(self, address: str) -> dict[str, Any] | None:
        attrs = await self.token(address)
        if not attrs:
            return None
        out = self.extract(attrs)

        # total_supply arrives in base units; scale it if decimals are given.
        dec = out.pop("decimals", None)
        if out.get("total_supply") is not None and isinstance(dec, (int, float, str)):
            try:
                out["total_supply"] = out["total_supply"] / (10 ** int(dec))
            except (TypeError, ValueError, ZeroDivisionError, OverflowError):
                out["total_supply"] = None
        return out

    async def probe(self, address: str | None = None) -> dict[str, Any]:
        if not self.enabled:
            reason = ("GECKOTERMINAL_NETWORK not set — without a network slug the URL "
                      "cannot be built, and guessing would query the wrong chain")
            return {"enabled": False, "reason": reason}
        if not address:
            return {"enabled": True, "ok": None, "reason": "no token address supplied"}
        try:
            attrs = await self.token(address)
        except ClientError as e:
            msg = str(e)
            hint = ""
            if "404" in msg:
                hint = (f"404 — network slug '{self.network}' or the token is not indexed. "
                        f"Check the slug against /api/v2/networks.")
            elif "429" in msg:
                hint = "429 — free tier is ~30 calls/min. Lower MAX_TOKENS_PER_CYCLE."
            return {"enabled": True, "ok": False, "error": msg, "hint": hint}

        if not attrs:
            return {"enabled": True, "ok": False, "reason": "empty JSON:API data envelope"}

        mapped = self.extract(attrs)
        return {
            "enabled": True, "ok": True,
            "observed_keys": sorted(attrs.keys()),
            "parsed": [k for k in EXPECTED_FIELDS if mapped.get(k) is not None],
            "unparsed": [k for k in EXPECTED_FIELDS if mapped.get(k) is None],
        }
