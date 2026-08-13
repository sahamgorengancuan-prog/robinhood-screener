# Implementation Roadmap

Phases 0–3 are **built and tested in this repo**. Phases 4+ are the ordered
backlog, with the reasoning for the order.

---

## Phase 0 — Foundations ✅ built

| Step | Deliverable | Status |
|---|---|---|
| 0.1 | Project structure, `pyproject`, pinned `requirements.txt` | ✅ |
| 0.2 | `.env` config with every threshold in one auditable place | ✅ `app/config.py` |
| 0.3 | SQLite schema + WAL + session scope | ✅ `app/models.py`, `app/db.py` |
| 0.4 | Normalized schema where **missing ≠ zero** | ✅ `app/schemas.py` |
| 0.5 | Shared HTTP client: retry, token-bucket, TTL cache | ✅ `app/clients/base.py` |

**Exit criteria:** `make test` green, `make demo` renders alerts offline. ✅

---

## Phase 1 — Read-only data layer ✅ built

| Step | Deliverable | Status |
|---|---|---|
| 1.1 | Node API client (JSON-RPC, ERC-20, bytecode scan, proxy detection) | ✅ |
| 1.2 | Data API client (config-driven paths, tolerant parsing) | ✅ disabled by default |
| 1.3 | Blockscout client for contract verification | ✅ |
| 1.4 | OKX Market client with correct request signing | ✅ |
| 1.5 | OKX Trading client — read paths only at this stage | ✅ |
| 1.6 | Chainlink oracle reader | ✅ optional |
| 1.7 | `probe_endpoints.py` to verify reality before trusting output | ✅ |

**Exit criteria:** `make probe` reports OK for Node + Explorer + OKX Market.
→ *This is where you are when you first clone the repo.*

---

## Phase 2 — Screening brain ✅ built

| Step | Deliverable | Status |
|---|---|---|
| 2.1 | Derived metrics (HHI, slippage, spread, base/drawdown) | ✅ `pipeline/metrics.py` |
| 2.2 | Normalizer with per-field provenance | ✅ `pipeline/normalize.py` |
| 2.3 | Price/liquidity reconciliation | ✅ `util/reconcile.py` |
| 2.4 | 21 risk gates across 4 severities | ✅ `pipeline/risk.py` |
| 2.5 | 5-component weighted score (30/25/20/15/10) | ✅ `pipeline/scoring.py` |
| 2.6 | 5-state decision engine with strict precedence | ✅ `pipeline/decision.py` |
| 2.7 | Alert formatter + 4 sinks with dedupe | ✅ `alerts/` |
| 2.8 | Ingestion cycle + APScheduler + FastAPI + dashboard | ✅ |

**Exit criteria:** run `ALERT_ONLY` for 2 weeks; review every alert by hand.

---

## Phase 3 — Execution scaffolding ✅ built

| Step | Deliverable | Status |
|---|---|---|
| 3.1 | Kill switch (3 independent triggers, fails closed) | ✅ |
| 3.2 | Automatic safe mode on violent reference moves | ✅ |
| 3.3 | Exposure accounting, paper and live tracked separately | ✅ |
| 3.4 | Pure order-safety checks | ✅ `execution/order_safety.py` |
| 3.5 | Paper executor sharing the live code path | ✅ |
| 3.6 | Live executor: 9 sequential guards, post-only only | ✅ |
| 3.7 | 251 tests covering scoring, gates, decisions, order safety | ✅ |

**Exit criteria:** 1 week of `PAPER` with `OKX_SIMULATED=true`, and every
PAPER_BUY reviewed manually.

---

## Phase 3.5 — Operator surface ✅ built

| Step | Deliverable | Status |
|---|---|---|
| 3.5.1 | Structured diagnostics: status, latency, observed API fields, remediation | ✅ `app/diagnostics.py` |
| 3.5.2 | Gradio control panel, 6 tabs | ✅ `app/ui/gradio_app.py` |
| 3.5.3 | Terminal probe sharing the same checks | ✅ `scripts/probe_endpoints.py` |
| 3.5.4 | Threshold Lab — live gate/score recomputation from sliders | ✅ |
| 3.5.5 | Read-only Token Inspector (no writes, no orders) | ✅ |

The UI is deliberately incapable of placing an order or editing risk limits;
both are asserted by tests. It binds to loopback and makes no outbound
requests of its own.

**Exit criteria:** the connection tab reports "Ready for PAPER" against your
real endpoints.

---

## Phase 3.6 — Windows packaging ✅ built

| Step | Deliverable | Status |
|---|---|---|
| 3.6.1 | One-click pipeline runner, logic in tested Python | ✅ `scripts/one_click.py` |
| 3.6.2 | A single double-clickable entry point | ✅ `START.bat` |
| 3.6.3 | Emergency stop that needs no Python or venv | ✅ `EMERGENCY_STOP.bat` |
| 3.6.4 | Console encoding fix + ASCII fallback for `cmd.exe` | ✅ `app/util/console.py` |
| 3.6.5 | Static guards for batch pitfalls (CRLF, `&`, labels, `pause`) | ✅ `tests/test_windows.py` |
| 3.6.6 | Typed confirmation before any unattended real-money run | ✅ |
| 3.6.7 | Collapsed to a single entry point; setup and pipeline control moved into the panel | ✅ `START.bat` |
| 3.6.8 | Dependency stack raised to versions with cp314 wheels; verified on 3.14.7 | ✅ |

Batch script cannot be executed on the machine it was written on, so it is kept
to "find Python, make a venv, call the tested script", and everything checkable
statically is asserted in tests.

**Exit criteria:** `START.bat` on a clean Windows box installs, configures and
opens the panel without touching a terminal.

---

## Phase 4 — Close the data gaps ⬜ next

**This is the highest-value remaining work.** Four gates currently degrade to
LIVE_ONLY because nothing feeds them (see `docs/ASSUMPTIONS.md` §B).

| Step | Work | Why it matters |
|---|---|---|
| 4.1 | Decode DEX swap logs to classify buys vs sells | Unblocks `buy_ratio_24h` — the main organic-flow signal |
| 4.2 | First-N-block holder analysis after deployment | Unblocks `sniper_wallet_pct` |
| 4.3 | Same-block multi-wallet buy clustering | Unblocks `bundled_buy_pct` |
| 4.4 | Optional manual unlock-schedule table (`token_unlock`) | Unblocks `days_to_major_unlock` honestly — entered by hand, never guessed |
| 4.5 | Simulated sell via `eth_call` against the router | Real honeypot detection, which bytecode scanning cannot do |

Until 4.1–4.4 land, expect LIVE_BUY to fire rarely. That is correct behaviour,
not a bug.

## Phase 5 — Position management ⬜

| Step | Work |
|---|---|
| 5.1 | `position` table: entry, size, current mark, unrealized PnL |
| 5.2 | Stop-loss and take-profit rules (sell logic — currently absent entirely) |
| 5.3 | Liquidity-drop exit trigger |
| 5.4 | Daily PnL report through the alert sinks |

**Do not run live unattended before this phase.** The system can currently enter
a position and cannot exit one.

## Phase 6 — Real-time layer ⬜

| Step | Work | Gate |
|---|---|---|
| 6.1 | Node WebSocket subscription for new-pair events | Only if 5-minute polling proves too slow |
| 6.2 | OKX liquidity WebSocket channel | Only if REST rate limits bite |
| 6.3 | Redis for cross-process cache | **Only if** you actually run multiple processes |

Each item here is explicitly gated on evidence. Adding them without that
evidence is the expensive mistake this design is trying to avoid.

## Phase 7 — Scale ⬜

| Trigger | Action |
|---|---|
| `token_snapshot` > ~10M rows, or a second writer | Migrate SQLite → Postgres (`DATABASE_URL` is the only change; add Alembic) |
| Reviewing alerts becomes a chore | Build a real dashboard with charts |
| Threshold tuning becomes guesswork | Replay stored snapshots against modified thresholds — the append-only snapshot table already supports this |

---

## What is deliberately not on this roadmap

- **Machine-learned scoring.** There is no labelled outcome data, and there
  won't be until the system has run for months. A model trained on nothing is
  worse than transparent hand-tuned weights you can argue with.
- **More chains.** Get one chain right first.
- **Higher frequency.** The strategy is base-building accumulation. Latency is
  not the edge, and pretending otherwise invites HFT-shaped infrastructure costs
  for no return.
- **Bigger position sizes.** The correct response to the system working well is
  a longer track record, not more capital.
