"""Exchange precision arithmetic.

Every price and size that leaves this process must sit exactly on the
instrument's grid. Floats cannot do that reliably (``0.1 + 0.2``), so all
quantisation runs through :class:`decimal.Decimal` and the results are returned
as strings -- which is also the only form OKX accepts in a JSON body.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal

from ..errors import OkxBotError


class PrecisionError(OkxBotError):
    """A quantised value fell below the instrument minimum or went non-positive."""


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _quantize(value, step, rounding: str) -> Decimal:
    step_d = _dec(step)
    if step_d <= 0:
        raise PrecisionError(f"step must be positive, got {step!r}")
    return (_dec(value) / step_d).quantize(Decimal(1), rounding=rounding) * step_d


def floor_to_step(value, step) -> Decimal:
    return _quantize(value, step, ROUND_DOWN)


def ceil_to_step(value, step) -> Decimal:
    return _quantize(value, step, ROUND_UP)


def round_to_step(value, step) -> Decimal:
    return _quantize(value, step, ROUND_HALF_UP)


def fmt(value: Decimal) -> str:
    """Render without scientific notation or trailing zero noise."""
    d = _dec(value).normalize()
    if d == d.to_integral_value():
        d = d.quantize(Decimal(1))
    return format(d, "f")


class InstrumentSpec:
    """The subset of ``/api/v5/public/instruments`` the executor needs."""

    def __init__(self, inst_id: str, tick_sz, lot_sz, min_sz, base_ccy: str, quote_ccy: str):
        self.inst_id = inst_id
        self.tick_sz = _dec(tick_sz)
        self.lot_sz = _dec(lot_sz)
        self.min_sz = _dec(min_sz)
        self.base_ccy = base_ccy
        self.quote_ccy = quote_ccy

    @classmethod
    def from_api(cls, payload: dict) -> "InstrumentSpec":
        return cls(
            inst_id=payload["instId"],
            tick_sz=payload["tickSz"],
            lot_sz=payload["lotSz"],
            min_sz=payload["minSz"],
            base_ccy=payload.get("baseCcy", ""),
            quote_ccy=payload.get("quoteCcy", ""),
        )

    def price(self, value) -> str:
        """Prices round to nearest tick: a limit is an intent, not a boundary."""
        px = round_to_step(value, self.tick_sz)
        if px <= 0:
            raise PrecisionError(f"{self.inst_id}: price {value} quantised to {px}")
        return fmt(px)

    def size(self, value) -> str:
        """Sizes always round *down*: never buy more than was authorised."""
        sz = floor_to_step(value, self.lot_sz)
        if sz < self.min_sz:
            raise PrecisionError(
                f"{self.inst_id}: size {value} -> {fmt(sz)} is below minSz {fmt(self.min_sz)}"
            )
        return fmt(sz)

    def size_or_none(self, value) -> str | None:
        """Same as :meth:`size` but returns ``None`` instead of raising.

        Used when splitting one position across several take-profit legs, where
        a dust-sized leg should be folded into its neighbour rather than abort
        the whole submission.
        """
        try:
            return self.size(value)
        except PrecisionError:
            return None

    def __repr__(self) -> str:
        return (
            f"InstrumentSpec({self.inst_id}, tick={fmt(self.tick_sz)}, "
            f"lot={fmt(self.lot_sz)}, min={fmt(self.min_sz)})"
        )
