"""
Layer 3 - Decision Agent.

Turns the trading signal (MA crossover) plus context (Layer 2's anomaly
flag, Layer 1's recent news headlines) into a single decision, written to
decision.json. Layer 4 (execution_engine.py) reads this file and just acts
on it - it no longer computes the trading signal or checks for anomalies
itself.

This is deliberately still a simple, rule-based decision - not a "smart" AI
model. The point of this layer is the separation between deciding and
executing, not sophistication. The rule here can be swapped for something
fancier later without touching execution_engine.py at all.
"""

import ccxt
import json
import os
import time
from pathlib import Path
from dotenv import load_dotenv

from email_utils import alert_error

load_dotenv()

EXCHANGE = os.environ.get("EXCHANGE", "binance")
SYMBOL = os.environ.get("SYMBOL", "BTC/USDT")
TIMEFRAME = os.environ.get("TIMEFRAME", "4h")  # 4h beat 1h after fees in long_backtest.py (2026-10-03)
FAST_WINDOW = int(os.environ.get("FAST_WINDOW", 10))
SLOW_WINDOW = int(os.environ.get("SLOW_WINDOW", 50))

CHECK_INTERVAL_SECONDS = int(os.environ.get("DECISION_CHECK_INTERVAL_SECONDS", 300))
ANOMALY_FLAG_MAX_AGE_MINUTES = int(os.environ.get("ANOMALY_FLAG_MAX_AGE_MINUTES", 60))
NEWS_LOOKBACK_HOURS = int(os.environ.get("NEWS_LOOKBACK_HOURS", 6))

BASE_DIR = Path(__file__).parent
NEWS_LOG = BASE_DIR / "data" / "news_log.jsonl"
ANOMALY_FLAG_FILE = BASE_DIR / "anomaly_flag.json"
DECISION_FILE = BASE_DIR / "decision.json"


def get_ma_signal(exchange):
    candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=SLOW_WINDOW + 5)
    # the exchange returns the still-forming candle last; using it makes the signal flip
    # back and forth within the candle, so only use candles that have closed
    candle_ms = exchange.parse_timeframe(TIMEFRAME) * 1000
    now_ms = exchange.milliseconds()
    candles = [c for c in candles if c[0] + candle_ms <= now_ms]
    if len(candles) < SLOW_WINDOW:
        raise ValueError(f"Only {len(candles)} closed {TIMEFRAME} candles returned, need {SLOW_WINDOW}")
    closes = [c[4] for c in candles]
    fast_ma = sum(closes[-FAST_WINDOW:]) / FAST_WINDOW
    slow_ma = sum(closes[-SLOW_WINDOW:]) / SLOW_WINDOW
    price = closes[-1]
    return (1 if fast_ma > slow_ma else 0), price, fast_ma, slow_ma


def anomaly_status():
    if not ANOMALY_FLAG_FILE.exists():
        return None
    flag = json.loads(ANOMALY_FLAG_FILE.read_text())
    age_minutes = (time.time() - flag["timestamp"]) / 60
    return flag if age_minutes <= ANOMALY_FLAG_MAX_AGE_MINUTES else None


def recent_headlines():
    """Headlines Layer 1 collected in the last NEWS_LOOKBACK_HOURS - shown
    alongside the decision for context, not scored/acted on yet."""
    if not NEWS_LOG.exists():
        return []

    cutoff = time.time() - NEWS_LOOKBACK_HOURS * 3600
    headlines = []
    with open(NEWS_LOG) as f:
        for line in f:
            record = json.loads(line)
            ts = time.mktime(time.strptime(record["timestamp"], "%Y-%m-%d %H:%M:%S")) - time.timezone
            if ts >= cutoff:
                headlines.append(record["title"])
    return headlines[-20:]  # cap so the decision file stays readable


def decide(exchange):
    ma_position, price, fast_ma, slow_ma = get_ma_signal(exchange)
    anomaly = anomaly_status()
    headlines = recent_headlines()

    if anomaly:
        position = 0
        reason = f"Anomaly flagged by Layer 2 ({anomaly['reason']}) - forcing flat regardless of MA signal."
    else:
        position = ma_position
        reason = (
            f"MA crossover: fast={fast_ma:.2f}, slow={slow_ma:.2f} -> "
            + ("trend up, go long." if position == 1 else "trend down/flat, stay out.")
        )

    decision = {
        "timestamp": time.time(),
        "symbol": SYMBOL,
        "position": position,
        "price": price,
        "reason": reason,
        "anomaly": anomaly,
        "recent_headlines_considered": headlines,
    }
    DECISION_FILE.write_text(json.dumps(decision, indent=2))
    print(f"Decision: position={position} | {reason}")
    return decision


if __name__ == "__main__":
    exchange = getattr(ccxt, EXCHANGE)({"options": {"fetchMarkets": ["spot"]}})
    print(f"Decision agent starting for {SYMBOL}, checking every {CHECK_INTERVAL_SECONDS}s")

    while True:
        try:
            decide(exchange)
        except Exception as e:
            print(f"Error: {e}")
            alert_error("decision_agent", e)
        time.sleep(CHECK_INTERVAL_SECONDS)
