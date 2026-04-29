# Crypto Anomali Bot v2

Bot monitoring crypto yang mengirimkan laporan token dengan **anomali harga + konteks berita nyata** ke Telegram. Bukan sekadar scanner angka — setiap token yang masuk laporan sudah divalidasi oleh Claude AI bahwa ada alasan di balik kenaikannya.

## Alur Kerja

```
Dexscreener (boosted tokens)
        │
        ▼
Filter Ketat: Gain >50%, Vol >$50K, Liq >$10K,
              Harga >$0.01, MCap $100K–$50M,
              Gain 1h positif, Usia >7 hari, TX >200/24h
        │
        ▼
Lolos? → Claude AI + Web Search cari konteks/berita
        │
        ├── Ada narasi valid? → Masuk laporan Telegram ✅
        └── Tidak ada konteks? → SKIP (pump tanpa alasan) ❌
```

## Fitur v2

- ✅ **Web search otomatis** — Claude mencari berita terkini sebelum menulis narasi
- ✅ **AI validation** — token tanpa narasi valid di-skip otomatis
- ✅ **Paralel processing** — fetch token & generate narasi berjalan bersamaan
- ✅ **Deduplication** — token yang sudah dikirim tidak akan muncul lagi
- ✅ **Scheduler aktif** — scan otomatis setiap 30 menit (configurable)
- ✅ **Debug logging** — log detail kenapa setiap token lolos/gagal filter
- ✅ **Filter via env** — semua konstanta bisa diubah tanpa edit kode

## Deploy ke Railway

1. Upload semua file ke repository GitHub
2. Hubungkan ke [Railway](https://railway.app/)
3. Set Environment Variables:

### Wajib
| Variable | Keterangan |
|---|---|
| `BOT_TOKEN` | Token bot Telegram dari @BotFather |
| `CHAT_ID` | ID chat/channel Telegram tujuan |
| `AGENTROUTER_API_KEY` | API Key dari AgentRouter |

### Opsional (ada default)
| Variable | Default | Keterangan |
|---|---|---|
| `MIN_GAIN_24H` | `50.0` | Minimal kenaikan 24h (%) |
| `MIN_VOLUME_24H` | `50000` | Minimal volume 24h (USD) |
| `MIN_LIQUIDITY` | `10000` | Minimal likuiditas (USD) |
| `MIN_MARKET_CAP` | `100000` | Minimal market cap (USD) |
| `MAX_MARKET_CAP` | `50000000` | Maksimal market cap (USD) |
| `MIN_PRICE` | `0.01` | Minimal harga token (USD) |
| `MIN_TOKEN_AGE_DAYS` | `7` | Minimal usia token (hari) |
| `MIN_TX_COUNT_24H` | `200` | Minimal jumlah transaksi 24h |
| `SCAN_INTERVAL_MIN` | `30` | Interval scan (menit) |

## Contoh Output Telegram

```
🚀 CRYPTO ANOMALI REPORT
📅 2026-04-29 22:57 WIB

── MEGA ANOMALI ──
🔥 $EVA +340% (ETHEREUM)
💡 Konteks: Token bertema Evangelion ini mengalami lonjakan signifikan
   bersamaan dengan viralnya perdebatan Elon Musk vs Sam Altman di X,
   yang memicu gelombang pembelian token-token bertema "AI vs Human".
📊 Vol: $2,100,000 | Liq: $450,000 | MCap: $4,200,000
🔗 Lihat di Dexscreener
```

## Disclaimer

Data ini hanya untuk tujuan informasi. Bukan saran investasi. Selalu lakukan riset sendiri (DYOR).
