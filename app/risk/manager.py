"""Risk manager.

Every proposed trade is evaluated here, independently of the strategy that
proposed it and independently of the transmit gate that will run afterwards.

The default configuration rejects everything. That is not an accident of the
limits all being zero -- it is the designed behaviour, and
:func:`RiskManager.evaluate` reports each unconfigured limit by name so the
operator can see precisely what is missing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from app.config import Config
from app.contracts.models import QualifiedContract
from app.enums import AccountType, ApplicationState, ConnectionState, OrderSide, TradingMode
from app.logging_config import get_logger
from app.risk import limits as limit_checks
from app.safety.gate import expected_account_type
from app.signals.models import TradeIntent

_LOG = get_logger("risk.manager")

# -- non-numeric rejection reasons ------------------------------------------

REASON_KILL_SWITCH = "KILL_SWITCH_ENGAGED"
REASON_TRADING_MODE_DISABLED = "TRADING_MODE_DISABLED"
REASON_TRANSMIT_NOT_ALLOWED = "ORDER_TRANSMIT_NOT_ALLOWED"
REASON_BROKER_DISCONNECTED = "BROKER_NOT_CONNECTED"
REASON_APP_NOT_READY = "APPLICATION_NOT_READY"
REASON_ACCOUNT_UNAVAILABLE = "ACCOUNT_DATA_UNAVAILABLE"
REASON_ACCOUNT_TYPE_UNKNOWN = "BROKER_ACCOUNT_TYPE_UNKNOWN"
REASON_ACCOUNT_TYPE_MISMATCH = "TRADING_MODE_ACCOUNT_TYPE_MISMATCH"
REASON_POSITIONS_NOT_RECONCILED = "POSITIONS_NOT_RECONCILED"
REASON_ORDERS_NOT_RECONCILED = "OPEN_ORDERS_NOT_RECONCILED"
REASON_CONTRACT_NOT_QUALIFIED = "CONTRACT_NOT_QUALIFIED"
REASON_CONTRACT_CONTINUOUS = "CONTRACT_IS_CONTINUOUS_FUTURE"
REASON_MARKET_DATA_MISSING = "MARKET_DATA_UNAVAILABLE"
REASON_MARKET_DATA_STALE = "MARKET_DATA_STALE"
REASON_MARKET_DATA_AGE_NOT_CONFIGURED = "MARKET_DATA_MAX_AGE_NOT_CONFIGURED"
REASON_MARKET_DATA_FUTURE = "MARKET_DATA_TIMESTAMP_IN_FUTURE"
REASON_PERMISSION_NOT_READY = "SOL_FUTURES_PERMISSION_NOT_READY"
REASON_BROKER_PERMISSION_UNKNOWN = "TRADING_PERMISSION_UNAVAILABLE_AT_BROKER"
REASON_STRATEGY_DISABLED = "STRATEGY_DISABLED"
REASON_DUPLICATE_ORDER = "DUPLICATE_ORDER_FOR_SIGNAL"
REASON_NO_POSITION_CHANGE = "INTENT_REQUIRES_NO_POSITION_CHANGE"
REASON_QUANTITY_INVALID = "ORDER_QUANTITY_INVALID"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Approval or rejection, with every reason found."""

    approved: bool
    reasons: tuple[str, ...] = ()
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        """First reason, matching the shorthand used in the project brief."""
        if self.reasons:
            return self.reasons[0]
        return "APPROVED" if self.approved else "REJECTED"

    @classmethod
    def approve(cls, **detail: object) -> RiskDecision:
        return cls(approved=True, reasons=(), detail=detail)

    @classmethod
    def reject(cls, *reasons: str, **detail: object) -> RiskDecision:
        return cls(approved=False, reasons=tuple(reasons), detail=detail)

    def as_dict(self) -> dict[str, object]:
        return {"approved": self.approved, "reasons": list(self.reasons), "detail": self.detail}


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Everything the risk manager needs, with unsafe-to-trade defaults."""

    intent: TradeIntent
    order_side: OrderSide
    order_quantity: int
    current_position: int = 0

    contract: QualifiedContract | None = None
    reference_price: Decimal | None = None
    market_data_age_seconds: float | None = None

    daily_pnl: Decimal = Decimal("0")
    open_orders_count: int = 0
    orders_last_hour: int = 0

    app_state: ApplicationState = ApplicationState.STARTING
    connection_state: ConnectionState = ConnectionState.DISCONNECTED
    account_type: AccountType = AccountType.UNKNOWN
    account_available: bool = False
    positions_reconciled: bool = False
    open_orders_reconciled: bool = False
    broker_permission_ready: bool = False
    strategy_enabled: bool = False
    kill_switch_engaged: bool = True
    duplicate_order_exists: bool = False

    @property
    def projected_position(self) -> int:
        return self.current_position + self.order_quantity * self.order_side.sign


class RiskManager:
    """Evaluates a proposed trade against every control."""

    def __init__(self, config: Config) -> None:
        self._config = config

    @property
    def config(self) -> Config:
        return self._config

    def evaluate(self, context: RiskContext) -> RiskDecision:
        """Approve or reject, collecting every failing control."""
        reasons: list[str] = []

        self._check_master_controls(context, reasons)
        self._check_system_state(context, reasons)
        self._check_account(context, reasons)
        self._check_contract(context, reasons)
        self._check_market_data(context, reasons)
        self._check_permissions(context, reasons)
        self._check_duplicates(context, reasons)
        self._check_numeric_limits(context, reasons)

        detail: dict[str, object] = {
            "symbol": context.intent.symbol,
            "side": context.order_side.value,
            "quantity": context.order_quantity,
            "current_position": context.current_position,
            "projected_position": context.projected_position,
            "daily_pnl": str(context.daily_pnl),
            "open_orders": context.open_orders_count,
            "orders_last_hour": context.orders_last_hour,
            "market_data_age_seconds": context.market_data_age_seconds,
        }

        decision = (
            RiskDecision(approved=False, reasons=tuple(reasons), detail=detail)
            if reasons
            else RiskDecision(approved=True, reasons=(), detail=detail)
        )

        log = _LOG.info if decision.approved else _LOG.warning
        log(
            "risk decision",
            extra={
                "event": "risk.approved" if decision.approved else "risk.rejected",
                "correlation_id": context.intent.correlation_id,
                "intent_id": context.intent.intent_id,
                **decision.as_dict(),
            },
        )
        return decision

    # -- individual controls ---------------------------------------------

    def _check_master_controls(self, context: RiskContext, reasons: list[str]) -> None:
        if context.kill_switch_engaged or self._config.kill_switch:
            reasons.append(REASON_KILL_SWITCH)
        if not self._config.allow_order_transmit:
            reasons.append(REASON_TRANSMIT_NOT_ALLOWED)
        if self._config.trading_mode is TradingMode.DISABLED:
            reasons.append(REASON_TRADING_MODE_DISABLED)
        if not (self._config.strategy_enabled and context.strategy_enabled):
            reasons.append(REASON_STRATEGY_DISABLED)

    def _check_system_state(self, context: RiskContext, reasons: list[str]) -> None:
        if context.app_state is not ApplicationState.READY:
            reasons.append(REASON_APP_NOT_READY)
        if context.connection_state is not ConnectionState.CONNECTED:
            reasons.append(REASON_BROKER_DISCONNECTED)
        if not context.positions_reconciled:
            reasons.append(REASON_POSITIONS_NOT_RECONCILED)
        if not context.open_orders_reconciled:
            reasons.append(REASON_ORDERS_NOT_RECONCILED)

    def _check_account(self, context: RiskContext, reasons: list[str]) -> None:
        if not context.account_available:
            reasons.append(REASON_ACCOUNT_UNAVAILABLE)
        if context.account_type is AccountType.UNKNOWN:
            reasons.append(REASON_ACCOUNT_TYPE_UNKNOWN)
            return
        expected = expected_account_type(self._config.trading_mode)
        if expected is not None and context.account_type is not expected:
            reasons.append(REASON_ACCOUNT_TYPE_MISMATCH)

    def _check_contract(self, context: RiskContext, reasons: list[str]) -> None:
        contract = context.contract
        if contract is None:
            reasons.append(REASON_CONTRACT_NOT_QUALIFIED)
            return
        if contract.is_continuous:
            reasons.append(REASON_CONTRACT_CONTINUOUS)
        if contract.con_id <= 0:
            reasons.append(REASON_CONTRACT_NOT_QUALIFIED)

    def _check_market_data(self, context: RiskContext, reasons: list[str]) -> None:
        max_age = self._config.market_data_max_age_seconds
        if max_age <= 0:
            reasons.append(REASON_MARKET_DATA_AGE_NOT_CONFIGURED)
        age = context.market_data_age_seconds
        if age is None:
            reasons.append(REASON_MARKET_DATA_MISSING)
        elif age < 0:
            reasons.append(REASON_MARKET_DATA_FUTURE)
        elif max_age > 0 and age > max_age:
            reasons.append(REASON_MARKET_DATA_STALE)

    def _check_permissions(self, context: RiskContext, reasons: list[str]) -> None:
        if self._config.uses_ibkr and not self._config.sol_futures_permission_ready:
            reasons.append(REASON_PERMISSION_NOT_READY)
        if not context.broker_permission_ready:
            reasons.append(REASON_BROKER_PERMISSION_UNKNOWN)

    @staticmethod
    def _check_duplicates(context: RiskContext, reasons: list[str]) -> None:
        if context.duplicate_order_exists:
            reasons.append(REASON_DUPLICATE_ORDER)

    def _check_numeric_limits(self, context: RiskContext, reasons: list[str]) -> None:
        if context.order_quantity <= 0:
            reasons.append(REASON_QUANTITY_INVALID)

        risk = self._config.risk
        checks: Sequence[str | None] = (
            limit_checks.check_order_size(risk, context.order_quantity),
            limit_checks.check_position_size(risk, context.projected_position),
            limit_checks.check_daily_loss(risk, context.daily_pnl),
            limit_checks.check_orders_per_hour(risk, context.orders_last_hour),
            limit_checks.check_open_orders(risk, context.open_orders_count),
            limit_checks.check_notional_exposure(
                risk,
                contract=context.contract,
                projected_position=context.projected_position,
                reference_price=context.reference_price,
            ),
        )
        reasons.extend(reason for reason in checks if reason is not None)

    # -- reporting --------------------------------------------------------

    def describe(self) -> dict[str, object]:
        """Risk configuration summary for /status."""
        return {
            "limits": self._config.risk.as_dict(),
            "all_limits_configured": self._config.risk.all_configured,
            "unconfigured": list(limit_checks.unconfigured_limits(self._config.risk)),
            "market_data_max_age_seconds": self._config.market_data_max_age_seconds,
        }


__all__ = ["RiskContext", "RiskDecision", "RiskManager"]
