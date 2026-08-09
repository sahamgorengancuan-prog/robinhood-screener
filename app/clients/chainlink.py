"""Chainlink aggregator reader (optional price sanity check).

Reads `latestRoundData()` + `decimals()` from an AggregatorV3Interface contract
over the Node API. Selectors below are from the standard interface.

This is only useful for assets that actually have a feed on Robinhood Chain —
in practice the quote asset (ETH/USDC), not the microcap being screened. Its job
is to validate the *denominator* of our USD prices, not the token price itself.
Disabled by default; configure `CHAINLINK_FEEDS_JSON` to enable.
"""

from __future__ import annotations

import json
import logging

from app.clients.rh_node import RobinhoodNodeClient, _hex_to_int

log = logging.getLogger(__name__)

SEL_LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData()
SEL_DECIMALS = "0x313ce567"           # decimals()


class ChainlinkClient:
    def __init__(self, node: RobinhoodNodeClient, feeds_json: str = "{}") -> None:
        self.node = node
        try:
            self.feeds: dict[str, str] = json.loads(feeds_json or "{}")
        except json.JSONDecodeError:
            log.warning("CHAINLINK_FEEDS_JSON is not valid JSON; disabling oracle checks")
            self.feeds = {}

    @property
    def enabled(self) -> bool:
        return bool(self.feeds) and self.node.enabled

    async def latest_price(self, symbol: str) -> float | None:
        feed = self.feeds.get(symbol.upper())
        if not feed or not self.node.enabled:
            return None
        try:
            raw = await self.node.call(feed, SEL_LATEST_ROUND_DATA)
            dec_raw = await self.node.call(feed, SEL_DECIMALS)
        except Exception as e:  # noqa: BLE001 - oracle is best-effort
            log.info("chainlink read failed for %s: %s", symbol, e)
            return None
        if not raw or len(raw) < 2 + 64 * 5:
            return None

        # latestRoundData -> (roundId, int256 answer, startedAt, updatedAt, answeredInRound)
        body = raw[2:]
        answer = int(body[64:128], 16)
        if answer >= 2**255:  # negative int256
            return None
        updated_at = int(body[192:256], 16)
        decimals = _hex_to_int(dec_raw) or 8
        if answer == 0 or updated_at == 0:
            return None
        return answer / (10**decimals)
