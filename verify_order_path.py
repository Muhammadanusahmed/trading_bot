"""
Exercises execution_engine's LIVE order path (buy -> exchange-side stop-loss
-> cancel/sell) against the Binance TESTNET only, using fake money.

Refuses to run unless USE_TESTNET=true. Uses throwaway state/log files so it
never touches the real trade_state.json / data/trade_log.jsonl.
"""

import os
import sys
import tempfile
from pathlib import Path

if os.environ.get("USE_TESTNET", "true").lower() != "true":
    sys.exit("Refusing to run: USE_TESTNET is not true.")

os.environ["DRY_RUN"] = "false"  # this script's whole point is to place (testnet) orders
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import execution_engine as ee  # noqa: E402  (must import after DRY_RUN is set)

assert ee.USE_TESTNET and not ee.DRY_RUN

tmp = Path(tempfile.mkdtemp())
ee.STATE_FILE = tmp / "trade_state.json"
ee.TRADE_LOG = tmp / "trade_log.jsonl"

exchange = ee.make_exchange()
state = ee.load_state()
base = ee.SYMBOL.split("/")[0]

before = exchange.fetch_balance()["total"].get(base, 0)
price = exchange.fetch_ticker(ee.SYMBOL)["last"]
print(f"[1] {base} balance before: {before} | market price: {price}")

ee.enter_position(exchange, state, price)
print(f"[2] entered. state: {state}")

open_orders = exchange.fetch_open_orders(ee.SYMBOL)
print(f"[3] open orders on exchange after entry: {len(open_orders)}")
for o in open_orders:
    print(f"    id={o['id']} type={o['type']} side={o['side']} amount={o['amount']} "
          f"price={o['price']} stopPrice={o.get('stopPrice')} status={o['status']}")
assert any(o["id"] == state["stop_order_id"] for o in open_orders), \
    "the stop-loss order is NOT sitting on the exchange - protection is not in place"

ee.exit_position(exchange, state, exchange.fetch_ticker(ee.SYMBOL)["last"])
remaining = exchange.fetch_open_orders(ee.SYMBOL)
after = exchange.fetch_balance()["total"].get(base, 0)
print(f"[4] exited. state: {state}")
print(f"[5] open orders after exit: {len(remaining)} | {base} balance after: {after}")
assert not remaining, "a stop order was left behind after exit"
print("\nORDER PATH OK: buy, exchange-side stop-loss, cancel, sell all worked on testnet.")
