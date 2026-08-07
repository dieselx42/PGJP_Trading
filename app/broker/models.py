"""Broker-neutral data models and errors.

These types are the entire vocabulary the application uses to talk about a
brokerage. Adding a second broker means implementing
:class:`~app.broker.base.Broker` against these models; it does not mean
touching risk, execution, or strategy code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.contracts.models import ContractSpec, QualifiedContract
from app.enums import AccountType, OrderSide, OrderStatus, OrderType, TimeInForce
from app.logging_config import mask_account_id

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BrokerError(Exception):
    """Base class for every broker failure."""

    #: Whether retrying the same call could plausibly succeed. Permission and
    #: configuration errors are not retryable, and the reconnect/retry logic
    #: uses this to avoid hammering IBKR with requests it has already refused.
    retryable: bool = False


class BrokerConnectionError(BrokerError):
    """Transport-level failure. Retryable with backoff."""

    retryable = True


class BrokerTimeoutError(BrokerConnectionError):
    """A request did not complete within its deadline."""


class BrokerPermissionError(BrokerError):
    """The account is not permitted to do this.

    Covers futures trading permission, market data permission, and
    product-not-enabled responses. Never retried: the answer will not change
    until a human changes an account setting at the broker.
    """

    retryable = False


class BrokerContractError(BrokerError):
    """A contract could not be resolved or qualified."""

    retryable = False


class BrokerOrderRejectedError(BrokerError):
    """The broker refused an order."""

    retryable = False


class BrokerUnsupportedOperationError(BrokerError):
    """The adapter does not implement this operation in this phase."""

    retryable = False


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    """What the adapter established about the session it is attached to."""

    connected: bool
    account_type: AccountType
    account_id: str | None = None
    server_version: str | None = None
    connection_time: datetime | None = None
    host: str | None = None
    port: int | None = None
    client_id: int | None = None
    detail: str | None = None

    def describe(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "account_type": self.account_type.value,
            "account_id": mask_account_id(self.account_id),
            "server_version": self.server_version,
            "connection_time": self.connection_time.isoformat() if self.connection_time else None,
            "host": self.host,
            "port": self.port,
            "client_id": self.client_id,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """Account-level figures, normalised across brokers."""

    account_id: str
    account_type: AccountType
    currency: str = "USD"
    net_liquidation: Decimal | None = None
    available_funds: Decimal | None = None
    buying_power: Decimal | None = None
    excess_liquidity: Decimal | None = None
    maintenance_margin: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None

    #: Broker-reported trading permissions, if the adapter can determine them.
    #: ``None`` means "unknown", which the gate treats as not permitted.
    futures_permission: bool | None = None

    raw: dict[str, str] = field(default_factory=dict)

    def describe(self) -> dict[str, object]:
        return {
            "account_id": mask_account_id(self.account_id),
            "account_type": self.account_type.value,
            "currency": self.currency,
            "net_liquidation": _opt_str(self.net_liquidation),
            "available_funds": _opt_str(self.available_funds),
            "buying_power": _opt_str(self.buying_power),
            "excess_liquidity": _opt_str(self.excess_liquidity),
            "realized_pnl": _opt_str(self.realized_pnl),
            "unrealized_pnl": _opt_str(self.unrealized_pnl),
            "futures_permission": self.futures_permission,
        }


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """A position as the broker reports it."""

    account_id: str
    con_id: int
    symbol: str
    local_symbol: str
    sec_type: str
    exchange: str
    currency: str
    quantity: int
    average_cost: Decimal

    def describe(self) -> dict[str, object]:
        return {
            "account_id": mask_account_id(self.account_id),
            "con_id": self.con_id,
            "symbol": self.symbol,
            "local_symbol": self.local_symbol,
            "quantity": self.quantity,
            "average_cost": str(self.average_cost),
        }


@dataclass(frozen=True, slots=True)
class BrokerOrderSnapshot:
    """An order that exists at the broker right now."""

    broker_order_id: str
    account_id: str
    con_id: int
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    status: OrderStatus
    filled_quantity: int = 0
    remaining_quantity: int = 0
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    average_fill_price: Decimal | None = None
    perm_id: str | None = None
    client_id: int | None = None
    time_in_force: TimeInForce = TimeInForce.DAY

    def describe(self) -> dict[str, object]:
        return {
            "broker_order_id": self.broker_order_id,
            "con_id": self.con_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "order_type": self.order_type.value,
            "status": self.status.value,
            "filled_quantity": self.filled_quantity,
            "limit_price": _opt_str(self.limit_price),
            "stop_price": _opt_str(self.stop_price),
        }


@dataclass(frozen=True, slots=True)
class BrokerFill:
    """A single execution."""

    execution_id: str
    broker_order_id: str
    account_id: str
    con_id: int
    symbol: str
    side: OrderSide
    quantity: int
    price: Decimal
    executed_at: datetime
    commission: Decimal | None = None
    commission_currency: str | None = None

    def describe(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "broker_order_id": self.broker_order_id,
            "con_id": self.con_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": str(self.price),
            "executed_at": self.executed_at.isoformat(),
            "commission": _opt_str(self.commission),
        }


@dataclass(frozen=True, slots=True)
class MarketDataTick:
    """A market-data update, always timestamped by the receiver in UTC."""

    contract_key: str
    received_at: datetime
    source: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    bid_size: int | None = None
    ask_size: int | None = None

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal(2)
        return self.last

    def describe(self) -> dict[str, object]:
        return {
            "contract_key": self.contract_key,
            "received_at": self.received_at.isoformat(),
            "source": self.source,
            "bid": _opt_str(self.bid),
            "ask": _opt_str(self.ask),
            "last": _opt_str(self.last),
        }


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A fully-formed order handed to a broker adapter.

    ``transmit`` is carried explicitly rather than assumed. The order manager
    only ever sets it to ``True`` after :class:`~app.safety.gate.TransmitGate`
    has allowed the order; adapters must refuse to send when it is ``False``.
    """

    internal_order_id: str
    correlation_id: str
    contract: QualifiedContract
    side: OrderSide
    quantity: int
    order_type: OrderType
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    transmit: bool = False

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"order quantity must be positive, got {self.quantity}")


@dataclass(frozen=True, slots=True)
class PlaceOrderResult:
    """Outcome of a placement attempt."""

    accepted: bool
    broker_order_id: str | None = None
    status: OrderStatus = OrderStatus.REJECTED
    error_code: str | None = None
    error_message: str | None = None

    def describe(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "broker_order_id": self.broker_order_id,
            "status": self.status.value,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class ContractLookupRequest:
    spec: ContractSpec
    include_expired: bool = False


def _opt_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "AccountSummary",
    "BrokerConnectionError",
    "BrokerContractError",
    "BrokerError",
    "BrokerFill",
    "BrokerOrderRejectedError",
    "BrokerOrderSnapshot",
    "BrokerPermissionError",
    "BrokerPosition",
    "BrokerTimeoutError",
    "BrokerUnsupportedOperationError",
    "ConnectionInfo",
    "ContractLookupRequest",
    "MarketDataTick",
    "OrderRequest",
    "PlaceOrderResult",
]
