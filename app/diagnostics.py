"""Connection & API diagnostics.

One structured implementation, consumed by both the Gradio UI and
`scripts/probe_endpoints.py`, so the terminal and the browser can never disagree
about whether a source works.

Every check returns a `CheckResult` carrying four things the operator actually
needs:

  * **status** — OK / WARN / FAIL / SKIP
  * **latency_ms** — is it working, or just barely working?
  * **detail** — what came back, including the *observed field names*, which is
    the whole point for the endpoints this repo could not verify offline
  * **fix** — the specific next action, not a generic error string

Checks are read-only. Nothing here can place an order, and the private OKX check
reads a balance rather than touching the trading endpoints.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from app.clients.base import ClientError
from app.config import Settings, get_settings
from app.services import Services, build_services

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"

STATUS_ICON = {OK: "🟢", WARN: "🟡", FAIL: "🔴", SKIP: "⚪"}


def build_diagnostic_services(c: Settings) -> Services:
    """Clients tuned for diagnostics: fail fast, don't retry.

    The screening loop retries with backoff because a transient 503 shouldn't
    lose a cycle. A connection test wants the opposite — an operator staring at
    a spinner needs the bad news in two seconds, not thirty.
    """
    return build_services(c.model_copy(update={
        "http_max_retries": 0,
        "rh_node_timeout_s": min(c.rh_node_timeout_s, 8.0),
        "okx_market_timeout_s": min(c.okx_market_timeout_s, 8.0),
        "okx_trade_timeout_s": min(c.okx_trade_timeout_s, 8.0),
        "explorer_timeout_s": min(c.explorer_timeout_s, 8.0),
    }))


@dataclass
class CheckResult:
    name: str
    group: str
    status: str
    summary: str
    latency_ms: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    fix: str = ""

    @property
    def icon(self) -> str:
        """Emoji in a browser or a UTF-8 terminal, ASCII on a legacy console."""
        from app.util.console import status_icon

        return status_icon(self.status)

    @property
    def emoji(self) -> str:
        """Always the emoji — safe for HTML output, which is never cp1252."""
        return STATUS_ICON.get(self.status, "⚪")

    def as_row(self, use_emoji: bool = False) -> list[Any]:
        """`use_emoji=True` for the browser, which is always UTF-8; the default
        adapts to whatever the console can actually encode."""
        lat = f"{self.latency_ms:.0f} ms" if self.latency_ms is not None else "-"
        mark = self.emoji if use_emoji else self.icon
        return [f"{mark} {self.status}", self.group, self.name, lat, self.summary, self.fix]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "group": self.group, "status": self.status,
            "summary": self.summary, "latency_ms": self.latency_ms,
            "detail": self.detail, "fix": self.fix,
        }


async def _timed(fn: Callable[[], Awaitable[Any]]) -> tuple[Any, float, Exception | None]:
    start = time.perf_counter()
    try:
        result = await fn()
        return result, (time.perf_counter() - start) * 1000, None
    except Exception as e:  # noqa: BLE001 - diagnostics must never raise
        return None, (time.perf_counter() - start) * 1000, e


def _keys_of(obj: Any, limit: int = 40) -> list[str]:
    if isinstance(obj, dict):
        return sorted(obj.keys())[:limit]
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        return sorted(obj[0].keys())[:limit]
    return []


# ===========================================================================
# Individual checks
# ===========================================================================
async def check_database(c: Settings) -> CheckResult:
    from sqlalchemy import func, select

    from app.db import init_db, session_scope
    from app.models import Alert, Evaluation, OrderRecord, Token, TokenSnapshot

    async def run() -> dict[str, int]:
        init_db()
        with session_scope() as s:
            return {
                "tokens": s.execute(select(func.count(Token.id))).scalar_one(),
                "snapshots": s.execute(select(func.count(TokenSnapshot.id))).scalar_one(),
                "evaluations": s.execute(select(func.count(Evaluation.id))).scalar_one(),
                "orders": s.execute(select(func.count(OrderRecord.id))).scalar_one(),
                "alerts": s.execute(select(func.count(Alert.id))).scalar_one(),
            }

    counts, ms, err = await _timed(run)
    if err:
        return CheckResult("Database", "Storage", FAIL, f"cannot open database: {err}", ms,
                           fix="Check DATABASE_URL and that the ./data directory is writable.")
    return CheckResult(
        "Database", "Storage", OK,
        f"{counts['tokens']} tokens · {counts['snapshots']} snapshots · {counts['orders']} orders",
        ms, detail=counts,
    )


async def check_node_rpc(svc: Services, c: Settings) -> list[CheckResult]:
    if not svc.node.enabled:
        return [CheckResult(
            "Node RPC", "Robinhood Chain", SKIP, "RH_NODE_RPC_URL is not configured",
            fix="Set RH_NODE_RPC_URL. This is the highest-trust source — without it, "
                "supply, contract flags and token age are all unavailable.",
        )]

    results: list[CheckResult] = []

    chain_id, ms, err = await _timed(svc.node.chain_id)
    if err:
        results.append(CheckResult(
            "Node RPC", "Robinhood Chain", FAIL, f"eth_chainId failed: {err}", ms,
            fix="Verify the RPC URL is reachable and accepts JSON-RPC POSTs.",
        ))
        return results

    if c.rh_chain_id and chain_id and c.rh_chain_id != chain_id:
        results.append(CheckResult(
            "Node RPC", "Robinhood Chain", FAIL,
            f"RH_CHAIN_ID={c.rh_chain_id} but the node reports {chain_id}", ms,
            detail={"configured": c.rh_chain_id, "actual": chain_id},
            fix=f"Set RH_CHAIN_ID={chain_id}. A wrong chainIndex makes OKX look up the wrong chain.",
        ))
    elif chain_id and not c.rh_chain_id:
        results.append(CheckResult(
            "Node RPC", "Robinhood Chain", WARN, f"connected — chain ID {chain_id}, but RH_CHAIN_ID is unset", ms,
            detail={"chain_id": chain_id},
            fix=f"Set RH_CHAIN_ID={chain_id} in .env to enable OKX on-chain lookups.",
        ))
    else:
        results.append(CheckResult("Node RPC", "Robinhood Chain", OK,
                                   f"connected — chain ID {chain_id}", ms,
                                   detail={"chain_id": chain_id}))

    block, ms2, err2 = await _timed(svc.node.block_number)
    if err2 or block is None:
        results.append(CheckResult("Node head", "Robinhood Chain", FAIL,
                                   f"eth_blockNumber failed: {err2}", ms2))
    else:
        ts, _, _ = await _timed(lambda: svc.node.get_block_timestamp(block))
        lag = None
        if ts:
            lag = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromtimestamp(ts, dt.timezone.utc)).total_seconds()
        if lag is not None and lag > 300:
            results.append(CheckResult(
                "Node head", "Robinhood Chain", WARN,
                f"block {block:,} is {lag/60:.1f} min old — node may be lagging", ms2,
                detail={"block": block, "lag_seconds": lag},
                fix="A lagging node feeds stale liquidity data. Switch RPC provider if this persists.",
            ))
        else:
            results.append(CheckResult(
                "Node head", "Robinhood Chain", OK,
                f"block {block:,}" + (f" · {lag:.0f}s old" if lag is not None else ""), ms2,
                detail={"block": block, "lag_seconds": lag},
            ))

    return results


async def check_node_token(svc: Services, address: str) -> list[CheckResult]:
    """ERC-20 + contract scan against a specific token."""
    if not svc.node.enabled or not address:
        return []

    results: list[CheckResult] = []

    async def erc20() -> dict[str, Any]:
        return {
            "symbol": await svc.node.erc20_symbol(address),
            "name": await svc.node.erc20_name(address),
            "decimals": await svc.node.erc20_decimals(address),
            "total_supply_raw": await svc.node.erc20_total_supply(address),
        }

    data, ms, err = await _timed(erc20)
    if err:
        results.append(CheckResult("ERC-20 read", "Token probe", FAIL, f"{err}", ms,
                                   fix="Confirm the address is an ERC-20 contract on this chain."))
    elif data and data.get("total_supply_raw") is None:
        results.append(CheckResult("ERC-20 read", "Token probe", WARN,
                                   "no totalSupply() — not a standard ERC-20?", ms, detail=data))
    else:
        dec = data.get("decimals")
        supply = data["total_supply_raw"] / (10 ** dec) if dec is not None else None
        results.append(CheckResult(
            "ERC-20 read", "Token probe", OK,
            f"{data.get('symbol') or '?'} · {data.get('name') or '?'} · "
            f"supply {supply:,.0f}" if supply else "read ok", ms,
            detail={**data, "total_supply": supply},
        ))

    flags, ms2, err2 = await _timed(lambda: svc.node.contract_risk_flags(address))
    if err2:
        results.append(CheckResult("Contract scan", "Token probe", FAIL, f"{err2}", ms2))
    else:
        flag_list, detail = flags
        status = FAIL if "NOT_A_CONTRACT" in flag_list else (WARN if flag_list else OK)
        results.append(CheckResult(
            "Contract scan", "Token probe", status,
            ", ".join(flag_list) if flag_list else "no dangerous privileges detected", ms2,
            detail=detail,
            fix="Flags are informational here; the risk gates decide what is disqualifying."
            if flag_list else "",
        ))
    return results


async def check_data_api(svc: Services, c: Settings) -> CheckResult:
    if not c.rh_data_enabled:
        return CheckResult(
            "Data API", "Robinhood Chain", SKIP, "RH_DATA_ENABLED=false (default)",
            fix="Optional. Its response contract is unverified in this repo — enable only "
                "after checking the returned keys below against app/clients/rh_data.py.",
        )
    if not svc.data.enabled:
        return CheckResult("Data API", "Robinhood Chain", FAIL, "enabled but RH_DATA_BASE_URL is empty",
                           fix="Set RH_DATA_BASE_URL.")

    sample, ms, err = await _timed(lambda: svc.data.list_tokens(limit=1))
    if err:
        return CheckResult("Data API", "Robinhood Chain", FAIL, f"{err}", ms,
                           fix="Check RH_DATA_BASE_URL, the API key, and RH_DATA_PATH_TOKEN_LIST.")
    if not sample:
        return CheckResult("Data API", "Robinhood Chain", WARN, "reachable but returned no tokens", ms,
                           fix="The path may be wrong, or the feed may be empty. Compare with the docs.")

    keys = _keys_of(sample)
    addr_key = next((k for k in ("address", "contract_address", "contractAddress") if k in sample[0]), None)
    if not addr_key:
        return CheckResult(
            "Data API", "Robinhood Chain", WARN,
            f"returned {len(sample)} item(s) but no recognised address field", ms,
            detail={"observed_keys": keys},
            fix="Add the real address key to discover_tokens() in app/pipeline/ingest.py.",
        )
    return CheckResult("Data API", "Robinhood Chain", OK,
                       f"reachable · address field '{addr_key}' · {len(keys)} fields", ms,
                       detail={"observed_keys": keys, "sample": sample[0]})


async def check_explorer(svc: Services, c: Settings, address: str | None) -> CheckResult:
    if not svc.explorer.enabled:
        return CheckResult("Explorer", "Blockscout", SKIP, "EXPLORER_ENABLED=false",
                           fix="Contract verification is a hard gate. With this off, no token can "
                               "reach LIVE_BUY.")
    if not address:
        return CheckResult("Explorer", "Blockscout", SKIP, "no token address supplied",
                           fix="Enter a token address to test contract verification lookup.")

    verified, ms, err = await _timed(lambda: svc.explorer.is_verified(address))
    if err:
        return CheckResult("Explorer", "Blockscout", FAIL, f"{err}", ms,
                           fix="Check EXPLORER_BASE_URL is reachable from this host.")
    if verified is None:
        return CheckResult(
            "Explorer", "Blockscout", WARN, "no verification record for this contract", ms,
            fix="Treated as 'cannot verify' — blocks live buying but still alerts. "
                "This is also the expected answer for an unverified contract.",
        )
    return CheckResult("Explorer", "Blockscout", OK if verified else WARN,
                       f"contract verified: {verified}", ms, detail={"is_verified": verified})


async def check_okx_market(svc: Services, c: Settings) -> CheckResult:
    if not svc.market.enabled:
        return CheckResult("OKX Market", "OKX", SKIP, "OKX_MARKET_ENABLED=false")

    res, ms, err = await _timed(lambda: svc.market.token_search("USDC"))
    if err:
        msg = str(err)
        fix = "Check OKX_API_KEY / SECRET / PASSPHRASE / PROJECT_ID."
        if "50113" in msg or "signature" in msg.lower() or "401" in msg:
            fix = ("Signature rejected. The signed request path must include the query string "
                   "exactly as sent, and OK-ACCESS-TIMESTAMP must be ISO-8601 with milliseconds.")
        elif "403" in msg:
            fix = "403 — the host may be blocked by a network/egress policy, or the key lacks access."
        return CheckResult("OKX Market", "OKX", FAIL, msg[:180], ms, fix=fix)

    keys = _keys_of(res)
    if not res:
        return CheckResult("OKX Market", "OKX", WARN, "authenticated but search returned nothing", ms,
                           fix="Endpoint path may have changed — see docs/ENDPOINTS.md.")
    return CheckResult("OKX Market", "OKX", OK, f"token search returned {len(res)} result(s)", ms,
                       detail={"observed_keys": keys, "sample": res[0] if res else None})


async def check_okx_price_info(svc: Services, c: Settings, address: str | None) -> CheckResult:
    """The field-mapping check that matters most: which metrics actually parse?"""
    if not svc.market.enabled or not address:
        return CheckResult("OKX price-info", "OKX", SKIP, "needs a token address")
    if not c.rh_chain_id:
        return CheckResult("OKX price-info", "OKX", SKIP, "RH_CHAIN_ID unset",
                           fix="Without a confirmed chain ID we refuse to query, rather than "
                               "risk matching a same-named token on another chain.")

    info, ms, err = await _timed(lambda: svc.market.price_info(str(c.rh_chain_id), [address]))
    if err:
        return CheckResult("OKX price-info", "OKX", FAIL, str(err)[:180], ms)

    item = (info or {}).get(address.lower())
    if not item:
        return CheckResult("OKX price-info", "OKX", WARN, "no data returned for this token", ms,
                           fix="The token may not be indexed by OKX, or chainIndex may be wrong.")

    from app.clients.okx_market import OKXMarketClient

    mapped = OKXMarketClient.extract_market(item)
    parsed = [k for k, v in mapped.items() if v is not None]
    unmapped = [k for k, v in mapped.items() if v is None]

    critical = {"price_usd", "liquidity_usd", "volume_24h"}
    missing_critical = sorted(critical - set(parsed))

    status = OK if not missing_critical else WARN
    fix = ""
    if unmapped:
        fix = (f"Unparsed: {', '.join(unmapped)}. Compare 'observed_keys' below with the candidate "
               f"names in OKXMarketClient.extract_market() and extend them. Unparsed fields stay "
               f"None and route tokens to WATCH — they never become 0.")
    return CheckResult(
        "OKX price-info", "OKX", status,
        f"{len(parsed)}/{len(mapped)} fields parsed"
        + (f" · missing critical: {', '.join(missing_critical)}" if missing_critical else ""),
        ms,
        detail={"observed_keys": _keys_of(item), "parsed": parsed, "unparsed": unmapped, "raw": item},
        fix=fix,
    )


async def check_okx_public(svc: Services, c: Settings) -> list[CheckResult]:
    results: list[CheckResult] = []

    insts, ms, err = await _timed(svc.trade.spot_instruments)
    if err:
        results.append(CheckResult("OKX instruments", "OKX", FAIL, str(err)[:180], ms,
                                   fix="Public endpoint — a failure here usually means network "
                                       "egress is blocked, not bad credentials."))
    else:
        results.append(CheckResult("OKX instruments", "OKX", OK,
                                   f"{len(insts)} live SPOT pairs", ms,
                                   detail={"count": len(insts)}))

    tick, ms2, err2 = await _timed(lambda: svc.trade.ticker(c.safe_mode_ref_instrument))
    if err2 or not tick:
        results.append(CheckResult("Safe-mode reference", "OKX", WARN,
                                   f"cannot read {c.safe_mode_ref_instrument}", ms2,
                                   fix="Safe mode fails closed: without this, live buying is "
                                       "suspended automatically."))
    else:
        results.append(CheckResult("Safe-mode reference", "OKX", OK,
                                   f"{c.safe_mode_ref_instrument} last {tick.get('last')}", ms2,
                                   detail=tick))
    return results


async def check_okx_private(svc: Services, c: Settings) -> CheckResult:
    if not svc.trade.credentialed:
        return CheckResult(
            "OKX trading auth", "OKX", SKIP, "trading credentials not configured",
            fix="Only needed for PAPER/LIVE order placement. Screening and alerting work without it.",
        )

    bal, ms, err = await _timed(lambda: svc.trade.balance(c.okx_quote_ccy))
    if err:
        return CheckResult("OKX trading auth", "OKX", FAIL, str(err)[:180], ms,
                           fix="Check the trading key, and that OKX_SIMULATED matches the key type "
                               "(demo keys only work with OKX_SIMULATED=true).")
    mode = "DEMO (simulated)" if svc.trade.simulated else "REAL MONEY"
    status = OK if bal is not None else WARN
    return CheckResult("OKX trading auth", "OKX", status,
                       f"authenticated · {mode} · {c.okx_quote_ccy} available: {bal}", ms,
                       detail={"balance": bal, "simulated": svc.trade.simulated})


async def check_chainlink(svc: Services, c: Settings) -> CheckResult:
    if not svc.chainlink.enabled:
        return CheckResult("Chainlink oracle", "Price sanity", SKIP,
                           "disabled (CHAINLINK_ENABLED / CHAINLINK_FEEDS_JSON)",
                           fix="Optional. Without it, a token with only one price source cannot "
                               "reach LIVE_BUY — cross-checking needs two.")
    symbol = next(iter(svc.chainlink.feeds), None)
    price, ms, err = await _timed(lambda: svc.chainlink.latest_price(symbol))
    if err or price is None:
        return CheckResult("Chainlink oracle", "Price sanity", WARN,
                           f"no answer from feed for {symbol}", ms,
                           fix="Verify the aggregator address is correct for this chain.")
    return CheckResult("Chainlink oracle", "Price sanity", OK, f"{symbol} = {price:,.6g}", ms,
                       detail={"symbol": symbol, "price": price})


def check_run_mode(c: Settings) -> CheckResult:
    """Not a network check — a safety posture summary."""
    from app.execution import killswitch

    killed, kill_reason = killswitch.is_killed()
    safe, safe_reason = killswitch.safe_mode_status()
    live_possible = c.run_mode == "LIVE" and not killed and not safe

    bits = [f"mode {c.run_mode}"]
    if killed:
        bits.append(f"KILL SWITCH ({kill_reason})")
    if safe:
        bits.append(f"safe mode ({safe_reason})")
    bits.append("OKX demo" if c.okx_simulated else "OKX REAL MONEY")

    if not live_possible:
        return CheckResult("Safety posture", "Execution", OK,
                           " · ".join(bits) + " → no live orders possible",
                           detail={"live_trading_possible": False})
    return CheckResult(
        "Safety posture", "Execution", WARN,
        " · ".join(bits) + " → LIVE ORDERS ARE POSSIBLE",
        detail={"live_trading_possible": True},
        fix="This is the only configuration that can spend real money. "
            "Confirm position sizing and that the kill switch works before leaving it running.",
    )


# ===========================================================================
# Runner
# ===========================================================================
async def run_all_checks(
    address: str | None = None,
    settings: Settings | None = None,
    svc: Services | None = None,
) -> list[CheckResult]:
    c = settings or get_settings()
    own = svc is None
    svc = svc or build_diagnostic_services(c)

    try:
        results: list[CheckResult] = [check_run_mode(c), await check_database(c)]

        node_res, data_res, explorer_res, market_res, public_res, private_res, link_res = (
            await asyncio.gather(
                check_node_rpc(svc, c),
                check_data_api(svc, c),
                check_explorer(svc, c, address),
                check_okx_market(svc, c),
                check_okx_public(svc, c),
                check_okx_private(svc, c),
                check_chainlink(svc, c),
                return_exceptions=False,
            )
        )
        results.extend(node_res)
        results.append(data_res)
        results.append(explorer_res)
        results.append(market_res)
        results.extend(public_res)
        results.append(private_res)
        results.append(link_res)

        if address:
            results.extend(await check_node_token(svc, address))
            results.append(await check_okx_price_info(svc, c, address))

        return results
    finally:
        if own:
            await svc.aclose()


def summarize_checks(results: list[CheckResult]) -> dict[str, int]:
    out = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    return out


def readiness(results: list[CheckResult]) -> tuple[str, str]:
    """Translate check results into what the system can actually do right now."""
    by_name = {r.name: r for r in results}
    node_ok = by_name.get("Node RPC", CheckResult("", "", SKIP, "")).status == OK
    market_ok = by_name.get("OKX Market", CheckResult("", "", SKIP, "")).status in (OK, WARN)
    trade_ok = by_name.get("OKX trading auth", CheckResult("", "", SKIP, "")).status == OK
    counts = summarize_checks(results)

    if counts[FAIL] == 0 and node_ok and market_ok and trade_ok:
        return OK, "Ready for PAPER trading. Screening, alerting and order simulation all work."
    if node_ok and market_ok:
        return OK, "Ready for ALERT_ONLY. Screening and alerting work; trading auth not configured."
    if node_ok or market_ok:
        return WARN, ("Partially ready. Some metrics will be unavailable, so tokens will land in "
                      "WATCH rather than being scored — that is the safe degradation, not a crash.")
    return FAIL, ("Not ready. No chain or market data source is reachable; every token will report "
                  "missing data and stay in WATCH.")
