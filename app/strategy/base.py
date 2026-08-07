"""Strategy base class.

Design constraints that later strategies must keep:

* ``on_quote`` is pure with respect to the outside world. It may keep internal
  state, but it performs no I/O and reaches no broker.
* It returns intents. It cannot create orders.
* It receives only :class:`~app.market_data.models.Quote`. Whether that quote
  came from IBKR, the mock source, or a historical replay is invisible to it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from app.market_data.models import Quote
from app.signals.models import TradeIntent


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

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "enabled": self._enabled,
            "quotes_seen": self._quotes_seen,
            "params": self._params,
        }


__all__ = ["Strategy"]
