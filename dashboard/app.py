"""
Bot dashboard: one small web app for mobile and laptop.

Read-only by design: it only reads the bot's files (see data_reader.py) and
has no route that changes the bot. On the server systemd also mounts the bot
folder read-only for this service, so even a bug here can't touch a trade.

One login (DASHBOARD_USER / DASHBOARD_PASSWORD_HASH from /etc/crypto-bot.env;
make the hash with: python dashboard/set_password.py). After 5 wrong
passwords in 15 minutes logins are locked for 15 minutes.

Run:  python dashboard/app.py      (listens on 127.0.0.1:8787 by default)
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data_reader as dr  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATE_DIR = Path(os.environ.get("DASHBOARD_STATE_DIR", Path(__file__).resolve().parent / ".state"))
LOGIN_LOG = STATE_DIR / "login_log.jsonl"

USER = os.environ.get("DASHBOARD_USER", "admin")
PASSWORD_HASH = os.environ.get("DASHBOARD_PASSWORD_HASH", "")
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60

app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=os.environ.get("DASHBOARD_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("DASHBOARD_COOKIE_SECURE", "true").lower() != "false",
    SESSION_COOKIE_NAME="botdash",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
_fails = []  # times of recent wrong passwords


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt:{salt.hex()}:{digest.hex()}"


def check_password(password, stored):
    try:
        algo, salt_hex, digest_hex = stored.split(":")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def log_login(ok, user):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGIN_LOG, "a") as f:
            f.write(json.dumps({"time": dr.iso(dr.datetime.now(dr.timezone.utc)), "ok": ok, "user": user[:40],
                                "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
                                "agent": request.headers.get("User-Agent", "")[:120]}) + "\n")
    except OSError:
        pass


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return jsonify({"error": "login required"}), 401
        return view(*args, **kwargs)
    return wrapper


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- pages and static files ----------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/sw.js")
def service_worker():
    resp = send_from_directory(STATIC_DIR, "sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC_DIR, name)


# ---------- auth ----------

@app.post("/api/login")
def login():
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < LOCK_SECONDS]
    if len(_fails) >= MAX_FAILS:
        wait = int(LOCK_SECONDS - (now - _fails[0])) // 60 + 1
        return jsonify({"error": f"Bohat ghalat koshishen. {wait} minute baad dobara try karein."}), 429
    body = request.get_json(silent=True) or {}
    user, password = str(body.get("user", "")), str(body.get("password", ""))
    if not PASSWORD_HASH:
        return jsonify({"error": "Server par password set nahi (DASHBOARD_PASSWORD_HASH)."}), 503
    ok = hmac.compare_digest(user, USER) & check_password(password, PASSWORD_HASH)
    log_login(ok, user)
    if not ok:
        _fails.append(now)
        time.sleep(1)  # slows down guessing
        return jsonify({"error": "Username ya password ghalat hai."}), 401
    session.clear()
    session.permanent = True
    session["user"] = USER
    return jsonify({"ok": True, "user": USER})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    return jsonify({"user": session.get("user")})


# ---------- data ----------

@app.get("/api/summary")
@login_required
def summary():
    log = dr.trade_log()
    latest = dr.latest_price()
    price = latest["price"] if latest else None
    pos = dr.open_position(price)
    start = dr.starting_balance(log)
    curve, realized_balance, equity, max_dd = dr.equity_curve(log, start, pos.get("unrealized_pnl"))
    trips = dr.round_trips(log)
    day_rows = dr.price_rows(hours=25)
    change_24h = None
    if day_rows and price:
        ref = day_rows[0]["price"]
        change_24h = (price - ref) / ref * 100
    decisions = dr.decisions(limit=1)
    return jsonify({
        "mode": dr.mode(),
        "symbol": dr.env("SYMBOL", "BTC/USDT"),
        "starting_balance": start,
        "realized_balance": realized_balance,
        "equity": equity,
        "total_pnl": equity - start,
        "total_pnl_percent": (equity / start - 1) * 100 if start else None,
        "realized_pnl": realized_balance - start,
        "periods": dr.period_totals(log),
        "max_drawdown_percent": max_dd,
        "position": pos,
        "price": price,
        "price_time": dr.iso(latest["t"]) if latest else None,
        "change_24h": change_24h,
        "stats": dr.trade_stats(trips),
        "agents": dr.agent_statuses(),
        "last_decision": decisions[0] if decisions else None,
    })


@app.get("/api/charts")
@login_required
def charts():
    period = request.args.get("period", "day")
    if period not in ("day", "week", "month"):
        period = "day"
    hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(request.args.get("range", "7d"), 24 * 7)
    log = dr.trade_log()
    latest = dr.latest_price()
    pos = dr.open_position(latest["price"] if latest else None)
    curve, _, _, max_dd = dr.equity_curve(log, dr.starting_balance(log), pos.get("unrealized_pnl"))
    prices = dr.downsample(dr.price_rows(hours=hours))
    since = prices[0]["t"] if prices else None
    markers = []
    for rec in log:
        action = rec.get("action", "")
        if since and rec["_t"] >= since and rec.get("price") and (
                action in dr.BUY_ACTIONS or action in dr.CLOSE_ACTIONS):
            markers.append({"t": dr.iso(rec["_t"]), "price": rec["price"],
                            "side": "buy" if action in dr.BUY_ACTIONS else "sell", "action": action})
    return jsonify({
        "pnl": dr.pnl_buckets(log, period),
        "period": period,
        "equity": curve,
        "max_drawdown_percent": max_dd,
        "prices": [{"t": dr.iso(p["t"]), "price": p["price"]} for p in prices],
        "markers": markers,
        "position": pos,
    })


@app.get("/api/agents")
@login_required
def agents():
    return jsonify({"agents": dr.agent_statuses()})


@app.get("/api/market")
@login_required
def market():
    latest = dr.latest_price()
    return jsonify({
        "symbol": dr.env("SYMBOL", "BTC/USDT"),
        "latest": {k: (dr.iso(v) if k == "t" else v) for k, v in latest.items()} if latest else None,
        "news": dr.news(40),
        "anomalies": dr.anomalies(30),
        "anomaly_flag": dr.read_json(dr.BOT_DIR / "anomaly_flag.json"),
    })


@app.get("/api/decisions")
@login_required
def decisions():
    return jsonify({"decisions": dr.decisions(300)})


@app.get("/api/trades")
@login_required
def trades():
    trips = dr.round_trips(dr.trade_log())
    lessons = {l.get("trade_id"): l for l in dr.learning()["lessons"] if l.get("trade_id")}
    for t in trips:
        lesson = lessons.get(t["id"])
        t["lesson"] = {"reason": lesson.get("reason"), "lesson": lesson.get("lesson")} if lesson else None
    return jsonify({"trades": trips[::-1], "stats": dr.trade_stats(trips)})


@app.get("/api/learning")
@login_required
def learning():
    return jsonify(dr.learning())


@app.get("/api/health")
@login_required
def health():
    log = dr.trade_log()
    return jsonify({"checks": dr.health(dr.agent_statuses(), dr.round_trips(log), log)})


@app.get("/api/emails")
@login_required
def emails():
    return jsonify({"emails": dr.emails(500)})


@app.get("/api/settings")
@login_required
def settings():
    logins = dr.read_jsonl(LOGIN_LOG, max_lines=20)[::-1]
    return jsonify({"groups": dr.settings(), "mode": dr.mode(), "logins": logins})


if __name__ == "__main__":
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", 8787))
    if not PASSWORD_HASH:
        print("WARNING: DASHBOARD_PASSWORD_HASH is not set - nobody can log in. Run dashboard/set_password.py")
    try:
        from waitress import serve
        print(f"Dashboard on http://{host}:{port}")
        serve(app, host=host, port=port, threads=4, trusted_proxy="127.0.0.1",
              trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"})
    except ImportError:
        app.run(host=host, port=port)
