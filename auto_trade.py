"""
Live / paper auto-trader for the Nifty Venn (T ∩ M ∩ V) strategy, via Upstox API.

*** READ BEFORE RUNNING ***
- DRY_RUN = True by default. In dry-run mode, no real orders are placed - signals and
  hypothetical fills are only logged to trade_log_live.csv. Leave this on until you have
  watched it run correctly for at least several sessions.
- The Nifty index itself cannot be traded directly. This script trades the current-month
  Nifty futures contract by default. Set INSTRUMENT_KEY yourself if you want a different
  instrument (e.g. a specific Nifty option strike).
- An Upstox access token is valid for one trading day only. You must regenerate it each
  morning (manually, or by automating Upstox's OAuth login flow, which needs a TOTP/2FA
  step - not something this script does for you).
- This places real orders with real money once DRY_RUN=False. Test in dry-run and/or a
  broker sandbox first. Nothing here guarantees profitability - see the strategy notes.

SETUP
-----
1. pip install -r requirements.txt
2. export UPSTOX_ACCESS_TOKEN="your_token_here"
3. Set INSTRUMENT_KEY below to your current Nifty futures contract, e.g. "NSE_FO|<token>"
   (look this up daily/monthly - futures contracts roll over and the key changes).
4. python auto_trade.py
"""
import os
import sys
import time
import datetime as dt
import requests
import pandas as pd

from indicators import compute_indicators, generate_sets

# ---------------- CONFIG ----------------
DRY_RUN = True  # <-- flip to False only when you are ready to place real orders

SIGNAL_INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"       # used to compute signals (index)
INSTRUMENT_KEY = "NSE_FO|REPLACE_WITH_FUT_TOKEN"    # used to actually place orders (futures)
LOT_SIZE = 75          # Nifty futures lot size - confirm current value with your broker before trading
INTERVAL_UNIT = "minutes"
INTERVAL_VALUE = "5"

CAPITAL = 1_000_000
RISK_PCT = 0.01
MAX_TRADES_PER_DAY = 3
RR_TARGET_MULT = 1.75
SESSION_START = dt.time(9, 30)
SESSION_END = dt.time(15, 0)
SQUARE_OFF = dt.time(15, 20)
POLL_SECONDS = 60  # check every minute; only acts on freshly-closed 5-min candles

BASE_URL = "https://api.upstox.com/v3"
LOG_FILE = "trade_log_live.csv"


def headers(token):
    return {"Accept": "application/json", "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"}


def fetch_intraday_candles(token):
    url = f"{BASE_URL}/historical-candle/intraday/{SIGNAL_INSTRUMENT_KEY}/{INTERVAL_UNIT}/{INTERVAL_VALUE}"
    r = requests.get(url, headers=headers(token), timeout=15)
    r.raise_for_status()
    candles = r.json().get("data", {}).get("candles", [])
    df = pd.DataFrame(candles, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


def place_order(token, side, qty, order_type="MARKET", price=0):
    """side: 'BUY' or 'SELL'. Returns order_id or None."""
    if DRY_RUN:
        print(f"[DRY RUN] Would place {side} order for {qty} qty of {INSTRUMENT_KEY}")
        return "DRYRUN-ORDER"

    payload = {
        "quantity": qty,
        "product": "I",  # intraday
        "validity": "DAY",
        "price": price,
        "instrument_token": INSTRUMENT_KEY,
        "order_type": order_type,
        "transaction_type": side,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
    }
    r = requests.post(f"{BASE_URL.replace('/v3','/v2')}/order/place", headers=headers(token), json=payload, timeout=15)
    r.raise_for_status()
    return r.json().get("data", {}).get("order_id")


def log_trade(row):
    df = pd.DataFrame([row])
    header = not os.path.exists(LOG_FILE)
    df.to_csv(LOG_FILE, mode="a", header=header, index=False)


def main():
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        sys.exit("UPSTOX_ACCESS_TOKEN not set. export UPSTOX_ACCESS_TOKEN='your_token' and retry.")

    if INSTRUMENT_KEY.endswith("REPLACE_WITH_FUT_TOKEN") and not DRY_RUN:
        sys.exit("Set INSTRUMENT_KEY to the current Nifty futures instrument key before going live.")

    print(f"Starting Nifty Venn auto-trader | DRY_RUN={DRY_RUN} | instrument={INSTRUMENT_KEY}")

    position = None
    capital = CAPITAL
    trades_today = 0
    cur_day = None
    last_seen_candle = None

    while True:
        now = dt.datetime.now()
        today = now.date()

        if today != cur_day:
            cur_day = today
            trades_today = 0

        if now.time() > SQUARE_OFF and position is not None:
            print("Session end - squaring off open position.")
            place_order(token, "SELL" if position["side"] == "long" else "BUY", position["qty"] * LOT_SIZE)
            log_trade({**position, "exit_reason": "square_off", "exit_time": now})
            position = None

        try:
            df = fetch_intraday_candles(token)
        except requests.RequestException as e:
            print(f"Data fetch error: {e}", file=sys.stderr)
            time.sleep(POLL_SECONDS)
            continue

        if df.empty or len(df) < 25:
            time.sleep(POLL_SECONDS)
            continue

        df = compute_indicators(df)
        df = generate_sets(df)
        latest = df.iloc[-1]

        # Only act once per newly closed candle
        if latest["datetime"] == last_seen_candle:
            time.sleep(POLL_SECONDS)
            continue
        last_seen_candle = latest["datetime"]

        cur_time = latest["datetime"].time()

        # Manage open position
        if position is not None:
            hit_sl = (latest["low"] <= position["sl"]) if position["side"] == "long" else (latest["high"] >= position["sl"])
            hit_target = (latest["high"] >= position["target"]) if position["side"] == "long" else (latest["low"] <= position["target"])
            vwap_break = (latest["close"] < latest["vwap"]) if position["side"] == "long" else (latest["close"] > latest["vwap"])

            if hit_sl or hit_target or vwap_break:
                reason = "stop_loss" if hit_sl else "target" if hit_target else "vwap_break"
                exit_side = "SELL" if position["side"] == "long" else "BUY"
                place_order(token, exit_side, position["qty"] * LOT_SIZE)
                pnl = (latest["close"] - position["entry_price"]) * position["qty"] * LOT_SIZE * (1 if position["side"] == "long" else -1)
                capital += pnl
                log_trade({**position, "exit_price": latest["close"], "exit_time": now,
                            "exit_reason": reason, "pnl": pnl})
                print(f"Closed {position['side']} @ {latest['close']} ({reason}), PnL={pnl:,.0f}")
                position = None

        # New entry
        if (position is None and trades_today < MAX_TRADES_PER_DAY
                and SESSION_START <= cur_time <= SESSION_END
                and latest["signal"] in ("long", "short") and not pd.isna(latest["atr14"])):

            entry_price = latest["close"]
            stop_dist = max(latest["atr14"], entry_price * 0.0005)
            if latest["signal"] == "long":
                sl, target = entry_price - stop_dist, entry_price + stop_dist * RR_TARGET_MULT
                order_side = "BUY"
            else:
                sl, target = entry_price + stop_dist, entry_price - stop_dist * RR_TARGET_MULT
                order_side = "SELL"

            risk_amount = capital * RISK_PCT
            lots = max(int(risk_amount / (stop_dist * LOT_SIZE)), 0)

            if lots > 0:
                place_order(token, order_side, lots * LOT_SIZE)
                position = {"side": latest["signal"], "entry_price": entry_price, "sl": sl,
                            "target": target, "entry_time": now, "qty": lots}
                trades_today += 1
                print(f"Opened {latest['signal']} @ {entry_price} | SL={sl:.1f} Target={target:.1f} Lots={lots}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
