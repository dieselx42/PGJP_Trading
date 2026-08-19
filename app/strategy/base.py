"""Strategy base classes.

Design constraints that later strategies must keep:

* ``on_quote`` / ``on_bar`` are pure with respect to the outside world. They
  may keep internal state, but they perform no I/O and reach no broker.
* They return intents. They cannot create orders.
* They receive only market data (:class:`~app.market_data.models.Quote` or
  :class:`~app.backtest.models.Bar`) and fill notifications. Whether the data
  came from IBKR, the mock source, or a historical replay is invisible.

Two kinds of strategy
---------------------
:class:`Strategy` consumes quotes -- point-in-time bid/ask/last. The live
runtime feeds these directly from broker ticks.

:class:`BarStrategy` consumes OHLCV bars. A strategy defined on candle
structure -- an opening range measured from a candle's high and low, a trailing
stop that follows "the highest price reached" -- cannot be expressed honestly
on closes alone: the range would be measured too narrow and the peak too low,
each wrong in a different direction. **The live runtime does not support bar
strategies yet** (it has no tick-to-bar builder) and refuses to start with one
rather than silently feeding it nothing; the backtest engine supports them
fully. This is deliberate: backtest first, wire live second.

Fill feedback
-------------
``on_fill`` tells a strategy what its intent actually cost. A strategy that
sets stops "$0.65 from the entry price" needs the *real* entry price -- the
signal candle's close is where it decided, not where it filled. The default is
a no-op so quote strategies that do not care are unaffected.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.enums import OrderSide
from app.market_data.models import Quote
from app.signals.models import TradeIntent

if TYPE_CHECKING:
    from app.backtest.models import Bar


class Strategy(ABC):
    """Base class for all trading strategies."""

    #: Registry name, matched against ``STRATEGY_NAME``.
    name: str = "abstract"

    def __init__(self, *, enabled: bool = True, params: dict[str, Any] | None = None) -> None:
        self._enabled = enabled
        self._params = dict(params or {})
        self._quotes_seen = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def disable(self, reason: str) -> None:
        """Disable the strategy. Intentionally one-way within a process run."""
        self._enabled = False
        self._params["disabled_reason"] = reason

    @property
    def params(self) -> dict[str, Any]:
        return dict(self._params)

    @property
    def quotes_seen(self) -> int:
        return self._quotes_seen

    def handle_quote(self, quote: Quote) -> Sequence[TradeIntent]:
        """Entry point used by the runtime.

        Counts the quote, short-circuits when disabled, and delegates to
        :meth:`on_quote`. A disabled strategy returns nothing regardless of what
        its own logic would have done.
        """
        self._quotes_seen += 1
        if not self._enabled:
            return ()
        return self.on_quote(quote)

    @abstractmethod
    def on_quote(self, quote: Quote) -> Sequence[TradeIntent]:
        """React to a quote. Return zero or more intents."""

    def on_fill(self, *, side: OrderSide, quantity: int, price: Decimal) -> None:  # noqa: B027
        """Notification that an order (traced back to this strategy) filled.

        Deliberately a no-op default rather than abstract: a strategy that
        computes levels from its entry price overrides this; one that only
        expresses targets does not care and should not be forced to say so.
        Never emits intents -- reacting to a fill happens on the next bar or
        quote, with market data in hand.
        """

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "enabled": self._enabled,
            "quotes_seen": self._quotes_seen,
            "params": self._params,
        }


class BarStrategy(Strategy):
    """A strategy defined on OHLCV bars rather than quotes.

    Supported by the backtest engine. The live runtime refuses to start with
    one until it has a tick-to-bar builder -- see the module docstring.
    """

    def handle_bar(self, bar: Bar) -> Sequence[TradeIntent]:
        """Entry point used by the replay. Mirrors :meth:`handle_quote`."""
        self._quotes_seen += 1
        if not self._enabled:
            return ()
        return self.on_bar(bar)

    @abstractmethod
    def on_bar(self, bar: Bar) -> Sequence[TradeIntent]:
        """React to a completed bar. Return zero or more intents."""

    def on_quote(self, quote: Quote) -> Sequence[TradeIntent]:
        """Bar strategies do not consume quotes.

        Raises rather than returning () so a runtime that wrongly feeds one
        quotes fails loudly on the first tick instead of silently never
        trading -- a strategy that cannot see the market must not appear to be
        watching it.
        """
        raise NotImplementedError(
            f"{self.name} is a bar strategy; it cannot run on a quote feed. "
            "The live runtime must refuse to start with it until a bar feed exists."
        )


__all__ = ["BarStrategy", "Strategy"]
