"""
Layer 6 - Learning Agent (Google Gemini, free tier).

Reads what the other layers recorded and writes lessons. It never changes
the bot: no settings, no rules, no orders. Every suggested rule is tested on
2017-now history (rule_lab.py) and shown on the dashboard, where a human
decides. Three jobs:

1. Loss analysis - every losing paper/live trade: what the market looked like
   (price, moving averages, news, alerts), why it lost, the lesson, and at
   most one suggested rule from rule_lab.RULES, backtested.
2. Daily market report - once a day: mood, risks, things to watch, and new
   risky news words the alert list doesn't have yet.
3. History study - once: statistics of the strategy's losing trades in
   2017-2022, from which Gemini suggests rules that are then checked on
   2023-now (data it didn't see).

Earlier lessons go into every prompt, so it builds on what it learned.
Output: data/lessons.jsonl, data/market_reports.jsonl, data/learning_status.json.
Needs GEMINI_API_KEY (free from aistudio.google.com); without it, it idles.
"""

import json
import os
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from agent_status import heartbeat, append_jsonl, utc_now

load_dotenv()

import gemini_client  # noqa: E402
import rule_lab  # noqa: E402

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
TRADE_LOG = DATA_DIR / "trade_log.jsonl"
DECISION_HISTORY = DATA_DIR / "decision_history.jsonl"
NEWS_LOG = DATA_DIR / "news_log.jsonl"
ANOMALY_LOG = DATA_DIR / "anomaly_log.jsonl"
PRICE_LOG = DATA_DIR / "price_history.csv"
LESSONS = DATA_DIR / "lessons.jsonl"
REPORTS = DATA_DIR / "market_reports.jsonl"
STATUS_FILE = DATA_DIR / "learning_status.json"

SYMBOL = os.environ.get("SYMBOL", "BTC/USDT")
EXCHANGE = os.environ.get("EXCHANGE", "binance")
TIMEFRAME = os.environ.get("TIMEFRAME", "4h")
CHECK_INTERVAL_SECONDS = int(os.environ.get("LEARNING_CHECK_INTERVAL_SECONDS", 600))
MAX_CALLS_PER_DAY = int(os.environ.get("GEMINI_MAX_CALLS_PER_DAY", 20))
REPORT_HOUR_UTC = int(os.environ.get("MARKET_REPORT_HOUR_UTC", 3))  # 3 UTC = 8 AM Pakistan
LANGUAGE = os.environ.get("LEARNING_LANGUAGE", "simple Roman Urdu (Urdu written in English letters)")
CLOSE_ACTIONS = ("DRY_RUN sell", "DRY_RUN stop-loss filled", "LIVE sell", "LIVE stop-loss filled", "LIVE emergency sell")

STRATEGY_TEXT = (
    f"The bot trades {SYMBOL} spot, long only. Strategy: on {TIMEFRAME} closed candles, buy when the "
    f"{os.environ.get('FAST_WINDOW', 10)}-candle moving average crosses above the {os.environ.get('SLOW_WINDOW', 50)}-candle one, "
    f"sell when it crosses below. A {os.environ.get('STOP_LOSS_PERCENT', 2.0)}% stop-loss sells automatically; after a stop-out it "
    "waits for the signal to go flat before buying again. An anomaly detector forces the bot flat on a sudden price shock or "
    f"on news containing risky words. Fee {os.environ.get('FEE_PERCENT', 0.1)}% per buy and per sell. Trade size "
    f"{os.environ.get('TRADE_AMOUNT_USDT', 50)} USDT."
)


# ---------- small helpers ----------

def parse_utc(text):
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def read_jsonl(path, max_bytes=3_000_000):
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def load_status():
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def save_status(status):
    status["max_calls"] = MAX_CALLS_PER_DAY
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2))
    tmp.replace(STATUS_FILE)


def budget_left(status):
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if status.get("calls_day") != today:
        status["calls_day"], status["calls_today"] = today, 0
    if status.get("quota_until", 0) > time.time():
        return False
    return status["calls_today"] < MAX_CALLS_PER_DAY


def ask(status, prompt):
    status["calls_today"] = status.get("calls_today", 0) + 1
    try:
        return gemini_client.ask_json(prompt)
    except gemini_client.QuotaExceeded:
        status["quota_until"] = time.time() + 3600  # free limit hit: wait an hour
        status["note"] = "Gemini free limit poori: 1 ghante baad dobara"
        raise


def past_lessons_text(limit=8):
    lessons = read_jsonl(LESSONS)[-limit:]
    if not lessons:
        return "None yet."
    return "\n".join(f"- [{l.get('market_type', '?')}] {l.get('title', '')}: {l.get('lesson', '')}" for l in lessons)


def output_rules():
    return (
        f"Write every text field in {LANGUAGE}, short and simple, for a beginner trader.\n"
        "You may suggest AT MOST ONE rule change, and only from this menu (use the exact name and a number in range), "
        "or null if no rule would clearly help. Do not suggest a rule from one trade alone unless the pattern is clear.\n"
        f"{rule_lab.rules_menu_text()}\n"
    )


def recent_candle_context():
    """Market features right now from public exchange candles (no keys)."""
    try:
        import ccxt
        import pandas as pd
        ex = getattr(ccxt, EXCHANGE)({"options": {"fetchMarkets": ["spot"]}})
        raw = ex.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=260)
        df = pd.DataFrame(raw, columns=["t", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
        return rule_lab.add_features(df)
    except Exception as e:
        print(f"Could not fetch candles: {e}")
        return None


def features_at(df, when):
    if df is None:
        return None
    part = df.loc[:when]
    if part.empty:
        return None
    row = part.iloc[-1]
    return {"market_type": rule_lab.market_type(row), "fast_ma": round(row["fast"], 2), "slow_ma": round(row["slow"], 2),
            "ma_gap_pct": round(row["gap_pct"], 3), "atr_pct": round(row["atr_pct"], 2),
            "range20_pct": round(row["range20_pct"], 2), "slow_ma_10candle_slope_pct": round(row["slope_pct"], 2)}


def save_lesson(lesson):
    lesson.setdefault("time", utc_now())
    lesson.setdefault("status", "pending" if lesson.get("suggested_rule") else "info")
    append_jsonl(LESSONS, lesson)


def backtest_rule(rule):
    if not rule:
        return None
    try:
        return rule_lab.compare(rule)
    except Exception as e:
        return {"error": str(e)[:300]}


# ---------- job 1: loss analysis ----------

def analysed_trade_ids():
    return {l.get("trade_id") for l in read_jsonl(LESSONS) if l.get("trade_id")}


def next_unanalysed_loss(status):
    done = analysed_trade_ids()
    failed = status.get("failed_attempts", {})
    for rec in read_jsonl(TRADE_LOG, max_bytes=20_000_000):
        if rec.get("action") in CLOSE_ACTIONS and (rec.get("net_pnl_usdt") or 0) < 0 and rec.get("timestamp") not in done \
                and failed.get(rec.get("timestamp"), 0) < 3:
            return rec
    return None


def loss_context(trade):
    exit_t = parse_utc(trade["timestamp"])
    entry_t = parse_utc(trade.get("entry_time")) or exit_t - timedelta(hours=12)
    window_start = entry_t - timedelta(hours=12)

    decisions = [d for d in read_jsonl(DECISION_HISTORY)
                 if (t := parse_utc(d.get("time"))) and entry_t - timedelta(hours=1) <= t <= exit_t]
    changes, prev = [], None
    for d in decisions:
        if d.get("position") != prev:
            changes.append({"time": d.get("time"), "position": d.get("position"), "price": d.get("price"), "reason": d.get("reason")})
            prev = d.get("position")

    prices = []
    try:
        import csv
        with open(PRICE_LOG) as f:
            for row in csv.DictReader(f):
                t = parse_utc(row.get("timestamp"))
                if t and entry_t <= t <= exit_t:
                    prices.append(float(row["price"]))
    except (OSError, ValueError, KeyError):
        pass

    news = [n.get("title") for n in read_jsonl(NEWS_LOG)
            if (t := parse_utc(n.get("timestamp"))) and window_start <= t <= exit_t][-30:]
    alerts = [{"time": a.get("timestamp"), "reason": a.get("reason"), "details": str(a.get("details"))[:200]}
              for a in read_jsonl(ANOMALY_LOG) if (t := parse_utc(a.get("timestamp"))) and window_start <= t <= exit_t]

    candles = recent_candle_context()
    return {
        "trade": {k: trade.get(k) for k in ("action", "entry_time", "timestamp", "entry_price", "price", "quantity",
                                              "gross_pnl_usdt", "fees_total_usdt", "net_pnl_usdt", "pnl_percent", "reason")},
        "hours_held": round((exit_t - entry_t).total_seconds() / 3600, 1),
        "price_during_trade": {"min": min(prices), "max": max(prices), "samples": len(prices)} if prices else None,
        "market_at_entry": features_at(candles, entry_t),
        "market_at_exit": features_at(candles, exit_t),
        "decision_changes": changes[-10:],
        "news_headlines_around_trade": news,
        "anomaly_alerts_around_trade": alerts,
    }


def analyse_loss(status, trade):
    ctx = loss_context(trade)
    prompt = (
        "You are the risk coach of a small crypto trading bot. A trade just closed at a loss. Explain why, and what "
        "the bot should learn.\n\n"
        f"STRATEGY:\n{STRATEGY_TEXT}\n\n"
        f"LESSONS LEARNED BEFORE (build on them, don't repeat them):\n{past_lessons_text()}\n\n"
        f"THE LOSING TRADE AND ITS CONTEXT (JSON):\n{json.dumps(ctx, default=str)}\n\n"
        f"{output_rules()}\n"
        'Reply with JSON only: {"title": "max 8 words", "market_type": "trend up|trend down|sideways|volatile|news shock", '
        '"category": "whipsaw|stop too tight|news|trend reversal|fees|bad entry|other", '
        '"reason": "2-3 sentences: why this trade lost", "lesson": "1-2 sentences: what to do next time in a similar situation", '
        '"suggested_rule": {"name": "...", "value": 0, "why": "..."} or null, "confidence": "low|medium|high"}'
    )
    answer = ask(status, prompt)
    rule = rule_lab.valid_rule(answer.get("suggested_rule"))
    save_lesson({
        "source": "trade", "trade_id": trade["timestamp"], "loss_usdt": trade.get("net_pnl_usdt"),
        "title": str(answer.get("title", ""))[:120], "market_type": answer.get("market_type") or (ctx["market_at_entry"] or {}).get("market_type"),
        "category": answer.get("category"), "reason": answer.get("reason"), "lesson": answer.get("lesson"),
        "confidence": answer.get("confidence"), "suggested_rule": rule, "backtest": backtest_rule(rule),
        "model": answer.get("_model"),
    })
    print(f"Lesson saved for losing trade {trade['timestamp']}")


# ---------- job 2: daily market report ----------

def report_due():
    now = datetime.now(timezone.utc)
    if now.hour < REPORT_HOUR_UTC:
        return False
    today = now.strftime("%Y-%m-%d")
    return not any(str(r.get("time", "")).startswith(today) for r in read_jsonl(REPORTS)[-3:])


def market_report(status):
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    prices = []
    try:
        import csv
        with open(PRICE_LOG) as f:
            for row in csv.DictReader(f):
                t = parse_utc(row.get("timestamp"))
                if t and t >= day_ago:
                    prices.append(float(row["price"]))
    except (OSError, ValueError, KeyError):
        pass
    news = [f"{n.get('source', '')}: {n.get('title', '')}" for n in read_jsonl(NEWS_LOG)
            if (t := parse_utc(n.get("timestamp"))) and t >= day_ago][-60:]
    alerts = [a.get("reason") for a in read_jsonl(ANOMALY_LOG) if (t := parse_utc(a.get("timestamp"))) and t >= day_ago]
    decisions = read_jsonl(DECISION_HISTORY)
    last = decisions[-1] if decisions else {}
    try:
        from anomaly_detector import NEWS_KEYWORDS
    except Exception:
        NEWS_KEYWORDS = []
    candles = recent_candle_context()
    ctx = {
        "price_24h": {"open": prices[0], "now": prices[-1], "high": max(prices), "low": min(prices),
                      "change_pct": round((prices[-1] / prices[0] - 1) * 100, 2)} if prices else None,
        "market_now": features_at(candles, now),
        "bot_signal_now": {"position": last.get("position"), "reason": last.get("reason")},
        "anomaly_alerts_24h": alerts,
        "news_headlines_24h": news,
        "current_risky_news_words": NEWS_KEYWORDS,
    }
    prompt = (
        "You are the market analyst for a small crypto trading bot. Study the last 24 hours and teach the bot's owner "
        "what is happening and what to watch. Do not predict prices with certainty.\n\n"
        f"STRATEGY:\n{STRATEGY_TEXT}\n\n"
        f"LESSONS LEARNED BEFORE:\n{past_lessons_text()}\n\n"
        f"LAST 24 HOURS (JSON):\n{json.dumps(ctx, default=str)}\n\n"
        f"Write every text field in {LANGUAGE}, short and simple.\n"
        "new_keywords: up to 5 single lowercase English words or short phrases that appear in risky headlines and are "
        "NOT already in current_risky_news_words (empty list if none). lesson: one general lesson for the bot from today, or null.\n"
        'Reply with JSON only: {"mood": "bullish|bearish|sideways|uncertain", "summary": "3-4 sentences", '
        '"risks": ["..."], "watch": "1-2 sentences", "new_keywords": ["..."], "lesson": "..." or null}'
    )
    answer = ask(status, prompt)
    known = {k.lower() for k in NEWS_KEYWORDS}
    keywords = [str(k).lower().strip()[:40] for k in (answer.get("new_keywords") or []) if str(k).strip()]
    report = {
        "time": utc_now(), "mood": answer.get("mood"), "summary": answer.get("summary"),
        "risks": [str(r) for r in (answer.get("risks") or [])][:6], "watch": answer.get("watch"),
        "new_keywords": [k for k in keywords if k not in known][:5], "market": ctx["market_now"],
        "price_24h": ctx["price_24h"], "model": answer.get("_model"),
    }
    append_jsonl(REPORTS, report)
    if answer.get("lesson"):
        save_lesson({"source": "market", "title": "Aaj ki market se sabaq", "lesson": answer["lesson"],
                     "market_type": (ctx["market_now"] or {}).get("market_type")})
    print("Market report saved")


# ---------- job 3: history study ----------

def history_study(status):
    print("Studying 2017-2022 losing trades (first run downloads ~100 monthly files, a few minutes)...")
    stats = rule_lab.losing_trade_study()
    prompt = (
        "You are the risk coach of a small crypto trading bot. Below are statistics of the bot's CURRENT strategy "
        "on BTC history 2017-2022, focused on its losing trades. Find the 2-3 most important patterns behind the losses.\n\n"
        f"STRATEGY:\n{STRATEGY_TEXT}\n\n"
        f"STATISTICS (JSON; loss_rate is 0-1, pnl in %):\n{json.dumps(stats, default=str)}\n\n"
        f"{output_rules()}\n"
        "Here you may suggest one rule per pattern. Each rule will be tested on 2023-now data you have not seen.\n"
        'Reply with JSON only: {"patterns": [{"title": "max 8 words", "market_type": "...", "reason": "2-3 sentences", '
        '"lesson": "1-2 sentences", "suggested_rule": {"name": "...", "value": 0, "why": "..."} or null}]}'
    )
    answer = ask(status, prompt)
    for p in (answer.get("patterns") or [])[:3]:
        rule = rule_lab.valid_rule(p.get("suggested_rule"))
        save_lesson({"source": "backtest", "title": str(p.get("title", ""))[:120], "market_type": p.get("market_type"),
                     "reason": p.get("reason"), "lesson": p.get("lesson"), "suggested_rule": rule,
                     "backtest": backtest_rule(rule), "model": answer.get("_model"),
                     "study": {"period": stats.get("period"), "losing_trades": stats.get("losing_trades")}})
    status["history_study_done"] = utc_now()
    print("History study saved")


# ---------- main loop ----------

def run_once(status):
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        status["note"] = "GEMINI_API_KEY set nahi: seekhna band"
        return
    if not budget_left(status):
        if status.get("quota_until", 0) <= time.time():
            status["note"] = f"Aaj ki limit ({MAX_CALLS_PER_DAY} calls) poori, kal phir"
        return
    trade = next_unanalysed_loss(status)
    if trade:
        status["note"] = "Loss ka analysis kar raha hai"
        job, key, fn = "trade", trade["timestamp"], lambda: analyse_loss(status, trade)
    elif report_due() and status.get("failed_attempts", {}).get("report-" + utc_now()[:10], 0) < 3:
        status["note"] = "Roz ki market report bana raha hai"
        job, key, fn = "report", "report-" + utc_now()[:10], lambda: market_report(status)
    elif not status.get("history_study_done") and status.get("failed_attempts", {}).get("history", 0) < 3:
        status["note"] = "Purane backtest ki losses parh raha hai"
        job, key, fn = "history", "history", lambda: history_study(status)
    else:
        status["note"] = "Waiting: nayi loss ya kal ki report ka intezar"
        return
    try:
        fn()
    except gemini_client.QuotaExceeded:
        raise
    except Exception:
        # count failures so one bad item can't use up the whole daily limit (gives up after 3 tries)
        failed = status.setdefault("failed_attempts", {})
        failed[key] = failed.get(key, 0) + 1
        raise
    status["note"] = "Waiting: nayi loss ya kal ki report ka intezar"


if __name__ == "__main__":
    print(f"Learning agent starting, checking every {CHECK_INTERVAL_SECONDS}s, max {MAX_CALLS_PER_DAY} Gemini calls/day")
    DATA_DIR.mkdir(exist_ok=True)
    while True:
        status = load_status()
        try:
            run_once(status)
            status.pop("last_error", None)
            heartbeat("learning_agent", note=status.get("note"))
        except gemini_client.QuotaExceeded:
            heartbeat("learning_agent", note=status.get("note"))
        except Exception as e:
            traceback.print_exc()
            status["last_error"] = str(e)[:300]
            heartbeat("learning_agent", "error", e, note="Error, agle round dobara koshish")
        status["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            save_status(status)
        except OSError as e:
            print(f"Could not save status: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
