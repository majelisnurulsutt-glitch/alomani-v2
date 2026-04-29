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

# ─── Configuration from Environment Variables ─────────────────────────────────
BOT_TOKEN           = os.environ.get("BOT_TOKEN")
CHAT_ID             = os.environ.get("CHAT_ID")
AGENTROUTER_API_KEY = os.environ.get("AGENTROUTER_API_KEY")

# ─── Filter Constants (dapat di-override via env) ─────────────────────────────
MIN_GAIN_24H    = float(os.environ.get("MIN_GAIN_24H",    50.0))
MIN_VOLUME_24H  = float(os.environ.get("MIN_VOLUME_24H",  50000.0))
MIN_LIQUIDITY   = float(os.environ.get("MIN_LIQUIDITY",   25000.0))
MIN_MARKET_CAP  = float(os.environ.get("MIN_MARKET_CAP",  500000.0))
MAX_MARKET_CAP  = float(os.environ.get("MAX_MARKET_CAP",  50000000.0))
MIN_PRICE       = float(os.environ.get("MIN_PRICE",       0.000001))
MIN_TOKEN_AGE_DAYS = int(os.environ.get("MIN_TOKEN_AGE_DAYS", 14))
MIN_TX_COUNT_24H   = int(os.environ.get("MIN_TX_COUNT_24H",   500))
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

# ─── API: Dexscreener ─────────────────────────────────────────────────────────
def fetch_boosts() -> list:
    """Fetch top boosted tokens dari Dexscreener."""
    url = "https://api.dexscreener.com/token-boosts/top/v1"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            logger.info(f"Fetched {len(data)} boosted tokens from Dexscreener")
            return data
        else:
            logger.warning(f"Dexscreener returned status {r.status_code}")
    except Exception as e:
        logger.error(f"Error fetching boosts: {e}")
    return []

def fetch_token_pairs(token_address: str) -> list:
    """Fetch semua pairs untuk satu token address."""
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

# ─── Filter & Categorize ──────────────────────────────────────────────────────
def process_single_token(boost: dict) -> dict | None:
    """Proses satu token: fetch pairs, filter, return data jika lolos."""
    token_address = boost.get("tokenAddress")
    if not token_address:
        return None

    pairs = fetch_token_pairs(token_address)
    if not pairs:
        logger.info(f"SKIP {token_address[:10]}… → pairs kosong (API error atau token tidak ditemukan)")
        return None

    # Gunakan pair dengan likuiditas tertinggi
    best_pair = max(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0) or 0)

    price_usd   = float(best_pair.get("priceUsd", 0) or 0)
    gain_24h    = float(best_pair.get("priceChange", {}).get("h24", 0) or 0)
    gain_1h     = float(best_pair.get("priceChange", {}).get("h1", 0) or 0)
    volume_24h  = float(best_pair.get("volume", {}).get("h24", 0) or 0)
    liquidity   = float(best_pair.get("liquidity", {}).get("usd", 0) or 0)
    market_cap  = float(best_pair.get("marketCap", 0) or best_pair.get("fdv", 0) or 0)
    age_days    = get_token_age_days(best_pair.get("pairCreatedAt"))
    tx_count    = int(best_pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0) + \
                  int(best_pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)

    # ── Log data mentah per token ──
    logger.info(
        f"DATA {token_address[:10]}… | gain24h={gain_24h}% | gain1h={gain_1h}% | "
        f"vol={volume_24h:.0f} | liq={liquidity:.0f} | mcap={market_cap:.0f} | "
        f"price={price_usd} | age={age_days}d | tx={tx_count}"
    )

    # ── Apply Filters ──
    reasons_skipped = []
    if gain_24h < MIN_GAIN_24H:
        reasons_skipped.append(f"gain_24h={gain_24h:.1f}% < {MIN_GAIN_24H}")
    if gain_1h <= 0:
        reasons_skipped.append(f"gain_1h={gain_1h:.1f}% not positive")
    if volume_24h < MIN_VOLUME_24H:
        reasons_skipped.append(f"volume={volume_24h:.0f} < {MIN_VOLUME_24H}")
    if liquidity < MIN_LIQUIDITY:
        reasons_skipped.append(f"liquidity={liquidity:.0f} < {MIN_LIQUIDITY}")
    if market_cap < MIN_MARKET_CAP or market_cap > MAX_MARKET_CAP:
        reasons_skipped.append(f"mcap={market_cap:.0f} out of range")
    if price_usd < MIN_PRICE:
        reasons_skipped.append(f"price={price_usd} < {MIN_PRICE}")
    if age_days < MIN_TOKEN_AGE_DAYS:
        reasons_skipped.append(f"age={age_days}d < {MIN_TOKEN_AGE_DAYS}d")
    if tx_count < MIN_TX_COUNT_24H:
        reasons_skipped.append(f"tx_count={tx_count} < {MIN_TX_COUNT_24H}")

    if reasons_skipped:
        logger.info(f"SKIP {token_address[:10]}… → {' | '.join(reasons_skipped)}")
        return None

    symbol = best_pair.get("baseToken", {}).get("symbol", "?")
    logger.info(f"PASS ✅ {symbol} ({token_address[:10]}…) | gain={gain_24h}%")

    return {
        "address":  token_address,
        "symbol":   symbol,
        "name":     best_pair.get("baseToken", {}).get("name", "?"),
        "chain":    best_pair.get("chainId", "").upper(),
        "gain_24h": gain_24h,
        "gain_1h":  gain_1h,
        "volume":   volume_24h,
        "liquidity": liquidity,
        "mcap":     market_cap,
        "url":      best_pair.get("url", ""),
    }

def filter_and_categorize(boosts: list, sent_cache: set) -> dict:
    """Filter semua token secara paralel, kategorikan hasilnya."""
    results = {"Mega": [], "Mid": [], "Micro": []}

    # Deduplicate address dari boost list
    seen = set()
    unique_boosts = []
    for b in boosts:
        addr = b.get("tokenAddress")
        if addr and addr not in seen:
            seen.add(addr)
            unique_boosts.append(b)

    logger.info(f"Processing {len(unique_boosts)} unique token addresses...")

    passed = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(process_single_token, b): b for b in unique_boosts}
        for future in as_completed(futures):
            result = future.result()
            if result and result["address"] not in sent_cache:
                passed.append(result)

    logger.info(f"{len(passed)} token lolos filter (belum pernah dikirim)")

    for token in passed:
        if token["gain_24h"] >= 300:
            results["Mega"].append(token)
        elif token["mcap"] >= 1_000_000:
            results["Mid"].append(token)
        else:
            results["Micro"].append(token)

    return results

# ─── AI Narrative (Claude via AgentRouter + Web Search) ───────────────────────
def generate_narrative(token: dict) -> str | None:
    """
    Minta Claude mencari konteks/berita di balik pump token ini.
    Return None jika tidak ada narasi yang valid (sinyal: SKIP).
    """
    if not AGENTROUTER_API_KEY:
        logger.warning("AGENTROUTER_API_KEY missing — narasi dinonaktifkan")
        return "Konteks tidak tersedia (API Key missing)."

    prompt = f"""Kamu adalah analis crypto yang bertugas mencari ALASAN di balik pergerakan harga token.

Token: {token['symbol']} ({token['name']})
Chain: {token['chain']}
Kenaikan: +{token['gain_24h']:.1f}% dalam 24 jam
Volume: ${token['volume']:,.0f}
Market Cap: ${token['mcap']:,.0f}

Tugasmu:
1. Gunakan web search untuk mencari berita, tren, atau event terkini yang berkaitan dengan ticker "{token['symbol']}" atau nama "{token['name']}"
2. Jika kamu menemukan korelasi yang masuk akal antara berita/tren tersebut dengan kenaikan harga ini, tulis narasi 2-3 kalimat dalam Bahasa Indonesia yang menjelaskan konteksnya.
3. Jika tidak ada narasi yang jelas, atau kenaikan tampak seperti manipulasi/pump tanpa konteks nyata, balas HANYA dengan kata: SKIP

Jangan berikan saran investasi. Fokus pada analisis konteks."""

    try:
        response = requests.post(
            "https://agentrouter.org/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {AGENTROUTER_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "claude-opus-4-6",
                "max_tokens": 300,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=45
        )

        if response.status_code == 200:
            content_blocks = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")

            # Handle jika content berupa list of blocks (tool use response)
            if isinstance(content_blocks, list):
                narrative = " ".join(
                    block.get("text", "") for block in content_blocks
                    if block.get("type") == "text"
                ).strip()
            else:
                narrative = str(content_blocks).strip()

            if not narrative or narrative.upper() == "SKIP":
                logger.info(f"Narasi SKIP untuk {token['symbol']} — tidak ada konteks yang valid")
                return None

            return narrative

        else:
            logger.error(f"AgentRouter error {response.status_code}: {response.text[:200]}")

    except Exception as e:
        logger.error(f"Error generating narrative for {token['symbol']}: {e}")

    return "Gagal mendapatkan narasi otomatis."

# ─── Telegram Report ──────────────────────────────────────────────────────────
TIER_EMOJI = {"Mega": "🔥", "Mid": "⚡", "Micro": "🌱"}

def send_telegram_report(categorized: dict, sent_cache: set):
    if not BOT_TOKEN or not CHAT_ID:
        logger.error("Telegram BOT_TOKEN atau CHAT_ID tidak dikonfigurasi.")
        return

    # Kumpulkan semua token yang perlu narasi
    all_tokens_flat = [
        (tier, token)
        for tier, tokens in categorized.items()
        for token in tokens
    ]

    if not all_tokens_flat:
        logger.info("Tidak ada token lolos filter — laporan tidak dikirim.")
        return

    logger.info(f"Generating narratives for {len(all_tokens_flat)} tokens...")

    # Generate semua narasi secara paralel
    narratives = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_to_token = {
            executor.submit(generate_narrative, token): (tier, token)
            for tier, token in all_tokens_flat
        }
        for future in as_completed(future_to_token):
            tier, token = future_to_token[future]
            narratives[token["address"]] = future.result()

    # Bangun laporan — hanya token yang punya narasi valid
    report = "🚀 *CRYPTO ANOMALI REPORT*\n"
    report += f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')} WIB\n\n"

    found_any = False
    new_sent = set()

    for tier, tokens in categorized.items():
        tier_block = ""
        for token in tokens:
            narrative = narratives.get(token["address"])
            if narrative is None:
                # Token di-skip karena tidak ada narasi valid
                continue

            found_any = True
            new_sent.add(token["address"])

            tier_block += f"{TIER_EMOJI[tier]} *${token['symbol']}* +{token['gain_24h']:.1f}% `({token['chain']})`\n"
            tier_block += f"💡 *Konteks:* {narrative}\n"
            tier_block += f"📊 Vol: `${token['volume']:,.0f}` | Liq: `${token['liquidity']:,.0f}` | MCap: `${token['mcap']:,.0f}`\n"
            tier_block += f"🔗 [Lihat di Dexscreener]({token['url']})\n\n"

        if tier_block:
            report += f"*── {tier.upper()} ANOMALI ──*\n{tier_block}"

    if not found_any:
        logger.info("Semua token di-skip oleh AI (tidak ada narasi valid) — laporan tidak dikirim.")
        return

    # Kirim ke Telegram
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": report,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }, timeout=15)

        if r.status_code == 200:
            logger.info(f"Laporan berhasil dikirim ke Telegram ({len(new_sent)} token)")
            sent_cache.update(new_sent)
            save_sent_cache(sent_cache)
        else:
            logger.error(f"Telegram error {r.status_code}: {r.text[:200]}")

    except Exception as e:
        logger.error(f"Error sending Telegram message: {e}")

# ─── Main Scan Job ────────────────────────────────────────────────────────────
def run_scan():
    logger.info("=" * 50)
    logger.info("Starting Crypto Anomali Scan...")

    sent_cache = load_sent_cache()

    boosts = fetch_boosts()
    if not boosts:
        logger.warning("Tidak ada data boost yang di-fetch. Scan dibatalkan.")
        return
    logger.info(f"Total raw boosts dari Dexscreener: {len(boosts)}")

    categorized = filter_and_categorize(boosts, sent_cache)

    total = sum(len(v) for v in categorized.values())
    logger.info(f"Token lolos filter: {total} (Mega={len(categorized['Mega'])}, Mid={len(categorized['Mid'])}, Micro={len(categorized['Micro'])})")

    send_telegram_report(categorized, sent_cache)

    logger.info("Scan selesai.")
    logger.info("=" * 50)

# ─── Entry Point ──────────────────────────────────────────────────────────────
def main():
    logger.info(f"Crypto Anomali Bot started — scan setiap {SCAN_INTERVAL_MIN} menit")

    # Jalankan sekali langsung saat start
    run_scan()

    # Schedule berikutnya
    schedule.every(SCAN_INTERVAL_MIN).minutes.do(run_scan)

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
