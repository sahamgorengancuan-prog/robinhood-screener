# Implementation Roadmap

## Phase 0 — foundation ✅

- Python/FastAPI/APScheduler/SQLite WAL
- `.env` configuration and conservative defaults
- shared HTTP retry, backoff, rate limit, TTL cache
- normalized optional-field schema; missing never becomes zero

## Phase 1 — verified data clients ✅

- Robinhood Node JSON-RPC and recent mint-log discovery
- Alchemy Token/Transfers/Portfolio client; no fictional REST paths
- Blockscout source verification/holder fallback
- OKX OnchainOS Basic/Premium clients with exact documented parameters
- Chainlink staleness + L2 sequencer checks
- runtime probe/diagnostics

## Phase 2 — screening engine ✅

- staged collection to avoid decimals/holder race
- price median, liquidity minimum, holder/pool exclusions
- recent-trade wash sample and advanced sniper/bundle/suspicious/dev-risk metrics
- hard gates and score weights 30/25/20/15/10
- strict `REJECT/WATCH/ALERT/PAPER_BUY/LIVE_BUY` semantics
- manual, source-backed unlock overrides

## Phase 3 — safe execution scaffold ✅

- contract-aware OKX CEX identity; no symbol-only live buy
- fresh CEX book spread and order-size VWAP slippage
- balance, exposure, daily order and position caps
- post-only limit, round down, intent-before-send, poll fills, TTL cancel
- env/file/DB kill switch and 1H-candle safe mode
- paper and demo trading support

## Phase 4 — validation before capital ⬜ operator work

1. Run at least two weeks `ALERT_ONLY`.
2. Audit false positives, missing fields, API quotas, and cross-source price drift.
3. Add reviewed tokenomics overrides only with source URLs.
4. Run at least one week `PAPER` + `OKX_SIMULATED=true`.
5. Rehearse kill switch and provider outage scenarios.
6. Set live thresholds from observed distribution, not from desired trade count.

## Phase 5 — required before unattended LIVE ⬜

- simulated on-chain sell through the actual router to detect conditional honeypots
- verified proxy implementation scan and upgrade/admin monitoring
- `position` table, exit policy, stop/target/liquidity-drop exits
- fee-aware PnL, fill reconciliation, stale/open-order recovery after restart
- Alembic migrations and encrypted/managed secrets

## Phase 6 — only after measured need ⬜

| Trigger | Upgrade |
|---|---|
| Public/free RPC rate-limit or archive gap | paid shared RPC |
| Need pair event below polling cadence | Node WebSocket |
| OKX REST cannot meet measured preflight latency | paid OKX WS channel |
| More than one process/writer | Postgres + Redis/advisory lock |
| Snapshot table >10M rows | retention/partitioning/Postgres |
| Enough labelled outcomes | replay/backtest, then calibration; not ML before data |

Full node remains outside core unless shared providers demonstrably cannot
serve required archive/log workloads. See `ENDPOINTS.md` for official hardware.
