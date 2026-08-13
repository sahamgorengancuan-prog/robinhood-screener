# Assumptions & Limitations

An honest list of what this system does **not** know. Read it before trusting a
score, and read it again before switching `RUN_MODE=LIVE`.

---

## A. Things that could not be verified while building this

| # | Item | Consequence | Mitigation |
|---|---|---|---|
| A1 | Robinhood Chain Data API request/response shape (docs blocked by egress proxy) | Field names may differ from the candidates in `rh_data.py` | Disabled by default; `pick()` tolerates alternatives; unmatched fields stay `None` → WATCH |
| A2 | OKX `token/basic-info` exact path | Client returns `None` on 404 instead of failing | Marked UNVERIFIED; not a core dependency |
| A3 | Robinhood Chain ID | `RH_CHAIN_ID` blank by default | Probe reads it via `eth_chainId`; OKX lookups are **skipped** rather than run against a guessed chain |
| A4 | Whether a Chainlink deployment exists on this chain | Oracle cross-check unavailable | Disabled by default; the price gate degrades to LIVE_ONLY, not to a false pass |

**Nothing in this repo fabricates a value for an unavailable field.** That is
enforced by `test_every_gate_rejects_an_empty_snapshot`.

---

## B. Metrics that are estimates, not measurements

| Metric | How it is produced | Why it is approximate |
|---|---|---|
| `slippage_bps` | Constant-product model: `n / (L/2 + n)` | Assumes a single x*y=k pool. Real routing is multi-pool with concentrated liquidity; the estimate is **conservative** (over-states slippage) for split routes, and can **under-state** it if the quoted liquidity is not all at the current tick. Re-validated against the real OKX book before any live order. |
| `top1/top10_holder_pct` | Sum of observed balances ÷ total supply | Only published when the basis is the **real total supply**. A partial holder list produces a lower bound, which is stored in `raw` and **not** used for gating. |
| `unique_holders` | Data API or explorer counter | Counts addresses, not people. One person with 40 wallets reads as 40 holders. Sybil-resistant identity is not solvable here. |
| `holder_growth_24h_pct` | Extrapolated from two snapshots ≥1h apart | Needs history. First-ever snapshot always yields `None` → WATCH. |
| `token_age_hours` | Binary search for the first block with code | Accurate, but costs ~25 RPC calls per new token. Cached on `Token.deployed_at`. |
| `price_vs_7d_base_pct` | Current price vs **median** of the 7d window | Median, not mean, so one blow-off candle cannot redefine the base. Needs ≥3 historical points. |
| `buy_ratio_24h` | DexScreener `txns.h24.buys` / `.sells` on the deepest pool | **Wired.** Counts come from one pool, so a token whose flow is split across venues is measured on its dominant pool only. Both sides must be present or the ratio stays `None`. |
| `sniper_wallet_pct`, `bundled_buy_pct` | Same — requires early-block log analysis | `None` today → LIVE_ONLY blockers. The gates exist and are tested; the feed is not wired. |
| `days_to_major_unlock` | Vesting schedules | **Not discoverable on-chain in the general case.** Stays `None` → LIVE_ONLY. The screener does not pretend to know a token is unlock-safe. |

### What "not wired" means in practice

Three metrics (`sniper_wallet_pct`, `bundled_buy_pct`, `days_to_major_unlock`)
have gates and tests but no live data source.
Their gates are `LIVE_ONLY`, so a token missing them can reach ALERT and
PAPER_BUY but **never LIVE_BUY**. This is deliberate: the system tells you what
it doesn't know instead of quietly scoring around it. Wire them (Roadmap phase 4)
before expecting live buys to fire regularly.

---

## C. Modelling assumptions

| Assumption | Value | Rationale & risk |
|---|---|---|
| Unknown metric scores `UNKNOWN_RATIO` | 0.35 | Silence is mildly bad, never neutral, never good. If it were 0.5 a data outage would look like an average token. |
| Paper fill: market touches `PAPER_ASSUMED_TOUCH_BPS` below the bid within the TTL | 60bps | The one genuinely unfalsifiable number here. Stands in for short-horizon volatility, which is not modelled. **Optimistic by design** — compare live `realized_slippage_bps` against paper before trusting the simulated hit rate. Affects paper only. |
| Maker fee | 8bps | Adjust to your actual OKX tier. |
| Liquidity is log-utility | `log_score` | $150k→$400k is a bigger jump in tradability than $3.0M→$3.25M. Linear scoring squashed every realistic candidate into the bottom third and made the thresholds meaningless. |
| Price consensus = median | `reconcile.py` | One manipulated source cannot drag a median the way it drags a mean. |
| Liquidity consensus = minimum | `reconcile.py` | If OKX says $900k and the pool says $200k, you can only exit into $200k. Sizing off the optimistic number is how you get stuck. |
| Symbol match on OKX must be exact | `find_spot_instrument` | A fuzzy match buys the wrong asset. Unresolved → `okx_available=None` → no live buy. |

---

## D. Structural limitations

1. **This is not a honeypot detector.** Bytecode scanning finds *declared*
   privileged functions. It cannot detect transfer logic that reverts
   conditionally for non-whitelisted sellers. A simulated sell (eth_call a
   swap out) would be the real test — not implemented. Treat "no contract flags"
   as "nothing obvious", not "safe".

2. **Proxies defeat static analysis.** `UPGRADEABLE_PROXY` blocks live buying
   precisely because the implementation can be swapped after you pass the scan.

3. **No sell-side logic exists.** The system can enter a position and cannot
   exit one. Exits are manual. Do not run this unattended with capital you would
   miss. (Roadmap phase 5.)

4. **Wash-trade detection is statistical, not forensic.** Turnover ratio, window
   concentration and average trade size catch lazy wash trading. A patient
   adversary trading across many wallets at realistic sizes will pass. Clustering
   counterparties by funding source would catch more; not implemented.

5. **SQLite is one writer.** Fine for one scheduler process. Running two
   instances against one file will produce lock contention and double-count
   exposure. Move to Postgres before scaling out.

6. **Exposure caps are advisory, not custodial.** They are enforced by this
   process reading its own ledger. They cannot stop a manual trade in the OKX UI,
   and they do not survive someone deleting the database. The API key permission
   scope is the real boundary.

7. **Backtesting is impossible from this data.** Snapshots begin the day you
   start running it. Any claim about historical performance would be fabricated.
   Run in ALERT_ONLY for weeks and judge the alerts yourself.

---

## E. Reconciliation policy (when sources disagree)

**Price** — `app/util/reconcile.py`

1. Take the **median** of available sources.
2. `divergence` = max relative distance from that median.
3. `divergence > PRICE_MAX_SOURCE_DIVERGENCE` (5%) → `gate_price_agreement`
   fails **HARD**. A stale feed and a manipulated pool look identical from here,
   and both are reasons not to trade.
4. Single source → usable for alerting, blocks live execution (LIVE_ONLY).

Source trust order: `chainlink > okx_market > dex_pool > explorer`. An oracle
aggregates many venues; OKX aggregates many pools; a single pool is the easiest
thing on the list to manipulate.

**Liquidity** — take the **minimum**. Always size off the number you could
actually exit into.

**Supply** — prefer the on-chain `totalSupply()` over any indexed value. It is
the only one that cannot be wrong.

---

## F. Cost model

| Component | Monthly |
|---|---|
| Shared/public RPC | $0 (paid tier ~$50–200 only if rate-limited) |
| Blockscout explorer | $0 |
| OKX market + trading API | $0 |
| SQLite | $0 |
| Host (1 vCPU / 1GB VPS) | ~$5 |
| **Total** | **~$5/month** |

Deliberately **not** used: paid data aggregators, hosted indexers, managed
Postgres, Redis, Kubernetes, or a self-hosted node. Each was considered and
rejected as unjustified at this cadence — see `docs/ENDPOINTS.md` for the node
arithmetic specifically.
