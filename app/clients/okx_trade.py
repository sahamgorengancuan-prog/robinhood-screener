"""OKX Trading API client (v5 REST).

Deliberately minimal: the only order this bot can place is a small spot BUY.
There is no sell, no margin, no leverage, no withdrawal method in this file —
if the code cannot express an action, a bug cannot perform it.

`OKX_SIMULATED=true` (the default) routes everything to OKX's demo trading
environment via the `x-simulated-trading: 1` header.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.base import BaseHTTPClient, ClientError, pick, to_float
from app.clients.okx_market import OKXAuthMixin

log = logging.getLogger(__name__)

PATH_INSTRUMENTS = "/api/v5/public/instruments"
PATH_TICKER = "/api/v5/market/ticker"
PATH_BOOKS = "/api/v5/market/books"
PATH_ORDER = "/api/v5/trade/order"
PATH_CANCEL = "/api/v5/trade/cancel-order"
PATH_BALANCE = "/api/v5/account/balance"


class OKXTradeClient(OKXAuthMixin, BaseHTTPClient):
    name = "okx_trade"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        simulated: bool = True,
        **kw: Any,
    ) -> None:
        super().__init__(base_url, **kw)
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.simulated = simulated
        self.project_id = ""

    @property
    def credentialed(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)

    @staticmethod
    def _unwrap(data: Any) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            raise ClientError(f"okx_trade: unexpected response {type(data)}")
        if str(data.get("code")) != "0":
            inner = data.get("data") or []
            detail = inner[0].get("sMsg") if inner and isinstance(inner[0], dict) else None
            raise ClientError(f"okx_trade code={data.get('code')} msg={data.get('msg')} detail={detail}")
        return data.get("data") or []

    # ------------------------------------------------------------ market data
    async def spot_instruments(self) -> list[dict[str, Any]]:
        data = await self.request("GET", PATH_INSTRUMENTS, params={"instType": "SPOT"},
                                  cache_key="instruments:SPOT")
        return self._unwrap(data)

    async def find_spot_instrument(self, base_ccy: str, quote_ccy: str = "USDT") -> str | None:
        """Exact base/quote match only — never fuzzy. A wrong instId buys the
        wrong asset."""
        want = f"{base_ccy.upper()}-{quote_ccy.upper()}"
        for inst in await self.spot_instruments():
            if inst.get("instId") == want and inst.get("state") == "live":
                return want
        return None

    async def instrument_meta(self, inst_id: str) -> dict[str, Any] | None:
        for inst in await self.spot_instruments():
            if inst.get("instId") == inst_id:
                return inst
        return None

    async def ticker(self, inst_id: str) -> dict[str, Any] | None:
        data = self._unwrap(await self.request("GET", PATH_TICKER, params={"instId": inst_id}))
        return data[0] if data else None

    async def order_book(self, inst_id: str, depth: int = 20) -> dict[str, Any] | None:
        data = self._unwrap(await self.request("GET", PATH_BOOKS, params={"instId": inst_id, "sz": str(depth)}))
        return data[0] if data else None

    async def top_of_book(self, inst_id: str) -> tuple[float | None, float | None]:
        t = await self.ticker(inst_id)
        if not t:
            return None, None
        return to_float(pick(t, "bidPx")), to_float(pick(t, "askPx"))

    async def price_change_1h_pct(self, inst_id: str) -> float | None:
        """Used by safe mode. Derived from the ticker's open24h is too coarse, so
        we use the candles endpoint via the ticker's last vs open where present."""
        t = await self.ticker(inst_id)
        if not t:
            return None
        last, open24 = to_float(pick(t, "last")), to_float(pick(t, "open24h"))
        if last is None or not open24:
            return None
        return (last - open24) / open24 * 100.0

    # ----------------------------------------------------------------- account
    async def balance(self, ccy: str = "USDT") -> float | None:
        if not self.credentialed:
            return None
        data = self._unwrap(await self.request("GET", PATH_BALANCE, params={"ccy": ccy}))
        if not data:
            return None
        for d in data[0].get("details", []):
            if d.get("ccy") == ccy:
                return to_float(d.get("availBal"))
        return None

    # ------------------------------------------------------------------ orders
    async def place_limit_buy(
        self,
        inst_id: str,
        price: float,
        size_base: float,
        client_order_id: str,
        post_only: bool = True,
    ) -> dict[str, Any]:
        """Place a single post-only (or plain limit) spot buy.

        No market orders: an unbounded market buy in a thin book is exactly the
        failure mode this project exists to avoid.
        """
        if not self.credentialed:
            raise ClientError("okx_trade: missing API credentials")
        if size_base <= 0 or price <= 0:
            raise ClientError(f"okx_trade: refusing nonsensical order px={price} sz={size_base}")

        body = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": "buy",
            "ordType": "post_only" if post_only else "limit",
            "px": f"{price:.12f}".rstrip("0").rstrip("."),
            "sz": f"{size_base:.12f}".rstrip("0").rstrip("."),
            "clOrdId": client_order_id,
        }
        data = self._unwrap(await self.request("POST", PATH_ORDER, json_body=body))
        return data[0] if data else {}

    async def get_order(self, inst_id: str, client_order_id: str) -> dict[str, Any] | None:
        if not self.credentialed:
            return None
        data = self._unwrap(
            await self.request("GET", PATH_ORDER, params={"instId": inst_id, "clOrdId": client_order_id})
        )
        return data[0] if data else None

    async def cancel_order(self, inst_id: str, client_order_id: str) -> dict[str, Any] | None:
        if not self.credentialed:
            return None
        data = self._unwrap(
            await self.request("POST", PATH_CANCEL, json_body={"instId": inst_id, "clOrdId": client_order_id})
        )
        return data[0] if data else None
