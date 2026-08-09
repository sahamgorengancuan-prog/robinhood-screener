# Endpoint & Client Reference

## Verification status — read this first

This repository was built in an environment where `docs.robinhood.com` and
`web3.okx.com` are blocked by an egress proxy. **The exact request/response
contracts of the Robinhood Chain Data API and the OKX Web3 Market API could not
be verified against live documentation.**

Rather than invent field names, the code is built so that unverified surfaces
degrade safely:

| Tactic | Effect |
|---|---|
| Paths are `.env` config, not literals | An endpoint change is a config edit |
| Fields read via `pick()` with candidate names | Tolerates naming differences |
| Unmatched field → `None`, recorded in `missing_fields` | Never silently becomes `0` |
| `None` can never satisfy a gate | Missing data routes to WATCH, never a buy |
| `RH_DATA_ENABLED=false` by default | The unverified surface is off until you check it |

Run `python scripts/probe_endpoints.py 0xToken` before trusting any number. It
prints the actual keys each API returns so you can extend the candidate lists in
the clients where they differ.

Legend: **VERIFIED** = confirmed from the official public API index ·
**UNVERIFIED** = plausible, must be confirmed by probing · **STANDARD** = defined
by a public standard, not a vendor.

---

## 1. Robinhood Chain — Node API (JSON-RPC)

`app/clients/rh_node.py` · configure `RH_NODE_RPC_URL`

This is the **highest-trust source in the system** because it depends only on
standards. If the indexed APIs disagree with it or disappear, the screener still
functions.

| Method | Status | Used for |
|---|---|---|
| `eth_chainId` | STANDARD | Chain identity; also the OKX `chainIndex` |
| `eth_blockNumber` | STANDARD | Head tracking, log range bounds |
| `eth_getBlockByNumber` | STANDARD | Block timestamps → token age |
| `eth_call` | STANDARD | ERC-20 reads, Chainlink reads |
| `eth_getCode` | STANDARD | Contract existence + bytecode scan |
| `eth_getStorageAt` | STANDARD | EIP-1967 proxy detection |
| `eth_getLogs` | STANDARD | `Transfer` events → holder reconstruction |

Constants used (all public standards, not guesses):

```
Transfer(address,address,uint256) topic0
  0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
EIP-1967 implementation slot
  0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc
ERC-20 selectors
  totalSupply() 0x18160ddd · decimals() 0x313ce567 · symbol() 0x95d89b41
  name() 0x06fdde03 · owner() 0x8da5cb5b · balanceOf(address) 0x70a08231
Chainlink AggregatorV3
  latestRoundData() 0xfeaf968c
```

**Cost note.** `eth_getLogs` over a wide block range is the only expensive call.
It is chunked to 2000 blocks per request (`LOG_CHUNK_BLOCKS`) and only used as a
fallback when indexed holder data is unavailable. Deploy-block discovery uses a
binary search over `eth_getCode` — about 25 calls for a 30M-block chain, which
buys a real `token_age` instead of an assumed one.

---

## 2. Robinhood Chain — Data API (indexed)

`app/clients/rh_data.py` · **disabled by default**

| Config key | Default template | Status |
|---|---|---|
| `RH_DATA_PATH_TOKEN_LIST` | `/tokens` | UNVERIFIED |
| `RH_DATA_PATH_TOKEN_META` | `/tokens/{address}` | UNVERIFIED |
| `RH_DATA_PATH_TOKEN_HOLDERS` | `/tokens/{address}/holders` | UNVERIFIED |
| `RH_DATA_PATH_TOKEN_TRANSFERS` | `/tokens/{address}/transfers` | UNVERIFIED |

Auth sends both `Authorization: Bearer <key>` and `X-API-Key: <key>`; keep
whichever your provider accepts.

Candidate key names the normalizer will accept for holders:
`address|holder|owner|wallet`, `balance|amount|value|quantity`,
`percentage|pct|share|percent`, and for the container
`data|items|tokens|result|results`.

---

## 3. Explorer — Blockscout

`app/clients/explorer.py` · `https://robinhoodchain.blockscout.com`

| Path | Status | Used for |
|---|---|---|
| `/api/v2/smart-contracts/{address}` | VERIFIED (Blockscout v2 is open source and stable) | **Contract verification — a hard gate** |
| `/api/v2/tokens/{address}` | VERIFIED | Token metadata |
| `/api/v2/tokens/{address}/holders` | VERIFIED | Top-holder fallback |
| `/api/v2/tokens/{address}/counters` | VERIFIED | Holder count fallback |

Free, no key. This is why the project does not need a paid contract-audit
provider for its verification gate.

---

## 4. OKX Market API (Web3 / DEX)

`app/clients/okx_market.py`

| Path | Method | Status |
|---|---|---|
| `/api/v6/dex/market/token/search` | GET | **VERIFIED** — listed in OKX's public API reference |
| `/api/v6/dex/market/price-info` | POST | **VERIFIED** — returns price, volume, supply, holders, liquidity; max 100 tokens per call |
| `/api/v6/dex/market/token/basic-info` | POST | UNVERIFIED — confirm before relying on it |

**Auth** (standard OKX scheme):

```
OK-ACCESS-KEY        <api key>
OK-ACCESS-SIGN       base64(HMAC-SHA256(secret, timestamp + METHOD + requestPath + body))
OK-ACCESS-TIMESTAMP  ISO-8601 with milliseconds, e.g. 2026-08-09T12:00:00.000Z
OK-ACCESS-PASSPHRASE <passphrase>
OK-ACCESS-PROJECT    <project id>   # Web3/DEX endpoints only
```

`requestPath` **must include the query string exactly as sent** — this is the
single most common cause of signature failures. `BaseHTTPClient.request()`
rebuilds it with `httpx.QueryParams` before signing for exactly this reason.

Field mapping is defensive (`OKXMarketClient.extract_market`). Candidates
include `price|priceUsd|lastPrice`, `liquidity|liquidityUsd|liquidityInUsd`,
`volume24H|volume24h|vol24H`, `holders|holderCount|uniqueHolders`. Probe and
extend as needed.

---

## 5. OKX Trading API (v5 CEX spot)

`app/clients/okx_trade.py` · these are long-stable, widely-used v5 endpoints.

| Path | Method | Used for |
|---|---|---|
| `/api/v5/public/instruments?instType=SPOT` | GET | Does this token exist as a live spot pair? |
| `/api/v5/market/ticker` | GET | Top of book, safe-mode reference move |
| `/api/v5/market/books` | GET | Depth |
| `/api/v5/trade/order` | POST | **Place the post-only limit buy** |
| `/api/v5/trade/order` | GET | Fill status |
| `/api/v5/trade/cancel-order` | POST | TTL cancel |
| `/api/v5/account/balance` | GET | Pre-flight balance |

`OKX_SIMULATED=true` adds `x-simulated-trading: 1`, routing everything to demo
trading.

**Deliberate omissions.** There is no sell, no margin, no leverage, and no
withdrawal method anywhere in this client. If the code cannot express an action,
a bug cannot perform it.

---

## 6. Chainlink (optional)

`app/clients/chainlink.py` · reads `AggregatorV3Interface` over the Node API.

Only useful for assets that actually have a feed on this chain — normally the
quote asset, not the microcap being screened. Its job is to validate the
*denominator* of USD prices. Disabled unless `CHAINLINK_FEEDS_JSON` is set.

---

## Do we need a full node?

**No — and here is the arithmetic.**

| Option | Monthly cost | Latency | Verdict |
|---|---|---|---|
| Public/shared RPC + free explorer | $0 | 200–800ms | **Sufficient for a 5-minute cadence** |
| Paid RPC tier | ~$50–200 | 50–150ms | Only if rate limits actually bite |
| Self-hosted Orbit full node | ~$150–400 (4–8 vCPU, 16–32GB RAM, 1–2TB NVMe) + your time | 5–20ms | **Not justified** |

The screener's cadence is minutes. A self-hosted node buys ~200ms of latency
that this strategy cannot use, in exchange for a real infrastructure burden:
snapshot syncs, disk growth, and the risk that your one node silently falls
behind the chain and feeds you stale data — a failure mode a shared provider
mostly protects you from.

Run your own node only if you hit rate limits you cannot batch around, or if
`eth_getLogs` over long ranges becomes a routine part of the workload rather
than a fallback.
