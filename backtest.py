"""
Training-wheels trend-following bot.

Strategy: Moving Average Crossover
- FAST average > SLOW average  -> price is trending up -> hold the coin
- FAST average < SLOW average  -> price is trending down -> stay in cash

This is a BACKTEST only. No real money, no exchange account needed.
"""

import ccxt
import pandas as pd
import matplotlib.pyplot as plt

# ---- 1. Settings (change these to experiment) ----
EXCHANGE = "binance"
SYMBOL = "BTC/USDT"
TIMEFRAME = "1h"       # candle size: 1h, 4h, 1d, etc.
CANDLES = 1000         # how many past candles to fetch
FAST_WINDOW = 10        # short moving average
SLOW_WINDOW = 50        # long moving average
FEE = 0.001             # 0.1% per trade, like a real exchange fee
STARTING_CASH = 1000    # pretend money


def fetch_data():
    # fetchMarkets restricted to spot: Binance's futures endpoint (fapi.binance.com)
    # times out on some networks, and we only need spot price data anyway.
    exchange = getattr(ccxt, EXCHANGE)({"options": {"fetchMarkets": ["spot"]}})
    raw = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=CANDLES)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def add_signals(df):
    df["fast_ma"] = df["close"].rolling(FAST_WINDOW).mean()
    df["slow_ma"] = df["close"].rolling(SLOW_WINDOW).mean()
    # 1 = be in the coin, 0 = be in cash
    df["position"] = (df["fast_ma"] > df["slow_ma"]).astype(int)
    return df


def run_backtest(df):
    df = df.dropna(subset=["fast_ma", "slow_ma"]).copy()

    cash = STARTING_CASH
    coin = 0.0
    equity_curve = []
    trades = 0
    prev_position = 0

    for _, row in df.iterrows():
        price = row["close"]
        target_position = row["position"]

        # position changed -> execute a trade
        if target_position != prev_position:
            trades += 1
            if target_position == 1:
                coin = (cash * (1 - FEE)) / price
                cash = 0.0
            else:
                cash = coin * price * (1 - FEE)
                coin = 0.0
            prev_position = target_position

        equity_curve.append(cash + coin * price)

    df["equity"] = equity_curve

    final_equity = df["equity"].iloc[-1]
    buy_and_hold = STARTING_CASH / df["close"].iloc[0] * df["close"].iloc[-1]
    running_max = df["equity"].cummax()
    max_drawdown = ((df["equity"] - running_max) / running_max).min()

    print(f"Symbol:            {SYMBOL} ({TIMEFRAME})")
    print(f"Candles used:      {len(df)}")
    print(f"Trades made:       {trades}")
    print(f"Starting cash:     {STARTING_CASH:.2f}")
    print(f"Strategy result:   {final_equity:.2f}  ({(final_equity/STARTING_CASH - 1) * 100:.1f}%)")
    print(f"Buy & hold result: {buy_and_hold:.2f}  ({(buy_and_hold/STARTING_CASH - 1) * 100:.1f}%)")
    print(f"Max drawdown:      {max_drawdown * 100:.1f}%")

    return df


def plot(df):
    plt.figure(figsize=(11, 6))
    plt.plot(df.index, df["equity"], label="Strategy (fake money)")
    plt.plot(df.index, STARTING_CASH / df["close"].iloc[0] * df["close"], label="Buy & hold", alpha=0.7)
    plt.title(f"{SYMBOL} - MA Crossover ({FAST_WINDOW}/{SLOW_WINDOW}) Backtest")
    plt.xlabel("Time")
    plt.ylabel("Portfolio value")
    plt.legend()
    plt.tight_layout()
    plt.savefig("backtest_result.png")
    print("\nChart saved to backtest_result.png")


if __name__ == "__main__":
    data = fetch_data()
    data = add_signals(data)
    data = run_backtest(data)
    plot(data)
