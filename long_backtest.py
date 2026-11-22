"""
Long-history backtest of the live strategy (MA crossover + exchange stop-loss).

Unlike backtest.py (last ~1000 candles, no stop), this:
- downloads every BTC/USDT 1h candle since 2017 from Binance's free public
  data files (data.binance.vision, no API key) and caches them in
  data/binance_klines/, so later runs are instant
- decides on CLOSED candles only and trades at the next candle's open
  (no look-ahead)
- models the live bot's 2% stop-loss: if a candle's low touches the stop,
  the position is sold at the stop price, or at the open if price gapped
  below it (a stop-limit might not fill at all in a gap - this assumes it does)
- re-enters on the next candle if the MA signal is still "long", which is
  what the live bot does after reconcile() sees the stop filled
- reports each market regime separately (2018 crash, 2020 COVID drop, 2021
  bull, 2022 bear, ...) and checks whether the 10/50 windows hold up on
  years they weren't picked on

Run:  python long_backtest.py
Output: printed tables, data/long_backtest_results.csv,
        data/long_backtest_timeframes.csv, long_backtest_result.png
"""

import io
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pandas as pd

SYMBOL = "BTCUSDT"
INTERVAL = "1h"
START = (2017, 8)          # BTCUSDT spot history starts Aug 2017
FEE = 0.001                # 0.1% per side, like backtest.py
FAST, SLOW = 10, 50        # the live bot's windows
STOP_PERCENT = 2.0         # the live bot's STOP_LOSS_PERCENT
STARTING_CASH = 1000.0

BASE_DIR = Path(__file__).parent
CACHE_DIR = BASE_DIR / "data" / "binance_klines"
RESULTS_CSV = BASE_DIR / "data" / "long_backtest_results.csv"
CHART_FILE = BASE_DIR / "long_backtest_result.png"
URL = "https://data.binance.vision/data/spot/monthly/klines/{s}/{i}/{s}-{i}-{y}-{m:02d}.zip"

REGIMES = [
    ("2018 crash",        "2018-01-01", "2018-12-31"),
    ("2019 recovery",     "2019-01-01", "2019-12-31"),
    ("2020 COVID + bull", "2020-01-01", "2020-12-31"),
    ("2021 bull top",     "2021-01-01", "2021-12-31"),
    ("2022 bear (LUNA/FTX)", "2022-01-01", "2022-12-31"),
    ("2023 recovery",     "2023-01-01", "2023-12-31"),
    ("2024-now",          "2024-01-01", "2100-01-01"),
]
IN_SAMPLE = ("2017-08-01", "2022-12-31")   # windows are chosen here...
OUT_OF_SAMPLE = ("2023-01-01", "2100-01-01")  # ...and checked here


# ---- 1. data ----

def months_to_fetch():
    y, m = START
    now = time.gmtime()
    while (y, m) < (now.tm_year, now.tm_mon):  # monthly files appear after the month ends
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def download_month(y, m):
    path = CACHE_DIR / f"{SYMBOL}-{INTERVAL}-{y}-{m:02d}.csv"
    if path.exists():
        return path
    url = URL.format(s=SYMBOL, i=INTERVAL, y=y, m=m)
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:  # not published yet (e.g. last month before the first Monday)
            return None
        raise
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        path.write_bytes(z.read(z.namelist()[0]))
    return path


def load_candles():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    frames = []
    for y, m in months_to_fetch():
        path = download_month(y, m)
        if path is None:
            continue
        df = pd.read_csv(path, header=None, usecols=range(6))
        if not str(df.iloc[0, 0]).isdigit():  # some files have a header row
            df = df.iloc[1:]
        frames.append(df)
        print(f"\rloaded {y}-{m:02d}", end="", flush=True)
    print()

    df = pd.concat(frames, ignore_index=True)
    df.columns = ["open_time", "open", "high", "low", "close", "volume"]
    df = df.astype(float)
    # Binance switched spot timestamps from milliseconds to microseconds on 2025-01-01
    df["open_time"] = df["open_time"].where(df["open_time"] < 1e14, df["open_time"] / 1000)
    df.index = pd.to_datetime(df["open_time"], unit="ms")
    df = df[~df.index.duplicated()].sort_index()
    return df[["open", "high", "low", "close", "volume"]]


# ---- 2. simulation ----

def simulate(df, fast=FAST, slow=SLOW, stop_percent=STOP_PERCENT, reenter_after_stop=True):
    """Returns (equity series, stats dict). Signal from candle i's close is
    acted on at candle i+1's open."""
    signal = (df["close"].rolling(fast).mean() > df["close"].rolling(slow).mean()).astype(int)
    signal = signal.shift(1).fillna(0).astype(int).to_numpy()  # yesterday's closed-candle signal
    opens, highs, lows, closes = (df[c].to_numpy() for c in ("open", "high", "low", "close"))

    cash, coin, stop = STARTING_CASH, 0.0, None
    entry_value = 0.0
    trades = stops = wins = 0
    fees = 0.0
    blocked = False  # after a stop-out, wait for the signal to go flat first (if not re-entering)
    equity = []

    for i in range(len(df)):
        want = signal[i]
        if want == 0:
            blocked = False

        if want == 1 and coin == 0 and not blocked:
            price = opens[i]
            fees += cash * FEE
            coin, entry_value, cash = cash * (1 - FEE) / price, cash, 0.0
            stop = price * (1 - stop_percent / 100) if stop_percent else None
            trades += 1
        elif want == 0 and coin > 0:
            value = coin * opens[i]
            fees += value * FEE
            cash, coin = value * (1 - FEE), 0.0
            wins += cash > entry_value

        if coin > 0 and stop is not None and lows[i] <= stop:
            fill = min(stop, opens[i])  # gapped below the stop -> worse fill
            value = coin * fill
            fees += value * FEE
            cash, coin = value * (1 - FEE), 0.0
            wins += cash > entry_value
            stops += 1
            blocked = not reenter_after_stop

        equity.append(cash + coin * closes[i])

    equity = pd.Series(equity, index=df.index)
    closed = trades - (1 if coin > 0 else 0)
    return equity, {
        "return_%": (equity.iloc[-1] / STARTING_CASH - 1) * 100,
        "buy_hold_%": (closes[-1] / opens[0] - 1) * 100,
        "max_drawdown_%": ((equity / equity.cummax()) - 1).min() * 100,
        "trades": trades,
        "stop_outs": stops,
        "win_rate_%": wins / closed * 100 if closed else 0.0,
        "fees_paid": fees,
    }


def pct(x):
    return f"{x:+.1f}%"


def print_table(rows, cols):
    widths = [max(len(str(c)), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))


# ---- 3. reports ----

def report_regimes(df):
    variants = [
        ("no stop (old backtest)", dict(stop_percent=0)),
        ("2% stop, re-enter (live bot)", dict(stop_percent=STOP_PERCENT)),
        ("2% stop, wait for new crossover", dict(stop_percent=STOP_PERCENT, reenter_after_stop=False)),
        ("5% stop, re-enter", dict(stop_percent=5.0)),
    ]
    rows, curves = [], {}
    for name, start, end in [("FULL HISTORY", None, None)] + REGIMES:
        part = df.loc[start:end] if start else df
        if len(part) < SLOW * 2:
            continue
        for vname, kw in variants:
            eq, s = simulate(part, **kw)
            rows.append({"period": name, "variant": vname, **s})
            if name == "FULL HISTORY":
                curves[vname] = eq
    table = [{
        "period": r["period"], "variant": r["variant"], "return": pct(r["return_%"]),
        "buy&hold": pct(r["buy_hold_%"]), "max DD": pct(r["max_drawdown_%"]),
        "trades": r["trades"], "stop-outs": r["stop_outs"], "win rate": f"{r['win_rate_%']:.0f}%",
        "fees": f"${r['fees_paid']:.0f}",
    } for r in rows]
    print(f"\n=== {FAST}/{SLOW} MA crossover, $1000 start, fee {FEE:.1%}/side ===\n")
    print_table(table, list(table[0].keys()))
    return rows, curves


def report_window_robustness(df):
    fasts, slows = [5, 10, 20, 30, 50], [30, 50, 100, 150, 200]
    ins = df.loc[IN_SAMPLE[0]:IN_SAMPLE[1]]
    outs = df.loc[OUT_OF_SAMPLE[0]:OUT_OF_SAMPLE[1]]
    rows = []
    for f in fasts:
        for s in slows:
            if f >= s:
                continue
            _, a = simulate(ins, f, s)
            _, b = simulate(outs, f, s)
            rows.append({"fast/slow": f"{f}/{s}", "in-sample": a["return_%"], "out-of-sample": b["return_%"],
                         "oos max DD": b["max_drawdown_%"], "oos trades": b["trades"]})
    rows.sort(key=lambda r: r["in-sample"], reverse=True)
    for rank, r in enumerate(rows, 1):
        r["rank"] = rank
    print(f"\n=== Window check (2% stop): best on {IN_SAMPLE[0][:4]}-{IN_SAMPLE[1][:4]}, "
          f"then tested on {OUT_OF_SAMPLE[0][:4]}-now ===\n")
    print_table([{
        "rank": r["rank"], "fast/slow": r["fast/slow"], "in-sample": pct(r["in-sample"]),
        "out-of-sample": pct(r["out-of-sample"]), "oos max DD": pct(r["oos max DD"]), "oos trades": r["oos trades"],
    } for r in rows], ["rank", "fast/slow", "in-sample", "out-of-sample", "oos max DD", "oos trades"])
    _, bh = simulate(outs, 1, 2, stop_percent=0)
    print(f"\n(out-of-sample buy & hold: {pct(bh['buy_hold_%'])})")
    return rows


def resample(df, rule):
    return df.resample(rule).agg({"open": "first", "high": "max", "low": "min",
                                  "close": "last", "volume": "sum"}).dropna()


def report_timeframes(df):
    """Same strategy on slower candles: fewer trades, so fees and whipsaw cost less."""
    windows = [(10, 30), (10, 50), (20, 50), (20, 100), (50, 200)]
    stops = [("no stop", dict(stop_percent=0)),
             ("5% stop", dict(stop_percent=5.0)),
             ("2% stop, wait", dict(stop_percent=2.0, reenter_after_stop=False))]
    rows = []
    for tf, rule in [("1h", None), ("4h", "4h"), ("1d", "1D")]:
        candles = resample(df, rule) if rule else df
        ins = candles.loc[IN_SAMPLE[0]:IN_SAMPLE[1]]
        outs = candles.loc[OUT_OF_SAMPLE[0]:OUT_OF_SAMPLE[1]]
        years = (outs.index[-1] - outs.index[0]).days / 365
        for f, s in windows:
            for sname, kw in stops:
                _, a = simulate(ins, f, s, **kw)
                _, b = simulate(outs, f, s, **kw)
                rows.append({"tf": tf, "fast/slow": f"{f}/{s}", "stop": sname,
                             "in-sample": pct(a["return_%"]), "out-of-sample": pct(b["return_%"]),
                             "oos max DD": pct(b["max_drawdown_%"]), "oos trades/yr": f"{b['trades'] / years:.0f}",
                             "_oos": b["return_%"]})
    print(f"\n=== Slower timeframes: {IN_SAMPLE[0][:4]}-{IN_SAMPLE[1][:4]} vs {OUT_OF_SAMPLE[0][:4]}-now ===\n")
    print_table(rows, ["tf", "fast/slow", "stop", "in-sample", "out-of-sample", "oos max DD", "oos trades/yr"])
    _, bh = simulate(df.loc[OUT_OF_SAMPLE[0]:], 1, 2, stop_percent=0)
    print(f"\n(out-of-sample buy & hold: {pct(bh['buy_hold_%'])})")
    best = sorted(rows, key=lambda r: r["_oos"], reverse=True)[:5]
    print("Best 5 out-of-sample: " + "; ".join(f"{r['tf']} {r['fast/slow']} {r['stop']} {r['out-of-sample']}" for r in best))
    return rows


def save_chart(df, curves):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(12, 6))
    hold = STARTING_CASH * df["close"] / df["open"].iloc[0]
    ax.plot(hold.index, hold, label="buy & hold", color="#999999", linewidth=1)
    for name, eq in curves.items():
        ax.plot(eq.index, eq, label=name, linewidth=1.2)
    ax.set_yscale("log")
    ax.set_ylabel("portfolio value, $ (log scale)")
    ax.set_title(f"{SYMBOL} {INTERVAL}, {FAST}/{SLOW} MA crossover since {df.index[0]:%Y-%m}")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(CHART_FILE, dpi=120)


if __name__ == "__main__":
    candles = load_candles()
    print(f"{len(candles):,} candles, {candles.index[0]:%Y-%m-%d} to {candles.index[-1]:%Y-%m-%d}")
    regime_rows, curves = report_regimes(candles)
    report_window_robustness(candles)
    timeframe_rows = report_timeframes(candles)
    pd.DataFrame(regime_rows).to_csv(RESULTS_CSV, index=False)
    pd.DataFrame(timeframe_rows).drop(columns="_oos").to_csv(RESULTS_CSV.with_name("long_backtest_timeframes.csv"), index=False)
    save_chart(candles, curves)
    print(f"\nSaved {RESULTS_CSV.name} and {CHART_FILE.name}")
