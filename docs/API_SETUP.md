# Panduan API — dari nol sampai pipeline penuh

Dokumen ini menjawab satu pertanyaan: **API apa saja yang harus saya punya, dan
apa yang terbuka setiap kali saya menambah satu.**

Semua yang di Tier 1–3 **gratis**. Yang berbayar hanya Tier 4, dan itu opsional.

> **Baca dulu ini.** Kunci API saja tidak membuat sistem ini "sempurna". Empat
> metrik risiko tidak punya sumber data sama sekali dan **tidak bisa dibeli dari
> provider mana pun** — itu pekerjaan kode, bukan langganan. Bagian
> [§5](#5-yang-tidak-bisa-diselesaikan-dengan-api) menjelaskan mana saja.

---

## 0. Ringkasan: apa yang terbuka di tiap tahap

| Tahap | Yang Anda punya | Yang bisa dilakukan sistem |
|---|---|---|
| **0** | tidak ada apa-apa | UI jalan, Threshold Lab jalan, semua token → WATCH |
| **1** | RPC + **DexScreener + GeckoTerminal** + explorer | harga (2 sumber), likuiditas, volume 5m/1h/24h, **arus beli/jual**, tx count, holder, verifikasi kontrak, umur token → **semua gate hidup**, cross-check harga aktif |
| **2** | + OKX Web3 | sumber harga ketiga, pembanding likuiditas |
| **3** | + OKX Trading | spread, order book, saldo, order → **PAPER_BUY** |
| **4** | + Data API | penemuan token otomatis → **pipeline penuh** |

Tahap 1 sudah membuka **semua gate yang bisa dibuka oleh API**, dan **hanya RPC
yang butuh pendaftaran**. DexScreener dan GeckoTerminal tanpa kunci sama sekali.

Setiap sumber yang gagal membuat metriknya **UNAVAILABLE**, yang mengarahkan
token ke WATCH — bukan crash, dan bukan pembelian. Itu jalur degradasi yang
disengaja.

---

## 1. Tier 1 — wajib, dan **dua di antaranya tanpa API key sama sekali**

### 1a. Robinhood Chain RPC node

**Satu-satunya yang butuh pendaftaran di tier ini.** Tanpa ini tidak ada data
on-chain.

**Dapatkan di:**

| Provider | Catatan |
|---|---|
| **Alchemy** | direkomendasikan di dokumentasi resmi Robinhood Chain; ada tier gratis |
| QuickNode | mendukung jaringan Robinhood termasuk testnet |
| RPC publik | jika tersedia; rawan rate limit |

**Langkah:**
1. Daftar → buat app → pilih jaringan Robinhood Chain → salin HTTPS URL
2. Tab **🚀 Setup** → *RPC URL* → klik **Deteksi** (chain ID dibaca dari node)
3. **Simpan**

**Yang terbuka:** supply, flag kontrak (mint/pause/blacklist/proxy/owner), umur
token dari deploy block, rekonstruksi holder dari log `Transfer`.

---

### 1b. DexScreener — **tanpa API key, tanpa daftar** ⭐

Ini sumber data pasar terpenting di proyek ini, dan **gratis total**.

Ia satu-satunya sumber gratis yang melaporkan **jumlah transaksi beli vs jual** —
metrik yang sebelumnya sama sekali tidak punya sumber dan membuat gate arus dana
selalu mati.

**Langkah:**
1. Tidak ada pendaftaran. Tidak ada kunci.
2. Cari tahu *chain slug* DexScreener untuk chain Anda: buka
   `https://dexscreener.com`, klik chain-nya, lihat URL —
   `dexscreener.com/base/0x...` berarti slug-nya `base`.
3. Tab **🚀 Setup** → bagian *1b* → isi **DexScreener chain slug** → **Simpan**
4. Tab **🩺 Koneksi** → isi alamat token → **Test Semua Koneksi**

**Yang terbuka:**

| Metrik | Gate |
|---|---|
| `buys_24h` / `sells_24h` → `buy_ratio_24h` | `buy_sell_balance` (HARD) |
| `tx_count_24h` | `tx_count_min` (HARD) |
| `volume_5m` / `volume_1h` / `volume_24h` | `volume_min`, `volume_organic`, `volume_spike` (HARD) |
| `liquidity_usd` | `liquidity_min` (HARD), `slippage_max` |
| `price_usd` | sumber harga #1 |
| `price_change_24h_pct` | `not_extended` (anti-chase) |

**Peringatan slug.** Slug kosong berarti klien menerima pool dari chain **mana
pun**. Fixture uji di repo ini sengaja berisi pool `ethereum` dengan likuiditas
50 juta dan harga $999 untuk alamat yang sama — kalau filter chain rusak,
screener akan memberi harga token yang sama sekali berbeda. Ada test khusus
untuk itu.

Batas laju: ~300 request/menit. Cadence 5 menit tidak akan mendekatinya.

---

### 1c. GeckoTerminal — **tanpa API key, tanpa daftar** ⭐

**Ini yang membuat LIVE_BUY mungkin sama sekali.**

Rekonsiliasi harga butuh **dua** sumber independen untuk mengambil median dan
mengukur divergensi. Dengan satu sumber, `gate_price_agreement` selalu
mengembalikan LIVE_ONLY — jadi berapa pun skornya, LIVE_BUY tidak tercapai.

**Langkah:**
1. Tidak ada pendaftaran. Tidak ada kunci.
2. Cari *network slug*: buka `https://api.geckoterminal.com/api/v2/networks`
   di browser, cari chain Anda, salin `id`-nya. Contoh: `eth`, `base`,
   `arbitrum`. (Perhatikan: slug ini **berbeda** dari slug DexScreener —
   `eth` vs `ethereum`.)
3. Tab **🚀 Setup** → *GeckoTerminal network slug* → **Simpan**

**Yang terbuka:** harga kedua (→ gate agreement lolos), likuiditas pembanding
(diambil **minimum**, jadi hanya bisa membuat sizing lebih konservatif),
`total_supply`, market cap.

Batas laju: ~30 panggilan/menit di tier gratis. Kalau kena 429, turunkan
`MAX_TOKENS_PER_CYCLE`.

---

### 1d. Blockscout explorer — tanpa kunci, sudah aktif default

`EXPLORER_BASE_URL=https://robinhoodchain.blockscout.com`

**Yang terbuka:** `contract_verified` (**kontrak tak terverifikasi = HARD
REJECT**), jumlah holder, daftar top holder.

**Ini satu-satunya sumber jumlah holder.** DexScreener dan GeckoTerminal tidak
melaporkannya.

---

## 2. Tier 2 — OKX Web3 (opsional, gratis)

### OKX Web3 / DEX Market API

**Sekarang opsional**, bukan wajib: DexScreener dan GeckoTerminal sudah menutupi
harga, likuiditas, volume, dan arus transaksi. OKX Web3 berguna sebagai sumber
harga **ketiga** dan pembanding likuiditas.

**Dapatkan di:** OKX → Developer Portal (Web3) → buat project → buat API key.
Anda akan mendapat **empat** nilai:

```
OKX_API_KEY          OK-ACCESS-KEY
OKX_API_SECRET       untuk tanda tangan HMAC
OKX_API_PASSPHRASE   OK-ACCESS-PASSPHRASE
OKX_PROJECT_ID       OK-ACCESS-PROJECT   <-- khusus Web3, sering terlewat
```

**Isi di:** tab **🚀 Setup** → bagian *2. OKX — data pasar*.

**Yang terbuka:**

| Metrik | Gate |
|---|---|
| `price_usd` | `price_agreement` |
| `liquidity_usd` | `liquidity_min` (HARD), `slippage_max` |
| `volume_5m/1h/24h` | `volume_min`, `volume_organic`, `volume_spike` (semua HARD) |
| `tx_count_24h` | `tx_count_min` (HARD) |
| `market_cap`, `fdv`, `circulating_supply` | `tokenomics` |
| `unique_holders` | `holders_min` |
| `price_change_1h/24h` | `not_extended` (anti-chase, HARD) |

**Verifikasi:** tab **🩺 Koneksi & API Test**, isi alamat token, klik **Test
Semua Koneksi**. Panel akan menampilkan **nama field yang benar-benar
dikembalikan OKX**, mana yang **parsed** dan mana yang **unparsed**.

> Kontrak respons OKX Web3 API **belum bisa saya verifikasi** saat membangun
> repo ini (host-nya diblokir dari lingkungan build). Kalau ada field di kolom
> `UNPARSED`, tambahkan nama field itu ke daftar kandidat di
> `OKXMarketClient.extract_market()` — lokasinya ditunjukkan di panel. Field yang
> tidak terparse tetap `None` dan mengirim token ke WATCH; ia **tidak pernah**
> menjadi `0`.

**Catatan:** OKX baru dipanggil kalau `RH_CHAIN_ID` sudah terisi. Ini disengaja
— tanpa chain ID yang pasti, pencarian bisa mencocokkan token bernama sama di
chain lain, dan itu bug paling mahal yang mungkin terjadi di sini.

---

## 3. Tier 3 — eksekusi (gratis)

### OKX Trading API (spot v5)

**Pakai API key yang BERBEDA dari Tier 2.**

**Aturan yang tidak bisa ditawar:**

- izin **trade saja** — **jangan pernah** aktifkan withdraw
- **kunci ke IP** server Anda
- pakai **sub-account** yang hanya berisi modal yang Anda ikhlas hilang
- mulai dengan `OKX_SIMULATED=true` (demo trading)

**Isi di:** tab **🚀 Setup** → bagian *3. OKX — trading*.

**Yang terbuka:**

| Kemampuan | Gate |
|---|---|
| `find_spot_instrument` — token ada di OKX? | keputusan `okx_available` |
| order book → `spread_bps` | `spread_max` |
| saldo, penempatan order post-only | PAPER_BUY / LIVE_BUY |

Token yang **tidak ada di OKX** tidak pernah dipaksa dibeli — ia ditandai
"on-chain only / manual review" dan berhenti di PAPER_BUY.

---

## 4. Tier 4 — pipeline penuh

### 4a. Robinhood Chain Data API — untuk penemuan token otomatis

**Ini yang mengubah screener dari "saya tambah kontrak manual" menjadi pipeline
yang menemukan token sendiri.**

Tanpa ini sistem tetap jalan, tapi Anda harus menambahkan kontrak satu per satu
di tab Screener.

**Pilihan penyedia:**

| Penyedia | Model | Catatan |
|---|---|---|
| **Hoodstack** | API key, ada tier gratis | infrastruktur khusus Robinhood Chain: buat project, mint API key, baca state chain |
| **SQD** | open-source SDK + Portal API | arsip blok/transaksi/log tervalidasi |
| **Bitquery** | GraphQL + WebSocket | terindeks, ada tier gratis terbatas |
| **QuickNode** | RPC + API tambahan | bisa sekaligus jadi Tier 1 |

**Isi di:** tab **🚀 Setup** → *Data API aktif?* = `true` + base URL.
(`.env`: `RH_DATA_ENABLED`, `RH_DATA_BASE_URL`, `RH_DATA_API_KEY`)

**Penting — jangan aktifkan lalu percaya begitu saja.** Kontrak respons Data API
juga **belum terverifikasi** di repo ini. Path-nya bisa diatur dari `.env`:

```
RH_DATA_PATH_TOKEN_LIST=/tokens
RH_DATA_PATH_TOKEN_META=/tokens/{address}
RH_DATA_PATH_TOKEN_HOLDERS=/tokens/{address}/holders
RH_DATA_PATH_TOKEN_TRANSFERS=/tokens/{address}/transfers
```

Aktifkan → jalankan **Test Semua Koneksi** → bandingkan `observed_keys` dengan
kandidat di `app/clients/rh_data.py` → sesuaikan bila perlu.

**Yang terbuka:** penemuan token otomatis, daftar holder terindeks (jauh lebih
murah daripada merekonstruksi dari `eth_getLogs`).

### 4b. Sumber harga kedua — **syarat mutlak LIVE_BUY**

Ini yang paling sering terlewat, jadi saya tegaskan:

> **Saat ini hanya ada satu sumber harga yang tersambung (OKX). Dengan satu
> sumber, `gate_price_agreement` selalu mengembalikan LIVE_ONLY, sehingga
> LIVE_BUY tidak akan pernah tercapai — berapa pun skornya.**

Rekonsiliasi harga butuh **minimal dua** sumber independen supaya bisa
mengambil median dan mengukur divergensi. Kalau keduanya tidak sepakat dalam 5%,
token ditolak HARD (feed basi dan pool yang dimanipulasi terlihat sama dari
sini, dan keduanya alasan untuk tidak membeli).

Dua cara memenuhinya:

1. **Chainlink feed** (paling mudah, tanpa biaya) — kalau ada deployment
   Chainlink di Robinhood Chain. Isi alamat aggregator:
   ```
   CHAINLINK_ENABLED=true
   CHAINLINK_FEEDS_JSON={"ETH":"0x...","USDC":"0x..."}
   ```
   Berguna terutama untuk memvalidasi **penyebut** harga USD (aset kuotasi),
   bukan harga microcap-nya.

2. **Pembaca harga DEX pool langsung** — ini **pekerjaan kode**, bukan API.
   Kerangkanya sudah ada (`bundle.dex_pool_price` dibaca normalizer) tapi belum
   ada yang mengisinya. Lihat Roadmap fase 4.

### 4c. Notifikasi (gratis, opsional)

| Kanal | Yang dibutuhkan |
|---|---|
| **Telegram** | chat `@BotFather` → `/newbot` → token; chat ID dari `@userinfobot` |
| **Webhook** | URL mana pun yang menerima POST JSON (Discord, Slack, n8n) |

Isi di tab Setup bagian *5. Notifikasi*, lalu klik **Kirim alert uji ke semua
sink** di tab Koneksi — supaya Anda tahu webhook-nya salah **sekarang**, bukan
saat sinyal sungguhan lewat.

---

## 5. Yang TIDAK bisa diselesaikan dengan API

Ini bagian yang harus jujur. Empat gate ini punya kode dan test lengkap, tapi
**tidak ada sumber data yang tersambung**, dan **tidak ada provider yang bisa
Anda beli untuk menyelesaikannya** — semuanya butuh pekerjaan kode di repo ini:

| Metrik | Kenapa tidak bisa dibeli | Yang dibutuhkan |
|---|---|---|
| `sniper_wallet_pct` | perlu analisis pemegang di blok-blok awal | analisis N blok pertama setelah deploy |
| `bundled_buy_pct` | perlu clustering dompet dalam satu blok | clustering same-block multi-wallet |
| `days_to_major_unlock` | **jadwal vesting tidak ada on-chain** | tabel manual, diisi tangan — bukan ditebak |

`buy_ratio_24h` **sudah tidak ada di daftar ini** — DexScreener menutupinya.
Tiga yang tersisa tetap `LIVE_ONLY`.

Semuanya bersifat `LIVE_ONLY`: token yang kehilangan metrik ini **bisa** mencapai
ALERT dan PAPER_BUY, tapi **tidak akan pernah** LIVE_BUY. Itu perilaku yang
benar — sistem memberi tahu apa yang tidak diketahuinya, bukan menilai di
sekitarnya.

Selama fase 4 belum dikerjakan, harapkan LIVE_BUY jarang atau tidak pernah
menyala. Itu bukan bug.

Detail rencananya ada di [`ROADMAP.md`](ROADMAP.md) fase 4, dan batasan
lengkapnya di [`ASSUMPTIONS.md`](ASSUMPTIONS.md) §B.

---

## 6. Urutan pengerjaan yang saya sarankan

### Untuk ALERT_ONLY yang berguna (bisa selesai hari ini)

1. RPC node → tab Setup → **Deteksi** chain ID → Simpan
2. **DexScreener chain slug** → Simpan  *(tanpa kunci)*
3. **GeckoTerminal network slug** → Simpan  *(tanpa kunci)*
4. Biarkan explorer aktif (default)
5. **Test Semua Koneksi** sampai hijau — perhatikan baris **Price cross-check**,
   ia harus melaporkan **2 sumber independen**
6. Tambah beberapa kontrak di tab Screener → **Jalankan 1 siklus**
7. Baca setiap alert. Tanyakan: *apakah saya akan mengambil trade ini secara
   manual?* Kalau tidak, ketatkan threshold di **Threshold Lab**.

**Biaya: $0, dan hanya satu pendaftaran (RPC).** Ini sudah screener yang
berfungsi penuh.

### Untuk pipeline penuh + PAPER

7. Data API (Hoodstack/SQD/Bitquery) → penemuan token otomatis
8. OKX Trading key, `OKX_SIMULATED=true` → `RUN_MODE=PAPER`
9. **Mulai otomatis** di tab Screener, biarkan berjalan
10. Bandingkan `realized_slippage_bps` di ledger order dengan asumsi model paper
    (model fill-nya sengaja optimis — lihat ASSUMPTIONS §C)

### Sebelum LIVE — dan hanya setelah semua di atas stabil berminggu-minggu

11. Sumber harga kedua (Chainlink atau pembaca DEX pool) — **tanpa ini LIVE_BUY
    mustahil**
12. Kerjakan fase 4 supaya empat gate di §5 punya data
13. Kerjakan fase 5 — **saat ini tidak ada logika jual sama sekali**; sistem bisa
    masuk posisi dan tidak bisa keluar
14. Selesaikan [`SECURITY_CHECKLIST.md`](SECURITY_CHECKLIST.md)
15. `RUN_MODE=LIVE` dengan `OKX_SIMULATED=true` dulu selama sehari
16. Baru `OKX_SIMULATED=false`, dengan `POSITION_USD` sekecil mungkin

---

## 7. Ringkasan variabel `.env`

Semua bisa diisi lewat tab **🚀 Setup**; ini rujukan kalau Anda mengedit file.

```bash
# Tier 1 — wajib
RH_NODE_RPC_URL=            # dari Alchemy/QuickNode
RH_CHAIN_ID=                # kosongkan, pakai tombol Deteksi
EXPLORER_BASE_URL=https://robinhoodchain.blockscout.com

# Tier 1 — TANPA API KEY, hanya slug chain
DEXSCREENER_ENABLED=true
DEXSCREENER_CHAIN_SLUG=     # dari URL dexscreener.com/<slug>/0x...
GECKOTERMINAL_ENABLED=true
GECKOTERMINAL_NETWORK=      # dari api.geckoterminal.com/api/v2/networks

# Tier 2 — data pasar
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
OKX_PROJECT_ID=             # khusus Web3, sering terlewat

# Tier 3 — eksekusi (key TERPISAH, trade-only, IP allowlist)
OKX_TRADE_API_KEY=
OKX_TRADE_API_SECRET=
OKX_TRADE_API_PASSPHRASE=
OKX_SIMULATED=true

# Tier 4 — pipeline penuh
RH_DATA_ENABLED=false
RH_DATA_BASE_URL=
RH_DATA_API_KEY=
CHAINLINK_ENABLED=false
CHAINLINK_FEEDS_JSON={}

# Notifikasi
ALERT_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## 8. Biaya bulanan

| Komponen | Biaya |
|---|---|
| RPC (tier gratis) | $0 — naik ke ~$50–200 hanya jika kena rate limit |
| Blockscout | $0 |
| OKX market + trading API | $0 |
| Data API (tier gratis Hoodstack/SQD/Bitquery) | $0 — berbayar hanya kalau volume query besar |
| Chainlink (baca on-chain) | $0 |
| SQLite + hosting lokal | $0 |
| **Total realistis** | **$0**, atau ~$5/bulan kalau di VPS |

Full node sendiri **tidak** dibutuhkan: ~$150–400/bulan untuk latensi ~200ms
yang strategi base-building ini tidak bisa manfaatkan, ditambah risiko node Anda
diam-diam tertinggal dan menyuapi data basi. Perhitungannya ada di
[`ENDPOINTS.md`](ENDPOINTS.md).
