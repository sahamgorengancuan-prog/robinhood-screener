# Robinhood Chain Token Screener

A risk-gated screener for early-stage tokens on Robinhood Chain, with optional
small auto-buys on OKX.

Its actual job is **saying no**. Anyone can build something that finds tokens
going up; the hard part is refusing the 99% that look good and aren't. Every
design decision here follows from that.

```
Default mode is ALERT_ONLY. A fresh clone cannot place an order.
Missing data is never treated as safe. A token that cannot be measured
cannot be bought.
```

**Status:** phases 0–3 built and tested (251 tests, green on Python 3.14.7 and 3.11). Four metrics have
gates but no wired data source yet — they block live buying rather than being
scored around. See [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) §B.

---

## Quick start (Windows)

**Double-click `START.bat`. That is the whole thing.**

It creates a private Python environment, installs everything, and opens the
control panel in your browser. Setup, connection tests, running the pipeline,
tuning thresholds and the kill switch all live in the panel — there is no other
file to run and no terminal to touch.

Verified on **Python 3.14.7** and 3.11. Full walkthrough:
[`docs/WINDOWS.md`](docs/WINDOWS.md). Which API keys you need and what each one
unlocks: [`docs/API_SETUP.md`](docs/API_SETUP.md).

The one exception is **`EMERGENCY_STOP.bat`**, kept as a separate file on
purpose: it halts execution using no Python and no virtualenv, so it still works
when everything else is broken.

<details>
<summary>Linux / macOS</summary>

```bash
make install && make init
make ui            # http://127.0.0.1:7860
make once          # one screening cycle, headless
```
</details>

Defaults are safe: `RUN_MODE=ALERT_ONLY`, no keys, no orders possible.

---

## Table of contents

0. [Control panel (Gradio UI)](#0-control-panel-gradio-ui)
0b. [Windows packaging](#0b-windows-packaging)
1. [Design & trade-offs](#1-design--trade-offs)
2. [Architecture](#2-architecture)
3. [Database schema](#3-database-schema)
4. [Data sources](#4-data-sources)
5. [Screening flow (pseudo-code)](#5-screening-flow-pseudo-code)
6. [Auto-buy flow (pseudo-code)](#6-auto-buy-flow-pseudo-code)
7. [Configuration](#7-configuration)
8. [Example alert](#8-example-alert)
9. [Runbook](#9-runbook-local-deployment)
10. [Security](#10-security)
11. [Assumptions & limits](#11-assumptions--limits)

---

## 0. Control panel (Gradio UI)

The single operator surface. `START.bat` launches only this; on Linux/macOS,
`make ui`. Seven tabs, ordered the way you actually use them.

### 🚀 Setup

Paste your RPC URL, click **Deteksi** to read the chain ID off the node rather
than hunting for it, click **Simpan**. Optionally add OKX keys, choose the run
mode and position size. Everything is written to `.env`.

The write is careful about the things that bite: it **preserves every comment**
(those annotations are the documentation for each risk threshold), keeps a
`.env.bak`, and **a blank secret field never erases a stored one** — otherwise
re-saving after a page reload would silently wipe your API keys.

### 🩺 Koneksi & API Test

The tab you use first and return to whenever a number looks wrong. It does more
than report up/down:

| Column | Why it's there |
|---|---|
| **Status** | 🟢 OK · 🟡 WARN · 🔴 FAIL · ⚪ SKIP |
| **Latensi** | Distinguishes "working" from "barely working" |
| **Ringkasan** | What actually came back |
| **Tindakan** | The specific next step, not a generic error string |

Three things make it genuinely useful rather than decorative:

1. **It shows the field names each API really returned.** For the OKX and
   Robinhood Data APIs — whose contracts this repo could not verify offline —
   the panel reports `observed_keys`, which of our fields **parsed**, and which
   are **unparsed**. That is exactly what you need to reconcile a parser against
   a live API. Unparsed fields stay `None` and route tokens to WATCH; they never
   become `0`.
2. **It fails fast.** Diagnostics run with retries disabled and shortened
   timeouts, so a dead endpoint reports in ~400ms instead of ~9s. The screening
   loop still retries with backoff — different job, different tuning.
3. **It translates results into capability**, not just status: *"Ready for
   ALERT_ONLY"*, *"Ready for PAPER"*, or *"Not ready — every token will report
   missing data and stay in WATCH."*

Also on this tab: a one-click **RPC ping**, and a **test alert** that pushes a
real message through every configured sink so you find out the webhook is wrong
now rather than during a live signal.

Credentials can also be typed into the collapsible override panel at the top of
the page to test them **without** saving. Those live in process memory only —
useful for trying a key before committing it on the Setup tab.

### 🎛️ Threshold Lab

The most useful tab for learning the system. Move any slider — token metrics on
the left, risk thresholds on the right — and the verdict, the 0–100 score
breakdown and all 21 gate results recompute **instantly**. The result panel is
sticky at the top, so you always see the effect of the slider you are dragging.

It is fully offline and pure: no network, no database, no persistence. Use it to
calibrate thresholds before copying them into `.env`, and to answer "why was
this rejected?" by reproducing the token's metrics and watching which gate
flips. It calls the same `evaluate_gates` / `score_snapshot` / `decide`
functions as production — it is not a separate model that can drift.

### 🔬 Token Inspector

Paste a contract address and it runs the full pipeline for that one token:
collect → normalize → 21 gates → score → decision, plus the rendered alert and a
provenance panel showing which source produced each field and what was missing.
**Read-only** — it writes no rows and creates no orders.

### 📊 Screener · 🛡️ Risiko & Order · ⚙️ Konfigurasi

The Screener tab runs **one cycle on demand** or starts the **continuous loop**
in-process, with a live status line (cycles completed, last run, errors). Below
that: ranked results with per-component score columns and state filters. Risiko
& Order holds the kill switch, live exposure counters and the order ledger.
Konfigurasi shows the effective settings with every secret redacted.

### What the panel can and cannot do

It **can** write `.env` and start or stop the screening loop. That is what makes
one-click setup possible, and it is the only place the browser changes
behaviour.

It **cannot place an order.** There is no order-submitting control anywhere in
the module, and a test asserts it never calls the execution functions. Orders
may only originate from a decision that passed the risk gates. The panel can
*stop* trading; it cannot *start* a trade.

Switching to LIVE with real keys **is** possible from the Setup tab — that is
the cost of the convenience — and the save handler says so in the loudest terms
it can. After that, the kill switch and the exposure caps are the only things
between the bot and your balance.

### Security

The UI binds to `127.0.0.1` by default and has **no authentication** — it can
engage and release the kill switch. Put it behind an authenticating reverse
proxy before binding it anywhere else; it logs a warning if you change
`GRADIO_HOST`. It also makes no outbound requests of its own (local font stack,
no CDN theme), so running it does not announce itself.

If you don't want the UI at all, drop the single `gradio` line from
`requirements.txt` — nothing else depends on it.

---

## 0b. Windows packaging

The deployment target is Windows on Python 3.14, so there is exactly **one**
entry point and no `make` in the loop.

| File | Purpose |
|---|---|
| `START.bat` | **Everything.** Bootstraps the venv, installs, creates `.env`, opens the panel. |
| `EMERGENCY_STOP.bat` | Halts execution instantly — **no Python required**. |

Everything that used to be a separate `.bat` — setup, connection tests, running
a cycle, the continuous scheduler — is now a control in the panel.

`START.bat` is deliberately thin: find Python, make a venv, launch
`app.ui.gradio_app`. Batch script is the one part of this project that cannot be
executed on the machine it was written on, so there is as little of it as
possible, and `tests/test_windows.py` asserts what can be checked statically —
CRLF endings, no unescaped `&`, valid `goto` targets, a `pause` at the end,
paths anchored to `%~dp0`, and that no stray `.bat` files reappear.

Three Windows-specific behaviours worth knowing:

- **Console encoding.** `cmd.exe` defaults to a legacy code page and raises
  `UnicodeEncodeError` on emoji, which would kill a screening cycle mid-run.
  Output is forced to UTF-8 and status icons fall back to ASCII
  (`[+] [!] [x] [-]`) unless Windows Terminal or VS Code is detected, since the
  classic console renders emoji as boxes even at code page 65001.
- **CRLF is mandatory** for `.bat` and pinned in `.gitattributes`; `cmd.exe`
  mis-parses `goto` labels in LF-only batch files.
- **Real-money mode still demands a typed confirmation** in the headless
  runner, and the panel warns loudly when you save it.

---

## 1. Design & trade-offs

### The three decisions that shape everything

**1. Missing data is a rejection, not a default.**

The classic screener bug is `float(response.get("liquidity", 0))`. An API hiccup
becomes `0`, `0` fails a `>` check somewhere, and eventually some inverted
condition lets a honeypot through. Here every metric is `Optional`, `None` can
never satisfy a gate, and a registry-wide test enforces it:

```python
def test_every_gate_rejects_an_empty_snapshot(...):
    passing = [g.name for g in evaluate_gates(empty_snapshot, settings) if g.passed]
    assert passing == []
```

*Cost:* more WATCH states early on, and LIVE_BUY fires rarely until the data
layer is complete. *Benefit:* the failure mode is a missed opportunity instead
of a bought rug.

**2. Gates and scores are separate, and gates always win.**

A score is a ranking of survivors. It is not a way to earn an exception. A
95/100 token with one HARD gate failure is REJECT, and no threshold change
alters that. Scores decide *which* of the acceptable tokens to look at; gates
decide *what is acceptable*.

**3. On-chain standards are the floor, vendors are the enhancement.**

`docs.robinhood.com` and `web3.okx.com` are blocked from this build environment,
so the exact Data API and Web3 API contracts **could not be verified**. Two
responses were possible: invent plausible field names, or build so that being
wrong is safe. This repo does the second — see
[`docs/ENDPOINTS.md`](docs/ENDPOINTS.md).

The Node API client depends only on published standards (JSON-RPC, the ERC-20
ABI, the `Transfer` topic, the EIP-1967 slot). It cannot be wrong about a
vendor's naming because it doesn't use one. Supply, contract privileges, proxy
status, token age and holder distribution can all be rebuilt from it if every
indexed source disappears.

### Cost vs quality

| Choice | Picked | Rejected | Why |
|---|---|---|---|
| Chain data | Node API + Data API + free Blockscout | Paid aggregators ($200–2000/mo) | Official + free covers every gate |
| Node | Shared RPC | Self-hosted Orbit node (~$150–400/mo + ops) | Buys ~200ms this strategy can't use, adds stale-data risk |
| Storage | SQLite + WAL | Postgres | One writer, tens of thousands of rows/day |
| Queue | None | Redis / Celery | Nothing to queue at a 5-minute cadence |
| Scheduler | APScheduler in-process | Celery beat / k8s CronJob | No broker, no second process |
| Contract safety | Bytecode scan + Blockscout verification | Paid audit API | Catches declared privileges for $0 |
| Real-time | REST polling | WebSocket everywhere | Base-building doesn't need tick data |

**Running cost: ~$5/month** (one small VPS). Every paid option was considered
and rejected with a reason, not skipped by accident.

### Why the score is shaped the way it is

| Component | Weight | What it rewards |
|---|---|---|
| Liquidity quality | 30 | Depth, tight spread, low slippage, liquidity backing the market cap |
| Holder distribution | 25 | Low top-1/top-10, many holders, healthy growth, low HHI |
| Volume quality | 20 | Volume spread across windows, many small trades, balanced flow |
| Tokenomics safety | 15 | Verified source, no mint/blacklist/pause, no unlock cliff |
| Early momentum | 10 | **Quiet** strength — flat-to-mildly-up, off the highs |

Two details worth noting. **Liquidity, holders and volume score on a log scale**,
because $150k→$400k of liquidity is a far bigger jump in tradability than
$3.0M→$3.25M; scoring them linearly squashed every realistic candidate into the
bottom third and made the thresholds meaningless. And **momentum is capped at 10
points and is anti-parabolic** — a token up 55% in 24h scores *worse* than one
up 3%. Being 22% off the local high scores better than sitting at it. The system
is built to buy bases, not breakouts.

---

## 2. Architecture

```
                        ┌──────────────────────────────────────┐
                        │  APScheduler  (every INGEST_INTERVAL) │
                        └────────────────┬─────────────────────┘
                                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│ A. INGESTION            app/pipeline/ingest.py                      │
│    discover → collect (5 sources in parallel, each fault-isolated)  │
│    RH Node · RH Data · Blockscout · OKX Market · OKX Trade          │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│ B. NORMALIZATION        app/pipeline/normalize.py + metrics.py      │
│    one schema · per-field provenance · missing_fields recorded      │
│    price/liquidity reconciliation (median / min)                    │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│ C. RISK GATES           app/pipeline/risk.py         21 gates       │
│    HARD → reject · LIVE_ONLY → no live buy · DATA → watch · SOFT    │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│ D. SCORING              app/pipeline/scoring.py      0–100          │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│ E. DECISION             app/pipeline/decision.py                    │
│    REJECT · WATCH · ALERT · PAPER_BUY · LIVE_BUY                    │
└───────────────┬─────────────────────────────────┬───────────────────┘
                ▼                                 ▼
┌───────────────────────────────┐   ┌─────────────────────────────────┐
│ G. ALERTING  app/alerts/      │   │ F. EXECUTION  app/execution/    │
│ console · file · webhook · TG │   │ kill switch → safe mode →       │
└───────────────────────────────┘   │ exposure → order safety →       │
                                    │ paper | live (post-only)        │
                                    └─────────────────────────────────┘
```

### Folder structure

```
robinhood-screener/
├── app/
│   ├── config.py               # every threshold, one auditable place
│   ├── models.py               # SQLAlchemy schema
│   ├── db.py                   # engine, WAL pragmas, session scope
│   ├── schemas.py              # NormalizedSnapshot, GateResult, Decision
│   ├── services.py             # client container
│   ├── diagnostics.py          # connection & API checks (UI + CLI share these)
│   ├── util/console.py         # Windows console encoding + ASCII fallback
│   ├── main.py                 # FastAPI + dashboard
│   ├── scheduler.py            # APScheduler wiring
│   ├── clients/
│   │   ├── base.py             # retry + rate limit + TTL cache + pick()
│   │   ├── rh_node.py          # JSON-RPC, ERC-20, bytecode scan, proxy detect
│   │   ├── rh_data.py          # indexed data (config-driven paths)
│   │   ├── explorer.py         # Blockscout: contract verification
│   │   ├── okx_market.py       # token search / price info / basic info
│   │   ├── okx_trade.py        # v5 spot: instruments, book, order
│   │   └── chainlink.py        # optional oracle cross-check
│   ├── pipeline/
│   │   ├── ingest.py           # orchestration
│   │   ├── normalize.py        # raw → NormalizedSnapshot
│   │   ├── metrics.py          # pure derived metrics
│   │   ├── risk.py             # 21 gates, 4 severities
│   │   ├── scoring.py          # 5 weighted components
│   │   └── decision.py         # 5-state machine
│   ├── execution/
│   │   ├── killswitch.py       # 3 triggers, fails closed
│   │   ├── order_safety.py     # pure pre-trade checks
│   │   ├── portfolio.py        # exposure accounting
│   │   ├── paper.py            # simulation on the live code path
│   │   └── live.py             # the only code that spends money
│   ├── alerts/
│   │   ├── formatter.py        # human + machine payloads
│   │   └── sinks.py            # console / file / webhook / telegram
│   ├── ui/
│   │   ├── gradio_app.py       # 6-tab control panel
│   │   └── theme.py            # styling, no external font/CDN requests
│   └── util/reconcile.py       # multi-source price & liquidity policy
├── setup.bat                   # Windows: first-time install
├── run_pipeline.bat            # Windows: THE one-click
├── start_ui.bat                # Windows: control panel
├── EMERGENCY_STOP.bat          # Windows: halt execution (no Python needed)
├── scripts/
│   ├── one_click.py            # all one-click logic (the .bat is a thin shell)
│   ├── init_db.py
│   ├── run_ui.py               # Gradio control panel
│   ├── probe_endpoints.py      # same checks, in the terminal
│   ├── run_once.py
│   └── demo_alert.py           # offline sample alerts
├── tests/                      # 251 tests
└── docs/
    ├── ENDPOINTS.md            # verified vs unverified matrix
    ├── ASSUMPTIONS.md          # what the system does NOT know
    ├── SECURITY_CHECKLIST.md
    └── ROADMAP.md
```

---

## 3. Database schema

```
token ──┬── token_snapshot   (append-only time series)
        ├── evaluation       (score + gates + decision, per run)
        │      └── order_record  (every paper & live order)
        └── alert
kv_state  (kill switch, safe mode, cursors)
```

**`token`** — identity, one row per `(chain, address)`. Holds `deployed_at`,
`contract_verified`, `is_proxy`, and OKX availability (`okx_available` is
tri-state: `None` = never resolved, `False` = confirmed absent).

**`token_snapshot`** — append-only. Never updated; re-runs insert new rows so
history stays intact and thresholds can be replayed against stored data later.
Carries every normalized metric plus three provenance columns: `sources`
(field → which client produced it), `missing_fields`, and `raw`.

**`evaluation`** — one row per scoring run: the five component scores, the
total, the state, `hard_fail` / `insufficient_data` flags, the full
`gate_results` JSON, and the human-readable `decision_reason`.

**`order_record`** — every paper and live order, each linked to the
`evaluation_id` that justified it. **No order can exist without the reasoning
attached.** Stores `client_order_id` (unique — replay-safe), exchange id,
requested notional, limit price, fill, `realized_slippage_bps`, `entry_reason`
and any error.

**`alert`** — outbound notifications with a `dedupe_key`
(`token:state:hour`) and a per-sink `delivered` map.

**`kv_state`** — small runtime flags. Kill switch and safe mode live here so
they survive restarts and are settable over the API.

Full definitions: [`app/models.py`](app/models.py).

---

## 4. Data sources

Priority order, and what each is trusted for:

| # | Source | Trusted for | Notes |
|---|---|---|---|
| 1 | **RH Node API** (JSON-RPC) | supply, decimals, contract privileges, proxy status, token age, holder reconstruction | Standards-only. Highest trust. |
| 2 | **RH Data API** | token discovery, indexed holders, transfers | **Disabled by default** — contract unverified |
| 3 | **Blockscout** | contract source verification, holder fallback | Free, no key. Verification is a hard gate. |
| 4 | **OKX Market API** | price, liquidity, volume, holders, supply | `token/search`, `price-info` verified; `basic-info` unverified |
| 5 | **OKX Trading API** | instrument existence, order book, execution | v5 spot. Buy-only surface. |
| 6 | **Chainlink** | quote-asset price sanity | Optional, off by default |

Details and the verified/unverified matrix: [`docs/ENDPOINTS.md`](docs/ENDPOINTS.md).

### When sources disagree

**Price** → median of available sources; divergence measured as max distance
from that median; >5% fails HARD. One source is usable for alerting but blocks
live execution. Trust order: `chainlink > okx_market > dex_pool > explorer`.

**Liquidity** → **minimum**. If OKX says $900k and the pool says $200k, you can
only exit into $200k.

**Supply** → on-chain `totalSupply()` wins. It cannot be wrong.

---

## 5. Screening flow (pseudo-code)

```python
def screening_cycle():
    if kill_switch_engaged():
        log("execution disabled — screening continues")   # alerts still flow

    if run_mode == LIVE:
        check_safe_mode(reference="BTC-USDT", max_1h_move=4%)

    for token in discover_tokens():          # Data API feed, else tracked tokens
        history = load_history(token)        # prior snapshots: holders, prices

        # ---- A. COLLECT (parallel, each source fault-isolated) -------------
        raw = gather(
            okx_price_info(token),           # price, liquidity, volume, holders
            okx_spot_instrument(symbol),     # is it tradable on OKX at all?
            onchain_erc20(token),            # supply, decimals, symbol
            onchain_contract_scan(token),    # mint/pause/blacklist/proxy/owner
            explorer_verification(token),    # source verified?
            holder_list(token),              # Data API, else explorer
        )
        # a failed source leaves its fields None — it never yields a zero

        # ---- B. NORMALIZE --------------------------------------------------
        snap = normalize(raw)
        snap.price      = median(price_sources)          # not mean
        snap.divergence = max_distance_from(median)
        snap.liquidity  = min(liquidity_sources)         # pessimistic
        snap.top1, snap.top10, snap.hhi = holder_distribution(balances, supply)
        #   ^ only published if the basis is real total supply, else recorded
        #     as partial and NOT used for gating
        snap.slippage   = xyk_estimate(liquidity, position_usd)
        snap.spread     = spread_bps(bid, ask)
        snap.age        = now - deploy_block_timestamp
        snap.vs_base    = price vs MEDIAN of 7d history  # median: one candle
        snap.missing    = [f for f in TRACKED if snap.f is None]  #   can't move it

        persist(snap)                        # append-only

        # ---- C. RISK GATES -------------------------------------------------
        gates = []
        for gate in REGISTRY:                # 21 gates
            try:
                gates.append(gate(snap, config))
            except Exception:
                gates.append(HARD_FAIL)      # a broken gate never opens the door

        # severities:
        #   HARD      → reject outright (liquidity floor, wash turnover, volume
        #               spike, top1/top10, holder growth, snipers, bundling,
        #               unverified contract, mint+owner, blacklist, unlock cliff,
        #               too young, parabolic, price disagreement)
        #   LIVE_ONLY → alert/paper allowed, live buy blocked (thin liquidity,
        #               no CEX book, proxy/pausable, unknown verification,
        #               unknown unlocks, single price source)
        #   DATA      → metric unavailable → WATCH
        #   SOFT      → score penalty only

        # ---- D. SCORE ------------------------------------------------------
        score = ( liquidity_quality  * 30    # log depth, slippage, spread, backing
                + holder_distribution * 25   # top1, top10, count, growth, HHI
                + volume_quality      * 20   # size, evenness, granularity, balance
                + tokenomics_safety   * 15   # verification + privilege penalties
                + early_momentum      * 10 ) # quiet strength, anti-parabolic
        # any component built from missing inputs scores UNKNOWN_RATIO (0.35),
        # never 1.0 — silence is mildly bad, never good

        # ---- E. DECIDE (first match wins) ----------------------------------
        if any(HARD failures):        state = REJECT
        elif any(DATA failures):      state = WATCH
        elif score < ALERT_MIN:       state = REJECT
        elif score < PAPER_MIN:       state = ALERT
        elif not stable_for_N_snapshots_with_low_variance:
                                      state = ALERT
        elif any(LIVE_ONLY failures) or not on_okx or kill_switch
             or safe_mode or exposure_exceeded or run_mode != LIVE
             or score < LIVE_MIN:     state = PAPER_BUY
        else:                         state = LIVE_BUY

        persist(evaluation)
        alert_if(state >= ALERT_MIN_STATE)    # deduped per token/state/hour

        if state == PAPER_BUY:  simulate_buy(...)
        if state == LIVE_BUY:   execute_live_buy(...)
```

---

## 6. Auto-buy flow (pseudo-code)

Nine guards, evaluated in order. Any failure aborts, and the reason is recorded.

```python
def execute_live_buy(token, evaluation, decision):
    # ---- 1-3. global switches -----------------------------------------
    require(run_mode == "LIVE")                    else abort
    require(not kill_switch.engaged())             else abort   # fails closed
    require(not safe_mode.active())                else abort

    # ---- 4-5. venue reality check -------------------------------------
    require(okx_credentials_present())             else abort
    meta = okx.instrument_meta(inst_id)
    require(meta and meta.state == "live")         else abort
    #   token not on OKX  →  never reaches here; decision engine already
    #   marked it "on-chain only / manual review" and capped it at PAPER_BUY

    # ---- 6. FRESH book, not the scored snapshot -----------------------
    bid, ask = okx.top_of_book(inst_id)

    # ---- 7. conditions must still hold --------------------------------
    require(liquidity_now >= MIN_LIQUIDITY_USD_LIVE)          else abort
    require(liquidity_now >= 0.75 * liquidity_at_decision)    else abort
    require(slippage_now  <= MAX_SLIPPAGE_BPS)                else abort

    # ---- 8. exposure caps ---------------------------------------------
    require(token_exposure  + clip <= MAX_PER_TOKEN)   else abort
    require(daily_exposure  + clip <= MAX_DAILY)       else abort
    require(open_positions  <  MAX_OPEN_POSITIONS)     else abort
    require(orders_today    <  MAX_ORDERS_PER_DAY)     else abort

    # ---- 9. build the order -------------------------------------------
    require(ask >= bid)                                          else abort
    require(spread_bps(bid, ask) <= MAX_SPREAD_BPS)              else abort
    require(|mid - reference_price| / reference <= 5%)           else abort
    #   ^ the book must agree with the price we actually scored

    limit = bid * (1 - LIMIT_OFFSET_BPS/10_000)   # 25bps BELOW the bid
    size  = round_DOWN(clip / limit, to=lot_size) # down: never overspend
    require(size >= min_size)                                    else abort
    require(size * limit <= clip * 1.01)                         else abort

    # ---- send ----------------------------------------------------------
    persist(order, status="created")   # intent recorded BEFORE the network call
    resp = okx.place_order(inst_id, side="buy", ordType="post_only",
                           px=limit, sz=size, clOrdId=unique_id, tdMode="cash")
    persist(order, status="live", exchange_id=resp.ordId)

    # ---- lifecycle ------------------------------------------------------
    poll: fills → realized_slippage_bps
    after ORDER_TTL_S unfilled → cancel   # a stale bid is one you don't want hit
```

**Why post-only below the bid.** A post-only order can never take liquidity, so
the worst outcome is *no fill* — never a surprise fill at the ask in a book that
just got thin. Rounding is always **down**, so a rounding bug can only underspend.

**Why paper shares this path.** `simulate_buy` calls the same
`build_order_plan` and the same safety checks; only the network call is
replaced. Paper mode is the live path with the order stubbed, not an optimistic
parallel implementation — otherwise its results wouldn't predict anything.

---

## 7. Configuration

Full annotated template: [`.env.example`](.env.example). The values that matter
most:

```bash
RUN_MODE=ALERT_ONLY        # ALERT_ONLY | PAPER | LIVE   ← start here
KILL_SWITCH_FILE=./KILL_SWITCH

RH_NODE_RPC_URL=           # required — standard JSON-RPC
RH_CHAIN_ID=               # leave blank; probe reads it via eth_chainId
RH_DATA_ENABLED=false      # unverified contract — enable after probing

EXPLORER_BASE_URL=https://robinhoodchain.blockscout.com
OKX_API_KEY=               # market data key
OKX_TRADE_API_KEY=         # SEPARATE key, trade-only, IP-allowlisted
OKX_SIMULATED=true         # demo trading

# hard gates
MIN_LIQUIDITY_USD=150000
MIN_LIQUIDITY_USD_LIVE=400000
MAX_SLIPPAGE_BPS=150
MAX_TOP1_HOLDER_PCT=12
MAX_TOP10_HOLDER_PCT=40
MIN_UNIQUE_HOLDERS=300
MAX_VOLUME_TO_LIQUIDITY_RATIO=8.0     # wash-trade screen
MAX_SINGLE_WINDOW_VOLUME_SHARE=0.35   # spike screen
MAX_PRICE_CHANGE_24H_PCT=60           # anti-chase
MIN_TOKEN_AGE_HOURS=24

# scores
SCORE_ALERT_MIN=60
SCORE_PAPER_BUY_MIN=72
SCORE_LIVE_BUY_MIN=78

# money
POSITION_USD=25
MAX_EXPOSURE_PER_TOKEN_USD=50
MAX_EXPOSURE_DAILY_USD=200
```

---

## 8. Example alert

Generated offline by `make demo` — no keys, no network:

```
[PAPER BUY] GOOD — score 85.4/100
==============================================================
Token      : Good Token (GOOD)
Contract   : 0xabababababababababababababababababababab
Chain      : robinhood

SCORE BREAKDOWN
  liquidity    23.4/30   [################....]
  holders      19.1/25   [###############.....]
  volume       19.5/20   [####################]
  tokenomics   15.0/15   [####################]
  momentum      8.4/10   [#################...]
  TOTAL        85.4/100

KEY METRICS
  price            : 0.05   (sources: okx_market, dex_pool, divergence 0.10%)
  liquidity        : $1,500,000
  volume 5m/1h/24h : $9,000 / $95,000 / $2,200,000
  tx count 24h     : 4100
  buy share 24h    : 51.0%
  holders          : 6500  (growth 12.0%/24h)
  top1 / top10     : 4.0% / 18.0%
  whale HHI        : 0.015
  spread / slippage: 0.20% / 0.30% (on $25)
  token age        : 12.0 days
  price 1h / 24h   : 0.8% / 4.0%
  vs 7d base       : 8.0%
  off local high   : 22.0%

RISK FLAGS
  contract verified: True
  contract flags   : none detected
  tokenomics flags : none detected
  sniper / bundled : 3.0% / 4.0%
  next unlock      : 90 days

PASSED GATES (21)
  + liquidity_min: liquidity $1,500,000
  + volume_organic: turnover 1.47x liquidity
  + volume_spike: recent window is 4% of 24h volume
  + top1_concentration: top holder 4.0%
  + holder_growth: holder growth +12.0%/24h
  + contract_flags: no dangerous contract privileges detected
  + not_extended: 24h change +4.0%
  ... (14 more)

VENUE
  OKX: available as GOOD-USDT

DECISION : PAPER_BUY
REASON   : passes all risk gates; simulated only — run_mode=ALERT_ONLY
ACTION   : Simulated only. Review manually before any real capital.
```

A rejection shows the same detail with the failures first:

```
[REJECT] RUGY — score 41.8/100
  tokenomics    0.0/15   [....................]

HARD FAILURES (disqualifying)
  x volume_organic: turnover 20.0x liquidity in 24h — wash-trading / churn pattern
  x volume_spike: 73% of 24h volume sits in one recent window — spike, not accumulation
  x buy_sell_balance: buy share 94% is implausibly one-sided — likely manufactured flow
  x top1_concentration: top holder controls 41.0% (max 12.0%)
  x holder_growth: holders +3000% in 24h — airdrop farming or sybil inflation
  x sniper_domination: first-block snipers still hold 52.0%
  x contract_flags: dangerous contract privileges: BLACKLIST, OWNER_CAN_MINT
  x unlock_proximity: major unlock in 2.0 days (min 14.0)
  x not_extended: price +340% in 24h — already extended, no chasing
```

And a token that can't be measured is held, not guessed at:

```
[WATCH] NEWT — score 65.0/100

MISSING DATA
  ? holders_min: unique_holders unavailable — cannot evaluate (unknown is never treated as safe)
  ? top1_concentration: top1_holder_pct unavailable — cannot evaluate (...)
  ? contract_flags: contract bytecode scan unavailable — cannot evaluate (...)

VENUE
  OKX: NOT LISTED — on-chain only / manual review (no auto-buy)

DECISION : WATCH
```

---

## 9. Runbook (local deployment)

### Install (Windows)

Double-click **`setup.bat`**. It creates the virtualenv, installs everything,
writes `.env`, builds the database and runs the tests. See
[`docs/WINDOWS.md`](docs/WINDOWS.md) for prerequisites and troubleshooting.

<details>
<summary>Linux / macOS</summary>

```bash
git clone <repo> && cd robinhood-screener
python3 -m venv .venv && source .venv/bin/activate
make install
make init                 # creates .env from template + builds the DB
```
</details>

### Step 1 — see it work with no keys at all

```bash
make ui                   # control panel — every tab works without credentials
make demo                 # renders three sample alerts offline
make test                 # the test suite
```

The **Threshold Lab** tab is the fastest way to understand the scoring engine:
drag a slider, watch the verdict change.

### Step 2 — point it at real endpoints

Edit `.env`, set `RH_NODE_RPC_URL`, then either open the **Koneksi & API Test**
tab in the UI, or run the identical checks in a terminal:

```bash
make ui                                   # browser
make probe TOKEN=0xYourTokenAddress       # terminal (same diagnostics module)
```

This is the **most important step**. It reports which endpoints actually
respond and prints the keys each returns, so you can compare them against the
candidate names in the clients. Set `RH_CHAIN_ID` to the value it reads from
`eth_chainId`. Anything that fails here will simply report its metrics as
unavailable, which routes tokens to WATCH — that is the intended degradation,
not a crash.

### Step 3 — run in ALERT_ONLY

```bash
make once                            # a single cycle, printed
make run                             # API + scheduler → http://localhost:8000
```

```bash
curl localhost:8000/health           # mode, kill switch, safe mode
curl localhost:8000/tokens           # ranked by score
curl localhost:8000/orders
curl -X POST localhost:8000/tokens \
  -H 'content-type: application/json' \
  -d '{"address":"0x...","symbol":"ABC"}'    # track a contract manually
```

**Stay here for at least two weeks.** Read every alert. Ask of each one: would
I have taken this trade? Tighten thresholds until the answer is usually yes.

### Step 4 — paper trading

```bash
RUN_MODE=PAPER
OKX_SIMULATED=true
```

Run a week. Review `/orders`. Compare `realized_slippage_bps` against reality —
the paper fill model is deliberately optimistic (see
[`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) §C).

### Step 5 — live, carefully

Work through [`docs/SECURITY_CHECKLIST.md`](docs/SECURITY_CHECKLIST.md) first.
Then:

```bash
RUN_MODE=LIVE
OKX_SIMULATED=true        # still demo — exercises the full live path safely
```

Only after that behaves for a day: `OKX_SIMULATED=false`, smallest possible
`POSITION_USD`.

### Kill switch

**Windows: double-click `EMERGENCY_STOP.bat`.** Make a desktop shortcut before
you need it — it uses no Python and no virtualenv, so it works when everything
else is broken. `resume_trading.bat` clears it.

```bash
make kill                 # touch KILL_SWITCH — instant, no restart
curl -X POST localhost:8000/kill-switch/engage -d '{"reason":"drill"}'
make unkill
```

Three independent triggers (env var, file, DB flag), and it **fails closed** —
if the check itself throws, it reports engaged. Screening and alerting continue
while killed; only execution stops.

### Run as a service

```ini
# /etc/systemd/system/screener.service
[Unit]
Description=Robinhood Chain Token Screener
After=network-online.target

[Service]
Type=simple
User=screener
WorkingDirectory=/opt/robinhood-screener
ExecStart=/opt/robinhood-screener/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Bind to `127.0.0.1`. These endpoints have **no authentication** — do not expose
port 8000.

---

## 10. Security

Full list: [`docs/SECURITY_CHECKLIST.md`](docs/SECURITY_CHECKLIST.md). The
essentials:

- **Separate API keys** for market data and trading
- Trading key: **trade permission only, never withdrawal**, IP-allowlisted
- The OKX sub-account holds only capital you would shrug at losing
- `.env` is gitignored and `chmod 600`
- API bound to localhost
- Kill switch tested **before** you need it
- There is **no sell logic** — you need a manual exit plan
- No market orders, no margin, no leverage, no withdrawal method exists anywhere
  in the codebase. If the code cannot express an action, a bug cannot perform it.

---

## 11. Assumptions & limits

Read [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) in full before trusting a
score. The four things most likely to bite you:

1. **Four gates have no data source yet** — `buy_ratio_24h`,
   `sniper_wallet_pct`, `bundled_buy_pct`, `days_to_major_unlock`. They are
   `LIVE_ONLY`, so tokens missing them reach ALERT/PAPER_BUY but never LIVE_BUY.
   Expect live buys to be rare until Roadmap phase 4 lands. That is correct
   behaviour, not a bug.

2. **This is not a honeypot detector.** Bytecode scanning finds *declared*
   privileged functions. It cannot detect transfer logic that reverts for
   non-whitelisted sellers. "No contract flags" means "nothing obvious", not
   "safe".

3. **Slippage is a constant-product estimate**, not a quote. It is re-validated
   against the real OKX book before any live order, but the on-chain figure is
   an approximation.

4. **You cannot backtest this.** Snapshots start the day you start running it.
   Any historical performance claim would be fabricated.

---

## Tests

```bash
make test    # 251 tests
```

| File | Covers |
|---|---|
| `test_risk_gates.py` | Every gate, both directions; the registry-wide "empty snapshot passes nothing" invariant; fail-closed on exceptions |
| `test_scoring.py` | Weights sum to 100; unknown scores below known-good; spikes/parabolas/concentration penalised; determinism |
| `test_decision.py` | State precedence; hard-fail beats a perfect score; kill switch, safe mode, exposure and OKX absence each block live buys |
| `test_order_safety.py` | Exposure caps; post-only pricing below the bid; crossed/wide books refused; lot rounding never rounds up; liquidity-drop and slippage aborts |
| `test_paper_execution.py` | Exposure accumulation against a real DB; paper and live budgets isolated; paper enforces the same safety checks |
| `test_metrics_and_reconcile.py` | HHI catches dispersed whales; median resists a manipulated source; liquidity reconciliation is pessimistic |
| `test_diagnostics.py` | Checks never raise; unreachable hosts report FAIL with a fix; skips explain their consequence; diagnostics don't retry |
| `test_ui.py` | Threshold lab agrees with the engine; every red flag rejects; overrides don't leak into global settings; the UI cannot place an order |
| `test_windows.py` | `.bat` files are CRLF with no unescaped `&`, valid `goto` targets and a `pause`; blank `RH_CHAIN_ID` parses; `.env.example` loads; ASCII console fallback works; emergency stop needs no Python |

---

## License

Provided as-is for research and educational use. Trading digital assets carries
substantial risk of total loss. Nothing here is financial advice. You are
responsible for every order this software places on your behalf — read the code
before giving it an API key.
