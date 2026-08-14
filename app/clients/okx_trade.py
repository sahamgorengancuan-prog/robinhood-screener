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
PATH_CANDLES = "/api/v5/market/candles"
PATH_CURRENCIES = "/api/v5/asset/currencies"
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
    async def spot_instruments(self, inst_id: str | None = None) -> list[dict[str, Any]]:
        params = {"instType": "SPOT"}
        if inst_id:
            params["instId"] = inst_id
        data = await self.request("GET", PATH_INSTRUMENTS, params=params,
                                  cache_key=f"instruments:SPOT:{inst_id or 'all'}")
        return self._unwrap(data)

    async def find_spot_instrument(self, base_ccy: str, quote_ccy: str = "USDT") -> str | None:
        """Exact base/quote match only — never fuzzy. A wrong instId buys the
        wrong asset."""
        want = f"{base_ccy.upper()}-{quote_ccy.upper()}"
        for inst in await self.spot_instruments(want):
            if inst.get("instId") == want and inst.get("state") == "live":
                return want
        return None

    async def currencies(self, ccy: str | None = None) -> list[dict[str, Any]]:
        """Authenticated currency/chain metadata, including ``ctAddr`` suffix.

        OKX deliberately masks all but the last six contract-address
        characters. That is still strong enough to reject a same-symbol token
        when combined with the Robinhood chain label and an exact live pair.
        """
        if not self.credentialed:
            return []
        params = {"ccy": ccy.upper()} if ccy else None
        return self._unwrap(await self.request("GET", PATH_CURRENCIES, params=params))

    async def resolve_contract_spot(
        self,
        *,
        symbol: str,
        contract_address: str,
        chain_hint: str,
        quote_ccy: str = "USDT",
    ) -> tuple[str | None, bool | None, str]:
        """Resolve a CEX pair without trusting symbol alone.

        LIVE requires: an authenticated currencies row, a chain-label match,
        an exact ``ctAddr`` suffix match, and a live SPOT instrument. If OKX
        cannot expose identity metadata for the account, resolution is safely
        left unresolved.
        """
        if not self.credentialed:
            return None, None, "OKX credentials unavailable; contract identity unresolved"
        rows = await self.currencies(symbol)
        if not rows:
            return None, False, f"no OKX currency row for {symbol.upper()}"
        suffix = contract_address.lower().removeprefix("0x")[-6:]
        hint = chain_hint.casefold().strip()
        matches = []
        for row in rows:
            if str(row.get("ccy", "")).upper() != symbol.upper():
                continue
            chain = str(row.get("chain", ""))
            ct_addr = str(row.get("ctAddr", "")).lower().removeprefix("0x")
            if hint and hint not in chain.casefold():
                continue
            if not ct_addr or not ct_addr.endswith(suffix):
                continue
            matches.append(row)
        if not matches:
            return None, False, "no OKX currency row matches Robinhood chain + contract suffix"
        if len(matches) != 1:
            return None, None, f"OKX contract identity match count={len(matches)} (need exactly 1)"
        inst = await self.find_spot_instrument(symbol, quote_ccy)
        if not inst:
            return None, False, f"no live {symbol.upper()}-{quote_ccy.upper()} SPOT instrument"
        return inst, True, "symbol + chain + ctAddr suffix + live instrument matched"

    async def instrument_meta(self, inst_id: str) -> dict[str, Any] | None:
        for inst in await self.spot_instruments(inst_id):
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
        """One-hour move from the official 1H candles endpoint."""
        data = self._unwrap(
            await self.request(
                "GET", PATH_CANDLES,
                params={"instId": inst_id, "bar": "1H", "limit": "2"},
            )
        )
        if not data or not isinstance(data[0], list) or len(data[0]) < 5:
            return None
        open_px, close_px = to_float(data[0][1]), to_float(data[0][4])
        if close_px is None or not open_px:
            return None
        return (close_px - open_px) / open_px * 100.0

    async def buy_slippage_bps(self, inst_id: str, quote_notional: float) -> float | None:
        """VWAP impact for the intended quote notional using fresh CEX asks."""
        book = await self.order_book(inst_id, depth=50)
        asks = (book or {}).get("asks") or []
        if not asks or quote_notional <= 0:
            return None
        best = to_float(asks[0][0])
        if not best or best <= 0:
            return None
        remaining = quote_notional
        base_bought = 0.0
        quote_spent = 0.0
        for level in asks:
            if len(level) < 2:
                continue
            px, size = to_float(level[0]), to_float(level[1])
            if not px or not size or px <= 0 or size <= 0:
                continue
            take_quote = min(remaining, px * size)
            quote_spent += take_quote
            base_bought += take_quote / px
            remaining -= take_quote
            if remaining <= 1e-9:
                break
        if remaining > 1e-6 or base_bought <= 0:
            return None
        vwap = quote_spent / base_bought
        return (vwap - best) / best * 10_000.0

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
