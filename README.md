# Nifty Venn (T ∩ M ∩ V) Intraday Strategy — Backtest + Auto-Trade

Implements the confluence strategy: only trade when Trend, Momentum, and Volume/Price-Action
sets all agree.

- **T (Trend):** price > EMA20 > EMA50 (bull) / price < EMA20 < EMA50 (bear)
- **M (Momentum):** RSI(14) > 60 and rising (bull) / RSI(14) < 40 and falling (bear)
- **V (Volume/Price-Action):** price vs VWAP + candle volume > 1.5x 20-candle average

## Files
- `indicators.py` — shared indicator/signal logic (used everywhere, so backtest/live/Streamlit never drift apart)
- `backtest.py` — CLI version: pulls real historical Nifty data from Upstox and simulates the strategy
- `auto_trade.py` — CLI version: polls live data and (optionally) places real orders via Upstox, runs continuously via a `while True` loop — meant to run on your own machine/VPS, not inside Streamlit
- `streamlit_app.py` — web UI version with a Backtest tab, a Live Signal Monitor tab, and an About tab
- `requirements.txt`

## Deploying streamlit_app.py to streamlit.app (Streamlit Community Cloud)
1. Push this whole folder to a GitHub repo (all 5 files, including `indicators.py` — the app imports it, so it must sit next to `streamlit_app.py`)
2. Go to https://share.streamlit.io → "New app" → point it at your repo, branch, and `streamlit_app.py` as the main file
3. Deploy. You'll paste your Upstox access token into the sidebar each session (it's a password-type input, not stored anywhere) — remember tokens expire daily
4. **Read the "About / Limitations" tab in the app** — it explains why the Live Signal Monitor tab is fine for watching signals but is not a substitute for `auto_trade.py` running unattended on a server

## Setup
```bash
pip install -r requirements.txt
```
1. Create an app at https://developer.upstox.com/
2. Complete the OAuth login flow to get an **access token** (valid for one trading day — you regenerate it every morning)
3. `export UPSTOX_ACCESS_TOKEN="your_token_here"`

## Running the backtest
```bash
python backtest.py
```
Outputs:
- Console summary: total trades, win rate, profit factor, expectancy, max drawdown, return %
- `trade_log.csv` — every trade with entry/exit price, time, reason
- `equity_curve.png` — capital over time
- `nifty_5min_cache.csv` — cached data so you don't re-download on every run (delete to refresh)

**Data limitation (Upstox platform limit):** minute-level candles are only available from
**January 2022 onward**. That's about 4.5 years, not the full 5 you asked for — Upstox simply
doesn't have finer-than-daily candles further back. If you need pre-2022 intraday data you'd
need a different vendor (e.g. TrueData, GDFL) and can point `backtest.py`'s data-fetch function
at that source instead — the strategy logic in `indicators.py` stays the same.

## Running auto-trade
```bash
python auto_trade.py
```
- **`DRY_RUN = True` by default** — no real orders, just logged hypothetical signals in `trade_log_live.csv`. Watch this run correctly for several sessions before considering `DRY_RUN = False`.
- Trades **Nifty futures** by default, since the index itself can't be bought/sold directly — set `INSTRUMENT_KEY` to the current front-month contract (it changes on rollover).
- Enforces the same risk rules as the backtest: 1% capital risk per trade, max 3 trades/day, no entries in the first 15 min or after 3 PM, forced square-off at 3:20 PM.

## Before risking real capital
- Backtest results are hypothetical — they don't include slippage, exact fill prices, or brokerage/STT/taxes, all of which eat into real returns, especially with a 5-min timeframe.
- Paper trade or use a broker sandbox for at least a few weeks after backtesting.
- Re-check `LOT_SIZE` and `INSTRUMENT_KEY` before every session — both change over time.
- Nothing here is investment advice — you are responsible for your own trading decisions and risk.
