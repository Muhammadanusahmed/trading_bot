"""
Layer 4 - Execution Engine.

Reads decision.json (written by decision_agent.py / Layer 3) and places the
real orders for it. This layer no longer decides anything itself - it just
executes, with safety defaults that must be deliberately turned off:

1. DRY_RUN=true (default) - logs what it WOULD do, places no real orders.
   Only set DRY_RUN=false once you've watched it run correctly for a while.
2. USE_TESTNET=true (default) - even with DRY_RUN off, orders go to the
   exchange's fake-money testnet, not the real market. Both DRY_RUN and
   USE_TESTNET must be explicitly set to false before this touches real
   money. Any value other than "false" (typos, "0", "no", blank) keeps the
   guard ON - the flags fail safe.
3. Exchange-side stop-loss - every entry immediately gets a stop-loss order
   placed ON THE EXCHANGE, so protection works even if this script crashes
   or loses network. This is the actual crash protection, not AI judgement.
   If the stop can't be placed, the position is sold straight back out
   rather than left unprotected.
4. Stale-decision check - if decision.json is missing or older than
   DECISION_MAX_AGE_MINUTES (Layer 3 not running, or stuck), this layer
   refuses to act rather than trading on outdated information.
5. No re-buy after a stop-out - once the stop-loss fills, the bot waits for
   Layer 3 to say "flat" before it will buy again, instead of buying straight
   back in while the trend signal is still "long". In long_backtest.py
   (2017-2026) buying straight back in was the worst variant by far.
6. Reconciliation - every LIVE cycle (and so on startup) checks the exchange
   before trusting trade_state.json: if the stop-loss filled, the position
   is marked closed; if the bot is in a position with no live stop, the
   stop is re-placed. It only ever sells what it bought, capped at the real
   free balance, so it can't sell coins held outside the bot.
7. No double-buy on a timeout - every buy carries its own client order id,
   saved to trade_state.json before the order is sent. If the request errors
   or times out, the bot asks the exchange for that id instead of guessing:
   if the buy went through it adopts the position (and places the stop); it
   only clears the record once the exchange says the order doesn't exist.
   No new buy is sent while one is still unresolved.
8. Paper trading - with DRY_RUN on, the engine needs no API keys and reads
   the REAL market's public prices (not the testnet's), whatever
   USE_TESTNET says, since it sends no orders. It charges FEE_PERCENT on each
   paper buy and sell, fills the stop-loss itself when the price reaches it
   (there is no exchange-side stop to do it), and keeps a paper balance that
   starts at STARTING_BALANCE_USDT, so the trade log gives a real P/L.
9. Early warning - once the price falls STOP_WARNING_PERCENT (default 1.5%)
   below the entry, both NOTIFY_TO addresses get one email per position
   saying the stop-loss is close, so a human can look before it sells. The
   stop itself stays on the exchange and sells regardless of the email.

The order path was verified on Binance TESTNET (see CLAUDE.md), never on the
real exchange.
"""

import ccxt
import json
import os
import time
from pathlib import Path
from dotenv import load_dotenv

from agent_status import heartbeat
from email_utils import send_email, alert_error

load_dotenv()


def env_guard_on(name):
    """Safety guards fail safe: only an explicit "false" turns one off."""
    return os.environ.get(name, "true").strip().lower() != "false"


EXCHANGE = os.environ.get("EXCHANGE", "binance")
SYMBOL = os.environ.get("SYMBOL", "BTC/USDT")
BASE_ASSET = SYMBOL.split("/")[0]

API_KEY = os.environ.get("EXCHANGE_API_KEY", "")
API_SECRET = os.environ.get("EXCHANGE_API_SECRET", "")
USE_TESTNET = env_guard_on("USE_TESTNET")
DRY_RUN = env_guard_on("DRY_RUN")

TRADE_AMOUNT_USDT = float(os.environ.get("TRADE_AMOUNT_USDT", 50))
STOP_LOSS_PERCENT = float(os.environ.get("STOP_LOSS_PERCENT", 2.0))
STOP_WARNING_PERCENT = float(os.environ.get("STOP_WARNING_PERCENT", 1.5))
CHECK_INTERVAL_SECONDS = int(os.environ.get("EXECUTION_CHECK_INTERVAL_SECONDS", 300))
DECISION_MAX_AGE_MINUTES = int(os.environ.get("DECISION_MAX_AGE_MINUTES", 20))
# Binance spot taker fee is 0.1% per side (0.075% when paying fees in BNB)
FEE_PERCENT = float(os.environ.get("FEE_PERCENT", 0.1))
# paper account size the weekly P/L is measured against
STARTING_BALANCE_USDT = float(os.environ.get("STARTING_BALANCE_USDT", 1000))
STOP_PLACE_ATTEMPTS = 3
STOP_RETRY_DELAY_SECONDS = 2
# a buy the exchange can't find yet may just not be visible yet - only treat it
# as "never placed" once it has been missing for this long
PENDING_BUY_GRACE_SECONDS = 60

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "trade_state.json"
TRADE_LOG = BASE_DIR / "data" / "trade_log.jsonl"
DECISION_FILE = BASE_DIR / "decision.json"

FLAT_STATE = {"in_position": False, "entry_price": None, "quantity": None, "stop_order_id": None, "stop_warning_sent": False}


def make_exchange():
    exchange_class = getattr(ccxt, EXCHANGE)
    exchange = exchange_class({
        # paper trading only reads public market data, so it needs no keys
        "apiKey": "" if DRY_RUN else API_KEY,
        "secret": "" if DRY_RUN else API_SECRET,
        # adjustForTimeDifference: Binance rejects signed requests if the local clock drifts >1s (error -1021)
        "options": {"fetchMarkets": ["spot"], "adjustForTimeDifference": True},
    })
    # paper trading must see real-market prices; the testnet's prices are not the real market's
    if USE_TESTNET and not DRY_RUN:
        exchange.set_sandbox_mode(True)
    if not DRY_RUN:
        exchange.load_markets()  # needed for amount/price precision on real orders
    return exchange


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return dict(FLAT_STATE)


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def urgent_email(subject, body):
    # never let a failed notification abort the caller mid-trade
    try:
        send_email(subject, body)
    except Exception as e:
        print(f"Urgent email failed: {subject}: {e}")


def log_trade(action, details):
    TRADE_LOG.parent.mkdir(exist_ok=True)
    record = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "action": action, **details}
    with open(TRADE_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"[{record['timestamp']}] {action}: {details}")

    if action.startswith("LIVE"):
        # real money just moved - always email, regardless of the error-alert cooldown.
        # the order already happened - a failed notification must never abort the
        # caller before it saves state, or the bot loses track of an open position
        detail_lines = "\n".join(f"{k}: {v}" for k, v in details.items())
        urgent_email(f"Crypto bot: {action} executed", detail_lines)


def read_decision():
    """Read Layer 3's decision.json. Returns None if it's missing or stale,
    so the caller refuses to trade on outdated information."""
    if not DECISION_FILE.exists():
        return None
    decision = json.loads(DECISION_FILE.read_text())
    age_minutes = (time.time() - decision["timestamp"]) / 60
    if age_minutes > DECISION_MAX_AGE_MINUTES:
        return None
    return decision


def sellable_quantity(exchange, wanted):
    """The most the bot may sell: what it thinks it holds, capped at the real
    free balance (fees, a filled stop, manual trades), rounded down to the
    exchange's precision. Returns 0.0 if that's below the exchange minimum."""
    free = float(exchange.fetch_balance()["free"].get(BASE_ASSET) or 0)
    quantity = min(float(wanted), free)
    min_amount = exchange.market(SYMBOL)["limits"]["amount"]["min"] or 0
    if quantity <= 0 or quantity < min_amount:
        return 0.0
    return float(exchange.amount_to_precision(SYMBOL, quantity))  # ccxt truncates amounts


def net_bought_quantity(order, fallback):
    """Binance takes the trading fee out of the coin received (unless paying
    fees in BNB), so the amount actually held is filled minus base-asset fees."""
    filled = order.get("filled")
    if not filled:
        return float(fallback)
    fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
    base_fee = sum(float(f.get("cost") or 0) for f in fees if f and f.get("currency") == BASE_ASSET)
    return float(filled) - base_fee


def emergency_sell(exchange, state, reason):
    """Flatten a position that couldn't be protected. If even this fails, the
    position stays recorded (with no stop) so the next cycle retries the stop
    instead of buying again."""
    try:
        quantity = sellable_quantity(exchange, state["quantity"])
        if quantity:
            order = exchange.create_market_sell_order(SYMBOL, quantity)
            log_trade("LIVE emergency sell", {"symbol": SYMBOL, "order_id": order["id"], "quantity": quantity, "reason": reason})
        state.update(FLAT_STATE)
        save_state(state)
    except Exception as e:
        urgent_email(
            "Crypto bot: UNPROTECTED POSITION",
            f"{SYMBOL} position of {state['quantity']} has NO stop-loss and the emergency sell failed.\n"
            f"Why the stop failed: {reason}\nWhy the sell failed: {e}\n"
            "The bot will retry next cycle. Check the exchange manually now.",
        )
        raise


def place_stop(exchange, state):
    """Place the exchange-side stop for the current position. If it can't be
    placed after a few tries, sell the position rather than hold it unprotected."""
    stop_price = state["entry_price"] * (1 - STOP_LOSS_PERCENT / 100)
    last_error = None
    for attempt in range(STOP_PLACE_ATTEMPTS):
        if attempt:
            time.sleep(STOP_RETRY_DELAY_SECONDS)
        try:
            stop_order = exchange.create_order(
                SYMBOL, "stop_loss_limit", "sell", state["quantity"],
                price=exchange.price_to_precision(SYMBOL, stop_price * 0.999),
                params={"stopPrice": exchange.price_to_precision(SYMBOL, stop_price)},
            )
            state["stop_order_id"] = stop_order["id"]
            save_state(state)
            log_trade("stop placed", {"symbol": SYMBOL, "stop_order_id": stop_order["id"], "stop_price": stop_price, "quantity": state["quantity"]})
            return
        except Exception as e:
            last_error = e
            print(f"Stop-loss attempt {attempt + 1}/{STOP_PLACE_ATTEMPTS} failed: {e}")

    urgent_email(
        "Crypto bot: stop-loss failed, selling position",
        f"Could not place a stop-loss for {state['quantity']} {SYMBOL} after "
        f"{STOP_PLACE_ATTEMPTS} attempts ({last_error}). Selling the position back out.",
    )
    emergency_sell(exchange, state, str(last_error))


def adopt_buy(exchange, state, order, fallback_price, fallback_quantity, label="LIVE buy"):
    """Record a filled buy as the open position and protect it with a stop."""
    # record the position BEFORE anything else can fail, so a later error can't
    # leave an open position the bot doesn't know about (and buys again next cycle)
    state.update({"in_position": True, "entry_price": fallback_price, "quantity": float(fallback_quantity), "stop_order_id": None, "stop_warning_sent": False})
    state.pop("pending_buy", None)
    save_state(state)

    state["entry_price"] = order.get("average") or fallback_price
    state["quantity"] = sellable_quantity(exchange, net_bought_quantity(order, fallback_quantity))
    save_state(state)
    log_trade(label, {"symbol": SYMBOL, "order_id": order["id"], "price": state["entry_price"], "quantity": state["quantity"]})

    place_stop(exchange, state)


def resolve_pending_buy(exchange, state):
    """A buy was sent but the bot never saw the reply (timeout, network error,
    crash). Ask the exchange by client order id what really happened."""
    pending = state.get("pending_buy")
    if not pending:
        return state

    try:
        order = exchange.fetch_order(None, SYMBOL, {"origClientOrderId": pending["client_order_id"]})
    except ccxt.OrderNotFound:
        if time.time() - pending["sent_at"] < PENDING_BUY_GRACE_SECONDS:
            print("Buy order not visible on the exchange yet - checking again next cycle.")
            return state
        log_trade("buy never placed", {"symbol": SYMBOL, "client_order_id": pending["client_order_id"]})
        state.pop("pending_buy")
        save_state(state)
        return state

    if order["status"] == "open":
        print(f"Buy order {order['id']} still open - checking again next cycle.")
        return state
    if float(order.get("filled") or 0) > 0:
        adopt_buy(exchange, state, order, pending["price"], order["filled"], label="LIVE buy (recovered after error)")
        return state

    # canceled / rejected / expired with nothing filled
    log_trade("buy not filled", {"symbol": SYMBOL, "order_id": order["id"], "status": order["status"]})
    state.pop("pending_buy")
    save_state(state)
    return state


def reconcile(exchange, state):
    """Check the exchange before trusting trade_state.json. Runs every LIVE cycle."""
    if not state["in_position"]:
        return state

    if state.get("stop_order_id"):
        stop = exchange.fetch_order(state["stop_order_id"], SYMBOL)
        if stop["status"] == "closed":
            log_trade("LIVE stop-loss filled", {"symbol": SYMBOL, "stop_order_id": stop["id"], "price": stop.get("average"), "quantity": stop.get("filled")})
            state.update(FLAT_STATE)
            state["wait_for_flat"] = True  # don't buy again until the trend signal resets
            save_state(state)
            return state
        if stop["status"] == "open":
            return state
        # canceled / expired / rejected: the position is unprotected, repair below
        state["stop_order_id"] = None
        save_state(state)

    quantity = sellable_quantity(exchange, state["quantity"])
    if not quantity:
        urgent_email(
            "Crypto bot: position missing on exchange",
            f"State said {state['quantity']} {SYMBOL} was held with no live stop, but the free "
            f"{BASE_ASSET} balance doesn't cover it. Marking the position closed.",
        )
        state.update(FLAT_STATE)
        save_state(state)
        return state

    state["quantity"] = quantity
    save_state(state)
    place_stop(exchange, state)
    return state


def paper_balance(state):
    return STARTING_BALANCE_USDT + state.get("paper_realized_pnl_usdt", 0.0)


def paper_close(state, action, exit_price, reason):
    """Close the paper position at exit_price and log its P/L after fees."""
    quantity = float(state["quantity"])
    entry_price = float(state["entry_price"])
    exit_fee = exit_price * quantity * FEE_PERCENT / 100
    entry_fee = float(state.get("entry_fee_usdt") or 0)
    gross_pnl = (exit_price - entry_price) * quantity
    net_pnl = gross_pnl - entry_fee - exit_fee
    state["paper_realized_pnl_usdt"] = state.get("paper_realized_pnl_usdt", 0.0) + net_pnl
    log_trade(action, {
        "symbol": SYMBOL, "price": exit_price, "quantity": quantity,
        "value_usdt": exit_price * quantity, "fee_usdt": exit_fee,
        "entry_price": entry_price, "entry_time": state.get("entry_time"),
        "gross_pnl_usdt": gross_pnl, "fees_total_usdt": entry_fee + exit_fee, "net_pnl_usdt": net_pnl,
        "pnl_percent": net_pnl / (entry_price * quantity) * 100,
        "paper_balance_usdt": paper_balance(state), "reason": reason,
    })
    for key in ("entry_time", "entry_fee_usdt"):
        state.pop(key, None)
    state.update(FLAT_STATE)


def paper_stop_check(state, price):
    """DRY_RUN has no exchange-side stop, so fill it here when the price
    reaches it, at the limit price the live stop order would use."""
    if not state["in_position"]:
        return
    stop_price = float(state["entry_price"]) * (1 - STOP_LOSS_PERCENT / 100)
    if price > stop_price:
        return
    paper_close(state, "DRY_RUN stop-loss filled", stop_price * 0.999,
                f"price {price} reached the {STOP_LOSS_PERCENT}% stop at {stop_price:.2f}")
    state["wait_for_flat"] = True  # same rule as a live stop-out
    save_state(state)


def enter_position(exchange, state, price, reason=None):
    raw_quantity = TRADE_AMOUNT_USDT / price

    if DRY_RUN:
        stop_price = price * (1 - STOP_LOSS_PERCENT / 100)
        fee = price * raw_quantity * FEE_PERCENT / 100
        state.update({"in_position": True, "entry_price": price, "quantity": raw_quantity, "stop_order_id": None, "stop_warning_sent": False,
                      "entry_time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "entry_fee_usdt": fee})
        log_trade("DRY_RUN buy", {"symbol": SYMBOL, "price": price, "quantity": raw_quantity, "value_usdt": price * raw_quantity,
                                  "fee_usdt": fee, "stop_price": stop_price, "paper_balance_usdt": paper_balance(state), "reason": reason})
        save_state(state)
        return

    quantity = exchange.amount_to_precision(SYMBOL, raw_quantity)
    # saved BEFORE sending: if the reply is lost, this id is how the bot finds
    # out whether the buy happened, instead of buying a second time
    client_order_id = f"trendbot-{int(time.time() * 1000)}"
    state["pending_buy"] = {"client_order_id": client_order_id, "price": price, "quantity": float(quantity), "sent_at": time.time()}
    save_state(state)

    try:
        order = exchange.create_market_buy_order(SYMBOL, quantity, params={"clientOrderId": client_order_id})
    except Exception as e:
        print(f"Buy request failed ({e}) - asking the exchange whether it went through.")
        time.sleep(STOP_RETRY_DELAY_SECONDS)
        resolve_pending_buy(exchange, state)
        if state.get("pending_buy"):
            raise RuntimeError(f"Buy order {client_order_id} status unknown, re-checking next cycle: {e}")
        return

    adopt_buy(exchange, state, order, price, quantity)


def exit_position(exchange, state, price, reason=None):
    if DRY_RUN:
        paper_close(state, "DRY_RUN sell", price, reason)
        save_state(state)
        return

    if state.get("stop_order_id"):
        try:
            exchange.cancel_order(state["stop_order_id"], SYMBOL)
        except Exception as e:
            # don't sell while a stop might still be live - it could later sell coins the bot doesn't own
            status = exchange.fetch_order(state["stop_order_id"], SYMBOL)["status"]
            if status == "closed":
                reconcile(exchange, state)  # records the stop fill and marks flat
                state["wait_for_flat"] = False  # the signal is already flat
                save_state(state)
                return
            if status == "open":
                raise RuntimeError(f"Could not cancel stop order before exiting: {e}")
        # recorded as unprotected: if the sell below fails, the next cycle re-places the stop
        state["stop_order_id"] = None
        save_state(state)

    quantity = sellable_quantity(exchange, state["quantity"])
    if quantity:
        order = exchange.create_market_sell_order(SYMBOL, quantity)
        log_trade("LIVE sell", {"symbol": SYMBOL, "order_id": order["id"], "price": price, "quantity": quantity})

    state.update(FLAT_STATE)
    save_state(state)


def warn_if_near_stop(state, price):
    """Email once per position when the price is STOP_WARNING_PERCENT below
    the entry, before the STOP_LOSS_PERCENT stop sells at a loss."""
    if not state["in_position"] or state.get("stop_warning_sent") or STOP_WARNING_PERCENT >= STOP_LOSS_PERCENT:
        return
    entry = float(state["entry_price"])
    drop_percent = (entry - price) / entry * 100
    if drop_percent < STOP_WARNING_PERCENT:
        return
    stop_price = entry * (1 - STOP_LOSS_PERCENT / 100)
    network = "PAPER TRADING, no real order" if DRY_RUN else ("TESTNET" if USE_TESTNET else "REAL EXCHANGE")
    urgent_email(
        f"Crypto bot WARNING: {SYMBOL} down {drop_percent:.1f}%, stop-loss close",
        f"{SYMBOL} is {drop_percent:.2f}% below the entry price ({network}).\n\n"
        f"Entry price: {entry:,.2f}\nCurrent price: {price:,.2f}\n"
        f"Stop-loss sells at: {stop_price:,.2f} (-{STOP_LOSS_PERCENT}%)\n"
        f"Quantity: {state['quantity']}\n\n"
        "If the price keeps falling, the stop-loss on the exchange will sell this position at a loss. "
        "It does this automatically, whether or not anyone acts on this email.\n\n"
        f"Qeemat entry se {drop_percent:.1f}% gir gayi hai. Agar {stop_price:,.2f} tak giri to bot loss mein bech dega.",
    )
    log_trade("stop warning", {"symbol": SYMBOL, "entry_price": entry, "price": price, "drop_percent": round(drop_percent, 2)})
    state["stop_warning_sent"] = True
    save_state(state)


def run_once(exchange, state):
    if not DRY_RUN:
        state = resolve_pending_buy(exchange, state)
        if state.get("pending_buy"):
            print("A buy order is still unresolved - not trading until the exchange confirms it.")
            return state
        state = reconcile(exchange, state)

    price = exchange.fetch_ticker(SYMBOL)["last"]  # fresh price for order sizing, not the decision's cached one
    if DRY_RUN:
        paper_stop_check(state, price)
    warn_if_near_stop(state, price)  # before the decision check, so a stuck Layer 3 can't silence it

    decision = read_decision()
    if decision is None:
        print("No fresh decision from Layer 3 (decision_agent.py) - skipping this cycle.")
        return state

    desired_position = decision["position"]
    if desired_position == 0 and state.get("wait_for_flat"):
        state["wait_for_flat"] = False
        save_state(state)

    if desired_position == 1 and not state["in_position"] and state.get("wait_for_flat"):
        print("Stopped out earlier - waiting for the trend signal to go flat before buying again.")
    elif desired_position == 1 and not state["in_position"]:
        enter_position(exchange, state, price, decision["reason"])
    elif desired_position == 0 and state["in_position"]:
        exit_position(exchange, state, price, decision["reason"])
    else:
        print(f"No change. In position: {state['in_position']}, price: {price}. Reason: {decision['reason']}")

    return state


if __name__ == "__main__":
    mode = "DRY RUN (no real orders)" if DRY_RUN else "LIVE ORDERS"
    network = "REAL MARKET PRICES (public data, no keys)" if DRY_RUN else ("TESTNET" if USE_TESTNET else "REAL EXCHANGE")
    print(f"Execution engine starting - mode: {mode}, network: {network}, symbol: {SYMBOL}")
    if not DRY_RUN and not USE_TESTNET:
        print("!!! LIVE TRADING WITH REAL MONEY ON A REAL EXCHANGE !!!")

    exchange = make_exchange()
    state = load_state()
    # one line per start, so the weekly report knows the settings and balance it ran with
    log_trade("engine start", {"symbol": SYMBOL, "mode": "DRY_RUN" if DRY_RUN else "LIVE", "network": network,
                               "starting_balance_usdt": STARTING_BALANCE_USDT, "paper_balance_usdt": paper_balance(state) if DRY_RUN else None,
                               "trade_amount_usdt": TRADE_AMOUNT_USDT, "fee_percent": FEE_PERCENT, "stop_loss_percent": STOP_LOSS_PERCENT,
                               "in_position": state["in_position"]})

    while True:
        try:
            state = run_once(exchange, state)
            heartbeat("execution_engine", mode="DRY_RUN" if DRY_RUN else "LIVE", in_position=state["in_position"],
                      paper_balance_usdt=paper_balance(state) if DRY_RUN else None)
        except Exception as e:
            print(f"Error: {e}")
            heartbeat("execution_engine", "error", e)
            alert_error("execution_engine", e)
        time.sleep(CHECK_INTERVAL_SECONDS)
