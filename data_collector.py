"""
Layer 1 - Research/Data Collector.

Runs continuously and builds a growing history of price data and news -
raw material for Layer 3 (the decision agent) to reason over later.

Unlike Layer 2 (anomaly_detector.py), this layer does not judge or alert on
anything - it just records everything, so nothing is missed even if it
turns out to matter only in hindsight.
"""

import ccxt
import csv
import feedparser
import json
import os
import time
from pathlib import Path
from dotenv import load_dotenv

from agent_status import heartbeat
from email_utils import alert_error

load_dotenv()

EXCHANGE = os.environ.get("EXCHANGE", "binance")
SYMBOL = os.environ.get("SYMBOL", "BTC/USDT")
PRICE_LOG_INTERVAL_SECONDS = int(os.environ.get("PRICE_LOG_INTERVAL_SECONDS", 300))
NEWS_LOG_INTERVAL_SECONDS = int(os.environ.get("NEWS_LOG_INTERVAL_SECONDS", 900))

NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
PRICE_LOG = DATA_DIR / "price_history.csv"
NEWS_LOG = DATA_DIR / "news_log.jsonl"
SEEN_NEWS_FILE = DATA_DIR / "seen_news_links.json"


def log_price(exchange):
    ticker = exchange.fetch_ticker(SYMBOL)
    row = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "symbol": SYMBOL,
        "price": ticker["last"],
        "volume_24h": ticker.get("quoteVolume"),
        "high_24h": ticker.get("high"),
        "low_24h": ticker.get("low"),
    }
    is_new_file = not PRICE_LOG.exists()
    with open(PRICE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if is_new_file:
            writer.writeheader()
        writer.writerow(row)


def load_seen_links():
    if SEEN_NEWS_FILE.exists():
        return set(json.loads(SEEN_NEWS_FILE.read_text()))
    return set()


def save_seen_links(seen_links):
    SEEN_NEWS_FILE.write_text(json.dumps(list(seen_links)[-2000:]))


def log_news(seen_links):
    with open(NEWS_LOG, "a") as f:
        for feed_url in NEWS_FEEDS:
            feed = feedparser.parse(feed_url)
            source = feed.feed.get("title", feed_url)
            for entry in feed.entries:
                link = entry.get("link", "")
                if not link or link in seen_links:
                    continue
                record = {
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                    "source": source,
                    "title": entry.get("title", ""),
                    "link": link,
                    "published": entry.get("published", ""),
                }
                f.write(json.dumps(record) + "\n")
                seen_links.add(link)
    return seen_links


if __name__ == "__main__":
    exchange = getattr(ccxt, EXCHANGE)({"options": {"fetchMarkets": ["spot"]}})
    seen_links = load_seen_links()
    last_news_log = 0

    print(
        f"Collecting {SYMBOL} price every {PRICE_LOG_INTERVAL_SECONDS}s "
        f"and news every {NEWS_LOG_INTERVAL_SECONDS}s -> {DATA_DIR}/"
    )

    while True:
        try:
            log_price(exchange)

            if time.time() - last_news_log >= NEWS_LOG_INTERVAL_SECONDS:
                seen_links = log_news(seen_links)
                save_seen_links(seen_links)
                last_news_log = time.time()
            heartbeat("data_collector")

        except Exception as e:
            print(f"Error: {e}")
            heartbeat("data_collector", "error", e)
            alert_error("data_collector", e)

        time.sleep(PRICE_LOG_INTERVAL_SECONDS)
