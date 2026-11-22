"""
Rule lab: tests a suggested rule on 2017-now BTC history before a human
decides whether to use it.

The learning agent may only suggest rules from RULES below, because those are
the ones this file knows how to backtest. The base strategy is the live one:
4h candles, fast/slow MA crossover on closed candles, next-open fills, 0.1%
fee per side, STOP_LOSS_PERCENT stop, and after a stop-out wait for the
signal to go flat before buying again.

Nothing here changes the bot. It only reports numbers.
"""

import os
from functools import lru_cache

import pandas as pd

FEE = float(os.environ.get("FEE_PERCENT", 0.1)) / 100
FAST = int(os.environ.get("FAST_WINDOW", 10))
SLOW = int(os.environ.get("SLOW_WINDOW", 50))
STOP = float(os.environ.get("STOP_LOSS_PERCENT", 2.0))
TIMEFRAME = os.environ.get("TIMEFRAME", "4h")
IN_SAMPLE_END = "2022-12-31"
OUT_OF_SAMPLE_START = "2023-01-01"

# name: (meaning, min, max)
RULES = {
    "min_ma_gap_percent": ("Buy only if the fast MA is at least this % above the slow MA (skips weak crossovers)", 0.05, 5),
    "trend_filter_ma": ("Buy only if the price is above the N-candle moving average (e.g. 200)", 20, 400),
    "stop_loss_percent": ("Use this stop-loss % instead of the current one", 0.5, 10),
    "cooldown_hours_after_loss": ("After a losing trade, wait this many hours before buying again", 4, 240),
    "max_volatility_percent": ("Skip buys when the 14-candle average range (ATR) is above this % of price", 0.5, 10),
    "min_range_percent": ("Skip buys when the last 20 candles moved less than this % high-to-low (sideways market)", 1, 20),
}


def rules_menu_text():
    return "\n".join(f'- "{name}": {desc} (allowed {lo} to {hi})' for name, (desc, lo, hi) in RULES.items())


def valid_rule(rule):
    """Return a clean {"name", "value", "why"} or None."""
    if not isinstance(rule, dict) or rule.get("name") not in RULES:
        return None
    try:
        value = float(rule.get("value"))
    except (TypeError, ValueError):
        return None
    _, lo, hi = RULES[rule["name"]]
    if not lo <= value <= hi:
        return None
    return {"name": rule["name"], "value": value, "why": str(rule.get("why", ""))[:400]}


@lru_cache(maxsize=1)
def candles():
    """4h candles since 2017 (1h Binance files cached in data/binance_klines by long_backtest.py)."""
    import long_backtest  # downloads missing months once, then reads the cache
    hourly = long_backtest.load_candles()
    df = long_backtest.resample(hourly, TIMEFRAME) if TIMEFRAME != "1h" else hourly
    return add_features(df)


def add_features(df):
    df = df.copy()
    df["fast"] = df["close"].rolling(FAST).mean()
    df["slow"] = df["close"].rolling(SLOW).mean()
    df["gap_pct"] = (df["fast"] - df["slow"]) / df["slow"] * 100
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    df["atr_pct"] = tr.rolling(14).mean() / df["close"] * 100
    df["range20_pct"] = (df["high"].rolling(20).max() - df["low"].rolling(20).min()) / df["close"] * 100
    df["slope_pct"] = df["slow"].pct_change(10) * 100  # how fast the slow MA is moving
    return df


def market_type(row):
    """Simple label for the market at one candle."""
    if pd.isna(row.get("slow")):
        return "unknown"
    if row["atr_pct"] > 2.5:
        return "volatile"
    if abs(row["slope_pct"]) < 1 and row["range20_pct"] < 8:
        return "sideways"
    return "trend up" if row["slope_pct"] > 0 else "trend down"


def simulate(df, rule=None):
    """Live strategy (+ optional rule). Returns (stats, trades)."""
    name = rule["name"] if rule else None
    value = rule["value"] if rule else None
    stop_pct = value if name == "stop_loss_percent" else STOP
    trend_ma = df["close"].rolling(int(value)).mean().to_numpy() if name == "trend_filter_ma" else None

    signal = (df["fast"] > df["slow"]).astype(int).shift(1).fillna(0).to_numpy()
    # filters look at the last CLOSED candle, like the signal
    prev = df.shift(1)
    gap, atr, rng = prev["gap_pct"].to_numpy(), prev["atr_pct"].to_numpy(), prev["range20_pct"].to_numpy()
    prev_close = prev["close"].to_numpy()
    opens, lows, closes = df["open"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    times = df.index

    cash, coin, stop_price, entry_value, entry_i = 1000.0, 0.0, None, 0.0, None
    blocked, cooldown_until = False, None
    trades, equity = [], []

    def close_trade(i, price, kind):
        nonlocal cash, coin, cooldown_until
        value_out = coin * price * (1 - FEE)
        pnl_pct = (value_out / entry_value - 1) * 100
        trades.append({"entry_i": entry_i, "exit_i": i, "pnl_pct": pnl_pct, "exit": kind,
                       "held_h": (times[i] - times[entry_i]).total_seconds() / 3600})
        cash, coin = value_out, 0.0
        if name == "cooldown_hours_after_loss" and pnl_pct < 0:
            cooldown_until = times[i] + pd.Timedelta(hours=value)

    for i in range(len(df)):
        want = signal[i]
        if want == 0:
            blocked = False
        if want == 1 and coin == 0 and not blocked:
            ok = True
            if name == "min_ma_gap_percent":
                ok = gap[i] >= value
            elif name == "trend_filter_ma":
                ok = i > 0 and not pd.isna(trend_ma[i - 1]) and prev_close[i] > trend_ma[i - 1]
            elif name == "max_volatility_percent":
                ok = atr[i] <= value
            elif name == "min_range_percent":
                ok = rng[i] >= value
            elif name == "cooldown_hours_after_loss" and cooldown_until is not None:
                ok = times[i] >= cooldown_until
            if ok:
                entry_value, entry_i = cash, i
                coin, cash = cash * (1 - FEE) / opens[i], 0.0
                stop_price = opens[i] * (1 - stop_pct / 100)
        elif want == 0 and coin > 0:
            close_trade(i, opens[i], "signal")
        if coin > 0 and lows[i] <= stop_price:
            close_trade(i, min(stop_price, opens[i]), "stop")
            blocked = True
        equity.append(cash + coin * closes[i])

    eq = pd.Series(equity, index=df.index)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    return {
        "return": (eq.iloc[-1] / 1000 - 1) * 100 if len(eq) else 0.0,
        "max_dd": ((eq / eq.cummax()) - 1).min() * 100 if len(eq) else 0.0,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0.0,
    }, trades


def compare(rule):
    """Current strategy vs current strategy + rule, on 2023-now and 2017-now."""
    df = candles()
    oos = df.loc[OUT_OF_SAMPLE_START:]
    base_full, _ = simulate(df)
    new_full, _ = simulate(df, rule)
    base_oos, _ = simulate(oos)
    new_oos, _ = simulate(oos, rule)
    diff = new_oos["return"] - base_oos["return"]
    if diff > 2 and new_oos["max_dd"] >= base_oos["max_dd"] - 3:
        verdict = "better"
    elif diff < -2:
        verdict = "worse"
    else:
        verdict = "same"
    pack = lambda full, oos_: {"full_return": round(float(full["return"]), 1), "oos_return": round(float(oos_["return"]), 1),
                               "oos_max_dd": round(float(oos_["max_dd"]), 1), "oos_trades": int(oos_["trades"]),
                               "oos_win_rate": round(float(oos_["win_rate"]), 0)}
    return {"verdict": verdict, "base": pack(base_full, base_oos), "new": pack(new_full, new_oos),
            "period": f"{OUT_OF_SAMPLE_START[:4]}-now (2017-now also shown)"}


def losing_trade_study(df=None):
    """Statistics about the base strategy's losing trades in 2017-2022 only,
    so rules suggested from them can be judged fairly on 2023-now."""
    df = df if df is not None else candles().loc[:IN_SAMPLE_END]
    stats, trades = simulate(df)
    rows = []
    for t in trades:
        r = df.iloc[t["entry_i"] - 1] if t["entry_i"] else df.iloc[0]
        rows.append({**t, "market": market_type(r), "gap_pct": r["gap_pct"], "atr_pct": r["atr_pct"],
                     "range20_pct": r["range20_pct"]})
    tdf = pd.DataFrame(rows)
    if tdf.empty:
        return {"overall": stats}
    tdf["loss"] = tdf["pnl_pct"] < 0
    by_market = tdf.groupby("market").agg(trades=("loss", "size"), loss_rate=("loss", "mean"),
                                          avg_pnl_pct=("pnl_pct", "mean")).round(3)
    by_exit = tdf.groupby("exit").agg(trades=("loss", "size"), loss_rate=("loss", "mean"),
                                      avg_pnl_pct=("pnl_pct", "mean")).round(3)
    tdf["gap_bucket"] = pd.cut(tdf["gap_pct"], [-100, 0.25, 0.5, 1, 2, 100]).astype(str)
    by_gap = tdf.groupby("gap_bucket").agg(trades=("loss", "size"), loss_rate=("loss", "mean"),
                                           avg_pnl_pct=("pnl_pct", "mean")).round(3)
    tdf["held_bucket"] = pd.cut(tdf["held_h"], [0, 8, 24, 72, 1e9], labels=["<8h", "8-24h", "1-3d", ">3d"]).astype(str)
    by_held = tdf.groupby("held_bucket").agg(trades=("loss", "size"), loss_rate=("loss", "mean"),
                                             avg_pnl_pct=("pnl_pct", "mean")).round(3)
    losers = tdf[tdf["loss"]]
    return {
        "period": f"{df.index[0]:%Y-%m} to {df.index[-1]:%Y-%m}",
        "overall": {k: round(v, 2) for k, v in stats.items()},
        "losing_trades": int(len(losers)),
        "avg_loss_pct": round(losers["pnl_pct"].mean(), 2) if len(losers) else None,
        "avg_win_pct": round(tdf[~tdf["loss"]]["pnl_pct"].mean(), 2),
        "by_market_type": by_market.to_dict("index"),
        "by_exit_type": by_exit.to_dict("index"),
        "by_ma_gap_at_entry_pct": by_gap.to_dict("index"),
        "by_time_held": by_held.to_dict("index"),
        "median_atr_pct_losers": round(losers["atr_pct"].median(), 2) if len(losers) else None,
        "median_atr_pct_winners": round(tdf[~tdf["loss"]]["atr_pct"].median(), 2),
        "median_range20_pct_losers": round(losers["range20_pct"].median(), 2) if len(losers) else None,
        "median_range20_pct_winners": round(tdf[~tdf["loss"]]["range20_pct"].median(), 2),
    }
