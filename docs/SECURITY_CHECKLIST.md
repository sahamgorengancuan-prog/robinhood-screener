# Security Checklist

Work top to bottom. Do not skip to the LIVE section.

---

## 1. Secrets

- [ ] `.env` is in `.gitignore` and has **never** been committed
      (`git log --all --full-history -- .env` must return nothing)
- [ ] `chmod 600 .env`
- [ ] Market-data and trading keys are **separate OKX API keys**
- [ ] The trading key has **Trade permission only** — never Withdraw
- [ ] The trading key is **IP-allowlisted** to your server's address
- [ ] The passphrase is not reused from any other service
- [ ] Keys are rotated on any suspicion, and after any laptop loss
- [ ] `/config` returns `***` for every secret (verify after adding new settings —
      the redaction matches on `secret|passphrase|api_key|token|chat_id`)

## 2. Blast radius

- [ ] `POSITION_USD` is small enough that total loss is genuinely irrelevant
- [ ] `MAX_EXPOSURE_DAILY_USD` is a number you would shrug at losing **every day**
- [ ] The OKX sub-account used holds only the capital you are willing to lose
- [ ] Withdrawal is disabled on that sub-account
- [ ] There is no sell logic in this system — you have a **manual exit plan**

## 3. Kill switch — test it before you need it

Three independent triggers, any one of which stops execution:

```bash
touch KILL_SWITCH                              # 1. instant, no restart
curl -X POST localhost:8000/kill-switch/engage \
     -H 'content-type: application/json' -d '{"reason":"drill"}'   # 2. API
# 3. KILL_SWITCH=true in .env (requires restart)
```

- [ ] Engaged the kill switch and confirmed `/health` shows
      `live_trading_possible: false`
- [ ] Confirmed a cycle still **screens and alerts** while killed (it should —
      only execution stops)
- [ ] The switch **fails closed**: if the check itself throws, it reports
      *engaged*

## 4. Network exposure

- [ ] The API binds to `127.0.0.1`, or sits behind a reverse proxy with auth
- [ ] Port 8000 is **not** open to the internet — there is no authentication on
      these endpoints
- [ ] TLS if it is reachable off-host
- [ ] Webhook / Telegram URLs are treated as secrets (alerts disclose your
      strategy and positions)

## 5. Code-level guarantees (already enforced — verify after any change)

- [ ] `make test` passes
- [ ] No market orders exist anywhere (`grep -rn "market" app/execution/` should
      show no `ordType`)
- [ ] No sell / withdraw / transfer method exists in any client
- [ ] Every order is `post_only` and priced **below** the bid
- [ ] Order intent is written to the DB **before** the network call, so a crash
      mid-flight is auditable
- [ ] `clOrdId` is unique per order (DB unique constraint) — replay-safe
- [ ] A gate that raises produces a HARD failure, never a skip
      (`test_a_raising_gate_fails_closed`)

## 6. Data integrity

- [ ] Probed every endpoint and compared returned keys against the client's
      candidate lists (`make probe TOKEN=0x...`)
- [ ] Confirmed `missing_fields` is populated for anything unavailable
- [ ] Confirmed at least two independent price sources are live before enabling
      LIVE — a single source blocks live buying by design
- [ ] Spot-checked several tokens by hand against the explorer

## 7. Before flipping `RUN_MODE=LIVE`

- [ ] Ran `ALERT_ONLY` for **at least 2 weeks** and reviewed the alerts yourself
- [ ] Ran `PAPER` with `OKX_SIMULATED=true` for at least 1 week
- [ ] Reviewed every PAPER_BUY: would you have taken that trade manually?
- [ ] Compared paper `realized_slippage_bps` against real fills on demo trading
- [ ] Confirmed no token reached PAPER_BUY that you would call obviously bad
- [ ] Checked the false-positive rate is tolerable; tightened thresholds if not
- [ ] `OKX_SIMULATED=true` **still set** for the first live day
      (demo trading with `RUN_MODE=LIVE` exercises the full path safely)
- [ ] Only then: `OKX_SIMULATED=false`, with the smallest possible `POSITION_USD`
- [ ] A calendar reminder to review the order ledger daily for the first week

## 8. Operational hygiene

- [ ] `data/screener.db` is backed up (it is your entire audit trail)
- [ ] Log rotation configured — alerts and logs grow without bound
- [ ] Disk space monitored (SQLite fails badly when the disk fills)
- [ ] You know how to read `/orders` and reconcile it against OKX's own history
- [ ] Someone other than you can find and hit the kill switch

---

## Red lines

Do not:

- raise `MAX_EXPOSURE_DAILY_USD` because the bot "seems to be working"
- disable a gate to make a specific token pass — if you want it, buy it manually
- run with withdrawal permission enabled, ever
- expose the API publicly
- trust a score for a token whose alert shows missing fields
- treat "no contract flags detected" as "audited and safe"
