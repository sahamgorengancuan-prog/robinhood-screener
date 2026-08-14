# Endpoint & Client Reference

Verified against official documentation on 2026-08-14. A runtime probe is
still mandatory because availability, quota, and account permissions can change.

**Independently corroborated from this build environment (2026-08-14):** chain
ID `4663`, the public RPC `https://rpc.mainnet.chain.robinhood.com`, the Alchemy
RPC/WS hosts, and the Blockscout explorer host. Everything else in this file —
in particular the OKX OnchainOS request/response shapes, tier split and pricing
— comes from the documentation and could **not** be re-checked here: the network
policy blocks `web3.okx.com`, `www.okx.com` and `docs.robinhood.com`. Run the
connection tab before trusting those field mappings.

## 1. Robinhood Chain

Official network facts:

- mainnet chain ID `4663`; testnet `46630`
- public RPC `https://rpc.mainnet.chain.robinhood.com` is rate-limited and not
  recommended for production
- recommended Alchemy RPC:
  `https://robinhood-mainnet.g.alchemy.com/v2/{API_KEY}`
- recommended Alchemy WS:
  `wss://robinhood-mainnet.g.alchemy.com/v2/{API_KEY}`

Source: [Robinhood Chain connection guide](https://docs.robinhood.com/chain/connecting/).

### Node API — `app/clients/rh_node.py`

| JSON-RPC method | Used for |
|---|---|
| `eth_chainId` | enforce chain identity |
| `eth_blockNumber` | discovery range and deploy search |
| `eth_getBlockByNumber` | block timestamp / token age |
| `eth_call` | ERC-20 and Chainlink reads |
| `eth_getCode` | contract check and first-code-block search |
| `eth_getStorageAt` | EIP-1967 proxy check |
| `eth_getLogs` | mint discovery and ERC-20 transfer history |

Recent candidate discovery filters `Transfer` events with `from=0x0`, then
requires exactly three topics and non-zero `data`. ERC-721 false positives are
discarded later when ERC-20 reads fail.

### Data API via Alchemy — `app/clients/rh_data.py`

The documented Robinhood Data API is provider-backed indexed data, not a
Robinhood-specific `/tokens` REST service. Core methods:

| API | Exact method/path | Purpose | Core? |
|---|---|---|---|
| Token API | `alchemy_getTokenMetadata` | symbol/name/decimals metadata | optional |
| Token API | `alchemy_getTokenBalances` | ERC-20 balances owned by known wallet | optional |
| Transfers API | `alchemy_getAssetTransfers` | wallet/contract ERC-20 activity | optional |
| Portfolio API | `POST https://api.g.alchemy.com/data/v1/{key}/assets/tokens/by-address` | portfolio activity for monitored wallets | optional/high-CU |

Official docs:
[Robinhood API overview](https://www.alchemy.com/docs/robinhood-chain/robinhood-chain-api-overview),
[token metadata](https://www.alchemy.com/docs/data/token-api/token-api-endpoints/alchemy-get-token-metadata),
[token balances](https://www.alchemy.com/docs/data/token-api/token-api-endpoints/alchemy-get-token-balances),
[asset transfers](https://www.alchemy.com/docs/data/transfers-api/transfers-endpoints/alchemy-get-asset-transfers),
[portfolio tokens](https://www.alchemy.com/docs/data/portfolio-apis/portfolio-api-endpoints/portfolio-api-endpoints/get-tokens-by-address).

There is no documented global new-token list or global top-holder endpoint in
those APIs. The code does not invent either one.

### Blockscout

Base: `https://robinhoodchain.blockscout.com`

| Path | Purpose |
|---|---|
| `GET /api/v2/smart-contracts/{address}` | source verification hard gate |
| `GET /api/v2/tokens/{address}` | token metadata fallback |
| `GET /api/v2/tokens/{address}/holders` | top-holder balance fallback |
| `GET /api/v2/tokens/{address}/counters` | holder count fallback |

Blockscout is free but not an official Robinhood Data API. Failure to verify a
contract is a hard reject.

## 2. OKX OnchainOS Market API v6

Base: `https://web3.okx.com`; Robinhood `chainIndex=4663`. OKX's supported
networks table marks Robinhood Market and Trade supported.

| Method/path | Tier | Used for |
|---|---|---|
| `GET /api/v6/dex/market/supported/chain` | Free | runtime chain support check |
| `GET /api/v6/dex/market/token/hot-token` | Basic | trending/active discovery and server-side prefilter |
| `GET /api/v6/dex/market/token/search?chains=4663&search=...` | Basic | exact contract/symbol/name search |
| `POST /api/v6/dex/market/token/basic-info` | Basic | tokenName/tokenSymbol/decimal |
| `GET /api/v6/dex/market/token/top-liquidity` | Basic | top-five pools, liquidity USD, pool addresses |
| `GET /api/v6/dex/market/trades` | Basic | up to 500 recent trades; wallet, side, USD volume, filtered flag |
| `POST /api/v6/dex/market/price-info` | Premium | price/change/volume/txs/circSupply/liquidity/holders |
| `GET /api/v6/dex/market/token/advanced-info` | Premium | risk level, honeypot tag, LP burn, dev rug history, top10/sniper/bundle/suspicious shares |
| `GET /api/v6/dex/market/token/holder` | Premium | top 100 holder percentages and wallet tags |

Official references:
[supported networks](https://web3.okx.com/onchainos/dev-docs/home/supported-chain),
[token search](https://web3.okx.com/onchainos/dev-docs/market/market-token-search),
[basic info](https://web3.okx.com/onchainos/dev-docs/market/market-token-basic-info),
[price info](https://web3.okx.com/onchainos/dev-docs/market/market-token-price-info),
[top liquidity](https://web3.okx.com/onchainos/dev-docs/market/market-token-top-liquidity),
[trades](https://web3.okx.com/onchainos/dev-docs/market/market-trades),
[advanced info](https://web3.okx.com/onchainos/dev-docs/market/market-token-advanced-info),
[holders](https://web3.okx.com/onchainos/dev-docs/market/market-token-holder),
[hot tokens](https://web3.okx.com/onchainos/dev-docs/market/market-token-hot-token).

Pricing documented by OKX: supported-chain endpoints are free; Basic includes
100,000 free calls/month then $0.0001/call; Premium includes 100,000 free
calls/month then $0.0002/call. Set `OKX_MARKET_PREMIUM_ENABLED=false` before
quota exhaustion if zero marginal cost is mandatory. Source:
[OKX Market API fee](https://web3.okx.com/onchainos/dev-docs/market/market-api-fee).

Default 15-minute/10-candidate cadence is budgeted at roughly 89k Basic and 86k
Premium calls/month before retry/probe margin. REST is the core path; liquidity
WebSocket is a later paid/latency optimization.

## 3. OKX CEX Trading API v5

Base: `https://www.okx.com`

| Method/path | Purpose |
|---|---|
| `GET /api/v5/asset/currencies?ccy=SYMBOL` | authenticated chain + masked `ctAddr` identity |
| `GET /api/v5/public/instruments?instType=SPOT&instId=X-USDT` | exact pair/state/lot/min size |
| `GET /api/v5/market/ticker?instId=X-USDT` | bid/ask snapshot |
| `GET /api/v5/market/books?instId=X-USDT&sz=50` | depth and order-size VWAP slippage |
| `GET /api/v5/market/candles?instId=BTC-USDT&bar=1H&limit=2` | correct 1-hour safe-mode move |
| `GET /api/v5/account/balance?ccy=USDT` | quote balance preflight |
| `POST /api/v5/trade/order` | post-only limit buy only |
| `GET /api/v5/trade/order` | fill/status poll |
| `POST /api/v5/trade/cancel-order` | cancel after TTL |

Important: CEX availability is not symbol-only. LIVE requires one currencies
row matching the Robinhood chain label and the documented last-six-character
`ctAddr` suffix, plus an exact live SPOT instrument. Ambiguous/unavailable
metadata produces `ALERT: on-chain only / manual review`.

Reference: [OKX API v5](https://www.okx.com/docs-v5/en/).

## 4. Chainlink

Read via Node API:

- `latestRoundData()` selector `0xfeaf968c`
- `decimals()` selector `0x313ce567`
- require `answer > 0`, `updatedAt > 0`, and age <= configured heartbeat
- when configured, require L2 sequencer status `0` and grace period elapsed

Feed addresses and heartbeats are not hardcoded. Robinhood explicitly points
to Chainlink's current feed page as the source of truth. Source:
[Robinhood oracles guide](https://docs.robinhood.com/chain/oracles-and-price-feeds/).

## 5. Optional no-key cross-checks

- DexScreener `GET /latest/dex/tokens/{address}`
- GeckoTerminal `GET /api/v2/networks/{network}/tokens/{address}`

These are optional third-party sources, not part of the official-first core.
Both require an explicit Robinhood chain/network slug. Blank slug disables the
client to prevent cross-chain same-address mispricing.

## 6. Full-node decision

Do not run one for the MVP. Official requirements are 8+ CPU cores, 64 GB RAM
(128 GB recommended), locally attached NVMe sized at twice current chain size
plus 20%, several TB in practice, and separate L1 execution + beacon access.
Use public/Alchemy RPC first. Revisit only after measured provider rate-limit,
archive-read, or availability failures. Source:
[Robinhood full-node guide](https://docs.robinhood.com/chain/run-a-full-node/).
