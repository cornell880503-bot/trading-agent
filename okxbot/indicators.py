"""Technical indicators, computed locally with pandas.

Deliberately dependency-light: no TA-Lib, no pandas-ta. Every function here is
a few lines, unit-tested, and produces numbers identical to what TradingView
shows -- which matters, because the analysis layer reasons about these values
and a silently different RSI means silently different decisions.

Nothing in this module knows about the LLM. It turns candles into numbers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# OKX candle tuple layout: ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm
CANDLE_COLUMNS = ["ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_quote", "confirm"]


def candles_to_frame(rows: list[list[str]], drop_unconfirmed: bool = True) -> pd.DataFrame:
    """Normalise a raw OKX candle response into an ascending, typed frame.

    Two traps are handled here:

    * OKX returns candles **newest first**; every indicator below assumes the
      opposite, so the frame is reversed.
    * The most recent candle is still forming and carries ``confirm == "0"``.
      Leaving it in makes indicators repaint and backtests disagree with live
      runs, so it is dropped by default.
    """
    if not rows:
        return pd.DataFrame(columns=CANDLE_COLUMNS[:-1]).set_index(
            pd.DatetimeIndex([], tz="UTC", name="ts")
        )

    width = len(rows[0])
    frame = pd.DataFrame(rows, columns=CANDLE_COLUMNS[:width])

    if drop_unconfirmed and "confirm" in frame.columns:
        frame = frame[frame["confirm"] == "1"]

    frame = frame.iloc[::-1].reset_index(drop=True)
    numeric = [c for c in ("open", "high", "low", "close", "vol", "vol_ccy", "vol_quote") if c in frame]
    frame[numeric] = frame[numeric].astype(float)
    frame["ts"] = pd.to_datetime(frame["ts"].astype("int64"), unit="ms", utc=True)
    frame = frame.set_index("ts")
    return frame.drop(columns=[c for c in ("confirm",) if c in frame.columns])


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's moving average: SMA seed, then ``alpha = 1/period`` recursion.

    ``ewm(alpha=1/period)`` alone is close but seeds from the first observation,
    which leaves a visible offset for the first few hundred bars. RSI/ATR/ADX all
    depend on this, so it is worth getting exactly right.
    """
    values = series.to_numpy(dtype=float)
    out = np.full(values.shape, np.nan)
    if len(values) < period or period < 1:
        return pd.Series(out, index=series.index, name=series.name)

    acc = float(np.nanmean(values[:period]))
    out[period - 1] = acc
    alpha = 1.0 / period
    for i in range(period, len(values)):
        acc += alpha * (values[i] - acc)
        out[i] = acc
    return pd.Series(out, index=series.index, name=series.name)


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    # A zero average loss is a genuine 100, not a division error.
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    return out.where(avg_loss != 0.0, 100.0).where(avg_gain.notna(), np.nan)


def true_range(frame: pd.DataFrame) -> pd.Series:
    prev_close = frame["close"].shift(1)
    spans = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return spans.max(axis=1)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    return rma(true_range(frame), period)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(close: pd.Series, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = sma(close, period)
    # ddof=0 (population) is what TradingView uses; ddof=1 shifts the bands.
    dev = close.rolling(period).std(ddof=0)
    return pd.DataFrame({"bb_mid": mid, "bb_upper": mid + std * dev, "bb_lower": mid - std * dev})


def donchian(frame: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    upper = frame["high"].rolling(period).max()
    lower = frame["low"].rolling(period).min()
    return pd.DataFrame({"dc_upper": upper, "dc_lower": lower, "dc_mid": (upper + lower) / 2.0})


def adx(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    up = frame["high"].diff()
    down = -frame["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=frame.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=frame.index)

    atr_n = rma(true_range(frame), period)
    plus_di = 100.0 * rma(plus_dm, period) / atr_n
    minus_di = 100.0 * rma(minus_dm, period) / atr_n
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": rma(dx, period)})


def pivots(frame: pd.DataFrame, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Fractal swing highs/lows.

    A pivot is only knowable ``right`` bars after the fact, so the returned
    flags are shifted into the past -- they are never set on the newest bars.
    That lag is real and the analysis layer must respect it.
    """
    window = left + right + 1
    is_high = (
        frame["high"].rolling(window, center=True).max().eq(frame["high"])
        & frame["high"].notna()
    )
    is_low = (
        frame["low"].rolling(window, center=True).min().eq(frame["low"])
        & frame["low"].notna()
    )
    # Blank the tail: those bars cannot be confirmed yet.
    if right:
        is_high.iloc[-right:] = False
        is_low.iloc[-right:] = False
    return pd.DataFrame({"pivot_high": is_high, "pivot_low": is_low})


def enrich(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach the standard indicator set used by the snapshot builder."""
    if frame.empty:
        return frame
    out = frame.copy()
    out["ema20"] = ema(out["close"], 20)
    out["ema50"] = ema(out["close"], 50)
    out["ema200"] = ema(out["close"], 200)
    out["rsi14"] = rsi(out["close"], 14)
    out["atr14"] = atr(out, 14)
    out["vol_sma20"] = sma(out["vol"], 20)
    out = out.join([macd(out["close"]), bollinger(out["close"]), donchian(out), adx(out), pivots(out)])
    out["atr_pct"] = 100.0 * out["atr14"] / out["close"]
    return out


def recent_levels(frame: pd.DataFrame, lookback: int = 120, max_levels: int = 4) -> dict:
    """Confirmed swing highs/lows near the current price, as support/resistance.

    Returned newest-first and split by side, because "the nearest untested level
    above" is the question an entry or target actually needs answered.
    """
    if frame.empty or "pivot_high" not in frame.columns:
        return {"resistance": [], "support": []}

    tail = frame.tail(lookback)
    last = float(tail["close"].iloc[-1])
    highs = tail.loc[tail["pivot_high"], "high"].tolist()
    lows = tail.loc[tail["pivot_low"], "low"].tolist()

    resistance = sorted({round(h, 8) for h in highs if h > last})[:max_levels]
    support = sorted({round(l, 8) for l in lows if l < last}, reverse=True)[:max_levels]
    return {"resistance": resistance, "support": support}
