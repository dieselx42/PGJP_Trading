"""Startup and post-reconnect reconciliation.

Reconciliation answers one question before trading resumes: *does what the
broker holds match what we think we hold?*

Until it answers yes, the application stays in SAFE and the transmit gate
refuses every order. An unexplained difference is never resolved by adopting
the broker's numbers silently -- it is reported loudly and requires a human,
because a mismatch means either we missed a fill or something else is trading
this account, and both are situations where more automated trading is the wrong
answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from app.broker.models import BrokerFill, BrokerOrderSnapshot, BrokerPosition
from app.enums import OrderStatus
from app.logging_config import get_logger
from app.portfolio.positions import Position, PositionBook
from app.utilities.timeutils import utc_now

_LOG = get_logger("portfolio.reconciliation")


@dataclass(frozen=True, slots=True)
class PositionDiscrepancy:
    con_id: int
    symbol: str
    local_quantity: int
    broker_quantity: int

    @property
    def delta(self) -> int:
        return self.broker_quantity - self.local_quantity

    def describe(self) -> dict[str, object]:
        return {
            "con_id": self.con_id,
            "symbol": self.symbol,
            "local_quantity": self.local_quantity,
            "broker_quantity": self.broker_quantity,
            "delta": self.delta,
        }


@dataclass(frozen=True, slots=True)
class OrderDiscrepancy:
    broker_order_id: str
    symbol: str
    kind: str
    """``unknown_at_broker`` or ``unknown_locally``."""

    detail: str = ""

    def describe(self) -> dict[str, object]:
        return {
            "broker_order_id": self.broker_order_id,
            "symbol": self.symbol,
            "kind": self.kind,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Outcome of one reconciliation pass."""

    positions_reconciled: bool = False
    orders_reconciled: bool = False
    account_available: bool = False
    position_discrepancies: tuple[PositionDiscrepancy, ...] = ()
    order_discrepancies: tuple[OrderDiscrepancy, ...] = ()
    fills_seen: int = 0
    errors: tuple[str, ...] = ()
    broker_positions: tuple[Position, ...] = field(default=())

    @property
    def succeeded(self) -> bool:
        """True only when everything lines up.

        Note that ``positions_reconciled`` is false whenever there is any
        discrepancy. There is no "close enough".
        """
        return (
            self.positions_reconciled
            and self.orders_reconciled
            and self.account_available
            and not self.errors
        )

    def describe(self) -> dict[str, object]:
        return {
            "succeeded": self.succeeded,
            "positions_reconciled": self.positions_reconciled,
            "orders_reconciled": self.orders_reconciled,
            "account_available": self.account_available,
            "position_discrepancies": [d.describe() for d in self.position_discrepancies],
            "order_discrepancies": [d.describe() for d in self.order_discrepancies],
            "fills_seen": self.fills_seen,
            "errors": list(self.errors),
        }


class Reconciler:
    """Compares broker state with local state."""

    def reconcile(
        self,
        *,
        book: PositionBook,
        broker_positions: Sequence[BrokerPosition],
        broker_open_orders: Sequence[BrokerOrderSnapshot],
        local_open_orders: Sequence[tuple[str, str | None, str]],
        broker_fills: Sequence[BrokerFill],
        account_available: bool,
    ) -> ReconciliationResult:
        """Run a full comparison.

        ``local_open_orders`` is ``(internal_order_id, broker_order_id, symbol)``
        for every order our database believes is working.
        """
        now = utc_now()
        broker_book = tuple(Position.from_broker(p, at=now) for p in broker_positions)

        position_discrepancies = self._compare_positions(book, broker_book)
        order_discrepancies = self._compare_orders(broker_open_orders, local_open_orders)

        result = ReconciliationResult(
            positions_reconciled=not position_discrepancies,
            orders_reconciled=not order_discrepancies,
            account_available=account_available,
            position_discrepancies=position_discrepancies,
            order_discrepancies=order_discrepancies,
            fills_seen=len(broker_fills),
            broker_positions=broker_book,
        )

        if result.succeeded:
            _LOG.info(
                "reconciliation succeeded",
                extra={"event": "reconciliation.succeeded", **result.describe()},
            )
        else:
            _LOG.error(
                "RECONCILIATION FAILED - trading remains prohibited until this is resolved "
                "by a human",
                extra={"event": "reconciliation.failed", **result.describe()},
            )
        return result

    @staticmethod
    def _compare_positions(
        book: PositionBook, broker_positions: Sequence[Position]
    ) -> tuple[PositionDiscrepancy, ...]:
        broker_by_id = {p.con_id: p for p in broker_positions}
        local_by_id = {p.con_id: p for p in book.all()}

        discrepancies: list[PositionDiscrepancy] = []
        for con_id in sorted(set(broker_by_id) | set(local_by_id)):
            broker_position = broker_by_id.get(con_id)
            local_position = local_by_id.get(con_id)
            broker_quantity = broker_position.quantity if broker_position else 0
            local_quantity = local_position.quantity if local_position else 0
            if broker_quantity != local_quantity:
                symbol = (
                    broker_position.symbol
                    if broker_position
                    else (local_position.symbol if local_position else "?")
                )
                discrepancies.append(
                    PositionDiscrepancy(
                        con_id=con_id,
                        symbol=symbol,
                        local_quantity=local_quantity,
                        broker_quantity=broker_quantity,
                    )
                )
        return tuple(discrepancies)

    @staticmethod
    def _compare_orders(
        broker_open_orders: Sequence[BrokerOrderSnapshot],
        local_open_orders: Sequence[tuple[str, str | None, str]],
    ) -> tuple[OrderDiscrepancy, ...]:
        broker_ids = {o.broker_order_id for o in broker_open_orders}
        local_ids = {
            broker_order_id
            for _, broker_order_id, _ in local_open_orders
            if broker_order_id is not None
        }

        discrepancies: list[OrderDiscrepancy] = []

        for snapshot in broker_open_orders:
            if snapshot.broker_order_id not in local_ids:
                discrepancies.append(
                    OrderDiscrepancy(
                        broker_order_id=snapshot.broker_order_id,
                        symbol=snapshot.symbol,
                        kind="unknown_locally",
                        detail=(
                            "an order is working at the broker that this system did not "
                            "create or has forgotten"
                        ),
                    )
                )

        for internal_id, broker_order_id, symbol in local_open_orders:
            if broker_order_id is None:
                discrepancies.append(
                    OrderDiscrepancy(
                        broker_order_id=internal_id,
                        symbol=symbol,
                        kind="unknown_at_broker",
                        detail=(
                            "local order is marked working but has no broker order id; "
                            "its true state is unknown"
                        ),
                    )
                )
            elif broker_order_id not in broker_ids:
                discrepancies.append(
                    OrderDiscrepancy(
                        broker_order_id=broker_order_id,
                        symbol=symbol,
                        kind="unknown_at_broker",
                        detail="local order is marked working but the broker does not have it",
                    )
                )

        return tuple(discrepancies)


def fills_to_position_deltas(fills: Sequence[BrokerFill]) -> dict[int, int]:
    """Net signed quantity per contract implied by a set of fills."""
    deltas: dict[int, int] = {}
    for fill in fills:
        deltas[fill.con_id] = deltas.get(fill.con_id, 0) + fill.quantity * fill.side.sign
    return deltas


def realized_commission(fills: Sequence[BrokerFill]) -> Decimal:
    return sum((f.commission or Decimal("0") for f in fills), Decimal("0"))


OPEN_ORDER_STATUSES = frozenset(
    {
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.SUBMITTED,
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_PENDING,
    }
)


__all__ = [
    "OPEN_ORDER_STATUSES",
    "OrderDiscrepancy",
    "PositionDiscrepancy",
    "Reconciler",
    "ReconciliationResult",
    "fills_to_position_deltas",
    "realized_commission",
]
