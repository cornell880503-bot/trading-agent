"""Market snapshots: the input to the analysis layer.

A snapshot is a compact, self-describing JSON document holding everything an
analyst needs to form a view on one instrument -- multi-timeframe indicator
readings, confirmed swing levels, and a short tail of raw candles for context.

Two decisions shape the format:

* **Numbers are computed here, not by the model.** Asking an LLM to derive RSI
  from raw candles is slow, expensive, and wrong often enough to matter.
* **Only confirmed candles are included.** The still-forming bar is excluded by
  :func:`okxbot.indicators.candles_to_frame`, so a snapshot taken twenty minutes
  into a 4H bar reads the same as one taken five minutes in.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from .indicators import candles_to_frame, enrich, recent_levels

RECENT_BARS = 20


def _round(value, digits: int = 6):
    """JSON-safe rounding. NaN is emitted as null, never as the string 'NaN'."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, digits)


def _trend_label(row) -> str:
    """One phrase describing where price sits in its own moving averages."""
    close, e20, e50, e200 = row.get("close"), row.get("ema20"), row.get("ema50"), row.get("ema200")
    if any(v is None or v != v for v in (close, e20, e50)):
        return "insufficient history"

    above = [name for name, value in (("20", e20), ("50", e50), ("200", e200))
             if value == value and close > value]
    if len(above) == 3:
        return "above all EMAs (20/50/200)"
    if not above:
        return "below all EMAs"
    return "above EMA " + "/".join(above) + ", below the rest"


def timeframe_view(frame: pd.DataFrame, timeframe: str) -> dict:
    if frame.empty:
        return {"timeframe": timeframe, "error": "no confirmed candles returned"}

    enriched = enrich(frame)
    row = enriched.iloc[-1]
    prior = enriched.iloc[-2] if len(enriched) > 1 else row

    macd_hist, prior_hist = row.get("hist"), prior.get("hist")
    momentum = "flat"
    if macd_hist == macd_hist and prior_hist == prior_hist:
        momentum = "expanding" if abs(macd_hist) > abs(prior_hist) else "contracting"

    return {
        "timeframe": timeframe,
        "bars": int(len(enriched)),
        "last_bar_open": enriched.index[-1].isoformat().replace("+00:00", "Z"),
        "close": _round(row["close"], 8),
        "trend": _trend_label(row),
        "ema": {k: _round(row.get(f"ema{k}"), 8) for k in ("20", "50", "200")},
        "rsi14": _round(row.get("rsi14"), 2),
        "macd": {
            "line": _round(row.get("macd"), 6),
            "signal": _round(row.get("signal"), 6),
            "hist": _round(macd_hist, 6),
            "histogram_momentum": momentum,
        },
        "adx14": _round(row.get("adx"), 2),
        "di": {"plus": _round(row.get("plus_di"), 2), "minus": _round(row.get("minus_di"), 2)},
        "atr14": _round(row.get("atr14"), 8),
        "atr_pct_of_price": _round(row.get("atr_pct"), 3),
        "bollinger": {
            "upper": _round(row.get("bb_upper"), 8),
            "mid": _round(row.get("bb_mid"), 8),
            "lower": _round(row.get("bb_lower"), 8),
        },
        "donchian20": {
            "upper": _round(row.get("dc_upper"), 8),
            "lower": _round(row.get("dc_lower"), 8),
        },
        "volume": {
            "last": _round(row.get("vol"), 4),
            "sma20": _round(row.get("vol_sma20"), 4),
            "ratio_to_sma20": _round(
                row.get("vol") / row["vol_sma20"] if row.get("vol_sma20") else None, 3
            ),
        },
        "levels": {
            side: [_round(v, 8) for v in values]
            for side, values in recent_levels(enriched).items()
        },
        "recent_bars": [
            {
                "t": index.isoformat().replace("+00:00", "Z"),
                "o": _round(bar["open"], 8),
                "h": _round(bar["high"], 8),
                "l": _round(bar["low"], 8),
                "c": _round(bar["close"], 8),
                "v": _round(bar["vol"], 4),
            }
            for index, bar in enriched.tail(RECENT_BARS).iterrows()
        ],
    }


def build_snapshot(rest, inst_id: str, timeframes=("1H", "4H", "1D"), limit: int = 300) -> dict:
    """Fetch and assemble a full snapshot for one instrument."""
    ticker = rest.ticker(inst_id)
    last = float(ticker.get("last") or 0)
    open24h = float(ticker.get("open24h") or 0)
    # OKX ships two reference prices and the web UI's headline percentage uses
    # the second one, not the first. Carrying both stops the snapshot from
    # looking wrong next to the exchange's own screen.
    sod_utc0 = float(ticker.get("sodUtc0") or 0)

    views = {}
    for timeframe in timeframes:
        rows = rest.candles(inst_id, bar=timeframe, limit=limit)
        views[timeframe] = timeframe_view(candles_to_frame(rows), timeframe)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "inst_id": inst_id,
        "last_price": _round(last, 8),
        "ticker_24h": {
            # Rolling: open24h is the price exactly 24 hours ago.
            "open": _round(open24h, 8),
            "high": _round(ticker.get("high24h"), 8),
            "low": _round(ticker.get("low24h"), 8),
            "change_pct": _round(100.0 * (last - open24h) / open24h, 3) if open24h else None,
            "base_volume": _round(ticker.get("vol24h"), 4),
            # Calendar: sodUtc0 is the 00:00 UTC open, which is what OKX's own
            # chart header reports. The two percentages legitimately differ.
            "utc_day_open": _round(sod_utc0, 8),
            "change_since_utc_open_pct": (
                _round(100.0 * (last - sod_utc0) / sod_utc0, 3) if sod_utc0 else None
            ),
        },
        "timeframes": views,
        "note": (
            "Indicators are computed on confirmed candles only; the forming bar "
            "is excluded. Swing levels lag by 3 bars by construction."
        ),
    }


def to_json(snapshot: dict) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=False)


def render_text(snapshot: dict) -> str:
    """Terminal view: the numbers a human scans before reading a plan.

    Each row names the bar it came from. Without that, these values look like
    they disagree with the exchange's chart, because the chart's rightmost
    candle is still forming while every number here is from the last closed
    one. Stating the bar open time turns a mystery into a lookup.
    """
    t24 = snapshot["ticker_24h"]
    lines = [
        f"{snapshot['inst_id']}  last {snapshot['last_price']}   "
        f"{t24['change_pct']}% rolling 24h   "
        f"{t24['change_since_utc_open_pct']}% since 00:00 UTC   "
        f"({snapshot['generated_at']})",
        f"  24h range {t24['low']} - {t24['high']}",
    ]
    for timeframe, view in snapshot["timeframes"].items():
        if "error" in view:
            lines.append(f"  [{timeframe}] {view['error']}")
            continue
        levels = view["levels"]
        lines += [
            f"  [{timeframe}] close {view['close']}   "
            f"bar {view['last_bar_open']} (closed)   {view['trend']}",
            f"        RSI(14) {view['rsi14']}   ADX(14) {view['adx14']}   "
            f"ATR(14) {view['atr14']} ({view['atr_pct_of_price']}%)   "
            f"MACD hist {view['macd']['hist']} ({view['macd']['histogram_momentum']})",
            f"        resistance {levels['resistance'] or '-'}   support {levels['support'] or '-'}",
        ]
    lines += [
        "",
        "  Comparing against an exchange chart? Match these first:",
        "    - the bar named above, not the one still forming at the right edge",
        "    - RSI/ADX/ATR period 14; OKX's default RSI panel plots 6/12/24",
        "    - MACD hist here is DIF - DEA. OKX plots 2 x (DIF - DEA), so its",
        "      histogram reads double this value",
        "    - bar times above are UTC; OKX's chart shows your local timezone",
        "    - OKX's headline percentage is the 00:00 UTC one, not rolling 24h",
    ]
    return "\n".join(lines)
