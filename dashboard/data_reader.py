"""
Read-only access to the bot's files for the dashboard.

Nothing in this module writes to the bot folder. Every reader tolerates a
missing or half-written file (the bot may be writing it right now) and
returns empty data instead of raising, so one bad line never breaks a page.
"""

import csv
import io
import json
import os
import shutil
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BOT_DIR = Path(os.environ.get("BOT_DIR", Path(__file__).resolve().parent.parent))
DATA_DIR = BOT_DIR / "data"
TZ = ZoneInfo(os.environ.get("DASHBOARD_TZ", "Asia/Karachi"))

AGENTS = {
    # agent: (env var with its loop interval, default seconds, what it does)
    "data_collector": ("PRICE_LOG_INTERVAL_SECONDS", 300, "Price aur news jama karta hai"),
    "anomaly_detector": ("PRICE_CHECK_INTERVAL_SECONDS", 60, "Achanak price girne aur khatarnak news par nazar"),
    "decision_agent": ("DECISION_CHECK_INTERVAL_SECONDS", 300, "Buy / sell / wait ka faisla"),
    "execution_engine": ("EXECUTION_CHECK_INTERVAL_SECONDS", 300, "Faislay par (paper) trade karta hai"),
    "learning_agent": ("LEARNING_CHECK_INTERVAL_SECONDS", 600, "Losses aur market se seekhta hai (Gemini)"),
}

# Settings shown on the Settings page. Anything not listed here (keys,
# passwords, tokens) is never sent to the browser.
SETTINGS_SHOWN = [
    ("Trading", ["EXCHANGE", "SYMBOL", "TIMEFRAME", "FAST_WINDOW", "SLOW_WINDOW", "TRADE_AMOUNT_USDT",
                 "STOP_LOSS_PERCENT", "STOP_WARNING_PERCENT", "FEE_PERCENT", "STARTING_BALANCE_USDT"]),
    ("Safety", ["DRY_RUN", "USE_TESTNET", "DECISION_MAX_AGE_MINUTES", "PRICE_SHOCK_PERCENT", "PRICE_WINDOW_MINUTES",
                "VOLUME_SPIKE_FORCES_FLAT", "ANOMALY_FLAG_MAX_AGE_MINUTES"]),
    ("Email", ["SMTP_HOST", "SMTP_PORT", "NOTIFY_TO", "ERROR_ALERT_COOLDOWN_MINUTES"]),
    ("Learning (Gemini)", ["GEMINI_MODEL", "GEMINI_MAX_CALLS_PER_DAY", "LEARNING_LANGUAGE", "MARKET_REPORT_HOUR_UTC"]),
]
SECRETS_PRESENCE = ["EXCHANGE_API_KEY", "EXCHANGE_API_SECRET", "SMTP_PASSWORD", "GEMINI_API_KEY"]


# ---------- low-level readers ----------

def _parse_utc(text):
    """'2026-11-22 16:35:21' (UTC, as the bot writes it) -> aware datetime."""
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def tail_lines(path, max_lines=None, max_bytes=2_000_000):
    """Last lines of a text file, reading at most max_bytes from the end."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # drop the partial first line
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [l for l in data.splitlines() if l.strip()]
    return lines[-max_lines:] if max_lines else lines


def read_jsonl(path, max_lines=None, max_bytes=2_000_000):
    out = []
    for line in tail_lines(path, max_lines, max_bytes):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a line still being written
    return out


def env(name, default=None):
    return os.environ.get(name, default)


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def guard_on(name):
    return os.environ.get(name, "true").strip().lower() != "false"


# ---------- prices ----------

def price_rows(hours=24 * 30):
    """price_history.csv rows from the last `hours`, oldest first."""
    path = DATA_DIR / "price_history.csv"
    # ~95 bytes a row, one row per 5 minutes
    max_bytes = int(hours * 12 * 130) + 4096
    try:
        with open(path) as f:
            header = f.readline().strip().split(",")
    except OSError:
        return []
    lines = tail_lines(path, max_bytes=max_bytes)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = []
    for rec in csv.DictReader(io.StringIO("\n".join(lines)), fieldnames=header):
        ts = _parse_utc(rec.get("timestamp"))
        if not ts or ts < cutoff:
            continue
        try:
            rows.append({"t": ts, "price": float(rec["price"]),
                         "high": float(rec["high_24h"] or 0) or None, "low": float(rec["low_24h"] or 0) or None,
                         "volume": float(rec["volume_24h"] or 0) or None})
        except (TypeError, ValueError, KeyError):
            continue
    return rows


def latest_price():
    rows = price_rows(hours=2)
    if rows:
        return rows[-1]
    decision = read_json(BOT_DIR / "decision.json") or {}
    if decision.get("price"):
        return {"t": datetime.fromtimestamp(decision["timestamp"], timezone.utc), "price": float(decision["price"]),
                "high": None, "low": None, "volume": None}
    return None


def downsample(rows, max_points=400):
    if len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    picked = [rows[int(i * step)] for i in range(max_points)]
    picked[-1] = rows[-1]
    return picked


# ---------- trades and P/L ----------

CLOSE_ACTIONS = ("DRY_RUN sell", "DRY_RUN stop-loss filled", "LIVE sell", "LIVE stop-loss filled", "LIVE emergency sell")
BUY_ACTIONS = ("DRY_RUN buy", "LIVE buy", "LIVE buy (adopted)")


def trade_log():
    rows = []
    for rec in read_jsonl(DATA_DIR / "trade_log.jsonl", max_bytes=20_000_000):
        rec["_t"] = _parse_utc(rec.get("timestamp"))
        if rec["_t"]:
            rows.append(rec)
    return rows


def starting_balance(log=None):
    for rec in reversed(log or []):
        if rec.get("action") == "engine start" and rec.get("starting_balance_usdt") is not None:
            return float(rec["starting_balance_usdt"])
    return env_float("STARTING_BALANCE_USDT", 1000)


def round_trips(log):
    """Pair every buy with the close that ended it."""
    trips, open_buy = [], None
    for rec in log:
        action = rec.get("action", "")
        if action in BUY_ACTIONS or action.startswith("LIVE buy"):
            open_buy = rec
        elif action in CLOSE_ACTIONS:
            net = rec.get("net_pnl_usdt")
            entry_t = _parse_utc(rec.get("entry_time")) or (open_buy or {}).get("_t")
            held_h = (rec["_t"] - entry_t).total_seconds() / 3600 if entry_t else None
            trips.append({
                "id": rec.get("timestamp"),
                "entry_time": iso(entry_t),
                "exit_time": iso(rec["_t"]),
                "entry_price": rec.get("entry_price") or (open_buy or {}).get("price"),
                "exit_price": rec.get("price"),
                "quantity": rec.get("quantity"),
                "net_pnl": net,
                "pnl_percent": rec.get("pnl_percent"),
                "fees": rec.get("fees_total_usdt"),
                "exit_type": "Stop-loss" if "stop-loss" in action else ("Emergency" if "emergency" in action else "Signal sell"),
                "entry_reason": (open_buy or {}).get("reason"),
                "exit_reason": rec.get("reason"),
                "held_hours": round(held_h, 1) if held_h is not None else None,
                "balance_after": rec.get("paper_balance_usdt"),
            })
            open_buy = None
    return trips


def trade_stats(trips):
    closed = [t for t in trips if t["net_pnl"] is not None]
    wins = [t["net_pnl"] for t in closed if t["net_pnl"] > 0]
    losses = [t["net_pnl"] for t in closed if t["net_pnl"] <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(closed) * 100 if closed else None,
        "avg_win": gross_win / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "biggest_loss": min(losses) if losses else None,
        "biggest_win": max(wins) if wins else None,
        "fees_total": sum(t["fees"] or 0 for t in closed),
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "stop_outs": sum(1 for t in closed if t["exit_type"] == "Stop-loss"),
    }


def open_position(price=None):
    state = read_json(BOT_DIR / "trade_state.json") or {}
    if not state.get("in_position"):
        return {"open": False, "wait_for_flat": bool(state.get("wait_for_flat")),
                "pending_buy": bool(state.get("pending_buy"))}
    entry = float(state["entry_price"])
    qty = float(state["quantity"])
    stop_pct = env_float("STOP_LOSS_PERCENT", 2.0)
    warn_pct = env_float("STOP_WARNING_PERCENT", 1.5)
    fee_pct = env_float("FEE_PERCENT", 0.1)
    pos = {
        "open": True,
        "entry_price": entry,
        "quantity": qty,
        "value_at_entry": entry * qty,
        "entry_time": iso(_parse_utc(state.get("entry_time"))),
        "stop_price": entry * (1 - stop_pct / 100),
        "warning_price": entry * (1 - warn_pct / 100),
        "stop_percent": stop_pct,
        "stop_warning_sent": bool(state.get("stop_warning_sent")),
        "exchange_stop_order": state.get("stop_order_id"),
    }
    if price:
        entry_fee = float(state.get("entry_fee_usdt") or entry * qty * fee_pct / 100)
        exit_fee = price * qty * fee_pct / 100
        pos["current_price"] = price
        pos["unrealized_pnl"] = (price - entry) * qty - entry_fee - exit_fee
        pos["move_percent"] = (price - entry) / entry * 100
        pos["distance_to_stop_percent"] = (price - pos["stop_price"]) / price * 100
    return pos


def equity_curve(log, start_balance, unrealized=None):
    """Balance after every closed trade, plus a 'now' point."""
    first = next((r["_t"] for r in log if r.get("action") == "engine start"), None) or (log[0]["_t"] if log else None)
    points = []
    if first:
        points.append({"t": iso(first), "balance": start_balance})
    balance = start_balance
    for rec in log:
        if rec.get("action") in CLOSE_ACTIONS and rec.get("net_pnl_usdt") is not None:
            balance = rec.get("paper_balance_usdt") or balance + rec["net_pnl_usdt"]
            points.append({"t": iso(rec["_t"]), "balance": round(balance, 4)})
    now_equity = balance + (unrealized or 0)
    points.append({"t": iso(datetime.now(timezone.utc)), "balance": round(now_equity, 4)})
    peak, max_dd = -1e18, 0.0
    for p in points:
        peak = max(peak, p["balance"])
        max_dd = min(max_dd, (p["balance"] / peak - 1) * 100 if peak else 0)
    return points, balance, now_equity, max_dd


def _bucket_key(dt, period):
    local = dt.astimezone(TZ)
    if period == "day":
        return local.strftime("%Y-%m-%d")
    if period == "week":
        monday = local - timedelta(days=local.weekday())
        return monday.strftime("%Y-%m-%d")
    return local.strftime("%Y-%m")


def pnl_buckets(log, period="day", count=None):
    """Realized net P/L per day/week/month (local time), newest last, gaps filled with 0."""
    count = count or {"day": 30, "week": 12, "month": 12}[period]
    sums, trades = defaultdict(float), defaultdict(int)
    for rec in log:
        if rec.get("action") in CLOSE_ACTIONS and rec.get("net_pnl_usdt") is not None:
            k = _bucket_key(rec["_t"], period)
            sums[k] += rec["net_pnl_usdt"]
            trades[k] += 1
    now = datetime.now(TZ)
    keys = []
    for i in range(count - 1, -1, -1):
        if period == "day":
            d = now - timedelta(days=i)
        elif period == "week":
            d = now - timedelta(days=now.weekday()) - timedelta(weeks=i)
        else:
            y, m = now.year, now.month - i
            while m <= 0:
                y, m = y - 1, m + 12
            d = now.replace(year=y, month=m, day=1)
        keys.append(_bucket_key(d, period))
    return [{"label": k, "pnl": round(sums.get(k, 0.0), 4), "trades": trades.get(k, 0)} for k in keys]


def period_totals(log):
    now = datetime.now(timezone.utc)
    out = {}
    for name, period in (("today", "day"), ("week", "week"), ("month", "month")):
        key = _bucket_key(now, period)
        out[name] = sum(r["net_pnl_usdt"] for r in log
                        if r.get("action") in CLOSE_ACTIONS and r.get("net_pnl_usdt") is not None
                        and _bucket_key(r["_t"], period) == key)
    return out


# ---------- agents ----------

def agent_statuses():
    now = time.time()
    state = read_json(BOT_DIR / "trade_state.json") or {}
    decision = read_json(BOT_DIR / "decision.json") or {}
    flag = read_json(BOT_DIR / "anomaly_flag.json")
    out = []
    for name, (var, default, role) in AGENTS.items():
        try:
            interval = int(os.environ.get(var, default))
        except ValueError:
            interval = default
        hb = read_json(DATA_DIR / "heartbeat" / f"{name}.json")
        if not hb:
            status, age = ("Not installed" if name == "learning_agent" else "Pending"), None
        else:
            age = now - float(hb.get("last_run_epoch") or 0)
            if age > interval * 3 + 120:
                status = "Stopped"
            elif hb.get("status") == "error":
                status = "Error"
            else:
                status = "Active"
        doing = role
        if status == "Active":
            if name == "execution_engine":
                if state.get("in_position"):
                    doing = "Position khuli hai, stop-loss par nazar"
                elif state.get("wait_for_flat"):
                    doing = "Waiting: stop-loss ke baad signal flat hone ka intezar"
                else:
                    doing = "Waiting: buy signal ka intezar"
            elif name == "decision_agent" and decision:
                doing = ("Signal: LONG (buy/hold)" if decision.get("position") == 1 else "Signal: FLAT (bahar raho)")
            elif name == "anomaly_detector" and flag and now - flag.get("timestamp", 0) < 3600:
                doing = f"ALERT active: {flag.get('reason')} (bot flat)"
            elif name == "learning_agent" and hb:
                doing = hb.get("note") or role
        out.append({
            "agent": name,
            "role": role,
            "status": status,
            "doing": doing,
            "last_run": iso(datetime.fromtimestamp(hb["last_run_epoch"], timezone.utc)) if hb and hb.get("last_run_epoch") else None,
            "age_seconds": round(age) if age is not None else None,
            "interval_seconds": interval,
            "runs": (hb or {}).get("runs"),
            "errors": (hb or {}).get("errors"),
            "restarts": (hb or {}).get("restarts"),
            "started_at": iso(_parse_utc((hb or {}).get("started_at"))),
            "last_error": (hb or {}).get("last_error"),
            "last_error_time": iso(_parse_utc((hb or {}).get("last_error_time"))),
        })
    return out


# ---------- decisions, news, alerts, emails ----------

def decisions(limit=300):
    rows = read_jsonl(DATA_DIR / "decision_history.jsonl", max_lines=5000)
    out, prev = [], None
    for r in rows:
        changed = prev is not None and r.get("position") != prev
        prev = r.get("position")
        out.append({
            "time": iso(_parse_utc(r.get("time"))),
            "position": r.get("position"),
            "price": r.get("price"),
            "reason": r.get("reason"),
            "fast_ma": r.get("fast_ma"),
            "slow_ma": r.get("slow_ma"),
            "anomaly": (r.get("anomaly") or {}).get("reason") if r.get("anomaly") else None,
            "headline_count": r.get("headline_count"),
            "changed": changed,
        })
    return out[-limit:][::-1]


def news(limit=40):
    rows = read_jsonl(DATA_DIR / "news_log.jsonl", max_lines=limit)
    return [{"time": iso(_parse_utc(r.get("timestamp"))), "source": r.get("source"), "title": r.get("title"),
             "link": r.get("link") if str(r.get("link", "")).startswith("http") else None} for r in rows][::-1]


def anomalies(limit=50):
    rows = read_jsonl(DATA_DIR / "anomaly_log.jsonl", max_lines=limit)
    return [{"time": iso(_parse_utc(r.get("timestamp"))), "reason": r.get("reason"), "details": r.get("details"),
             "forced_flat": r.get("forced_flat")} for r in rows][::-1]


def emails(limit=500):
    rows = read_jsonl(DATA_DIR / "email_log.jsonl", max_lines=limit)
    return [{"time": iso(_parse_utc(r.get("time"))), "agent": r.get("agent"), "subject": r.get("subject"),
             "to": r.get("to"), "status": r.get("status"), "error": r.get("error")} for r in rows][::-1]


# ---------- learning ----------

def learning():
    lessons = read_jsonl(DATA_DIR / "lessons.jsonl", max_lines=300)
    reports = read_jsonl(DATA_DIR / "market_reports.jsonl", max_lines=30)
    status = read_json(DATA_DIR / "learning_status.json") or {}
    return {"lessons": lessons[::-1], "reports": reports[::-1], "status": status,
            "key_set": bool(env("GEMINI_API_KEY"))}


# ---------- settings ----------

def settings():
    groups = []
    for title, keys in SETTINGS_SHOWN:
        items = [{"key": k, "value": os.environ[k]} for k in keys if k in os.environ]
        if items:
            groups.append({"title": title, "items": items})
    groups.append({"title": "Keys (sirf set hai ya nahi)",
                   "items": [{"key": k, "value": "set" if env(k) else "not set"} for k in SECRETS_PRESENCE]})
    return groups


def mode():
    dry, testnet = guard_on("DRY_RUN"), guard_on("USE_TESTNET")
    if dry:
        return {"mode": "PAPER", "label": "PAPER TRADING", "note": "Fake paisa, asli market prices. Koi order nahi jata."}
    if testnet:
        return {"mode": "TESTNET", "label": "TESTNET", "note": "Binance testnet par fake paisa."}
    return {"mode": "LIVE", "label": "LIVE - REAL MONEY", "note": "Asli paisa, asli exchange."}


# ---------- project health ----------

def health(agents, trips, log):
    checks = []

    def add(level, title, detail):
        checks.append({"level": level, "title": title, "detail": detail})

    stopped = [a["agent"] for a in agents if a["status"] in ("Stopped", "Pending") and a["agent"] != "learning_agent"]
    erroring = [a["agent"] for a in agents if a["status"] == "Error"]
    if stopped:
        add("bad", "Agents ruke hue hain", ", ".join(stopped) + " chal nahi rahe. Server par: sudo systemctl status 'crypto-*'")
    elif erroring:
        add("warn", "Agents mein error", ", ".join(erroring) + " error de rahe hain. Agents page dekhein.")
    else:
        add("good", "Chaaron agents chal rahe hain", "Sab heartbeat taaza hain.")

    rows = price_rows(hours=24 * 7)
    gaps = []
    for a, b in zip(rows, rows[1:]):
        if (b["t"] - a["t"]).total_seconds() > 20 * 60:
            gaps.append((a["t"], b["t"]))
    if not rows:
        add("bad", "Price data nahi mila", "data/price_history.csv khali ya missing hai.")
    elif gaps:
        longest = max(gaps, key=lambda g: g[1] - g[0])
        add("warn", f"Price data mein {len(gaps)} gap (7 din)",
            f"Sab se lamba: {(longest[1] - longest[0]).total_seconds() / 3600:.1f} ghante. Server ya internet band hua tha.")
    else:
        add("good", "Price data poora hai", "Pichle 7 din mein koi bara gap nahi.")

    starts = [r for r in log if r.get("action") == "engine start" and r.get("mode") == "DRY_RUN"]
    if starts:
        days = (datetime.now(timezone.utc) - starts[0]["_t"]).total_seconds() / 86400
        level = "good" if days >= 7 else "info"
        add(level, f"Paper trading: {days:.1f} din pooray", "Live paise se pehle kam se kam kuch hafte paper/testnet par chalana zaroori hai.")

    failed = [e for e in emails(200) if e["status"] == "failed"]
    skipped = [e for e in emails(200) if e["status"] == "skipped"]
    if failed:
        add("warn", f"{len(failed)} email fail hui", "Emails page par error dekhein (SMTP password ya limit).")
    if skipped:
        add("warn", f"{len(skipped)} email nahi gayi (SMTP set nahi)", "/etc/crypto-bot.env mein SMTP_USER aur SMTP_PASSWORD bharein.")

    stats = trade_stats(trips)
    if stats["trades"] < 10:
        add("info", f"Abhi sirf {stats['trades']} trade band hui hain",
            "4h strategy saal mein ~29 trade karti hai. Natija samajhne ke liye kam az kam 20-30 trade chahiye.")
    elif stats["win_rate"] is not None and stats["profit_factor"] is not None and stats["profit_factor"] < 1:
        add("bad", "Strategy abhi nuksan mein hai", f"Profit factor {stats['profit_factor']:.2f} (1 se kam = nuksan).")

    if not env("GEMINI_API_KEY"):
        add("warn", "Seekhne wala system band hai", "GEMINI_API_KEY set nahi. Deploy README ka Gemini step dekhein.")

    try:
        usage = shutil.disk_usage(BOT_DIR)
        free_gb = usage.free / 1e9
        add("good" if free_gb > 2 else "bad", f"Server disk: {free_gb:.1f} GB khali", "2 GB se kam ho to purana data saaf karna hoga.")
    except OSError:
        pass

    # what the project is still missing (known gaps, not computed)
    add("info", "Sirf 1 coin aur 1 strategy", f"Abhi sirf {env('SYMBOL', 'BTC/USDT')} aur moving-average crossover. Aage aur coins/strategies.")
    add("info", "Data ka backup nahi", "data/ folder sirf server par hai. Hafte mein ek baar laptop par copy karna behtar.")
    if guard_on("DRY_RUN"):
        add("info", "Live order path abhi test nahi", "Paper mode mein koi order nahi jata. Live se pehle testnet par order path dobara test karna hai.")
    return checks
