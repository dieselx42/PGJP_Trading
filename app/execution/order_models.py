"""Order models.

Models exist for market, limit, stop, stop-limit, and bracket orders so the
shapes are settled and persisted consistently. Only market and limit orders are
actually transmittable in this phase (see
:data:`TRANSMITTABLE_ORDER_TYPES`); the rest are refused at the point of
transmission rather than being half-supported.

Every order carries the full audit set required by the specification:
internal id, correlation id, strategy id, timestamps, contract identity, side,
quantity, type, prices, broker id, status, fills, commission, and error detail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.contracts.models import QualifiedContract
from app.enums import (
    AccountType,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
    TradingMode,
)
from app.utilities.ids import new_internal_order_id
from app.utilities.timeutils import ensure_utc, utc_now

#: Order types the system will transmit today. Widening this set is a
#: deliberate change that must come with its own tests.
TRANSMITTABLE_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.LIMIT})


class OrderValidationError(ValueError):
    """An order is structurally invalid for its type."""


@dataclass(slots=True)
class Order:
    """A single order and its complete lifecycle state."""

    # -- identity ---------------------------------------------------------
    idempotency_key: str
    correlation_id: str
    strategy_id: str
    run_id: str
    internal_order_id: str = field(default_factory=new_internal_order_id)
    intent_id: str | None = None

    # -- instrument -------------------------------------------------------
    symbol: str = ""
    con_id: int = 0
    local_symbol: str = ""
    exchange: str = ""
    currency: str = "USD"

    # -- terms ------------------------------------------------------------
    side: OrderSide = OrderSide.BUY
    quantity: int = 0
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None

    # -- lifecycle --------------------------------------------------------
    status: OrderStatus = OrderStatus.NEW
    broker_order_id: str | None = None
    filled_quantity: int = 0
    average_fill_price: Decimal | None = None
    commission: Decimal | None = None
    commission_currency: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    block_reasons: tuple[str, ...] = ()

    # -- context ----------------------------------------------------------
    trading_mode: TradingMode = TradingMode.DISABLED
    account_type: AccountType = AccountType.UNKNOWN

    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    transmitted_at: datetime | None = None

    def __post_init__(self) -> None:
        ensure_utc(self.created_at)
        ensure_utc(self.updated_at)
        if self.transmitted_at is not None:
            ensure_utc(self.transmitted_at)

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """Structural validation. Raises :class:`OrderValidationError`."""
        if self.quantity <= 0:
            raise OrderValidationError(f"quantity must be positive, got {self.quantity}")
        if self.con_id <= 0:
            raise OrderValidationError("order requires a qualified contract (positive conId)")
        if not self.symbol:
            raise OrderValidationError("order requires a symbol")

        if self.order_type is OrderType.MARKET:
            if self.limit_price is not None or self.stop_price is not None:
                raise OrderValidationError("a market order must not carry a limit or stop price")
        elif self.order_type is OrderType.LIMIT:
            _require_positive(self.limit_price, "limit order requires a positive limit price")
            if self.stop_price is not None:
                raise OrderValidationError("a limit order must not carry a stop price")
        elif self.order_type is OrderType.STOP:
            _require_positive(self.stop_price, "stop order requires a positive stop price")
            if self.limit_price is not None:
                raise OrderValidationError("a stop order must not carry a limit price")
        elif self.order_type is OrderType.STOP_LIMIT:
            _require_positive(self.stop_price, "stop-limit order requires a positive stop price")
            _require_positive(self.limit_price, "stop-limit order requires a positive limit price")
        elif self.order_type is OrderType.BRACKET:
            raise OrderValidationError(
                "a bracket is submitted as a BracketOrder, not as a single order"
            )

    def require_transmittable_type(self) -> None:
        if self.order_type not in TRANSMITTABLE_ORDER_TYPES:
            raise OrderValidationError(
                f"{self.order_type.value} orders are modelled but not transmitted in this phase"
            )

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    @property
    def was_transmitted(self) -> bool:
        """True if this order may exist at the broker.

        The idempotency guard uses this: an order that was ever transmitted is
        never transmitted again, whatever the caller asks for.
        """
        return self.status.reached_broker or self.broker_order_id is not None

    def mark_blocked(self, reasons: tuple[str, ...], *, at: datetime | None = None) -> None:
        self.status = OrderStatus.BLOCKED
        self.block_reasons = reasons
        self.updated_at = at or utc_now()

    def mark_pending_submit(self, *, at: datetime | None = None) -> None:
        self.status = OrderStatus.PENDING_SUBMIT
        self.transmitted_at = at or utc_now()
        self.updated_at = self.transmitted_at

    def mark_submitted(self, broker_order_id: str, *, at: datetime | None = None) -> None:
        self.broker_order_id = broker_order_id
        self.status = OrderStatus.SUBMITTED
        self.updated_at = at or utc_now()

    def mark_status(self, status: OrderStatus, *, at: datetime | None = None) -> None:
        self.status = status
        self.updated_at = at or utc_now()

    def mark_rejected(self, code: str | None, message: str, *, at: datetime | None = None) -> None:
        self.status = OrderStatus.REJECTED
        self.error_code = code
        self.error_message = message
        self.updated_at = at or utc_now()

    def mark_error(self, code: str | None, message: str, *, at: datetime | None = None) -> None:
        self.status = OrderStatus.ERROR
        self.error_code = code
        self.error_message = message
        self.updated_at = at or utc_now()

    def apply_fill(
        self,
        *,
        quantity: int,
        price: Decimal,
        commission: Decimal | None = None,
        commission_currency: str | None = None,
        at: datetime | None = None,
    ) -> None:
        previous_filled = self.filled_quantity
        new_filled = previous_filled + quantity
        if new_filled > self.quantity:
            raise OrderValidationError(
                f"fill of {quantity} would over-fill order {self.internal_order_id} "
                f"({previous_filled}/{self.quantity} already filled)"
            )
        if self.average_fill_price is None or previous_filled == 0:
            self.average_fill_price = price
        else:
            self.average_fill_price = (
                self.average_fill_price * Decimal(previous_filled) + price * Decimal(quantity)
            ) / Decimal(new_filled)
        self.filled_quantity = new_filled
        if commission is not None:
            self.commission = (self.commission or Decimal("0")) + commission
            self.commission_currency = commission_currency
        self.status = (
            OrderStatus.FILLED if new_filled == self.quantity else (OrderStatus.PARTIALLY_FILLED)
        )
        self.updated_at = at or utc_now()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def block_reasons_json(self) -> str:
        return json.dumps(list(self.block_reasons))

    def describe(self) -> dict[str, object]:
        return {
            "internal_order_id": self.internal_order_id,
            "idempotency_key": self.idempotency_key,
            "correlation_id": self.correlation_id,
            "strategy_id": self.strategy_id,
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "con_id": self.con_id,
            "local_symbol": self.local_symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "order_type": self.order_type.value,
            "time_in_force": self.time_in_force.value,
            "limit_price": _opt_str(self.limit_price),
            "stop_price": _opt_str(self.stop_price),
            "status": self.status.value,
            "broker_order_id": self.broker_order_id,
            "filled_quantity": self.filled_quantity,
            "average_fill_price": _opt_str(self.average_fill_price),
            "commission": _opt_str(self.commission),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "block_reasons": list(self.block_reasons),
            "trading_mode": self.trading_mode.value,
            "account_type": self.account_type.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "transmitted_at": self.transmitted_at.isoformat() if self.transmitted_at else None,
            "run_id": self.run_id,
        }


@dataclass(slots=True)
class BracketOrder:
    """Parent entry plus protective children.

    Modelled but not transmitted in this phase. Persisting the shape now means
    enabling brackets later is a change in the execution path only, not a
    schema migration under pressure.
    """

    parent: Order
    take_profit: Order | None = None
    stop_loss: Order | None = None

    def validate(self) -> None:
        self.parent.validate()
        if self.take_profit is None and self.stop_loss is None:
            raise OrderValidationError("a bracket requires at least one protective child order")
        for child in (self.take_profit, self.stop_loss):
            if child is None:
                continue
            child.validate()
            if child.side is self.parent.side:
                raise OrderValidationError(
                    "bracket child orders must be on the opposite side of the parent"
                )
            if child.quantity != self.parent.quantity:
                raise OrderValidationError("bracket child quantity must match the parent quantity")

    def all_orders(self) -> tuple[Order, ...]:
        return tuple(o for o in (self.parent, self.take_profit, self.stop_loss) if o is not None)


@dataclass(frozen=True, slots=True)
class Fill:
    """A recorded execution."""

    execution_id: str
    internal_order_id: str
    correlation_id: str
    run_id: str
    con_id: int
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    executed_at: datetime
    commission: Decimal | None = None
    commission_currency: str | None = None
    broker_order_id: str | None = None

    def __post_init__(self) -> None:
        ensure_utc(self.executed_at)

    def describe(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "internal_order_id": self.internal_order_id,
            "con_id": self.con_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": str(self.price),
            "executed_at": self.executed_at.isoformat(),
            "commission": _opt_str(self.commission),
        }


def build_order(
    *,
    contract: QualifiedContract,
    side: OrderSide,
    quantity: int,
    order_type: OrderType,
    idempotency_key: str,
    correlation_id: str,
    strategy_id: str,
    run_id: str,
    trading_mode: TradingMode,
    account_type: AccountType,
    intent_id: str | None = None,
    limit_price: Decimal | None = None,
    stop_price: Decimal | None = None,
    time_in_force: TimeInForce = TimeInForce.DAY,
    at: datetime | None = None,
) -> Order:
    """Construct a validated order from a qualified contract."""
    timestamp = at or utc_now()
    order = Order(
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        strategy_id=strategy_id,
        run_id=run_id,
        intent_id=intent_id,
        symbol=contract.symbol,
        con_id=contract.con_id,
        local_symbol=contract.local_symbol,
        exchange=contract.exchange,
        currency=contract.currency,
        side=side,
        quantity=quantity,
        order_type=order_type,
        time_in_force=time_in_force,
        limit_price=limit_price,
        stop_price=stop_price,
        trading_mode=trading_mode,
        account_type=account_type,
        created_at=timestamp,
        updated_at=timestamp,
    )
    order.validate()
    return order


def _require_positive(value: Decimal | None, message: str) -> None:
    if value is None or value <= 0:
        raise OrderValidationError(message)


def _opt_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "TRANSMITTABLE_ORDER_TYPES",
    "BracketOrder",
    "Fill",
    "Order",
    "OrderValidationError",
    "build_order",
]
