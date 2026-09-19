from decimal import Decimal

import pytest

from okxbot.okx.precision import (
    InstrumentSpec,
    PrecisionError,
    ceil_to_step,
    floor_to_step,
    fmt,
    round_to_step,
)

BTC = InstrumentSpec("BTC-USDT", "0.1", "0.00000001", "0.00001", "BTC", "USDT")
DOGE = InstrumentSpec("DOGE-USDT", "0.00001", "1", "1", "DOGE", "USDT")


def test_steps_round_in_the_requested_direction():
    assert floor_to_step("61234.56", "0.1") == Decimal("61234.5")
    assert ceil_to_step("61234.51", "0.1") == Decimal("61234.6")
    assert round_to_step("61234.55", "0.1") == Decimal("61234.6")


def test_sizes_always_round_down():
    # 0.199999 lots of DOGE must never become 200.
    assert BTC.size(0.123456789) == "0.12345678"
    assert DOGE.size(199.99) == "199"


def test_size_below_minimum_is_an_error_not_a_zero():
    with pytest.raises(PrecisionError, match="below minSz"):
        BTC.size(0.000001)


def test_size_or_none_is_the_non_raising_variant():
    assert BTC.size_or_none(0.000001) is None
    assert BTC.size_or_none(0.5) == "0.5"


def test_prices_are_snapped_to_the_tick_grid():
    assert BTC.price(61234.567) == "61234.6"
    assert DOGE.price(0.123456) == "0.12346"


def test_zero_or_negative_price_is_rejected():
    with pytest.raises(PrecisionError):
        BTC.price(0.0001)


def test_fmt_never_emits_scientific_notation():
    # A float would render 1e-08 here, which OKX rejects outright.
    assert fmt(Decimal("0.00000001")) == "0.00000001"
    assert fmt(Decimal("100.000")) == "100"


def test_step_must_be_positive():
    with pytest.raises(PrecisionError):
        floor_to_step("1", "0")
