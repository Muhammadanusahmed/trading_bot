"""
Layer 5 - Treasury notifier.

Watches your exchange account balance and sends an EMAIL when it crosses
a threshold you set. It does NOT move or withdraw any funds - the actual
transfer stays a manual, human decision. This only tells you "it's time".
"""

import ccxt
import os
import json
import time
from pathlib import Path
from dotenv import load_dotenv

from email_utils import send_email, alert_error

load_dotenv()

EXCHANGE = os.environ["EXCHANGE"]                      # e.g. "binance"
API_KEY = os.environ["EXCHANGE_API_KEY"]                # use a READ-ONLY key, no withdraw permission
API_SECRET = os.environ["EXCHANGE_API_SECRET"]
ASSET = os.environ.get("ASSET", "USDT")                 # which balance to watch
THRESHOLD = float(os.environ["THRESHOLD"])               # e.g. 500

CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", 30))
STATE_FILE = Path(__file__).parent / "notify_state.json"


def get_balance():
    exchange_class = getattr(ccxt, EXCHANGE)
    exchange = exchange_class({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        # Binance rejects signed requests if the local clock drifts >1s (error -1021)
        "options": {"fetchMarkets": ["spot"], "adjustForTimeDifference": True},
    })
    balance = exchange.fetch_balance()
    return balance["total"].get(ASSET, 0.0)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"already_notified": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def check_once():
    state = load_state()
    balance = get_balance()
    print(f"[check] balance = {balance:.2f} {ASSET}")

    if balance >= THRESHOLD and not state["already_notified"]:
        subject = f"Crypto bot: balance crossed {THRESHOLD} {ASSET}"
        body = (
            f"Your {EXCHANGE} account balance is now {balance:.2f} {ASSET}, "
            f"above your {THRESHOLD:.2f} {ASSET} threshold.\n\n"
            f"This is just a notification - no funds were moved automatically."
        )
        send_email(subject, body)
        state["already_notified"] = True
        save_state(state)
        print("Email sent.")
    elif balance < THRESHOLD and state["already_notified"]:
        # balance dropped back below threshold - re-arm so the next crossing notifies again
        state["already_notified"] = False
        save_state(state)


if __name__ == "__main__":
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"Error: {e}")
            alert_error("notify_on_balance", e)
        time.sleep(CHECK_INTERVAL_MINUTES * 60)
