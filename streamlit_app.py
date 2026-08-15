"""
Streamlit app for the Nifty Venn (T ∩ M ∩ V) intraday strategy.

Deploy on streamlit.app (Streamlit Community Cloud) by pushing this repo to GitHub and
pointing Streamlit Cloud at streamlit_app.py.

IMPORTANT — read this about "auto trading" in Streamlit:
Streamlit apps are request/response: the script reruns top-to-bottom on each interaction,
and a hosted app can sleep or restart at any time. That makes it a poor place to run an
unattended, always-on trading loop with real money — if the app sleeps or restarts mid-day,
an order-management loop dies with it. This app therefore gives you:
  1. A full historical Backtest tab (safe, no orders).
  2. A Live Signal Monitor tab you keep open, which polls on a timer and can place a SINGLE
     order per confirmed signal when you flip Dry Run off - but it only works while this
     browser tab/app session is open and running.
For genuine unattended auto-trading, run auto_trade.py (included) as a background process on
your own machine or a small VPS with cron/systemd - not inside Streamlit.
"""
import os
import time
import datetime as dt

import pandas as pd
import numpy as np
import requests
import streamlit as st

from indicators import compute_indicators, generate_sets

st.set_page_config(page_title="Nifty Venn Strategy", layout="wide")

INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"
INTERVAL_UNIT = "minutes"
INTERVAL_VALUE = "5"
DATA_START = "2022-01-01"
BASE_URL_V3 = "https://api.upstox.com/v3/historical-candle"
BASE_URL_V2 = "https://api.upstox.com/v2"

SESSION_START = dt.time(9, 30)
SESSION_END = dt.time(15, 0)
SQUARE_OFF = dt.time(15, 20)


# ---------------------------------------------------------------- helpers
def get_headers(token):
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_month(instrument_key, unit, interval, from_date, to_date, token):
    url = f"{BASE_URL_V3}/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
    r = requests.get(url, headers=get_headers(token), timeout=30)
    r.raise_for_status()
    return r.json().get("data", {}).get("candles", [])


def fetch_all_history(token, progress_cb=None):
    start = dt.date.fromisoformat(DATA_START)
    end = dt.date.today()
    all_rows = []
    cur = start
    total_months = max((end.year - start.year) * 12 + (end.month - start.month) + 1, 1)
    done = 0
    while cur < end:
        chunk_end = min(dt.date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1) - dt.timedelta(days=1), end)
        try:
            candles = fetch_month(INSTRUMENT_KEY, INTERVAL_UNIT, INTERVAL_VALUE,
                                   cur.isoformat(), chunk_end.isoformat(), token)
            all_rows.extend(candles)
        except requests.HTTPError as e:
            st.warning(f"Failed fetching {cur} to {chunk_end}: {e}")
        done += 1
        if progress_cb:
            progress_cb(min(done / total_months, 1.0), f"Fetched {cur} to {chunk_end}")
        cur = chunk_end + dt.timedelta(days=1)
        time.sleep(0.2)

    if not all_rows:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume", "oi"])

    df = pd.DataFrame(all_rows, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").drop_duplicates(subset="datetime").reset_index(drop=True)


def run_backtest(df, capital, risk_pct, max_trades_per_day, rr_mult):
    df = compute_indicators(df)
    df, diagnostics = generate_sets(df)
    df["time"] = df["datetime"].dt.time
    df["date"] = df["datetime"].dt.date

    cap = capital
    equity_curve, trades = [], []
    position, trades_today, cur_day = None, 0, None

    for _, row in df.iterrows():
        if row["date"] != cur_day:
            cur_day = row["date"]
            trades_today = 0
            position = None  # safety: don't carry positions across days

        if position is not None and row["time"] >= SQUARE_OFF:
            exit_price = row["close"]
            pnl = (exit_price - position["entry_price"]) * position["qty"] * (1 if position["side"] == "long" else -1)
            cap += pnl
            trades.append({**position, "exit_price": exit_price, "exit_time": row["datetime"],
                            "exit_reason": "square_off", "pnl": pnl})
            position = None

        if position is not None:
            hit_sl = (row["low"] <= position["sl"]) if position["side"] == "long" else (row["high"] >= position["sl"])
            hit_target = (row["high"] >= position["target"]) if position["side"] == "long" else (row["low"] <= position["target"])
            vwap_break = (row["close"] < row["vwap"]) if position["side"] == "long" else (row["close"] > row["vwap"])
            if hit_sl or hit_target or vwap_break:
                exit_price = position["sl"] if hit_sl else position["target"] if hit_target else row["close"]
                reason = "stop_loss" if hit_sl else "target" if hit_target else "vwap_break"
                pnl = (exit_price - position["entry_price"]) * position["qty"] * (1 if position["side"] == "long" else -1)
                cap += pnl
                trades.append({**position, "exit_price": exit_price, "exit_time": row["datetime"],
                                "exit_reason": reason, "pnl": pnl})
                position = None

        if (position is None and trades_today < max_trades_per_day
                and SESSION_START <= row["time"] <= SESSION_END
                and row["signal"] in ("long", "short") and not pd.isna(row["atr14"])):
            entry_price = row["close"]
            stop_dist = max(row["atr14"], entry_price * 0.0005)
            if row["signal"] == "long":
                sl, target = entry_price - stop_dist, entry_price + stop_dist * rr_mult
            else:
                sl, target = entry_price + stop_dist, entry_price - stop_dist * rr_mult
            qty = int((cap * risk_pct) / stop_dist) if stop_dist > 0 else 0
            if qty > 0:
                position = {"side": row["signal"], "entry_price": entry_price, "sl": sl,
                            "target": target, "entry_time": row["datetime"], "qty": qty}
                trades_today += 1

        equity_curve.append({"datetime": row["datetime"], "equity": cap})

    return pd.DataFrame(trades), pd.DataFrame(equity_curve), diagnostics


def fetch_intraday(token):
    url = f"{BASE_URL_V3}/intraday/{INSTRUMENT_KEY}/{INTERVAL_UNIT}/{INTERVAL_VALUE}"
    r = requests.get(url, headers=get_headers(token), timeout=15)
    r.raise_for_status()
    candles = r.json().get("data", {}).get("candles", [])
    df = pd.DataFrame(candles, columns=["datetime", "open", "high", "low", "close", "volume", "oi"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


def place_order(token, dry_run, side, qty, instrument_key):
    if dry_run:
        return {"status": "dry_run", "order_id": None}
    payload = {"quantity": qty, "product": "I", "validity": "DAY", "price": 0,
               "instrument_token": instrument_key, "order_type": "MARKET",
               "transaction_type": side, "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False}
    r = requests.post(f"{BASE_URL_V2}/order/place", headers=get_headers(token), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- sidebar
st.sidebar.title("Nifty Venn Strategy")
token = st.sidebar.text_input("Upstox Access Token", type="password",
                               help="Generate daily via Upstox OAuth login. Required for real data.")
capital = st.sidebar.number_input("Starting Capital (INR)", value=1_000_000, step=50_000)
risk_pct = st.sidebar.slider("Risk per trade (%)", 0.25, 3.0, 1.0, 0.25) / 100
max_trades = st.sidebar.slider("Max trades per day", 1, 6, 3)
rr_mult = st.sidebar.slider("Reward:Risk multiple", 1.0, 3.0, 1.75, 0.25)

st.sidebar.markdown("---")
st.sidebar.caption(
    "T = Trend (EMA20 vs EMA50) · M = Momentum (RSI>60/<40) · "
    "V = Volume/Price-Action (VWAP + 1.5x volume). Trade only fires on T ∩ M ∩ V."
)

tab_backtest, tab_live, tab_about = st.tabs(["📊 Backtest", "🔴 Live Signal Monitor", "ℹ️ About / Limitations"])

# ---------------------------------------------------------------- Backtest tab
with tab_backtest:
    st.header("Historical Backtest")
    st.caption("Pulls real 5-min Nifty candles from Upstox (available from Jan 2022 onward — "
               "that's Upstox's own data limit, not a full 5 years).")

    if not token:
        st.info("Enter your Upstox access token in the sidebar to fetch real data.")
    else:
        if st.button("Run Backtest", type="primary"):
            progress = st.progress(0.0, text="Starting...")
            df = fetch_all_history(token, progress_cb=lambda p, msg: progress.progress(p, text=msg))
            progress.empty()

            if df.empty:
                st.error("No data returned. Check your access token is valid and not expired "
                         "(Upstox tokens expire daily).")
            else:
                st.success(f"Loaded {len(df):,} candles: {df['datetime'].min()} → {df['datetime'].max()}")
                trades, equity, diagnostics = run_backtest(df, capital, risk_pct, max_trades, rr_mult)

                if not diagnostics["volume_available"]:
                    st.warning(
                        "⚠️ This instrument (Nifty **index**) reports zero traded volume in Upstox's "
                        "data — an index isn't itself traded, only its futures/options are. The "
                        "Volume/Price-Action set has automatically fallen back to **VWAP-position only** "
                        "(volume-spike check dropped) so the strategy can still generate signals. "
                        "For a genuine volume-confirmed V-set, point the instrument at Nifty futures instead."
                    )

                with st.expander("Signal diagnostics (why did/didn't trades fire?)"):
                    d1, d2, d3 = st.columns(3)
                    d1.metric("T bullish / bearish candles", f"{diagnostics['t_bull_count']} / {diagnostics['t_bear_count']}")
                    d2.metric("M bullish / bearish candles", f"{diagnostics['m_bull_count']} / {diagnostics['m_bear_count']}")
                    d3.metric("V bullish / bearish candles", f"{diagnostics['v_bull_count']} / {diagnostics['v_bear_count']}")
                    st.caption(f"T ∩ M ∩ V fired on {diagnostics['signal_count']} candles out of {len(df):,} total.")

                if trades.empty:
                    st.warning("No trades were generated — the T ∩ M ∩ V intersection never fired "
                               "in this data even after the volume fallback above. Try loosening the "
                               "RSI thresholds in indicators.py, or check the diagnostics above to see "
                               "which set is the bottleneck.")
                else:
                    wins = trades[trades["pnl"] > 0]
                    losses = trades[trades["pnl"] <= 0]
                    total_pnl = trades["pnl"].sum()
                    win_rate = len(wins) / len(trades) * 100
                    profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if losses["pnl"].sum() != 0 else np.inf
                    equity["peak"] = equity["equity"].cummax()
                    equity["drawdown"] = equity["equity"] - equity["peak"]
                    max_dd_pct = (equity["drawdown"].min() / equity["peak"].max()) * 100

                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Total Trades", len(trades))
                    c2.metric("Win Rate", f"{win_rate:.1f}%")
                    c3.metric("Profit Factor", f"{profit_factor:.2f}")
                    c4.metric("Total Return", f"{(total_pnl/capital)*100:.1f}%")
                    c5.metric("Max Drawdown", f"{max_dd_pct:.1f}%")

                    st.subheader("Equity Curve")
                    st.line_chart(equity.set_index("datetime")["equity"])

                    st.subheader("Trade Log")
                    st.dataframe(trades, use_container_width=True)
                    st.download_button("Download trade log (CSV)", trades.to_csv(index=False),
                                        "trade_log.csv", "text/csv")

                st.caption("Backtest ignores slippage, brokerage, and STT/taxes — real results will be worse.")

# ---------------------------------------------------------------- Live tab
with tab_live:
    st.header("Live Signal Monitor")
    st.warning(
        "This only runs while this app session stays open in your browser — it is NOT a reliable "
        "substitute for a dedicated always-on trading process. For genuine unattended auto-trading, "
        "run the included auto_trade.py on your own server. See the About tab."
    )

    dry_run = st.toggle("Dry Run (no real orders)", value=True)
    live_instrument_key = st.text_input("Order instrument key (futures, not spot)",
                                          value="NSE_FO|REPLACE_WITH_FUT_TOKEN")
    lot_size = st.number_input("Lot size", value=75, step=25)
    auto_poll = st.toggle("Auto-refresh every 60s while this tab is open", value=False)

    if "live_position" not in st.session_state:
        st.session_state.live_position = None
    if "live_trades_today" not in st.session_state:
        st.session_state.live_trades_today = 0
    if "live_log" not in st.session_state:
        st.session_state.live_log = []

    check_col, status_col = st.columns([1, 3])
    with check_col:
        check_now = st.button("Check Latest Signal", type="primary", disabled=not token)

    if not token:
        st.info("Enter your Upstox access token in the sidebar to check live signals.")
    elif check_now or auto_poll:
        try:
            df = fetch_intraday(token)
        except requests.RequestException as e:
            st.error(f"Data fetch failed: {e}")
            df = pd.DataFrame()

        if not df.empty and len(df) >= 25:
            df = compute_indicators(df)
            df, live_diagnostics = generate_sets(df)
            latest = df.iloc[-1]

            if not live_diagnostics["volume_available"]:
                st.warning(
                    "⚠️ Zero volume reported for this instrument (normal for the Nifty index itself) — "
                    "V-set has fallen back to VWAP-position only, volume-spike check dropped."
                )

            colA, colB, colC = st.columns(3)
            colA.metric("Trend (T)", "Bull" if latest["t_bull"] else "Bear" if latest["t_bear"] else "Neutral")
            colB.metric("Momentum (M)", "Bull" if latest["m_bull"] else "Bear" if latest["m_bear"] else "Neutral")
            colC.metric("Volume (V)", "Bull" if latest["v_bull"] else "Bear" if latest["v_bear"] else "Neutral")

            st.metric("Combined Signal (T ∩ M ∩ V)", latest["signal"] or "No trade")
            st.line_chart(df.set_index("datetime")[["close", "vwap", "ema20", "ema50"]].tail(60))

            now_time = latest["datetime"].time()
            can_trade = SESSION_START <= now_time <= SESSION_END
            position = st.session_state.live_position

            if position is None and latest["signal"] in ("long", "short") and can_trade \
                    and st.session_state.live_trades_today < max_trades:
                entry_price = latest["close"]
                stop_dist = max(latest["atr14"], entry_price * 0.0005)
                if latest["signal"] == "long":
                    sl, target, side = entry_price - stop_dist, entry_price + stop_dist * rr_mult, "BUY"
                else:
                    sl, target, side = entry_price + stop_dist, entry_price - stop_dist * rr_mult, "SELL"
                lots = max(int((capital * risk_pct) / (stop_dist * lot_size)), 0)

                if lots > 0:
                    result = place_order(token, dry_run, side, lots * lot_size, live_instrument_key)
                    st.session_state.live_position = {"side": latest["signal"], "entry_price": entry_price,
                                                        "sl": sl, "target": target, "qty": lots,
                                                        "entry_time": str(latest["datetime"])}
                    st.session_state.live_trades_today += 1
                    st.session_state.live_log.append({**st.session_state.live_position, "order_result": str(result)})
                    st.success(f"{'[DRY RUN] ' if dry_run else ''}Opened {latest['signal']} at {entry_price:.1f} "
                               f"| SL {sl:.1f} | Target {target:.1f} | Lots {lots}")

            elif position is not None:
                hit_sl = (latest["low"] <= position["sl"]) if position["side"] == "long" else (latest["high"] >= position["sl"])
                hit_target = (latest["high"] >= position["target"]) if position["side"] == "long" else (latest["low"] <= position["target"])
                vwap_break = (latest["close"] < latest["vwap"]) if position["side"] == "long" else (latest["close"] > latest["vwap"])
                if hit_sl or hit_target or vwap_break or now_time >= SQUARE_OFF:
                    reason = "stop_loss" if hit_sl else "target" if hit_target else "vwap_break" if vwap_break else "square_off"
                    exit_side = "SELL" if position["side"] == "long" else "BUY"
                    result = place_order(token, dry_run, exit_side, position["qty"] * lot_size, live_instrument_key)
                    pnl = (latest["close"] - position["entry_price"]) * position["qty"] * lot_size * (1 if position["side"] == "long" else -1)
                    st.session_state.live_log.append({**position, "exit_price": latest["close"], "exit_reason": reason,
                                                        "pnl": pnl, "order_result": str(result)})
                    st.session_state.live_position = None
                    st.info(f"{'[DRY RUN] ' if dry_run else ''}Closed position ({reason}), PnL {pnl:,.0f}")
                else:
                    st.write(f"Holding {position['side']} from {position['entry_price']:.1f} "
                             f"| SL {position['sl']:.1f} | Target {position['target']:.1f}")
        else:
            st.info("Not enough candles yet for indicators (need 20+ this session).")

    if st.session_state.live_log:
        st.subheader("Session Log")
        st.dataframe(pd.DataFrame(st.session_state.live_log), use_container_width=True)

    if auto_poll:
        time.sleep(60)
        st.rerun()

# ---------------------------------------------------------------- About tab
with tab_about:
    st.markdown("""
### Strategy Rules
- **T (Trend):** Price > EMA20 > EMA50 = bull, reverse = bear
- **M (Momentum):** RSI(14) > 60 & rising = bull, RSI(14) < 40 & falling = bear
- **V (Volume/Price-Action):** Price vs VWAP + candle volume > 1.5x 20-candle average
- **Entry:** Only when T, M, and V all agree (T ∩ M ∩ V)
- **Exit:** Stop-loss (ATR-based), target (1.5–2x stop), or VWAP break — whichever comes first
- **Risk:** 1% capital per trade (default), max 3 trades/day, no entries before 9:30 or after 3:00 PM

### Why this app can't be a full unattended auto-trader
Streamlit reruns the whole script on every interaction and hosted apps (streamlit.app / Community
Cloud) can sleep, restart, or lose session state at any time — especially on the free tier. That's
fine for backtesting and for watching signals while you're at your desk, but it is **not** a safe
foundation for a real trading loop that must keep running and managing risk even if nobody's
watching. For that, run `auto_trade.py` as a background process on your own machine or a small VPS
(e.g. with `systemd` or `cron` + a process manager), where it can run continuously independent of
any browser tab.

### Data limitations
- Upstox minute-level historical candles only go back to **January 2022** (not 5 full years —
  this is Upstox's own platform limit).
- Backtest results exclude slippage, brokerage, and taxes (STT, etc.) — real performance will be
  worse than the backtest shows.
- Upstox access tokens expire daily and must be regenerated each morning.

### Not financial advice
This tool is for education and testing your own strategy logic. Nothing here is a recommendation
to trade, and past backtested performance does not predict future results.
""")
