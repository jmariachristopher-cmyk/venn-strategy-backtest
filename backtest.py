"""
Backtest: Nifty Venn (T ∩ M ∩ V) intraday strategy on real historical data via Upstox API.

SETUP
-----
1. pip install -r requirements.txt
2. Create an app at https://developer.upstox.com and generate an access token
   (valid for 1 trading day - regenerate each morning, or automate the login flow).
3. export UPSTOX_ACCESS_TOKEN="your_token_here"
4. python backtest.py

DATA LIMITATION (Upstox platform limit, not adjustable):
Minute-level historical candles are only available from January 2022 onward.
This script pulls the maximum available history (~4.5 years as of 2026), not a full 5 years,
because that data does not exist on Upstox for finer-than-daily candles.

INSTRUMENT:
Defaults to the NIFTY 50 index (NSE_INDEX|Nifty 50) for signal generation.
Index candles are used for signals; note the index itself isn't directly tradable -
see auto_trade.py which trades Nifty futures.
"""
import os
import sys
import time
import datetime as dt
import requests
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from indicators import compute_indicators, generate_sets

INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
INTERVAL_UNIT = "minutes"
INTERVAL_VALUE = "5"
DATA_START = "2022-01-01"   # earliest Upstox supports for minute candles
CACHE_FILE = "nifty_5min_cache.csv"

CAPITAL = 1_000_000          # starting capital, INR - change to your own
RISK_PCT = 0.01              # 1% of capital risked per trade
MAX_TRADES_PER_DAY = 3
RR_TARGET_MULT = 1.75        # target = 1.75x stop distance (within the 1.5-2x band)
SESSION_START = dt.time(9, 30)   # skip first 15 min of noise
SESSION_END = dt.time(15, 0)     # no new entries after 3:00 PM
SQUARE_OFF = dt.time(15, 20)     # force-close any open position

BASE_URL = "https://api.upstox.com/v3/historical-candle"


def fetch_month(instrument_key, unit, interval, from_date, to_date, token):
    url = f"{BASE_URL}/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    candles = data.get("data", {}).get("candles", [])
    return candles


def fetch_all_history(token):
    """Pull month-by-month from DATA_START to today, respecting Upstox chunk limits."""
    if os.path.exists(CACHE_FILE):
        print(f"Using cached data: {CACHE_FILE}")
        return pd.read_csv(CACHE_FILE, parse_dates=["datetime"])

    start = dt.date.fromisoformat(DATA_START)
    end = dt.date.today()
    all_rows = []
    cur = start
    while cur < end:
        chunk_end = min(dt.date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1) - dt.timedelta(days=1), end)
        try:
            candles = fetch_month(
                INSTRUMENT_KEY, INTERVAL_UNIT, INTERVAL_VALUE,
                cur.isoformat(), chunk_end.isoformat(), token
            )
            all_rows.extend(candles)
            print(f"Fetched {cur} -> {chunk_end}: {len(candles)} candles")
        except requests.HTTPError as e:
            print(f"Failed {cur} -> {chunk_end}: {e}", file=sys.stderr)
        cur = chunk_end + dt.timedelta(days=1)
        time.sleep(0.3)  # be polite to the API rate limit

    if not all_rows:
        raise RuntimeError(
            "No data returned. Check UPSTOX_ACCESS_TOKEN is set and valid, "
            "and that your Upstox plan has historical data access."
        )

    # Upstox candle row: [timestamp, open, high, low, close, volume, oi]
    df = pd.DataFrame(all_rows, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").drop_duplicates(subset="datetime").reset_index(drop=True)
    df.to_csv(CACHE_FILE, index=False)
    return df


def run_backtest(df: pd.DataFrame):
    df = compute_indicators(df)
    df, diagnostics = generate_sets(df)
    if not diagnostics["volume_available"]:
        print("WARNING: zero volume reported for this instrument (expected for the Nifty index itself, "
              "since only its futures/options are actually traded). Falling back to VWAP-position-only "
              "for the V-set. For a genuine volume-confirmed V-set, point INSTRUMENT_KEY at Nifty futures.")
    print(f"Signal diagnostics: T bull/bear={diagnostics['t_bull_count']}/{diagnostics['t_bear_count']}  "
          f"M bull/bear={diagnostics['m_bull_count']}/{diagnostics['m_bear_count']}  "
          f"V bull/bear={diagnostics['v_bull_count']}/{diagnostics['v_bear_count']}  "
          f"signals={diagnostics['signal_count']}")
    df["time"] = df["datetime"].dt.time
    df["date"] = df["datetime"].dt.date

    capital = CAPITAL
    equity_curve = []
    trades = []

    position = None  # dict: side, entry_price, sl, target, entry_time, qty
    trades_today = 0
    cur_day = None

    for i, row in df.iterrows():
        if row["date"] != cur_day:
            cur_day = row["date"]
            trades_today = 0
            # force-close any position left open from prior day (shouldn't happen, safety net)
            if position is not None:
                position = None

        # Force square-off at end of session
        if position is not None and row["time"] >= SQUARE_OFF:
            exit_price = row["close"]
            pnl = (exit_price - position["entry_price"]) * position["qty"] * (1 if position["side"] == "long" else -1)
            capital += pnl
            trades.append({**position, "exit_price": exit_price, "exit_time": row["datetime"],
                            "exit_reason": "square_off", "pnl": pnl})
            position = None

        # Manage open position: SL / target / VWAP-break exit
        if position is not None:
            hit_sl = (row["low"] <= position["sl"]) if position["side"] == "long" else (row["high"] >= position["sl"])
            hit_target = (row["high"] >= position["target"]) if position["side"] == "long" else (row["low"] <= position["target"])
            vwap_break = (row["close"] < row["vwap"]) if position["side"] == "long" else (row["close"] > row["vwap"])

            if hit_sl or hit_target or vwap_break:
                if hit_sl:
                    exit_price, reason = position["sl"], "stop_loss"
                elif hit_target:
                    exit_price, reason = position["target"], "target"
                else:
                    exit_price, reason = row["close"], "vwap_break"
                pnl = (exit_price - position["entry_price"]) * position["qty"] * (1 if position["side"] == "long" else -1)
                capital += pnl
                trades.append({**position, "exit_price": exit_price, "exit_time": row["datetime"],
                                "exit_reason": reason, "pnl": pnl})
                position = None

        # New entries
        if (position is None and trades_today < MAX_TRADES_PER_DAY
                and SESSION_START <= row["time"] <= SESSION_END
                and row["signal"] in ("long", "short") and not pd.isna(row["atr14"])):

            entry_price = row["close"]
            stop_dist = max(row["atr14"], 0.05 * entry_price / 100)  # tiny floor to avoid zero-width stops
            if row["signal"] == "long":
                sl = entry_price - stop_dist
                target = entry_price + stop_dist * RR_TARGET_MULT
            else:
                sl = entry_price + stop_dist
                target = entry_price - stop_dist * RR_TARGET_MULT

            risk_amount = capital * RISK_PCT
            qty = int(risk_amount / stop_dist) if stop_dist > 0 else 0

            if qty > 0:
                position = {
                    "side": row["signal"], "entry_price": entry_price, "sl": sl,
                    "target": target, "entry_time": row["datetime"], "qty": qty,
                }
                trades_today += 1

        equity_curve.append({"datetime": row["datetime"], "equity": capital})

    return pd.DataFrame(trades), pd.DataFrame(equity_curve)


def summarize(trades: pd.DataFrame, equity: pd.DataFrame):
    if trades.empty:
        print("No trades were generated - the T ∩ M ∩ V intersection never fired. "
              "Consider loosening thresholds or checking data quality.")
        return

    total_trades = len(trades)
    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]
    win_rate = len(wins) / total_trades * 100
    avg_win = wins["pnl"].mean() if len(wins) else 0
    avg_loss = losses["pnl"].mean() if len(losses) else 0
    total_pnl = trades["pnl"].sum()
    profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if losses["pnl"].sum() != 0 else np.inf
    expectancy = trades["pnl"].mean()

    equity["peak"] = equity["equity"].cummax()
    equity["drawdown"] = equity["equity"] - equity["peak"]
    max_dd = equity["drawdown"].min()
    max_dd_pct = (max_dd / equity["peak"].max()) * 100

    n_days = trades["entry_time"].dt.date.nunique()

    print("\n===== BACKTEST RESULTS: Nifty Venn (T ∩ M ∩ V) Strategy =====")
    print(f"Period covered:        {equity['datetime'].min().date()} to {equity['datetime'].max().date()}")
    print(f"Trading days with data: {equity['datetime'].dt.date.nunique()}")
    print(f"Total trades:           {total_trades}  ({n_days} trading days had a trade)")
    print(f"Win rate:               {win_rate:.1f}%")
    print(f"Average win:            {avg_win:,.0f}")
    print(f"Average loss:           {avg_loss:,.0f}")
    print(f"Profit factor:          {profit_factor:.2f}")
    print(f"Expectancy per trade:   {expectancy:,.0f}")
    print(f"Total P&L:              {total_pnl:,.0f}")
    print(f"Starting capital:       {CAPITAL:,.0f}")
    print(f"Ending capital:         {CAPITAL + total_pnl:,.0f}  ({(total_pnl / CAPITAL) * 100:.1f}% return)")
    print(f"Max drawdown:           {max_dd:,.0f}  ({max_dd_pct:.1f}%)")
    print("================================================================\n")

    trades.to_csv("trade_log.csv", index=False)
    print("Full trade log saved to trade_log.csv")

    plt.figure(figsize=(11, 5))
    plt.plot(equity["datetime"], equity["equity"])
    plt.title("Equity Curve — Nifty Venn Strategy")
    plt.xlabel("Date")
    plt.ylabel("Capital (INR)")
    plt.tight_layout()
    plt.savefig("equity_curve.png", dpi=130)
    print("Equity curve chart saved to equity_curve.png")


if __name__ == "__main__":
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        print("WARNING: UPSTOX_ACCESS_TOKEN not set. Public historical endpoints may still work "
              "for some accounts, but this will likely fail. Set your token first:\n"
              "  export UPSTOX_ACCESS_TOKEN='your_token'", file=sys.stderr)

    df = fetch_all_history(token)
    print(f"Loaded {len(df)} candles from {df['datetime'].min()} to {df['datetime'].max()}")

    trades, equity = run_backtest(df)
    summarize(trades, equity)
