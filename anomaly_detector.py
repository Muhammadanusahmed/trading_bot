"""
Layer 2 - Event/Anomaly Detector.

Runs continuously and watches for trouble the strategy layer would
otherwise only notice a day too late:

1. Price shock  - price moves too far, too fast (hard-coded, instant check,
                   no AI reasoning involved - this is the actual crash
                   protection).
2. Volume spike - unusually high trading volume, often the first sign
                   something (news, a whale, an exchange event) is happening.
                   Email only by default (at most once per
                   VOLUME_ALERT_COOLDOWN_MINUTES): one busy minute is noise
                   for the 4h strategy, and forcing flat on it wasn't in the
                   backtest. VOLUME_SPIKE_FORCES_FLAT=true restores the old
                   behaviour.
3. News keyword match - fresh headlines containing words that historically
                   coincide with sudden moves (regulation, bans, hacks,
                   government action, interest rates, etc).

This layer does NOT place trades. It only raises alerts (email) and writes
`anomaly_flag.json`, which a future decision/execution layer should check
before opening or continuing a trade.
"""

import ccxt
import feedparser
import json
import os
import time
from collections import deque
from pathlib import Path
from dotenv import load_dotenv

from email_utils import send_email, alert_error

load_dotenv()

EXCHANGE = os.environ.get("EXCHANGE", "binance")
SYMBOL = os.environ.get("SYMBOL", "BTC/USDT")

# price shock settings
PRICE_CHECK_INTERVAL_SECONDS = int(os.environ.get("PRICE_CHECK_INTERVAL_SECONDS", 60))
PRICE_WINDOW_MINUTES = int(os.environ.get("PRICE_WINDOW_MINUTES", 5))
PRICE_SHOCK_PERCENT = float(os.environ.get("PRICE_SHOCK_PERCENT", 3.0))

# volume shock settings
VOLUME_LOOKBACK_CANDLES = int(os.environ.get("VOLUME_LOOKBACK_CANDLES", 20))
VOLUME_SPIKE_MULTIPLIER = float(os.environ.get("VOLUME_SPIKE_MULTIPLIER", 3.0))
VOLUME_SPIKE_FORCES_FLAT = os.environ.get("VOLUME_SPIKE_FORCES_FLAT", "false").strip().lower() == "true"
VOLUME_ALERT_COOLDOWN_MINUTES = int(os.environ.get("VOLUME_ALERT_COOLDOWN_MINUTES", 60))

# news shock settings (free RSS feeds, no API key needed)
NEWS_CHECK_INTERVAL_SECONDS = int(os.environ.get("NEWS_CHECK_INTERVAL_SECONDS", 900))
NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]
NEWS_KEYWORDS = [
    "sec", "ban", "regulation", "regulator", "hack", "exploit",
    "government", "bond", "interest rate", "lawsuit", "halt",
    "collapse", "crash", "seize", "sanction",
]

STATE_FILE = Path(__file__).parent / "anomaly_state.json"
FLAG_FILE = Path(__file__).parent / "anomaly_flag.json"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"seen_news_links": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def raise_alert(reason, details, force_flat=True):
    print(f"[ALERT] {reason}: {details}")
    send_email(f"Crypto bot alert: {reason}", details)
    if not force_flat:
        return
    FLAG_FILE.write_text(json.dumps({
        "reason": reason,
        "details": details,
        "timestamp": time.time(),
    }))


def check_price_shock(exchange, price_history):
    price = exchange.fetch_ticker(SYMBOL)["last"]
    now = time.time()
    price_history.append((now, price))

    cutoff = now - PRICE_WINDOW_MINUTES * 60
    while price_history and price_history[0][0] < cutoff:
        price_history.popleft()

    if len(price_history) >= 2:
        oldest_price = price_history[0][1]
        change_percent = (price - oldest_price) / oldest_price * 100
        if abs(change_percent) >= PRICE_SHOCK_PERCENT:
            direction = "dropped" if change_percent < 0 else "jumped"
            raise_alert(
                "Price shock",
                f"{SYMBOL} {direction} {abs(change_percent):.1f}% in the last "
                f"{PRICE_WINDOW_MINUTES} minutes (now {price:.2f}).",
            )
            price_history.clear()
            price_history.append((now, price))


_last_volume_alert = 0.0


def check_volume_shock(exchange):
    global _last_volume_alert
    candles = exchange.fetch_ohlcv(SYMBOL, timeframe="1m", limit=VOLUME_LOOKBACK_CANDLES + 1)
    volumes = [c[5] for c in candles]
    latest_volume = volumes[-1]
    baseline = sum(volumes[:-1]) / len(volumes[:-1])

    if baseline > 0 and latest_volume >= baseline * VOLUME_SPIKE_MULTIPLIER:
        if time.time() - _last_volume_alert < VOLUME_ALERT_COOLDOWN_MINUTES * 60:
            return
        _last_volume_alert = time.time()
        raise_alert(
            "Volume spike",
            f"{SYMBOL} 1m volume is {latest_volume:.2f}, "
            f"{latest_volume / baseline:.1f}x the last {VOLUME_LOOKBACK_CANDLES}-candle average."
            + ("" if VOLUME_SPIKE_FORCES_FLAT else "\nFor information only - the bot keeps its position."),
            force_flat=VOLUME_SPIKE_FORCES_FLAT,
        )


def check_news(state):
    for feed_url in NEWS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            link = entry.get("link", "")
            if not link or link in state["seen_news_links"]:
                continue

            title = entry.get("title", "").lower()
            matched = [kw for kw in NEWS_KEYWORDS if kw in title]
            if matched:
                raise_alert(
                    "News keyword match",
                    f'"{entry.get("title")}" matched: {", ".join(matched)}\n{link}',
                )

            state["seen_news_links"].append(link)

    state["seen_news_links"] = state["seen_news_links"][-500:]  # don't grow forever
    return state


if __name__ == "__main__":
    exchange = getattr(ccxt, EXCHANGE)({"options": {"fetchMarkets": ["spot"]}})
    price_history = deque()
    state = load_state()
    last_news_check = 0

    print(f"Watching {SYMBOL} on {EXCHANGE} for price/volume/news anomalies...")

    while True:
        try:
            check_price_shock(exchange, price_history)
            check_volume_shock(exchange)

            if time.time() - last_news_check >= NEWS_CHECK_INTERVAL_SECONDS:
                state = check_news(state)
                save_state(state)
                last_news_check = time.time()

        except Exception as e:
            print(f"Error: {e}")
            alert_error("anomaly_detector", e)

        time.sleep(PRICE_CHECK_INTERVAL_SECONDS)
