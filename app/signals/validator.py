"""Signal validation.

The first checkpoint after a strategy speaks. It answers "is this intent
structurally sane and not a duplicate?" -- it does not answer "is this trade
allowed?", which is the risk manager's job.

Duplicate detection is durable, not in-memory-only: the seen set is seeded from
the database at startup so a restart cannot re-admit an intent that has already
been processed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from app.config import SUPPORTED_FUTURES_SYMBOLS
from app.logging_config import get_logger
from app.signals.models import SignalValidation, TradeIntent
from app.utilities.timeutils import age_seconds, utc_now

_LOG = get_logger("signals.validator")

REASON_UNKNOWN_SYMBOL = "SIGNAL_SYMBOL_NOT_SUPPORTED"
REASON_SYMBOL_MISMATCH = "SIGNAL_SYMBOL_NOT_CONFIGURED_INSTRUMENT"
REASON_DUPLICATE = "DUPLICATE_SIGNAL"
REASON_STALE = "SIGNAL_TIMESTAMP_STALE"
REASON_FUTURE = "SIGNAL_TIMESTAMP_IN_FUTURE"
REASON_NOT_ACTIONABLE = "SIGNAL_NOT_ACTIONABLE"
REASON_STRATEGY_MISMATCH = "SIGNAL_FROM_UNKNOWN_STRATEGY"
REASON_POSITION_UNREASONABLE = "SIGNAL_REQUESTED_POSITION_UNREASONABLE"

#: An intent older than this was produced before some event we cannot see and
#: must not be acted on. Independent of market-data freshness.
DEFAULT_MAX_SIGNAL_AGE_SECONDS = 60.0

#: Sanity ceiling that exists to catch a runaway strategy, not to express risk
#: appetite. Real sizing limits live in the risk manager.
ABSOLUTE_POSITION_SANITY_LIMIT = 10_000


class SignalValidator:
    """Validates intents before they reach risk."""

    def __init__(
        self,
        *,
        configured_symbol: str,
        known_strategies: Iterable[str],
        max_signal_age_seconds: float = DEFAULT_MAX_SIGNAL_AGE_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._configured_symbol = configured_symbol.upper()
        self._known_strategies = {name.lower() for name in known_strategies}
        self._max_signal_age_seconds = max_signal_age_seconds
        self._clock = clock
        self._seen_intent_ids: set[str] = set()

    def seed_seen(self, intent_ids: Iterable[str]) -> None:
        """Load previously-processed intent ids, typically from the database."""
        self._seen_intent_ids.update(intent_ids)

    @property
    def seen_count(self) -> int:
        return len(self._seen_intent_ids)

    def validate(self, intent: TradeIntent) -> SignalValidation:
        """Return the validation outcome, listing every problem found."""
        reasons: list[str] = []

        symbol = intent.symbol.upper()
        if symbol not in SUPPORTED_FUTURES_SYMBOLS:
            reasons.append(REASON_UNKNOWN_SYMBOL)
        elif symbol != self._configured_symbol:
            # The process trades exactly one configured instrument. It never
            # switches symbol on its own, including when a request looks
            # plausible.
            reasons.append(REASON_SYMBOL_MISMATCH)

        if intent.strategy_name.lower() not in self._known_strategies:
            reasons.append(REASON_STRATEGY_MISMATCH)

        age = age_seconds(intent.created_at, now=self._clock())
        if age < 0:
            reasons.append(REASON_FUTURE)
        elif age > self._max_signal_age_seconds:
            reasons.append(REASON_STALE)

        if not intent.is_actionable:
            reasons.append(REASON_NOT_ACTIONABLE)

        if abs(intent.requested_position) > ABSOLUTE_POSITION_SANITY_LIMIT:
            reasons.append(REASON_POSITION_UNREASONABLE)

        if intent.intent_id in self._seen_intent_ids:
            reasons.append(REASON_DUPLICATE)

        accepted = not reasons
        if accepted:
            self._seen_intent_ids.add(intent.intent_id)
        else:
            _LOG.info(
                "signal rejected",
                extra={
                    "event": "signal.rejected",
                    "reasons": reasons,
                    **intent.describe(),
                },
            )
        return SignalValidation(accepted=accepted, reasons=tuple(reasons))


__all__ = [
    "ABSOLUTE_POSITION_SANITY_LIMIT",
    "DEFAULT_MAX_SIGNAL_AGE_SECONDS",
    "REASON_DUPLICATE",
    "REASON_FUTURE",
    "REASON_NOT_ACTIONABLE",
    "REASON_POSITION_UNREASONABLE",
    "REASON_STALE",
    "REASON_STRATEGY_MISMATCH",
    "REASON_SYMBOL_MISMATCH",
    "REASON_UNKNOWN_SYMBOL",
    "SignalValidator",
]
