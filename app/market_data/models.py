"""Market data models.

Every quote carries the UTC instant it was received and the source that
produced it. Freshness is computed from that timestamp, never from "when we
happened to look", so a stalled feed is detectable rather than invisible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.utilities.timeutils import age_seconds, ensure_utc


@dataclass(frozen=True, slots=True)
class Quote:
    """A point-in-time view of an instrument."""

    contract_key: str
    symbol: str
    received_at: datetime
    source: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    bid_size: int | None = None
    ask_size: int | None = None

    #: Carried from the broker tick. A delayed quote is not a price an
    #: automated system may act on, and the transmit gate refuses it.
    is_delayed: bool = False

    def __post_init__(self) -> None:
        # Rejects naive datetimes outright; see app.utilities.timeutils.
        ensure_utc(self.received_at)

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal(2)
        return self.last

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    def age_seconds(self, *, now: datetime | None = None) -> float:
        return age_seconds(self.received_at, now=now)

    def is_fresh(self, max_age_seconds: float, *, now: datetime | None = None) -> bool:
        """Freshness test used by the risk manager and the transmit gate.

        ``max_age_seconds <= 0`` means the limit is unconfigured, which means
        NOT fresh. A negative age (a tick stamped in the future) also fails,
        because it indicates a clock problem.
        """
        if max_age_seconds <= 0:
            return False
        age = self.age_seconds(now=now)
        return 0 <= age <= max_age_seconds

    def has_prices(self) -> bool:
        return self.mid is not None

    def describe(self) -> dict[str, object]:
        return {
            "contract_key": self.contract_key,
            "symbol": self.symbol,
            "received_at": self.received_at.isoformat(),
            "source": self.source,
            "bid": None if self.bid is None else str(self.bid),
            "ask": None if self.ask is None else str(self.ask),
            "last": None if self.last is None else str(self.last),
            "mid": None if self.mid is None else str(self.mid),
        }


@dataclass(frozen=True, slots=True)
class MarketDataStatus:
    """Snapshot for /status and the risk context."""

    contract_key: str | None
    last_update_at: datetime | None
    age_seconds: float | None
    max_age_seconds: float
    fresh: bool
    source: str | None
    subscribed: bool

    def describe(self) -> dict[str, object]:
        return {
            "contract_key": self.contract_key,
            "last_update_at": self.last_update_at.isoformat() if self.last_update_at else None,
            "age_seconds": self.age_seconds,
            "max_age_seconds": self.max_age_seconds,
            "fresh": self.fresh,
            "source": self.source,
            "subscribed": self.subscribed,
        }


__all__ = ["MarketDataStatus", "Quote"]
