"""The no-op strategy.

Consumes market data and never trades. This is the only strategy in this phase,
and it is deliberate: the infrastructure is being proven, not a trading edge.

It is genuinely useful rather than a stub -- it exercises the full data path
(subscribe -> tick -> quote -> strategy) so the deployed system demonstrates
that the pipeline works while being structurally incapable of producing a
trade.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.logging_config import get_logger
from app.market_data.models import Quote
from app.signals.models import TradeIntent
from app.strategy.base import Strategy

_LOG = get_logger("strategy.noop")

#: How often to log that data is flowing. Logging every tick would drown the
#: log; logging nothing would make a stalled feed indistinguishable from a quiet
#: one.
_LOG_EVERY_N_QUOTES = 60


class NoOpStrategy(Strategy):
    """Observes quotes. Produces no intents, ever."""

    name = "noop"

    def on_quote(self, quote: Quote) -> Sequence[TradeIntent]:
        if self._quotes_seen % _LOG_EVERY_N_QUOTES == 1:
            _LOG.info(
                "noop strategy observing market data",
                extra={
                    "event": "strategy.observed",
                    "strategy": self.name,
                    "quotes_seen": self._quotes_seen,
                    **quote.describe(),
                },
            )
        return ()


#: Strategy registry. Adding a strategy means adding it here; the runtime
#: refuses to start with an unknown ``STRATEGY_NAME`` rather than silently
#: falling back to a default.
STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    NoOpStrategy.name: NoOpStrategy,
}


def build_strategy(name: str, *, enabled: bool = True) -> Strategy:
    key = name.strip().lower()
    strategy_class = STRATEGY_REGISTRY.get(key)
    if strategy_class is None:
        raise KeyError(
            f"unknown strategy {name!r}; registered strategies: {sorted(STRATEGY_REGISTRY)}"
        )
    return strategy_class(enabled=enabled)


__all__ = ["STRATEGY_REGISTRY", "NoOpStrategy", "build_strategy"]
