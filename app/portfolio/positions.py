"""Position book.

The broker is always the authority on what we actually hold. The local book
exists so that a *disagreement* is detectable -- if we only ever asked the
broker, a missed fill or a stale local state would be invisible rather than
loud.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from app.broker.models import BrokerPosition
from app.enums import OrderSide
from app.utilities.timeutils import ensure_utc, utc_now


@dataclass(frozen=True, slots=True)
class Position:
    """A held position, signed (positive long, negative short)."""

    con_id: int
    symbol: str
    local_symbol: str
    quantity: int
    average_cost: Decimal
    updated_at: datetime
    account_id: str = ""
    source: str = "local"

    def __post_init__(self) -> None:
        ensure_utc(self.updated_at)

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    def describe(self) -> dict[str, object]:
        return {
            "con_id": self.con_id,
            "symbol": self.symbol,
            "local_symbol": self.local_symbol,
            "quantity": self.quantity,
            "average_cost": str(self.average_cost),
            "updated_at": self.updated_at.isoformat(),
            "source": self.source,
        }

    @classmethod
    def from_broker(cls, position: BrokerPosition, *, at: datetime | None = None) -> Position:
        return cls(
            con_id=position.con_id,
            symbol=position.symbol,
            local_symbol=position.local_symbol,
            quantity=position.quantity,
            average_cost=position.average_cost,
            updated_at=at or utc_now(),
            account_id=position.account_id,
            source="broker",
        )


class PositionBook:
    """In-memory view of current positions, keyed by conId."""

    def __init__(self, positions: Iterable[Position] = ()) -> None:
        self._positions: dict[int, Position] = {p.con_id: p for p in positions if not p.is_flat}

    def __len__(self) -> int:
        return len(self._positions)

    def __contains__(self, con_id: object) -> bool:
        return con_id in self._positions

    def all(self) -> Sequence[Position]:
        return tuple(self._positions.values())

    def get(self, con_id: int) -> Position | None:
        return self._positions.get(con_id)

    def quantity(self, con_id: int) -> int:
        """Signed position size. Zero when we hold nothing."""
        position = self._positions.get(con_id)
        return 0 if position is None else position.quantity

    def upsert(self, position: Position) -> None:
        if position.is_flat:
            self._positions.pop(position.con_id, None)
            return
        self._positions[position.con_id] = position

    def replace_all(self, positions: Iterable[Position]) -> None:
        """Adopt the broker's view wholesale, used after a successful reconcile."""
        self._positions = {p.con_id: p for p in positions if not p.is_flat}

    def apply_fill(
        self,
        *,
        con_id: int,
        symbol: str,
        local_symbol: str,
        side: OrderSide,
        quantity: int,
        price: Decimal,
        at: datetime | None = None,
    ) -> Position | None:
        """Apply an execution to the local book.

        Average cost is only recalculated when the position grows in the same
        direction; reducing or reversing a position keeps the cost basis of the
        remaining side, which is the conventional treatment.
        """
        timestamp = at or utc_now()
        delta = quantity * side.sign
        existing = self._positions.get(con_id)
        previous_quantity = existing.quantity if existing else 0
        new_quantity = previous_quantity + delta

        if new_quantity == 0:
            self._positions.pop(con_id, None)
            return None

        if existing is None or previous_quantity == 0:
            average_cost = price
        elif (previous_quantity > 0) == (delta > 0):
            total = abs(previous_quantity) + abs(delta)
            average_cost = (
                existing.average_cost * abs(previous_quantity) + price * abs(delta)
            ) / Decimal(total)
        else:
            average_cost = existing.average_cost

        updated = Position(
            con_id=con_id,
            symbol=symbol,
            local_symbol=local_symbol,
            quantity=new_quantity,
            average_cost=average_cost,
            updated_at=timestamp,
            account_id=existing.account_id if existing else "",
            source="local",
        )
        self._positions[con_id] = updated
        return updated

    def touch(self, con_id: int, *, at: datetime) -> None:
        existing = self._positions.get(con_id)
        if existing is not None:
            self._positions[con_id] = replace(existing, updated_at=at)

    def describe(self) -> list[dict[str, object]]:
        return [p.describe() for p in self._positions.values()]


__all__ = ["Position", "PositionBook"]
