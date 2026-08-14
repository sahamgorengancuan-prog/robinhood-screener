# Assumptions & Limitations

## Yang dapat diverifikasi

- Robinhood mainnet chain ID adalah 4663; Alchemy direkomendasikan, public RPC
  tersedia tetapi rate-limited.
- OKX OnchainOS menyatakan Robinhood `chainIndex=4663` mendukung Market dan Trade.
- Exact OKX endpoints/fields/tier yang dipakai tercatat di `ENDPOINTS.md`.
- Alchemy Token API bersifat wallet-centric; repo tidak mengarang global token
  list atau top-holder endpoint.

## Estimasi dan sample

| Metric | Basis | Limitasi |
|---|---|---|
| DEX slippage fallback | aggregate liquidity + x*y=k | bukan concentrated-liquidity route; LIVE memakai CEX book VWAP |
| CEX slippage | hingga 50 ask levels | depth dapat berubah setelah read; dipanggil ulang preflight |
| top1/top10 | OKX `holdPercent` atau balance/totalSupply | pool/burn dikeluarkan; contract/router lain mungkin masih dihitung |
| unique holders | OKX/Blockscout count | address bukan manusia; sybil tetap mungkin |
| holder growth | dua snapshot, annualized ke 24h | butuh history; snapshot awal tetap WATCH |
| wash sample | maksimal 500 recent OKX trades | bukan forensic 24h; multi-wallet adversary dapat lolos |
| token age | first block dengan bytecode | binary search RPC mahal untuk token baru |
| 7d base | median local snapshots | tidak tersedia sebelum history terkumpul |

## Contract dan tokenomics

- Static selector scan bukan audit. Ia mendeteksi surface yang jelas, bukan
  sell honeypot kondisional. OKX honeypot/risk tag menambah defense, bukan bukti aman.
- Proxy/mutable surface memblokir live karena implementation dapat berubah.
- Source verification dan bytecode scan yang unavailable adalah hard reject.
- Unlock schedule tidak generik on-chain. Hanya manual override dengan
  `source_url` yang digunakan; unknown adalah hard reject.
- Tidak ada sell-side automation. Entry live tetap tidak layak unattended
  sebelum exit policy dan position/PnL lifecycle dibuat.

## Harga dan rekonsiliasi

1. Harga = median sumber yang tersedia.
2. Divergence = jarak relatif maksimum dari median.
3. Divergence di atas `PRICE_MAX_SOURCE_DIVERGENCE` = hard reject.
4. Satu sumber = `LIVE_ONLY`, sehingga tidak ada live buy.
5. CEX book mid hanya dipakai setelah contract identity OKX terbukti.
6. Chainlink wajib answer positif, decimals dibaca, heartbeat tidak stale, dan
   optional sequencer feed sehat setelah grace period.
7. Likuiditas lintas sumber memakai minimum; supply memakai on-chain totalSupply.

## Venue identity

Symbol tidak unik. `ABC-USDT` dapat merepresentasikan asset lain. LIVE membutuhkan:

1. `GET /api/v5/asset/currencies?ccy=ABC` berhasil dengan authenticated key;
2. tepat satu row chain label Robinhood;
3. documented masked `ctAddr` suffix cocok contract;
4. `GET /api/v5/public/instruments` menunjukkan exact pair dalam state `live`.

Jika salah satu tidak terbukti, state tetap `ALERT: on-chain only/manual review`.

## Infrastruktur

- SQLite cocok untuk satu scheduler writer. Multi-writer memerlukan Postgres dan migrations.
- APScheduler cocok cadence menit. Tidak ada alasan Redis/Celery untuk satu proses.
- Public/shared RPC cukup untuk MVP. Full node resmi membutuhkan 8+ CPU, 64 GB
  RAM (128 GB recommended), NVMe beberapa TB, dan dua L1 endpoints.
- OKX Premium masih memiliki free allowance, tetapi quota/pricing dapat berubah;
  runtime/operator harus memantau usage.
- Optional DexScreener/GeckoTerminal bukan official-first dependency dan wajib
  explicit chain slug; blank berarti disabled.

## Risiko strategi

- Score adalah ranking, bukan probabilitas terkalibrasi dan bukan prediksi return.
- Tidak ada backtest sebelum local snapshot cukup panjang.
- Base-building heuristics dapat melewatkan winner dan tetap menerima loser.
- Sistem tidak menjamin profit, rug-pull immunity, fill, atau exit liquidity.
