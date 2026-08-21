"""Daily aggregation and the indicators computed from it.

Strategies here receive 1-minute bars, but the ones that survive this
instrument's cost floor decide on *daily* structure -- a $0.373/SOL round trip
is 25-93% of an intraday target and 1-4% of a multi-day move. So more than one
strategy needs the same three things: 1-minute bars rolled into UTC calendar
days, an ATR over those days, and Donchian channels over them.

This module is that shared machinery. It is deliberately free of any trading
opinion: it aggregates and measures, and every rule about what to *do* with
the numbers lives in the strategy that asks for them.

Two conventions the callers depend on, stated once here rather than repeated:

* **Days are UTC calendar days.** Crypto trades around the clock and UTC is
  the only day boundary this system uses anywhere; a "daily close" is the last
  1-minute bar before midnight UTC.
* **A day is complete when a bar belonging to a LATER day arrives.** Nothing
  is ever decided on a day still in progress, which is what keeps a strategy
  from trading a close it has not seen.

Missing days are skipped, never invented. Every window here runs over the last
N days the data actually contains, so a gap shortens history rather than
fabricating a candle -- `bars-info` is where gaps get reported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import pairwise

from app.backtest.models import Bar


@dataclass
class DailyBar:
    """One UTC calendar day, aggregated from the 1-minute bars it contained."""

    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def absorb(self, bar: Bar) -> None:
        self.high = max(self.high, bar.high)
        self.low = min(self.low, bar.low)
        self.close = bar.close


class DailyAggregator:
    """Rolls 1-minute bars into completed UTC days, keeping a bounded history.

    ``feed`` returns the day just completed, or ``None`` while the current day
    is still accumulating -- so a caller's "evaluate once per day" logic is a
    plain ``if completed is not None``, with no date bookkeeping of its own.
    """

    def __init__(self, *, keep: int) -> None:
        if keep < 1:
            raise ValueError(f"keep must be at least 1, got {keep}")
        # One more than the longest window any caller needs: a channel that
        # EXCLUDES the completed day still has to be measurable on the bar
        # that completes it.
        self._keep = keep + 1
        self._days: list[DailyBar] = []
        self._current: DailyBar | None = None

    @property
    def days(self) -> Sequence[DailyBar]:
        """Completed days, oldest first. The last entry is the newest."""
        return self._days

    @property
    def current_day(self) -> date | None:
        """The day still accumulating, if any."""
        return self._current.day if self._current is not None else None

    def feed(self, bar: Bar) -> DailyBar | None:
        day = bar.opened_at.date()
        if self._current is None:
            self._current = DailyBar(
                day=day, open=bar.open, high=bar.high, low=bar.low, close=bar.close
            )
            return None
        if day == self._current.day:
            self._current.absorb(bar)
            return None
        # The date changed, so the previous day is complete. Bars arrive in
        # time order -- the replay sorts and the live bar builder emits
        # monotonically -- so a date change is always forward.
        completed = self._current
        self._days.append(completed)
        del self._days[: -self._keep]
        self._current = DailyBar(
            day=day, open=bar.open, high=bar.high, low=bar.low, close=bar.close
        )
        return completed


def true_range(previous: DailyBar, current: DailyBar) -> Decimal:
    """The classic true range: the day's range, extended by any overnight gap."""
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def average_true_range(days: Sequence[DailyBar], window: int) -> Decimal | None:
    """Simple mean true range over the last ``window`` day-pairs.

    A simple mean rather than Wilder's smoothing, so a reader can check the
    number against the day table by hand. ``None`` while there is not yet
    enough history -- never a partial average, which would read as a real one.
    """
    if window < 1 or len(days) < window + 1:
        return None
    total = Decimal(0)
    for previous, current in pairwise(days[-(window + 1) :]):
        total += true_range(previous, current)
    return total / window


def channel(
    days: Sequence[DailyBar], window: int, *, excluding_last: bool = True
) -> tuple[Decimal, Decimal] | None:
    """``(high, low)`` of the Donchian channel over ``window`` days.

    ``excluding_last`` drops the newest completed day from the window, which
    is what a *breakout* test needs: a channel containing the day that broke
    out can never be broken, so the comparison would silently never fire.
    Pass ``False`` for a channel used as a level rather than as a breakout
    reference. ``None`` while history is short.
    """
    if window < 1:
        return None
    needed = window + 1 if excluding_last else window
    if len(days) < needed:
        return None
    span = days[-(window + 1) : -1] if excluding_last else days[-window:]
    return max(d.high for d in span), min(d.low for d in span)


def mean_close(days: Sequence[DailyBar], window: int) -> Decimal | None:
    """Simple moving average of the last ``window`` daily closes."""
    if window < 1 or len(days) < window:
        return None
    return sum((d.close for d in days[-window:]), Decimal(0)) / window


__all__ = [
    "DailyAggregator",
    "DailyBar",
    "average_true_range",
    "channel",
    "mean_close",
    "true_range",
]
