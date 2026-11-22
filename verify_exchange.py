"""
Read-only sanity check for the exchange keys in .env. Never prints keys.

Refuses to run unless USE_TESTNET=true, so it can't touch a real account.
Stage 1 (default): authenticate and print non-zero balances. Places no orders.
"""

import os
import sys
import ccxt
from dotenv import load_dotenv

load_dotenv()

if os.environ.get("USE_TESTNET", "true").lower() != "true":
    sys.exit("Refusing to run: USE_TESTNET is not true.")

api_key = os.environ.get("EXCHANGE_API_KEY", "")
api_secret = os.environ.get("EXCHANGE_API_SECRET", "")
if not api_key or not api_secret or api_key.startswith("your_"):
    sys.exit("EXCHANGE_API_KEY / EXCHANGE_API_SECRET are not set in .env")

exchange = getattr(ccxt, os.environ.get("EXCHANGE", "binance"))({
    "apiKey": api_key,
    "secret": api_secret,
    # adjustForTimeDifference: Binance rejects signed requests if the local clock drifts >1s (error -1021)
    "options": {"fetchMarkets": ["spot"], "adjustForTimeDifference": True},
})
exchange.set_sandbox_mode(True)

try:
    balance = exchange.fetch_balance()
except Exception as e:
    sys.exit(f"TESTNET authentication FAILED: {type(e).__name__}: {str(e)[:300]}")

held = {asset: amount for asset, amount in balance["total"].items() if amount}
print("TESTNET authentication OK.")
# testnet balances include odd asset names that Windows' default console encoding can't print
for asset in ("USDT", "BTC", "BNB", "ETH"):
    print(f"{asset}: {held.get(asset, 0)}")
print(f"({len(held)} assets with a non-zero balance in total)")
