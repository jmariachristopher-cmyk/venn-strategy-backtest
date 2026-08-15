"""
Indicator + signal-set calculations for the T ∩ M ∩ V (Venn) intraday strategy.
Shared by backtest.py and auto_trade.py so live and backtested logic never drift apart.
"""
import pandas as pd
import numpy as np


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    df must have columns: datetime, open, high, low, close, volume
    datetime must be tz-aware or naive IST timestamps, one row per candle.
    Adds: ema20, ema50, rsi14, atr14, vwap, vol_avg20
    VWAP and vol_avg20 reset every trading day.
    """
    df = df.copy()
    df["date"] = df["datetime"].dt.date

    # EMAs
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    # RSI(14) - Wilder's smoothing
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi14"] = 100 - (100 / (1 + rs))
    df["rsi14"] = df["rsi14"].fillna(50)
    df["rsi_slope"] = df["rsi14"].diff()

    # ATR(14)
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

    # VWAP - resets daily. If an instrument reports zero volume all day (true for the
    # NSE_INDEX itself - an index isn't traded, only its futures/options are), a real
    # volume-weighted average is undefined (0/0). Fall back to a simple running average
    # of typical price for that day instead of leaving VWAP as NaN all session.
    def _vwap(group):
        typical = (group["high"] + group["low"] + group["close"]) / 3
        if group["volume"].fillna(0).sum() == 0:
            return typical.expanding().mean()
        cum_vp = (typical * group["volume"]).cumsum()
        cum_vol = group["volume"].cumsum().replace(0, np.nan)
        return cum_vp / cum_vol

    df["vwap"] = df.groupby("date", group_keys=False).apply(_vwap)

    # Rolling 20-candle average volume, reset daily (so it doesn't blend into prior session)
    df["vol_avg20"] = df.groupby("date")["volume"].transform(
        lambda s: s.rolling(20, min_periods=5).mean()
    )

    return df


def generate_sets(df: pd.DataFrame):
    """
    Adds boolean columns: t_bull, t_bear, m_bull, m_bear, v_bull, v_bear,
    and combined 'signal' column: 'long', 'short', or None.

    Returns (df, diagnostics) where diagnostics is a dict with per-set counts and a
    'volume_available' flag. NSE_INDEX instruments (e.g. "NSE_INDEX|Nifty 50") report
    zero volume from Upstox, since an index isn't itself traded - only its futures/options
    are. If total volume in the dataset is 0, the volume-spike requirement is automatically
    dropped and V becomes VWAP-position only, so the strategy doesn't silently produce zero
    trades forever. Switch to a futures instrument key for a genuine volume-confirmed V-set.
    """
    df = df.copy()

    df["t_bull"] = (df["close"] > df["ema20"]) & (df["ema20"] > df["ema50"])
    df["t_bear"] = (df["close"] < df["ema20"]) & (df["ema20"] < df["ema50"])

    df["m_bull"] = (df["rsi14"] > 60) & (df["rsi_slope"] > 0)
    df["m_bear"] = (df["rsi14"] < 40) & (df["rsi_slope"] < 0)

    volume_available = df["volume"].fillna(0).sum() > 0
    if volume_available:
        vol_ok = df["volume"] > 1.5 * df["vol_avg20"]
    else:
        vol_ok = pd.Series(True, index=df.index)

    df["v_bull"] = (df["close"] > df["vwap"]) & vol_ok
    df["v_bear"] = (df["close"] < df["vwap"]) & vol_ok

    long_signal = df["t_bull"] & df["m_bull"] & df["v_bull"]
    short_signal = df["t_bear"] & df["m_bear"] & df["v_bear"]

    df["signal"] = None
    df.loc[long_signal, "signal"] = "long"
    df.loc[short_signal, "signal"] = "short"

    diagnostics = {
        "volume_available": volume_available,
        "t_bull_count": int(df["t_bull"].sum()),
        "t_bear_count": int(df["t_bear"].sum()),
        "m_bull_count": int(df["m_bull"].sum()),
        "m_bear_count": int(df["m_bear"].sum()),
        "v_bull_count": int(df["v_bull"].sum()),
        "v_bear_count": int(df["v_bear"].sum()),
        "signal_count": int(df["signal"].notna().sum()),
    }

    return df, diagnostics
