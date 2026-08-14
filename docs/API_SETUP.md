# Setup API: urutan termurah dan paling aman

## Tier 0 — jalankan offline

Salin `.env.example` menjadi `.env`, biarkan `RUN_MODE=ALERT_ONLY`, inisialisasi
DB, lalu jalankan tests dan demo alert. Tidak ada order yang dapat terkirim.

## Tier 1 — Robinhood Node API

Untuk smoke test bisa memakai public endpoint:

```dotenv
RH_CHAIN_ID=4663
RH_NODE_RPC_URL=https://rpc.mainnet.chain.robinhood.com
```

Endpoint itu rate-limited. Untuk operasi, buat app Alchemy free pada Robinhood
mainnet dan ganti URL dengan:

```dotenv
RH_NODE_RPC_URL=https://robinhood-mainnet.g.alchemy.com/v2/YOUR_KEY
```

Node membuka supply/decimals/metadata, bytecode/proxy scan, token age, mint-log
discovery, dan Chainlink reads. Verifikasi `eth_chainId == 0x1237` (4663).

## Tier 2 — indexed Data API Alchemy (opsional)

Gunakan bila membutuhkan metadata, wallet balances, dan transaction history
terindeks. Tidak perlu jika Node + OKX sudah cukup.

```dotenv
RH_DATA_ENABLED=true
RH_DATA_API_KEY=YOUR_KEY
# atau isi full URL berikut dan key boleh tetap diisi untuk Portfolio API
RH_DATA_RPC_URL=https://robinhood-mainnet.g.alchemy.com/v2/YOUR_KEY
ALCHEMY_NETWORK=robinhood-mainnet
```

Client memanggil `alchemy_getTokenMetadata`, `alchemy_getTokenBalances`, dan
`alchemy_getAssetTransfers`. API ini bukan global token discovery dan bukan
global top-holder list.

## Tier 3 — OKX OnchainOS Market

Buat project/key di OKX OnchainOS Developer Portal. Isi empat nilai:

```dotenv
OKX_MARKET_ENABLED=true
OKX_API_KEY=...
OKX_API_SECRET=...
OKX_API_PASSPHRASE=...
OKX_PROJECT_ID=...
OKX_MARKET_PREMIUM_ENABLED=true
OKX_HOT_TOKEN_ENABLED=true
```

Basic endpoints mengisi discovery, trades, pools, token metadata, dan search.
Premium free allowance mengisi price-info, top holder, sniper/bundle/suspicious,
honeypot/risk tags, dan developer rug history. Bila kuota Premium tidak boleh
terpakai, set `OKX_MARKET_PREMIUM_ENABLED=false`; konsekuensinya beberapa gate
tidak dapat lolos dan live buy tetap tertutup.

## Tier 4 — contract verification dan optional price sources

Blockscout sudah aktif tanpa key:

```dotenv
EXPLORER_ENABLED=true
EXPLORER_BASE_URL=https://robinhoodchain.blockscout.com
```

Untuk cross-check harga tambahan, isi slug hanya setelah dipastikan provider
memang mendukung Robinhood. Slug kosong menonaktifkan client:

```dotenv
DEXSCREENER_ENABLED=true
DEXSCREENER_CHAIN_SLUG=
GECKOTERMINAL_ENABLED=true
GECKOTERMINAL_NETWORK=
```

Harga CEX book OKX juga menjadi sumber independen setelah contract identity
berhasil. Chainlink opsional dan wajib menyertakan heartbeat:

```dotenv
CHAINLINK_ENABLED=true
CHAINLINK_FEEDS_JSON={"USDC":"0xFeedProxy"}
CHAINLINK_HEARTBEATS_JSON={"USDC":3600}
CHAINLINK_SEQUENCER_FEED=0xSequencerUptimeFeed
CHAINLINK_SEQUENCER_GRACE_S=3600
```

Ambil alamat/heartbeat saat deploy dari daftar Chainlink terkini; jangan copy
alamat contoh.

## Tier 5 — OKX trading

Gunakan API key berbeda dari Market key. Permissions: **Read + Trade only**,
tanpa Withdraw, IP allowlist, sub-account dengan saldo kecil.

```dotenv
OKX_TRADE_API_KEY=...
OKX_TRADE_API_SECRET=...
OKX_TRADE_API_PASSPHRASE=...
OKX_SIMULATED=true
OKX_QUOTE_CCY=USDT
OKX_ROBINHOOD_CHAIN_HINT=Robinhood
RUN_MODE=PAPER
```

`GET /api/v5/asset/currencies` harus mengembalikan row yang cocok chain label
dan suffix contract; jika tidak, token adalah on-chain-only/manual review.

## Unlock/tokenomics review

Jadwal unlock tidak boleh ditebak. Masukkan hanya hasil review dengan sumber:

```dotenv
TOKENOMICS_OVERRIDES_JSON={"0xcontract":{"next_unlock_at":"2026-10-01T00:00:00Z","unlock_pct":7.5,"source_url":"https://project.example/tokenomics"}}
```

Tanpa reviewed major-unlock schedule, gate unlock hard-reject sesuai policy.

## Probe dan urutan aktivasi

```bash
python -m scripts.probe_endpoints 0xTokenContract
python -m scripts.run_once
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

1. Node + Blockscout + OKX Market, `ALERT_ONLY`, minimal dua minggu.
2. Lengkapi tokenomics review dan periksa provenance/missing fields.
3. `PAPER` + `OKX_SIMULATED=true`, minimal satu minggu.
4. Uji kill switch dan safe mode.
5. Hanya setelah semua stabil: `LIVE`, masih demo dahulu.
6. Real account terakhir, posisi paling kecil.

## Estimasi biaya

| Item | MVP |
|---|---:|
| Public/Alchemy free RPC | $0 sampai kuota |
| OKX Basic + Premium | $0 sampai masing-masing free allowance |
| Blockscout, SQLite, APScheduler | $0 |
| Small VPS | sekitar $5–10/bulan |
| Full node, Redis, Postgres managed, WS paid | bukan core dependency |

Lihat `ENDPOINTS.md` untuk exact endpoint/tier dan `MVP_BLUEPRINT.md` untuk
alur operasional lengkap.
