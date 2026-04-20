import os
import sys
import sqlite3
import time
import logging
import threading
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import pandas as pd
import matplotlib
matplotlib.use('Agg')  # Без GUI для сервера
import matplotlib.pyplot as plt
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# ================= НАСТРОЙКИ =================
TON_API_KEY = os.getenv("TON_API_KEY")
if not TON_API_KEY:
    raise ValueError("❌ TON_API_KEY не найден. Создайте файл .env или передайте переменную окружения.")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.2"))

DATA_DIR = "/app/data"
DB_PATH = os.path.join(DATA_DIR, "gifts_cache.db")
OUTPUT_CSV = os.path.join(DATA_DIR, "telegram_gifts_report.csv")
CHART_FILE = os.path.join(DATA_DIR, "gift_price_trend.png")
LOG_FILE = os.path.join(DATA_DIR, "gift_stats.log")

HEADERS_TON = {"Authorization": f"Bearer {TON_API_KEY}", "Accept": "application/json"}
BASE_URL = "https://tonapi.io/v2"
GETGEMS_API = "https://api.getgems.io/graphql"

# Настройка логирования
os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# Глобальный семафор для Rate Limit
API_LOCK = threading.Lock()

# =================================================

def safe_request(url, method="GET", params=None, json_data=None, headers=None):
    """Потокобезопасный HTTP-запрос с повторами и защитой от 429."""
    with API_LOCK:
        time.sleep(REQUEST_DELAY)

    for attempt in range(3):
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=json_data, timeout=20)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
                logger.warning(f"⏳ Rate limit. Ожидание {retry_after}с...")
                with API_LOCK: time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка запроса ({attempt+1}/3): {e}")
            if attempt < 2: time.sleep(2)
    return None

def init_db():
    """Инициализация SQLite кэша."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS collections (
            address TEXT PRIMARY KEY, name TEXT, is_official INTEGER, floor_price_ton REAL
        );
        CREATE TABLE IF NOT EXISTS gifts_sync (
            gift_address TEXT PRIMARY KEY, last_sync_ts REAL, events_count INTEGER
        );
        CREATE TABLE IF NOT EXISTS price_events (
            gift_address TEXT, event_type TEXT, timestamp REAL, price_ton REAL, price_usd REAL,
            UNIQUE(gift_address, timestamp, event_type)
        );
    """)
    conn.commit()
    return conn

def fetch_official_collections():
    """Поиск официальных коллекций Telegram."""
    query = {
        "query": """
        query { 
          collections(first: 100, search: "Gift") { 
            items { address name stats { floorPrice } } 
          } 
        }
        """
    }
    data = safe_request(GETGEMS_API, method="POST", headers={"Content-Type": "application/json"}, json_data=query)
    
    collections = []
    if not data or "data" not in data or "collections" not in data["data"]:
        logger.warning("⚠️ GetGems API недоступен. Пропускаем автопоиск.")
        return collections

    for item in data["data"]["collections"]["items"]:
        addr = item["address"]
        name = item.get("name", "")
        floor = float(item.get("stats", {}).get("floorPrice", 0) or 0)
        
        meta = safe_request(f"{BASE_URL}/collections/{addr}", headers=HEADERS_TON)
        approved = meta.get("approved_by", []) if meta else []
        
        is_official = bool(approved) or any(kw in name.lower() for kw in ["telegram", "premium", "official", "seasonal"])
        if is_official:
            collections.append({"address": addr, "name": name, "floor": floor})
            
    logger.info(f"🔍 Найдено официальных коллекций: {len(collections)}")
    return collections

def sync_gift_history(gift_addr, ton_usd):
    """Загрузка истории одного подарка с кэшированием."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    cur = conn.cursor()
    
    cur.execute("SELECT events_count FROM gifts_sync WHERE gift_address=?", (gift_addr,))
    if cur.fetchone():
        conn.close()
        return 0

    events = []
    offset = 0
    while True:
        data = safe_request(f"{BASE_URL}/nfts/{gift_addr}/history", 
                            headers=HEADERS_TON, params={"limit": 100, "offset": offset})
        if not data or "actions" not in data: break
        actions = data["actions"]
        events.extend(actions)
        if len(actions) < 100: break
        offset += 100

    if not events:
        cur.execute("INSERT OR IGNORE INTO gifts_sync VALUES (?, ?, 0)", (gift_addr, time.time()))
        conn.commit()
        conn.close()
        return 0

    processed = []
    for evt in events:
        price_val = evt.get("price", {}).get("value")
        price_ton = float(price_val) / 1e9 if price_val and price_val != "0" else 0.0
        if price_ton > 0:
            ts = evt.get("timestamp", 0)
            processed.append((gift_addr, evt.get("type", "unknown"), ts, price_ton, round(price_ton * ton_usd, 4)))

    if processed:
        cur.executemany("INSERT OR IGNORE INTO price_events VALUES (?, ?, ?, ?, ?)", processed)
        cur.execute("INSERT OR REPLACE INTO gifts_sync VALUES (?, ?, ?)", (gift_addr, time.time(), len(processed)))
        conn.commit()
    conn.close()
    return len(processed)

def main():
    logger.info("🚀 Запуск сборщика статистики Telegram Gifts...")
    
    rate_data = safe_request("https://api.coingecko.com/api/v3/simple/price", 
                             params={"ids": "the-open-network", "vs_currencies": "usd"})
    ton_usd = rate_data.get("the-open-network", {}).get("usd", 1.0)
    logger.info(f"💱 Текущий курс TON: ${ton_usd:.2f}")

    conn = init_db()
    collections = fetch_official_collections()
    cur = conn.cursor()
    cur.executemany("INSERT OR REPLACE INTO collections VALUES (?,?,?,?)",
                   [(c["address"], c["name"], 1, c["floor"]) for c in collections])
    conn.commit()

    all_gifts = []
    for col in collections:
        logger.info(f"📦 Сканирование: {col['name']}")
        offset = 0
        while True:
            data = safe_request(f"{BASE_URL}/collections/{col['address']}/items", 
                                headers=HEADERS_TON, params={"limit": 100, "offset": offset})
            if not data or "nft_items" not in data: break
            for it in data["nft_items"]:
                all_gifts.append(it["address"]["address"])
            if len(data["nft_items"]) < 100: break
            offset += 100

    logger.info(f"📊 Всего подарков для проверки: {len(all_gifts)}")

    new_events = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(sync_gift_history, addr, ton_usd): addr for addr in all_gifts}
        for future in as_completed(futures):
            try:
                count = future.result()
                new_events += count
            except Exception as e:
                logger.error(f"❌ Ошибка в потоке: {e}")

    logger.info(f"📥 Загружено новых ценовых событий: {new_events}")

    df = pd.read_sql_query("SELECT * FROM price_events ORDER BY timestamp", conn)
    conn.close()
    
    if df.empty:
        logger.warning("⚠️ Нет данных для отчёта. Возможно, подарки не продавались или кэш уже полон.")
        return

    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"✅ Отчёт сохранён: {OUTPUT_CSV} ({len(df)} записей)")

    daily_avg = df.groupby(df["datetime"].dt.date)["price_ton"].mean().reset_index()
    daily_avg.columns = ["date", "avg_price"]
    
    plt.figure(figsize=(11, 5))
    plt.plot(daily_avg["date"], daily_avg["avg_price"], marker="o", linestyle="-", color="#0088cc", linewidth=2)
    plt.title("Средняя цена официальных подарков Telegram (TON)", fontsize=14)
    plt.ylabel("Цена (TON)", fontsize=12)
    plt.xlabel("Дата", fontsize=12)
    plt.grid(True, alpha=0.3)
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(CHART_FILE, dpi=150)
    plt.close()
    logger.info(f"📈 График сохранён: {CHART_FILE}")
    logger.info("🏁 Сбор данных завершён!")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)

