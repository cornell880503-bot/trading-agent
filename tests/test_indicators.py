import numpy as np
import pandas as pd

from okxbot.indicators import (
    atr, candles_to_frame, ema, enrich, pivots, recent_levels, rma, rsi,
)


def make_rows(n=60, start=100.0, step=1.0, confirm_last=False):
    """Newest-first rows in OKX's candle layout, like the API returns."""
    rows = []
    for i in range(n):
        price = start + i * step
        ts = str(1_700_000_000_000 + i * 3_600_000)
        rows.append([ts, str(price), str(price + 1), str(price - 1), str(price + 0.5),
                     "10", "10", "1000", "1" if (i < n - 1 or confirm_last) else "0"])
    return list(reversed(rows))


def test_frame_is_reversed_into_ascending_order():
    frame = candles_to_frame(make_rows(10, confirm_last=True))
    assert frame.index.is_monotonic_increasing
    assert frame["close"].iloc[0] < frame["close"].iloc[-1]


def test_forming_candle_is_dropped_by_default():
    rows = make_rows(10, confirm_last=False)
    kept = candles_to_frame(rows)
    everything = candles_to_frame(rows, drop_unconfirmed=False)
    assert len(kept) == 9
    assert len(everything) == 10


def test_empty_response_yields_an_empty_frame_not_a_crash():
    frame = candles_to_frame([])
    assert frame.empty


def test_timestamps_become_utc_aware():
    frame = candles_to_frame(make_rows(5, confirm_last=True))
    assert str(frame.index.tz) == "UTC"


def test_rma_matches_wilders_recursion_by_hand():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    got = rma(series, 3)
    # Seed is the SMA of the first three, then acc += (x - acc)/3.
    acc = 2.0
    expected = [np.nan, np.nan, 2.0]
    for value in (4.0, 5.0, 6.0):
        acc += (value - acc) / 3.0
        expected.append(acc)
    np.testing.assert_allclose(got.to_numpy(), expected, rtol=1e-12)


def test_rma_is_all_nan_when_history_is_too_short():
    assert rma(pd.Series([1.0, 2.0]), 5).isna().all()


def test_rsi_pins_to_100_on_an_unbroken_advance():
    close = pd.Series(np.arange(100, 140, dtype=float))
    assert rsi(close, 14).iloc[-1] == 100.0


def test_rsi_sits_midrange_on_an_oscillating_series():
    close = pd.Series([100 + (i % 2) for i in range(60)], dtype=float)
    value = rsi(close, 14).iloc[-1]
    assert 30.0 < value < 70.0


def test_ema_reaches_the_level_of_a_flat_series():
    close = pd.Series([50.0] * 40)
    assert abs(ema(close, 20).iloc[-1] - 50.0) < 1e-9


def test_atr_is_positive_and_tracks_the_true_range():
    frame = candles_to_frame(make_rows(40, confirm_last=True))
    value = atr(frame, 14).iloc[-1]
    assert value > 0
    # Bars are a constant 2 wide with a 1-wide gap, so TR settles at 2.
    assert abs(value - 2.0) < 0.2


def test_enrich_attaches_every_column_the_snapshot_reads():
    frame = candles_to_frame(make_rows(260, confirm_last=True))
    out = enrich(frame)
    for column in ("ema20", "ema50", "ema200", "rsi14", "atr14", "macd",
                   "signal", "hist", "bb_upper", "dc_upper", "adx", "pivot_high"):
        assert column in out.columns, column


def test_pivots_are_never_flagged_on_unconfirmable_tail_bars():
    frame = candles_to_frame(make_rows(60, confirm_last=True))
    flags = pivots(frame, left=3, right=3)
    assert not flags["pivot_high"].iloc[-3:].any()
    assert not flags["pivot_low"].iloc[-3:].any()


def test_levels_split_around_the_last_close():
    highs = [10, 12, 11, 15, 13, 14, 12, 16, 14, 15] * 6
    frame = pd.DataFrame({
        "open": highs, "high": [h + 1 for h in highs],
        "low": [h - 1 for h in highs], "close": highs, "vol": [1.0] * 60,
    }, index=pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC"))
    levels = recent_levels(enrich(frame))
    last = frame["close"].iloc[-1]
    assert all(r > last for r in levels["resistance"])
    assert all(s < last for s in levels["support"])
