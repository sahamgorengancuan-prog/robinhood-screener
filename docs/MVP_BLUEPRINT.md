# Blueprint MVP Operasional

Dokumen ini adalah spesifikasi implementasi yang sesuai dengan kode di repo.
Semua keputusan default bersifat fail-closed: kehilangan data penting menghasilkan
`REJECT` atau `WATCH`, tidak pernah dianggap aman.

## 1. Desain terbaik: biaya vs kualitas

| Lapisan | Pilihan MVP | Biaya | Trade-off |
|---|---|---:|---|
| RPC Robinhood | Public RPC untuk uji, Alchemy free untuk operasi | $0 | Public RPC rate-limited; Alchemy perlu key |
| Indexed data | Alchemy Token + Transfers API | $0 pada free tier | Tidak menyediakan global token list/top holders |
| Discovery | OKX `hot-token` + `eth_getLogs` mint + watchlist lokal | $0 dalam kuota | Log mint juga menangkap NFT; ERC-20 reads menyaringnya |
| Market/risk | OKX OnchainOS Basic + Premium free quota | $0 hingga kuota | Premium perlu dimatikan atau dibayar setelah kuota |
| Verification | Blockscout v2 | $0 | Ketersediaan explorer adalah dependency hard gate |
| Oracle | Chainlink via `eth_call` | Gas $0 karena read-only | Hanya aset yang memiliki feed; address/heartbeat harus diisi |
| Storage | SQLite WAL | $0 | Satu writer; pindah Postgres bila ada banyak worker atau >10 juta snapshot |
| Scheduler | APScheduler | $0 | Default cadence 15 menit; bukan distributed scheduler |
| Queue/cache | Tidak ada | $0 | Redis baru perlu bila ada lebih dari satu proses |
| Eksekusi | OKX v5 REST, post-only limit | Fee trading | Lebih aman tetapi sering tidak terisi |
| Host | 1–2 vCPU, 1–2 GB RAM | sekitar $5–10/bulan | Bukan sistem HFT |

Default `900s × 10 token` menghasilkan sekitar 89 ribu Basic dan 86 ribu Premium
OKX calls/bulan (sebelum retry/probe), sehingga sengaja berada di bawah dua
allowance 100 ribu. Naikkan cadence/universe hanya sambil memonitor quota.

Full node sengaja bukan dependency. Dokumentasi Robinhood meminta CPU 8+ core,
RAM 64 GB (128 GB direkomendasikan), dan NVMe beberapa TB serta endpoint L1
execution + beacon. Itu tidak rasional untuk screener 15-menit sampai shared RPC
terbukti tidak cukup.

## 2. Roadmap implementasi

1. **Fondasi — selesai.** Struktur modul, Pydantic settings, `.env`, SQLite WAL,
   HTTP retry/rate limit/cache, FastAPI, APScheduler.
2. **Client resmi — selesai.** Robinhood Node JSON-RPC, Alchemy Token/Transfers,
   OKX OnchainOS, OKX CEX v5, Blockscout, Chainlink.
3. **Discovery — selesai untuk MVP.** Kandidat dari OKX hot-token yang sudah
   server-side prefiltered, mint `Transfer` logs dalam lookback terbatas, dan
   watchlist lokal; dedup berdasarkan contract address.
4. **Normalisasi — selesai.** Satu `NormalizedSnapshot`, provenance per field,
   `None != 0`, median harga, minimum likuiditas, top-holder tanpa pool/burn,
   CEX VWAP slippage, trade-sample wash indicators.
5. **Risk + scoring — selesai.** Hard gates lebih dulu; skor 30/25/20/15/10
   hanya meranking token yang lolos.
6. **State machine — selesai.** `PAPER_BUY` hanya bila seluruh gate lolos;
   on-chain-only, identity OKX ambigu, atau risk metric unresolved tetap `ALERT`.
7. **Paper + live execution — selesai untuk entry.** Small post-only buy, saldo,
   depth, spread, exposure, safe mode, kill switch, TTL cancel, audit ledger.
8. **Alerting + operator UI — selesai.** Console, file, webhook, Telegram,
   FastAPI, Gradio lokal, diagnostics/probe.
9. **Shadow operation — wajib sebelum LIVE.** Minimal dua minggu `ALERT_ONLY`,
   lalu satu minggu `PAPER`/OKX demo. Kalibrasi false-positive dan data gaps.
10. **Sebelum unattended live — belum selesai.** Tambahkan sell/exit policy,
    position/PnL table, simulated on-chain sell/honeypot check, dan DB migration.
11. **Scale hanya bila ada bukti.** Postgres untuk multiple writer; Redis untuk
    multi-process cache; paid RPC ketika rate-limit nyata; WS berbayar hanya bila
    REST 15-menit tidak cukup.

## 3. Arsitektur folder

```text
app/
  clients/          # rh_node, rh_data/Alchemy, OKX market/trade, oracle, explorer
  pipeline/         # discovery, collect, normalize, metrics, gates, score, decision
  execution/        # kill switch, exposure, paper, order safety, live buy
  alerts/           # format + console/file/webhook/Telegram sinks
  ui/               # control panel lokal; tidak dapat mengirim order langsung
  config.py         # seluruh threshold dan secrets mapping
  models.py         # SQLAlchemy schema
  schemas.py        # normalized DTO + gate/score/decision models
  db.py             # SQLite WAL/session
  main.py           # FastAPI
  scheduler.py      # APScheduler
scripts/            # init DB, probe, run once, UI, demo alert
tests/              # gates, score, decision, clients, order safety, integration
docs/               # endpoint matrix, setup, assumptions, security, roadmap
```

## 4. Schema database

| Tabel | Tujuan | Field penting |
|---|---|---|
| `token` | Identitas unik `(chain,address)` | metadata, deploy time, verification, proxy, OKX identity |
| `token_snapshot` | Time series append-only | price/liquidity/volume, holder metrics, wash sample, spread/slippage, momentum, risk flags, `sources`, `missing_fields`, `raw` |
| `evaluation` | Hasil satu screening | 5 component scores, total, state, hard/data flags, gate JSON, reasons |
| `order_record` | Audit paper/live order | evaluation id, clOrdId unik, exchange id, notional, limit, fills, slippage, error |
| `alert` | Dedupe dan delivery log | state, dedupe key, body/payload, sink status |
| `kv_state` | State kecil persisten | kill switch, safe mode, discovery cursor |

Schema aktual ada di `app/models.py`. Untuk database SQLite lama, backup lalu
buat database baru saat field bertambah; sebelum produksi tambahkan Alembic.

### Endpoint FastAPI lokal

| Method/path | Fungsi |
|---|---|
| `GET /health` | mode, kill switch, safe mode, live possibility |
| `GET /config` | effective config dengan secrets disensor |
| `GET /tokens` | latest evaluation, filter state |
| `POST /tokens` | tambah contract ke watchlist; tidak memberi izin trade |
| `GET /tokens/{address}` | latest token/snapshot/evaluation |
| `GET /tokens/{address}/history` | append-only metric history |
| `GET /alerts` / `GET /alerts/{id}/text` | notification ledger |
| `GET /orders` | paper/live order audit ledger |
| `POST /kill-switch/engage` / `release` | manual execution stop control |
| `POST /run-cycle` | jalankan screening; tidak ada endpoint place-order |
| `GET /docs` | OpenAPI UI bawaan FastAPI |

## 5. Screening flow

```python
async def screening_cycle():
    update_safe_mode_from_okx_btc_1h_candle()
    candidates = dedupe(
        okx_hot_token(prefilter=risk_floors)
        + node_recent_mint_transfer_logs()
        + sqlite_watchlist()
    )

    for token in candidates:
        # stage 1: address-based reads; failure leaves None
        raw = await gather(
            node_erc20_and_contract_scan(token),
            blockscout_verification(token),
            okx_basic_price_advanced_holders_trades_liquidity(token),
            optional_dex_price_sources(token),
        )
        # stage 2 occurs after symbol/decimals/pool addresses exist
        raw += await gather(
            okx_contract_aware_cex_identity(token),
            holder_distribution_fallback(token),
        )

        snap = normalize(raw, history)
        persist_append_only(snap)
        gates = evaluate_all_fail_closed(snap)
        score = weighted_score(snap, weights=(30, 25, 20, 15, 10))

        if hard_gate_failed: state = REJECT
        elif data_gate_failed: state = WATCH
        elif score < alert_min: state = REJECT
        elif score < paper_min or not stable: state = ALERT
        elif live_only_gate_failed or okx_identity_not_exact: state = ALERT
        elif mode == "ALERT_ONLY": state = ALERT
        elif mode == "PAPER" or score < live_min: state = PAPER_BUY
        elif kill_or_safe_mode_or_exposure_block: state = ALERT
        else: state = LIVE_BUY

        persist_evaluation_and_alert()
        if state == PAPER_BUY: simulate_post_only_buy()
        if state == LIVE_BUY: await live_buy()
```

Critical missing values—contract verification, bytecode scan, liquidity,
holder count/top1/top10, total supply, and reviewed major-unlock data—are hard
rejects sesuai requirement. Metric yang butuh history, misalnya holder growth,
tetap `WATCH` sampai snapshot pembanding tersedia.

## 6. Auto-buy flow

```python
async def live_buy(token, evaluation):
    require(RUN_MODE == "LIVE")
    require(not kill_switch and not safe_mode)
    require(trade_key_has_credentials)
    require(okx_quote_balance >= POSITION_USD)

    # Cegah membeli asset lain dengan ticker yang sama.
    identity = await okx.asset_currencies(token.symbol)
    require(exactly_one(identity where
        chain contains OKX_ROBINHOOD_CHAIN_HINT and
        ctAddr_suffix == token.contract_suffix))
    require(exact_live_spot_pair(f"{symbol}-{quote}"))

    book = await okx.books(depth=50)
    require(spread(book) <= MAX_SPREAD_BPS)
    require(vwap_buy_slippage(book, POSITION_USD) <= MAX_SLIPPAGE_BPS)
    require(liquidity_not_dropped_more_than_25_percent)
    require(book_mid_agrees_with_screened_price)
    require(exposure_and_daily_order_caps)

    limit = bid * (1 - LIMIT_OFFSET_BPS / 10_000)
    size = round_down(POSITION_USD / limit, lot_size)
    require(size >= min_size)
    require(not kill_switch and not safe_mode)  # recheck immediately before send

    persist_intent_before_network_call()
    ack = await okx.place_order(side="buy", ordType="post_only", tdMode="cash")
    poll_order_status_and_fills()
    cancel_if_unfilled_after(ORDER_TTL_S)
```

ACK dari OKX bukan bukti fill. `order_record` baru dianggap filled setelah
poll status mengembalikan state dan fill quantity.

## 7. Konfigurasi minimum

```dotenv
RUN_MODE=ALERT_ONLY
DATABASE_URL=sqlite:///./data/screener.db
KILL_SWITCH=false
KILL_SWITCH_FILE=./KILL_SWITCH

RH_CHAIN_ID=4663
RH_NODE_RPC_URL=https://rpc.mainnet.chain.robinhood.com
RH_DATA_ENABLED=false
RH_DATA_RPC_URL=
RH_DATA_API_KEY=

OKX_MARKET_ENABLED=true
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
OKX_PROJECT_ID=
OKX_MARKET_PREMIUM_ENABLED=true

OKX_TRADE_API_KEY=
OKX_TRADE_API_SECRET=
OKX_TRADE_API_PASSPHRASE=
OKX_SIMULATED=true
OKX_ROBINHOOD_CHAIN_HINT=Robinhood

CHAINLINK_ENABLED=false
CHAINLINK_FEEDS_JSON={}
CHAINLINK_HEARTBEATS_JSON={}
CHAINLINK_SEQUENCER_FEED=

TOKENOMICS_OVERRIDES_JSON={}
POSITION_USD=25
MAX_EXPOSURE_PER_TOKEN_USD=50
MAX_EXPOSURE_DAILY_USD=200
```

Template lengkap dan threshold konservatif ada di `.env.example`.

## 8. Contoh alert

```text
[ALERT] BASE — 82.6/100 — on-chain only / manual review
Token     : Base Token (BASE)
Contract  : 0x1234...abcd
Liquidity : $1,240,000 | spread: n/a | slippage($25): 11 bps
Volume    : 5m $4,100 | 1h $51,000 | 24h $870,000 | buy 52%
Holders   : 4,812 | top1 4.2% | top10 24.8% | growth +6.1%/24h
Wash      : 500 trades | unique ratio 0.34 | top wallet 6.8% | filtered 0.4%
Age       : 9.4 days | 24h +7.1% | vs 7d base +11.0%
Flags     : none critical
Score     : liquidity 25.1/30 | holders 20.4/25 | volume 17.9/20 |
            tokenomics 12.2/15 | momentum 7.0/10
Decision  : ALERT
Reason    : OKX contract identity unresolved; no auto-buy
Action    : manual review only
```

## 9. Checklist keamanan

- [ ] Start `ALERT_ONLY`; jangan menganggap test hijau sebagai bukti profit.
- [ ] Trading key terpisah, trade-only, tanpa withdraw, IP allowlist, sub-account kecil.
- [ ] Verifikasi chain ID node adalah 4663 dan OKX supported-chain memuat 4663.
- [ ] Probe endpoint dan cek `missing_fields`/provenance pada token nyata.
- [ ] Contract source, bytecode scan, liquidity, holder metrics, dan unlock review tersedia.
- [ ] Dua sumber harga sepakat dalam threshold; Chainlink tidak stale dan sequencer sehat.
- [ ] OKX CEX identity lolos chain + `ctAddr` suffix + exact live instrument.
- [ ] Test tiga kill switch: env, file sentinel, dan API/DB.
- [ ] Pastikan kill switch dicek ulang tepat sebelum network order.
- [ ] Safe mode memakai candle 1H, bukan `open24h` ticker.
- [ ] Exposure/token/day/order-count cap rendah dan OKX balance cukup.
- [ ] Order hanya post-only limit, rounding down, TTL cancel, dan fill dipoll.
- [ ] API/UI tetap loopback atau di belakang authenticating reverse proxy.
- [ ] SQLite/log dibackup dan disk dimonitor.
- [ ] Punya manual exit plan; MVP belum memiliki sell-side automation.

## 10. Asumsi dan batasan

1. OKX holder/trade analytics adalah data pihak ketiga; raw payload dan source
   provenance disimpan agar hasil bisa diaudit.
2. `holdPercent` adalah percentage points sesuai dokumentasi OKX; pool dan burn
   address dikeluarkan. Contract/router lain belum otomatis dikeluarkan.
3. Wash indicators berasal dari maksimum 500 transaksi terbaru, bukan forensic
   clustering 24 jam. Adversary multi-wallet yang sabar dapat lolos.
4. Static selector scan bukan audit dan proxy diblokir dari live. Simulated sell
   pada router belum diimplementasikan.
5. Jadwal unlock tidak generik on-chain. Hanya override manual dengan
   `source_url` yang dipakai; unknown adalah hard reject.
6. Chainlink feed address dan heartbeat tidak di-hardcode; operator mengambil
   nilai terkini dari Chainlink. Stale/negative/sequencer-down menghasilkan no data.
7. DexScreener/GeckoTerminal opsional. Slug kosong menonaktifkan client untuk
   mencegah membaca token dengan address sama di chain lain.
8. Discovery mint-log bounded dapat melewatkan token di luar lookback. OKX
   hot-token dan watchlist menutup sebagian gap, bukan menjamin universe lengkap.
9. Token yang tidak ada atau tidak dapat dibuktikan identik di OKX tetap
   `on-chain only / manual review`; kode tidak memaksa pembelian.
10. Tidak ada backtest bawaan sebelum local snapshots terkumpul, tidak ada
    jaminan profit, dan tidak ada sell-side automation pada MVP ini.

## 11. Run lokal

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m scripts.init_db
python -m scripts.probe_endpoints
python -m scripts.run_once
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Windows: jalankan `START.bat`. Untuk emergency stop gunakan
`EMERGENCY_STOP.bat`; di Unix gunakan `touch KILL_SWITCH`.
