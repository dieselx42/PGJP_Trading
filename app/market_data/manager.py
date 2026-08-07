"""Market data manager.

Owns subscriptions, stores the latest :class:`~app.market_data.models.Quote`
per contract, and answers the one question the safety machinery cares about:
*is this data fresh enough to trade on right now?*

The answer is deliberately conservative:

* no data at all -> not fresh
* data older than ``MARKET_DATA_MAX_AGE_SECONDS`` -> not fresh
* ``MARKET_DATA_MAX_AGE_SECONDS`` unconfigured (0) -> not fresh
* a timestamp in the future -> not fresh (the clock is wrong)
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from app.broker.base import Broker
from app.broker.models import BrokerError, BrokerPermissionError, MarketDataTick
from app.contracts.models import QualifiedContract
from app.logging_config import get_logger
from app.market_data.models import MarketDataStatus, Quote
from app.utilities.timeutils import utc_now

_LOG = get_logger("market_data")

QuoteListener = Callable[[Quote], None]


class MarketDataManager:
    """Tracks the freshest quote for each subscribed contract."""

    def __init__(
        self,
        broker: Broker,
        *,
        max_age_seconds: float,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._broker = broker
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        self._quotes: dict[str, Quote] = {}
        self._subscribed: dict[str, QualifiedContract] = {}
        self._listeners: list[QuoteListener] = []
        self._permission_denied: dict[str, str] = {}

    @property
    def max_age_seconds(self) -> float:
        return self._max_age_seconds

    def add_listener(self, listener: QuoteListener) -> None:
        self._listeners.append(listener)

    def subscribed_contracts(self) -> Sequence[QualifiedContract]:
        return tuple(self._subscribed.values())

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    async def subscribe(self, contract: QualifiedContract) -> None:
        key = contract.key()
        if key in self._subscribed:
            return
        self._subscribed[key] = contract
        _LOG.info(
            "market data subscription requested",
            extra={
                "event": "market_data.subscribe",
                "contract_key": key,
                "symbol": contract.symbol,
                "con_id": contract.con_id,
            },
        )
        await self.poll(contract)

    async def unsubscribe(self, contract: QualifiedContract) -> None:
        key = contract.key()
        self._subscribed.pop(key, None)
        try:
            await self._broker.cancel_market_data(contract)
        except BrokerError as exc:
            _LOG.warning(
                "failed to cancel market data",
                extra={"event": "market_data.cancel_failed", "error": str(exc)},
            )

    def clear(self) -> None:
        """Drop all cached quotes.

        Called on broker disconnect. Retaining quotes across a disconnect would
        let the system believe it has fresh data while the feed is gone, which
        is exactly the failure the freshness check exists to prevent.
        """
        self._quotes.clear()
        self._subscribed.clear()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def poll(self, contract: QualifiedContract) -> Quote | None:
        """Fetch the current tick for one contract and record it."""
        key = contract.key()
        try:
            tick = await self._broker.request_market_data(contract)
        except BrokerPermissionError as exc:
            # Record once and stop asking. Repeating a request the broker has
            # already refused on entitlement grounds achieves nothing.
            if key not in self._permission_denied:
                self._permission_denied[key] = str(exc)
                _LOG.error(
                    "market data permission denied",
                    extra={
                        "event": "market_data.permission_denied",
                        "contract_key": key,
                        "symbol": contract.symbol,
                        "error": str(exc),
                        "retryable": False,
                    },
                )
            return None
        except BrokerError as exc:
            _LOG.warning(
                "market data request failed",
                extra={
                    "event": "market_data.request_failed",
                    "contract_key": key,
                    "error": str(exc),
                },
            )
            return None

        return self.record_tick(contract, tick)

    async def poll_all(self) -> Sequence[Quote]:
        quotes: list[Quote] = []
        for contract in tuple(self._subscribed.values()):
            quote = await self.poll(contract)
            if quote is not None:
                quotes.append(quote)
        return tuple(quotes)

    def record_tick(self, contract: QualifiedContract, tick: MarketDataTick) -> Quote | None:
        """Convert a broker tick into a quote, dropping price-less updates."""
        quote = Quote(
            contract_key=contract.key(),
            symbol=contract.symbol,
            received_at=tick.received_at,
            source=tick.source,
            bid=tick.bid,
            ask=tick.ask,
            last=tick.last,
            bid_size=tick.bid_size,
            ask_size=tick.ask_size,
        )
        if not quote.has_prices():
            # A tick with no prices tells us the feed is alive but gives us
            # nothing to trade on. Recording it would falsely refresh the age.
            _LOG.debug(
                "ignoring price-less market data update",
                extra={"event": "market_data.empty_tick", "contract_key": quote.contract_key},
            )
            return None

        self._quotes[quote.contract_key] = quote
        for listener in self._listeners:
            listener(quote)
        return quote

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def latest(self, contract_key: str) -> Quote | None:
        return self._quotes.get(contract_key)

    def age_seconds(self, contract_key: str) -> float | None:
        quote = self._quotes.get(contract_key)
        if quote is None:
            return None
        return quote.age_seconds(now=self._clock())

    def is_fresh(self, contract_key: str) -> bool:
        quote = self._quotes.get(contract_key)
        if quote is None:
            return False
        return quote.is_fresh(self._max_age_seconds, now=self._clock())

    def status(self, contract_key: str | None) -> MarketDataStatus:
        quote = self._quotes.get(contract_key) if contract_key else None
        return MarketDataStatus(
            contract_key=contract_key,
            last_update_at=quote.received_at if quote else None,
            age_seconds=quote.age_seconds(now=self._clock()) if quote else None,
            max_age_seconds=self._max_age_seconds,
            fresh=self.is_fresh(contract_key) if contract_key else False,
            source=quote.source if quote else None,
            subscribed=bool(contract_key and contract_key in self._subscribed),
        )

    def permission_denials(self) -> dict[str, str]:
        return dict(self._permission_denied)


__all__ = ["MarketDataManager", "QuoteListener"]
