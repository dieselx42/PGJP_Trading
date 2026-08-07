"""Domain enumerations.

Deliberately kept in one dependency-free module so that every layer
(config, safety gate, risk, execution, broker, persistence) shares exactly one
definition of these values and no import cycles are possible.
"""

from __future__ import annotations

from enum import StrEnum


class _ParsableEnum(StrEnum):
    """Enum with strict, case-insensitive parsing.

    Parsing is strict on purpose: an unrecognised value raises rather than
    silently falling back to a default. In safety-critical configuration a
    typo must stop the application, not quietly select a behaviour.
    """

    @classmethod
    def parse(cls, raw: str) -> _ParsableEnum:
        candidate = raw.strip().lower()
        for member in cls:
            if str(member.value).lower() == candidate:
                return member
        valid = ", ".join(str(m.value) for m in cls)
        raise ValueError(f"invalid {cls.__name__} {raw!r}; expected one of: {valid}")


class TradingMode(_ParsableEnum):
    """Operational mode of the application."""

    DISABLED = "disabled"
    """No broker, no orders, no simulated execution. Monitoring only."""

    MOCK = "mock"
    """MockBroker only. Simulated market data and simulated order lifecycle."""

    PAPER = "paper"
    """IBKR paper session only. Connecting to a live account is refused."""

    LIVE = "live"
    """IBKR live account. Requires every interlock in TransmitGate to pass."""

    @classmethod
    def parse(cls, raw: str) -> TradingMode:
        result = super().parse(raw)
        assert isinstance(result, TradingMode)
        return result

    @property
    def uses_ibkr(self) -> bool:
        return self in (TradingMode.PAPER, TradingMode.LIVE)


class AccountType(_ParsableEnum):
    """Independently verified identity of the connected brokerage account.

    UNKNOWN is the default and is never tradeable. The application must
    positively establish the account type from broker-reported data before any
    order may be transmitted; it never infers the account type from
    configuration, because configuration is exactly what we are guarding
    against being wrong.
    """

    UNKNOWN = "unknown"
    SIMULATED = "simulated"
    """MockBroker. Never touches IBKR."""

    PAPER = "paper"
    LIVE = "live"

    @classmethod
    def parse(cls, raw: str) -> AccountType:
        result = super().parse(raw)
        assert isinstance(result, AccountType)
        return result


class ConnectionState(StrEnum):
    """Broker transport state."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


class ApplicationState(StrEnum):
    """Lifecycle state of the trading application.

    A running process is NOT a ready process. Only READY permits transmission,
    and READY is only reachable through successful reconciliation.
    """

    STARTING = "STARTING"
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    RECONCILING = "RECONCILING"
    SAFE = "SAFE"
    """Operational but explicitly not authorised to trade."""

    READY = "READY"
    HALTED = "HALTED"
    """Deliberately stopped, e.g. kill switch engaged."""

    ERROR = "ERROR"


class OrderSide(_ParsableEnum):
    BUY = "BUY"
    SELL = "SELL"

    @classmethod
    def parse(cls, raw: str) -> OrderSide:
        result = super().parse(raw)
        assert isinstance(result, OrderSide)
        return result

    @property
    def sign(self) -> int:
        return 1 if self is OrderSide.BUY else -1


class OrderType(_ParsableEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    BRACKET = "BRACKET"

    @classmethod
    def parse(cls, raw: str) -> OrderType:
        result = super().parse(raw)
        assert isinstance(result, OrderType)
        return result


class TimeInForce(_ParsableEnum):
    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    """Order lifecycle.

    NEW and BLOCKED are terminal-or-pre-transmission states that guarantee
    nothing was ever sent to a broker.
    """

    NEW = "NEW"
    """Persisted locally. Never transmitted."""

    BLOCKED = "BLOCKED"
    """Refused by the risk manager or the transmit gate. Never transmitted."""

    PENDING_SUBMIT = "PENDING_SUBMIT"
    """Transmission has been authorised and is in flight."""

    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES

    @property
    def is_open(self) -> bool:
        return self in _OPEN_STATUSES

    @property
    def reached_broker(self) -> bool:
        """True once the order may exist at the broker.

        Used by the idempotency guard: an order in any of these states must
        never be transmitted a second time.
        """
        return self not in (OrderStatus.NEW, OrderStatus.BLOCKED)


_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.BLOCKED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.ERROR,
    }
)

_OPEN_STATUSES = frozenset(
    {
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.SUBMITTED,
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_PENDING,
    }
)


class Direction(_ParsableEnum):
    """Direction expressed by a strategy intent."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"

    @classmethod
    def parse(cls, raw: str) -> Direction:
        result = super().parse(raw)
        assert isinstance(result, Direction)
        return result


class SecurityType(StrEnum):
    FUTURE = "FUT"
    CONTINUOUS_FUTURE = "CONTFUT"


__all__ = [
    "AccountType",
    "ApplicationState",
    "ConnectionState",
    "Direction",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "SecurityType",
    "TimeInForce",
    "TradingMode",
]
