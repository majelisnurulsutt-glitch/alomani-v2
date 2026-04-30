import os
import requests
import time
import logging
import schedule
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────
BOT_TOKEN           = os.environ.get("BOT_TOKEN")
CHAT_ID             = os.environ.get("CHAT_ID")
AGENTROUTER_API_KEY = os.environ.get("AGENTROUTER_API_KEY")

# ─── Filter Constants ─────────────────────────────────────────────────────────
MIN_GAIN_24H       = float(os.environ.get("MIN_GAIN_24H",    50.0))
MIN_VOLUME_24H     = float(os.environ.get("MIN_VOLUME_24H",  50000.0))
MIN_LIQUIDITY      = float(os.environ.get("MIN_LIQUIDITY",   10000.0))
MIN_MARKET_CAP     = float(os.environ.get("MIN_MARKET_CAP",  100000.0))
MAX_MARKET_CAP     = float(os.environ.get("MAX_MARKET_CAP",  50000000.0))
MIN_TOKEN_AGE_DAYS = int(os.environ.get("MIN_TOKEN_AGE_DAYS", 1))
MIN_TX_COUNT_24H   = int(os.environ.get("MIN_TX_COUNT_24H",   200))
SCAN_INTERVAL_MIN  = int(os.environ.get("SCAN_INTERVAL_MIN",  30))

# ─── Deduplication Cache ──────────────────────────────────────────────────────
SENT_CACHE_FILE = "sent_tokens_cache.json"

def load_sent_cache() -> set:
    if os.path.exists(SENT_CACHE_FILE):
        try:
            with open(SENT_CACHE_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_sent_cache(cache: set):
    try:
        with open(SENT_CACHE_FILE, "w") as f:
            json.dump(list(cache), f)
    except Exception as e:
        logger.warning(f"Failed to save sent cache: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — DEXSCREENER (Micin Watch)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_boosts() -> list:
    url = "https://api.dexscreener.com/token-boosts/top/v1"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            logger.info(f"Dexscreener: fetched {len(data)} boosted tokens")
            return data
        logger.warning(f"Dexscreener status {r.status_code}")
    except Exception as e:
        logger.error(f"Error fetching boosts: {e}")
    return []

def fetch_token_pairs(token_address: str) -> list:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.json().get("pairs", [])
    except Exception as e:
        logger.warning(f"Error fetching pairs for {token_address}: {e}")
    return []

def get_token_age_days(pair_created_at) -> int:
    if not pair_created_at:
        return 0
    created_time = datetime.fromtimestamp(pair_created_at / 1000)
    return (datetime.now() - created_time).days

def process_dex_token(boost: dict) -> dict | None:
    token_address = boost.get("tokenAddress")
    if not token_address:
        return None

    pairs = fetch_token_pairs(token_address)
    if not pairs:
        logger.info(f"SKIP {token_address[:10]}... -> pairs kosong")
        return None

    best_pair  = max(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0)
    price_usd  = float(best_pair.get("priceUsd", 0) or 0)
    gain_24h   = float(best_pair.get("priceChange", {}).get("h24", 0) or 0)
    gain_1h    = float(best_pair.get("priceChange", {}).get("h1", 0) or 0)
    volume_24h = float(best_pair.get("volume", {}).get("h24", 0) or 0)
    liquidity  = float(best_pair.get("liquidity", {}).get("usd", 0) or 0)
    market_cap = float(best_pair.get("marketCap", 0) or best_pair.get("fdv", 0) or 0)
    age_days   = get_token_age_days(best_pair.get("pairCreatedAt"))
    tx_count   = (int(best_pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0) +
                  int(best_pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0))

    logger.info(
        f"DEX {token_address[:10]}... | gain24h={gain_24h}% | gain1h={gain_1h}% | "
        f"vol={volume_24h:.0f} | liq={liquidity:.0f} | mcap={market_cap:.0f} | "
        f"price={price_usd} | age={age_days}d | tx={tx_count}"
    )

    reasons = []
    if gain_24h < MIN_GAIN_24H:       reasons.append(f"gain_24h={gain_24h:.1f}% < {MIN_GAIN_24H}")
    if gain_1h <= 0:                  reasons.append(f"gain_1h={gain_1h:.1f}% not positive")
    if volume_24h < MIN_VOLUME_24H:   reasons.append(f"volume={volume_24h:.0f} < {MIN_VOLUME_24H}")
    if liquidity < MIN_LIQUIDITY:     reasons.append(f"liquidity={liquidity:.0f} < {MIN_LIQUIDITY}")
    if not (MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP):
                                      reasons.append(f"mcap={market_cap:.0f} out of range")
    if age_days < MIN_TOKEN_AGE_DAYS: reasons.append(f"age={age_days}d < {MIN_TOKEN_AGE_DAYS}d")
    if tx_count < MIN_TX_COUNT_24H:   reasons.append(f"tx_count={tx_count} < {MIN_TX_COUNT_24H}")

    if reasons:
        logger.info(f"SKIP {token_address[:10]}... -> {' | '.join(reasons)}")
        return None

    symbol = best_pair.get("baseToken", {}).get("symbol", "?")
    logger.info(f"PASS DEX {symbol} | gain={gain_24h}%")

    return {
        "address":   token_address,
        "symbol":    symbol,
        "name":      best_pair.get("baseToken", {}).get("name", "?"),
        "chain":     best_pair.get("chainId", "").upper(),
        "gain_24h":  gain_24h,
        "gain_1h":   gain_1h,
        "volume":    volume_24h,
        "liquidity": liquidity,
        "mcap":      market_cap,
        "url":       best_pair.get("url", ""),
        "source":    "dex",
    }

def fetch_and_filter_dex(boosts: list, sent_cache: set) -> dict:
    results = {"Mega": [], "Mid": [], "Micro": []}

    seen, unique = set(), []
    for b in boosts:
        addr = b.get("tokenAddress")
        if addr and addr not in seen:
            seen.add(addr)
            unique.append(b)

    logger.info(f"DEX: processing {len(unique)} unique addresses...")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(process_dex_token, b): b for b in unique}
        for future in as_completed(futures):
            result = future.result()
            if result and result["address"] not in sent_cache:
                if result["gain_24h"] >= 300:
                    results["Mega"].append(result)
                elif result["mcap"] >= 1_000_000:
                    results["Mid"].append(result)
                else:
                    results["Micro"].append(result)

    total = sum(len(v) for v in results.values())
    logger.info(f"DEX: {total} token lolos filter")
    return results

# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — GECKOTERMINAL (Non-Micin Gems)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_gecko_trending() -> list:
    url = "https://api.geckoterminal.com/api/v2/networks/trending_pools?include=base_token&page=1"
    try:
        r = requests.get(url, timeout=10, headers={"Accept": "application/json"})
        if r.status_code == 200:
            pools = r.json().get("data", [])
            logger.info(f"GeckoTerminal: fetched {len(pools)} trending pools")
            return pools
        logger.warning(f"GeckoTerminal status {r.status_code}")
    except Exception as e:
        logger.error(f"Error fetching GeckoTerminal: {e}")
    return []

def fetch_and_filter_gecko(pools: list, sent_cache: set) -> list:
    results = []

    for pool in pools:
        try:
            attr = pool.get("attributes", {})
            rel  = pool.get("relationships", {})

            price_change = attr.get("price_change_percentage", {})
            gain_24h   = float(price_change.get("h24", 0) or 0)
            gain_1h    = float(price_change.get("h1", 0) or 0)
            volume_24h = float(attr.get("volume_usd", {}).get("h24", 0) or 0)
            liquidity  = float(attr.get("reserve_in_usd", 0) or 0)
            market_cap = float(attr.get("market_cap_usd", 0) or attr.get("fdv_usd", 0) or 0)
            tx_buys    = int(attr.get("transactions", {}).get("h24", {}).get("buys", 0) or 0)
            tx_sells   = int(attr.get("transactions", {}).get("h24", {}).get("sells", 0) or 0)
            tx_count   = tx_buys + tx_sells

            token_id   = rel.get("base_token", {}).get("data", {}).get("id", pool.get("id", ""))
            network    = rel.get("network", {}).get("data", {}).get("id", "").upper()
            pool_name  = attr.get("name", "?")
            symbol     = pool_name.split("/")[0].strip()
            pool_addr  = attr.get("address", "")
            url        = f"https://www.geckoterminal.com/{network.lower()}/pools/{pool_addr}"

            logger.info(
                f"GECKO {symbol[:12]} | gain24h={gain_24h}% | gain1h={gain_1h}% | "
                f"vol={volume_24h:.0f} | liq={liquidity:.0f} | mcap={market_cap:.0f} | tx={tx_count}"
            )

            reasons = []
            if gain_24h < MIN_GAIN_24H:     reasons.append(f"gain_24h={gain_24h:.1f}%")
            if gain_1h <= 0:                reasons.append("gain_1h not positive")
            if volume_24h < MIN_VOLUME_24H: reasons.append("volume low")
            if liquidity < MIN_LIQUIDITY:   reasons.append("liquidity low")
            if market_cap > 0 and market_cap < MIN_MARKET_CAP:
                                            reasons.append("mcap too low")
            if tx_count < MIN_TX_COUNT_24H: reasons.append("tx low")
            if token_id in sent_cache:      reasons.append("already sent")

            if reasons:
                logger.info(f"SKIP GECKO {symbol} -> {' | '.join(reasons)}")
                continue

            logger.info(f"PASS GECKO {symbol} | gain={gain_24h}%")
            results.append({
                "address":   token_id,
                "symbol":    symbol,
                "name":      pool_name,
                "chain":     network,
                "gain_24h":  gain_24h,
                "gain_1h":   gain_1h,
                "volume":    volume_24h,
                "liquidity": liquidity,
                "mcap":      market_cap,
                "url":       url,
                "source":    "gecko",
            })

        except Exception as e:
            logger.warning(f"Error parsing gecko pool: {e}")

    logger.info(f"GeckoTerminal: {len(results)} token lolos filter")
    return results

# ═══════════════════════════════════════════════════════════════════════════════
# AI NARRATIVE
# ═══════════════════════════════════════════════════════════════════════════════

def generate_narrative(token: dict) -> tuple:
    """
    Return (narasi, kategori).
    Micin (dex): kategori = None.
    Non-micin (gecko): kategori = string misal "AI", "RWA", dll.
    Return (None, None) jika SKIP.
    """
    if not AGENTROUTER_API_KEY:
        return "Konteks tidak tersedia (API Key missing).", None

    is_gecko = token.get("source") == "gecko"

    if is_gecko:
        prompt = f"""Kamu adalah analis crypto. Analisis token berikut yang sedang trending secara organik:

Token: {token['symbol']} ({token['name']})
Chain: {token['chain']}
Kenaikan: +{token['gain_24h']:.1f}% dalam 24 jam
Volume: ${token['volume']:,.0f}
Market Cap: ${token['mcap']:,.0f}

Tugasmu:
1. Gunakan web search untuk mencari konteks di balik kenaikan token ini
2. Tentukan apakah ini token micin/scam. Jika YA, balas HANYA: SKIP
3. Jika bukan micin, tentukan KATEGORI token ini. Contoh: AI, RWA, DeFi, GameFi, Layer2, Infrastructure, Meme Established, SocialFi, atau kategori lain yang paling sesuai
4. Tulis narasi 2-3 kalimat Bahasa Indonesia yang menjelaskan konteks kenaikannya

Format jawaban WAJIB (jika tidak SKIP):
KATEGORI: [isi kategori]
NARASI: [isi narasi]

Jangan berikan saran investasi."""

    else:
        prompt = f"""Kamu adalah analis crypto yang mencari ALASAN di balik pergerakan harga token.

Token: {token['symbol']} ({token['name']})
Chain: {token['chain']}
Kenaikan: +{token['gain_24h']:.1f}% dalam 24 jam
Volume: ${token['volume']:,.0f}
Market Cap: ${token['mcap']:,.0f}

Tugasmu:
1. Gunakan web search untuk mencari berita, tren, atau event terkini yang berkaitan dengan token ini
2. Jika ada korelasi yang masuk akal antara berita/tren dengan kenaikan harga, tulis narasi 2-3 kalimat Bahasa Indonesia
3. Jika tidak ada konteks nyata atau tampak manipulasi, balas HANYA: SKIP

Jangan berikan saran investasi."""

    try:
        response = requests.post(
            "https://agentrouter.org/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {AGENTROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 50000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=45
        )
        if not response.text or not response.text.strip():
            logger.error(f"AgentRouter: response kosong untuk {token['symbol']}")
            return None, None
            
        if response.status_code == 200:
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                text = " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            else:
                text = str(content).strip()

            if not text or text.strip().upper() == "SKIP":
                logger.info(f"AI SKIP: {token['symbol']}")
                return None, None

            if is_gecko and "KATEGORI:" in text and "NARASI:" in text:
                kategori, narasi = "", []
                for line in text.split("\n"):
                    if line.startswith("KATEGORI:"):
                        kategori = line.replace("KATEGORI:", "").strip()
                    elif line.startswith("NARASI:"):
                        narasi.append(line.replace("NARASI:", "").strip())
                    elif narasi:
                        narasi.append(line.strip())
                return " ".join(narasi).strip(), kategori

            return text, None

        logger.error(f"AgentRouter {response.status_code}: {response.text[:200]}")
        logger.error(f"AgentRouter response body: {response.text[:500]}")

    except Exception as e:
        logger.error(f"Error narrative {token['symbol']}: {e}")

    return "Gagal mendapatkan narasi otomatis.", None

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM REPORT
# ═══════════════════════════════════════════════════════════════════════════════

TIER_EMOJI = {"Mega": "🔥", "Mid": "⚡", "Micro": "🌱"}

def send_telegram_report(dex_categorized: dict, gecko_tokens: list, sent_cache: set):
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Telegram BOT_TOKEN atau CHAT_ID tidak dikonfigurasi.")
        return

    all_tokens = (
        [(t, "dex") for tier in dex_categorized.values() for t in tier] +
        [(t, "gecko") for t in gecko_tokens]
    )

    if not all_tokens:
        logger.info("Tidak ada token untuk dikirim.")
        return

    logger.info(f"Generating narratives untuk {len(all_tokens)} token...")

    narratives = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_map = {executor.submit(generate_narrative, t): t for t, _ in all_tokens}
        for future in as_completed(future_map):
            token = future_map[future]
            narratives[token["address"]] = future.result()

    report = "🚀 *CRYPTO ANOMALI REPORT*\n"
    report += f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} WIB\n\n"
    found_any = False
    new_sent = set()

    # Micin Watch
    micin_block = ""
    for tier, tokens in dex_categorized.items():
        for token in tokens:
            narasi, _ = narratives.get(token["address"], (None, None))
            if narasi is None:
                continue
            found_any = True
            new_sent.add(token["address"])
            micin_block += f"{TIER_EMOJI[tier]} *${token['symbol']}* +{token['gain_24h']:.1f}% `({token['chain']})`\n"
            micin_block += f"💡 *Konteks:* {narasi}\n"
            micin_block += f"📊 Vol: `${token['volume']:,.0f}` | Liq: `${token['liquidity']:,.0f}` | MCap: `${token['mcap']:,.0f}`\n"
            micin_block += f"🔗 [Dexscreener]({token['url']})\n\n"

    if micin_block:
        report += "━━━ 🌱 *MICIN WATCH* ━━━\n" + micin_block

    # Non-Micin Gems
    gem_block = ""
    for token in gecko_tokens:
        narasi, kategori = narratives.get(token["address"], (None, None))
        if narasi is None:
            continue
        found_any = True
        new_sent.add(token["address"])
        gem_block += f"💎 *${token['symbol']}* +{token['gain_24h']:.1f}% `({token['chain']})`\n"
        if kategori:
            gem_block += f"🏷️ *Kategori:* `{kategori}`\n"
        gem_block += f"💡 *Konteks:* {narasi}\n"
        gem_block += f"📊 Vol: `${token['volume']:,.0f}` | Liq: `${token['liquidity']:,.0f}` | MCap: `${token['mcap']:,.0f}`\n"
        gem_block += f"🔗 [GeckoTerminal]({token['url']})\n\n"

    if gem_block:
        report += "━━━ 💎 *NON-MICIN GEMS* ━━━\n" + gem_block

    if not found_any:
        logger.info("Semua token di-skip AI — laporan tidak dikirim.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": report,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }, timeout=15)
        if r.status_code == 200:
            logger.info(f"Laporan dikirim ({len(new_sent)} token)")
            sent_cache.update(new_sent)
            save_sent_cache(sent_cache)
        else:
            logger.error(f"Telegram error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        logger.error(f"Error sending Telegram: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run_scan():
    logger.info("=" * 50)
    logger.info("Starting Crypto Anomali Scan...")

    sent_cache = load_sent_cache()

    # Layer 1 — Dexscreener (Micin Watch)
    boosts = fetch_boosts()
    dex_categorized = fetch_and_filter_dex(boosts, sent_cache) if boosts else {"Mega": [], "Mid": [], "Micro": []}

    # Layer 2 — GeckoTerminal (Non-Micin Gems)
    gecko_pools  = fetch_gecko_trending()
    gecko_tokens = fetch_and_filter_gecko(gecko_pools, sent_cache) if gecko_pools else []

    total_dex = sum(len(v) for v in dex_categorized.values())
    logger.info(f"Siap dikirim — Micin: {total_dex} | Gems: {len(gecko_tokens)}")

    send_telegram_report(dex_categorized, gecko_tokens, sent_cache)

    logger.info("Scan selesai.")
    logger.info("=" * 50)


def main():
    logger.info(f"Crypto Anomali Bot started — scan setiap {SCAN_INTERVAL_MIN} menit")
    run_scan()
    schedule.every(SCAN_INTERVAL_MIN).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
