"""Historical bars, and where they came from.

The whole of this package exists downstream of one warning in
``docs/BACKTESTING_SCOPE.md``: **the price history available today is Solana
spot, and the system trades CME futures.** They are different instruments. Spot
has no basis, no roll, no CME session breaks, and volume that does not reflect
the futures book.

That is acceptable for developing and sanity-checking a strategy and not
acceptable for estimating fills on MSL. The design consequence is that
provenance is carried on every bar and recorded in every result, so a run over
spot data can never be read later as a statement about futures. ``source`` is
part of the primary key rather than a comment.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Final

from app.utilities.timeutils import ensure_utc

#: The only sources that carry bars for the instrument this system actually
#: trades. **Everything else is a proxy**, and that direction matters.
#:
#: This started as the opposite -- a list of known proxy venues -- which meant
#: any source name not on it was silently treated as real futures data and the
#: result quietly dropped its "NOT CME FUTURES" warning. Adding a venue is
#: exactly when that mistake gets made: `binance-us` is not `binance`. An
#: allowlist of futures sources fails closed, so an unrecognised name is
#: labelled a proxy and over-warns rather than under-warns.
FUTURES_SOURCES: Final[frozenset[str]] = frozenset({"ibkr"})

#: Interval name -> length. Used to detect gaps, never to invent a bar.
INTERVAL_SECONDS: Final[dict[str, int]] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}


class BarError(ValueError):
    """Raised when a bar is structurally impossible."""


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV candle.

    Prices are :class:`~decimal.Decimal` and stored as strings, the same as
    every other price in this system. A backtest that accumulates float error
    over 500,000 bars produces a number nobody can reproduce.
    """

    source: str
    symbol: str
    interval: str
    opened_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        ensure_utc(self.opened_at)
        if not self.source.strip():
            raise BarError("source is required; a bar with no provenance is unusable")
        if self.interval not in INTERVAL_SECONDS:
            raise BarError(f"unknown interval {self.interval!r}")
        if self.high < self.low:
            raise BarError(f"high {self.high} is below low {self.low}")
        for name, price in (("open", self.open), ("close", self.close)):
            if not self.low <= price <= self.high:
                raise BarError(f"{name} {price} is outside the low-high range")
        if self.volume < 0:
            raise BarError(f"negative volume {self.volume}")

    @property
    def is_proxy(self) -> bool:
        """True when this bar is not the instrument being traded.

        An unrecognised source is a proxy. Over-warning costs a paragraph in a
        report; under-warning lets a spot backtest be quoted as a futures one.
        """
        return self.source.lower() not in FUTURES_SOURCES

    @property
    def closed_at(self) -> datetime:
        return self.opened_at + timedelta(seconds=INTERVAL_SECONDS[self.interval])

    def describe(self) -> dict[str, object]:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "interval": self.interval,
            "opened_at": self.opened_at.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
        }


@dataclass(frozen=True, slots=True)
class BarGap:
    """A stretch of missing bars.

    Reported, never filled. A missing hour is a fact about the data; inventing
    a price to cover it is how a backtest starts lying, and the invented price
    is indistinguishable from a real one by the time it reaches a strategy.
    """

    after: datetime
    before: datetime
    missing_bars: int

    def describe(self) -> dict[str, object]:
        return {
            "after": self.after.isoformat(),
            "before": self.before.isoformat(),
            "missing_bars": self.missing_bars,
        }


def find_gaps(bars: Sequence[Bar]) -> tuple[BarGap, ...]:
    """Every discontinuity in a time-ordered series.

    Markets close, so not every gap is a fault -- which is exactly why these
    are reported rather than acted on. A human reading the output knows the
    difference between a weekend and a failed download; this function does not
    and should not pretend to.
    """
    if len(bars) < 2:
        return ()
    step = INTERVAL_SECONDS[bars[0].interval]
    gaps: list[BarGap] = []
    for previous, current in pairwise(bars):
        elapsed = (current.opened_at - previous.opened_at).total_seconds()
        if elapsed > step:
            gaps.append(
                BarGap(
                    after=previous.opened_at,
                    before=current.opened_at,
                    missing_bars=int(elapsed // step) - 1,
                )
            )
    return tuple(gaps)


def to_decimal(value: object, *, field: str) -> Decimal:
    """Parse a price, refusing rather than guessing."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BarError(f"{field}: {value!r} is not a number") from exc


__all__ = [
    "FUTURES_SOURCES",
    "INTERVAL_SECONDS",
    "Bar",
    "BarError",
    "BarGap",
    "find_gaps",
    "to_decimal",
]
