"""DexScreener client — free, no API key, no signup.

Why this provider is the backbone of the market-data layer:

  * it is the only free source here that reports **buy vs sell transaction
    counts**, which is what `buy_ratio_24h` needs — the main organic-flow signal
    and previously the largest hole in the pipeline;
  * it reports volume across four windows (m5/h1/h6/h24), which drives the
    single-candle spike gate;
  * it reports pool liquidity in USD, which drives the slippage estimate;
  * `pairCreatedAt` gives a pool age that cross-checks the on-chain token age.

Endpoint (public, documented, unauthenticated):

    GET https://api.dexscreener.com/latest/dex/tokens/{addresses}

Rate limit is roughly 300 requests/minute for this endpoint. The screener's
5-minute cadence is nowhere near that.

MULTI-PAIR POLICY
-----------------
A token usually has several pools. This client picks the **single deepest pool
on the configured chain** and reads every metric from that one pool, rather than
summing across pools. Mixing them is how you get a wrong answer: summed volume
against one pool's liquidity inflates the turnover ratio and would trip the
wash-trading gate on a perfectly healthy token. The deepest pool is also the one
you would actually trade against, so it is the honest basis for slippage.

`pair_count` and `total_liquidity_usd` are still reported for context.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app.clients.base import BaseHTTPClient, ClientError, to_float, to_int

log = logging.getLogger(__name__)

PATH_TOKENS = "/latest/dex/tokens/{addresses}"

# Fields the normalizer depends on. The runtime contract check reports which of
# these were actually present, so a silent upstream change is visible.
EXPECTED_FIELDS = (
    "price_usd", "liquidity_usd", "volume_24h", "volume_1h", "volume_5m",
    "buys_24h", "sells_24h", "tx_count_24h", "price_change_24h_pct",
)


class DexScreenerClient(BaseHTTPClient):
    name = "dexscreener"

    def __init__(self, base_url: str = "https://api.dexscreener.com", chain_id: str = "", **kw: Any):
        super().__init__(base_url, headers={"Accept": "application/json"}, **kw)
        # DexScreener's own chain slug ("ethereum", "base", "arbitrum", ...).
        # Left blank means "accept pairs from any chain", which is only safe when
        # you know the address is unique — so the caller normally sets it.
        self.chain_slug = (chain_id or "").strip().lower()
        self.enabled = bool(base_url)

    async def token_pairs(self, address: str) -> list[dict[str, Any]]:
        """Raw pair list for one token address."""
        if not self.enabled:
            return []
        data = await self.request(
            "GET", PATH_TOKENS.format(addresses=address), cache_key=f"ds:{address}"
        )
        if not isinstance(data, dict):
            raise ClientError(f"dexscreener: unexpected payload type {type(data).__name__}")
        pairs = data.get("pairs")
        # A token with no pools returns {"pairs": null} rather than an error.
        return pairs if isinstance(pairs, list) else []

    def select_primary_pair(self, pairs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Deepest pool on the configured chain. See MULTI-PAIR POLICY above."""
        candidates = [p for p in pairs if isinstance(p, dict)]
        if self.chain_slug:
            on_chain = [p for p in candidates if str(p.get("chainId", "")).lower() == self.chain_slug]
            # Falling back to other chains would silently price a different asset.
            candidates = on_chain
        if not candidates:
            return None
        return max(candidates, key=lambda p: to_float((p.get("liquidity") or {}).get("usd")) or 0.0)

    @staticmethod
    def extract(pair: dict[str, Any]) -> dict[str, Any]:
        """Map one DexScreener pair onto our normalized names.

        Every value is `None` when absent — never coerced to 0, because a zero
        that means "unknown" is what lets a screener buy something it cannot see.
        """
        liq = pair.get("liquidity") or {}
        vol = pair.get("volume") or {}
        txns = pair.get("txns") or {}
        chg = pair.get("priceChange") or {}
        base = pair.get("baseToken") or {}

        h24 = txns.get("h24") or {}
        buys = to_int(h24.get("buys"))
        sells = to_int(h24.get("sells"))
        tx_total = (buys + sells) if (buys is not None and sells is not None) else None

        created_ms = to_float(pair.get("pairCreatedAt"))
        pair_age_hours = None
        if created_ms:
            created = dt.datetime.fromtimestamp(created_ms / 1000.0, dt.timezone.utc)
            pair_age_hours = (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600.0

        return {
            "price_usd": to_float(pair.get("priceUsd")),
            "liquidity_usd": to_float(liq.get("usd")),
            "volume_24h": to_float(vol.get("h24")),
            "volume_6h": to_float(vol.get("h6")),
            "volume_1h": to_float(vol.get("h1")),
            "volume_5m": to_float(vol.get("m5")),
            "buys_24h": buys,
            "sells_24h": sells,
            "tx_count_24h": tx_total,
            "price_change_24h_pct": to_float(chg.get("h24")),
            "price_change_1h_pct": to_float(chg.get("h1")),
            "fdv_usd": to_float(pair.get("fdv")),
            "market_cap_usd": to_float(pair.get("marketCap")),
            "pair_age_hours": pair_age_hours,
            "symbol": base.get("symbol") or None,
            "name": base.get("name") or None,
            "dex_id": pair.get("dexId") or None,
            "pair_address": pair.get("pairAddress") or None,
            "chain_id": pair.get("chainId") or None,
        }

    async def token_market(self, address: str) -> dict[str, Any] | None:
        """Everything we need about one token, from its deepest pool."""
        pairs = await self.token_pairs(address)
        if not pairs:
            return None
        primary = self.select_primary_pair(pairs)
        if primary is None:
            return None

        out = self.extract(primary)
        same_chain = [
            p for p in pairs
            if not self.chain_slug or str(p.get("chainId", "")).lower() == self.chain_slug
        ]
        out["pair_count"] = len(same_chain)
        out["total_liquidity_usd"] = sum(
            to_float((p.get("liquidity") or {}).get("usd")) or 0.0 for p in same_chain
        ) or None
        return out

    async def probe(self, address: str | None = None) -> dict[str, Any]:
        """Contract self-check used by the connection tab."""
        if not self.enabled:
            return {"enabled": False}
        if not address:
            return {"enabled": True, "ok": None, "reason": "no token address supplied"}
        try:
            pairs = await self.token_pairs(address)
        except ClientError as e:
            return {"enabled": True, "ok": False, "error": str(e)}

        if not pairs:
            return {"enabled": True, "ok": True, "pairs": 0,
                    "reason": "reachable, but this token has no pools indexed here"}

        primary = self.select_primary_pair(pairs)
        if primary is None:
            chains = sorted({str(p.get("chainId")) for p in pairs if isinstance(p, dict)})
            return {"enabled": True, "ok": False, "pairs": len(pairs),
                    "reason": f"no pool on chain '{self.chain_slug}'; indexed chains: {chains}",
                    "observed_chains": chains}

        mapped = self.extract(primary)
        parsed = [k for k in EXPECTED_FIELDS if mapped.get(k) is not None]
        missing = [k for k in EXPECTED_FIELDS if mapped.get(k) is None]
        return {
            "enabled": True, "ok": True, "pairs": len(pairs),
            "observed_keys": sorted(primary.keys()),
            "parsed": parsed, "unparsed": missing,
            "primary_pair": {"dex": mapped["dex_id"], "address": mapped["pair_address"],
                             "liquidity_usd": mapped["liquidity_usd"]},
        }
