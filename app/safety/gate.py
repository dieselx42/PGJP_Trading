"""The transmit gate: the last thing that runs before an order reaches a broker.

Every interlock in the specification is implemented here as an independent
check that must return *positively true*. There is no short-circuit that can
allow transmission, no default branch that permits, and no configuration value
that can bypass a check.

Two properties make this auditable:

* :func:`TransmitGate.evaluate` collects **all** failing reasons rather than
  returning at the first one, so an operator sees the full picture.
* The function is pure. It reads a frozen :class:`GateContext` and returns a
  frozen :class:`GateDecision`. It performs no I/O and holds no state, so its
  behaviour is entirely determined by its inputs and is exhaustively testable.

The caller is responsible for building an honest :class:`GateContext`. The
fields default to their unsafe-to-trade values, so a context assembled from
incomplete information denies rather than permits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.config import Config
from app.enums import AccountType, ApplicationState, ConnectionState, TradingMode

# ---------------------------------------------------------------------------
# Rejection reasons (stable strings -- they are logged, persisted, and asserted
# on in tests, so treat them as part of the interface)
# ---------------------------------------------------------------------------

REASON_KILL_SWITCH = "KILL_SWITCH_ENGAGED"
REASON_TRANSMIT_NOT_ALLOWED = "ORDER_TRANSMIT_NOT_ALLOWED"
REASON_MODE_DISABLED = "TRADING_MODE_DISABLED"
REASON_LIVE_NOT_ENABLED = "LIVE_TRADING_NOT_ENABLED"
REASON_ACCOUNT_TYPE_UNKNOWN = "BROKER_ACCOUNT_TYPE_UNKNOWN"
REASON_ACCOUNT_MISMATCH_EXPECTED_LIVE = "ACCOUNT_TYPE_MISMATCH_EXPECTED_LIVE"
REASON_ACCOUNT_MISMATCH_EXPECTED_PAPER = "ACCOUNT_TYPE_MISMATCH_EXPECTED_PAPER"
REASON_ACCOUNT_MISMATCH_EXPECTED_SIMULATED = "ACCOUNT_TYPE_MISMATCH_EXPECTED_SIMULATED"
REASON_APP_NOT_READY = "APPLICATION_NOT_READY"
REASON_BROKER_NOT_CONNECTED = "BROKER_NOT_CONNECTED"
REASON_CONTRACT_NOT_QUALIFIED = "CONTRACT_NOT_QUALIFIED"
REASON_CONTRACT_IS_CONTINUOUS = "CONTRACT_IS_CONTINUOUS_FUTURE"
REASON_ACCOUNT_UNAVAILABLE = "ACCOUNT_DATA_UNAVAILABLE"
REASON_POSITIONS_NOT_RECONCILED = "POSITIONS_NOT_RECONCILED"
REASON_ORDERS_NOT_RECONCILED = "OPEN_ORDERS_NOT_RECONCILED"
REASON_MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"
REASON_MARKET_DATA_STALE = "MARKET_DATA_STALE"
REASON_MARKET_DATA_TIMESTAMP_IN_FUTURE = "MARKET_DATA_TIMESTAMP_IN_FUTURE"
REASON_MARKET_DATA_AGE_NOT_CONFIGURED = "MARKET_DATA_MAX_AGE_NOT_CONFIGURED"
REASON_MARKET_DATA_DELAYED = "MARKET_DATA_IS_DELAYED"
REASON_PERMISSION_CONFIG_NOT_READY = "SOL_FUTURES_PERMISSION_NOT_CONFIGURED_READY"
REASON_PERMISSION_BROKER_NOT_READY = "TRADING_PERMISSION_UNAVAILABLE_AT_BROKER"
REASON_STRATEGY_DISABLED = "STRATEGY_DISABLED"
REASON_RISK_NOT_APPROVED = "RISK_CHECKS_NOT_PASSED"


@dataclass(frozen=True, slots=True)
class GateContext:
    """Everything the gate needs, with unsafe-to-trade defaults.

    A caller that forgets to populate a field gets a denial, never a
    permission. That is the whole point of the default values below.
    """

    app_state: ApplicationState = ApplicationState.STARTING
    connection_state: ConnectionState = ConnectionState.DISCONNECTED

    #: Independently established from broker-reported data, never inferred
    #: from configuration.
    broker_account_type: AccountType = AccountType.UNKNOWN

    contract_qualified: bool = False
    contract_is_continuous: bool = True
    account_available: bool = False
    positions_reconciled: bool = False
    open_orders_reconciled: bool = False

    #: ``None`` means "no market data at all", which is not the same as stale
    #: and gets its own rejection reason.
    market_data_age_seconds: float | None = None

    #: Whether the last tick was DELAYED rather than real-time. Defaults to
    #: True -- the unsafe answer -- because a caller that does not know cannot
    #: be assumed to have live data. There is deliberately no configuration to
    #: permit trading on delayed prices: a fifteen-minute-old quote is not a
    #: price, and an automated system cannot tell the difference by looking.
    market_data_is_delayed: bool = True

    #: Broker-reported trading permission for the instrument. In mock mode this
    #: is the MockBroker's simulated permission; in paper/live it reflects what
    #: IBKR actually told us.
    broker_permission_ready: bool = False

    strategy_enabled: bool = False
    risk_approved: bool = False


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Result of evaluating the gate."""

    allowed: bool
    reasons: tuple[str, ...] = field(default=())

    @property
    def reason(self) -> str:
        """First failing reason, or ``"ALL_INTERLOCKS_PASSED"`` when allowed."""
        if self.reasons:
            return self.reasons[0]
        return "ALL_INTERLOCKS_PASSED" if self.allowed else "REJECTED"

    def as_dict(self) -> dict[str, object]:
        return {"allowed": self.allowed, "reasons": list(self.reasons)}


class TransmitGate:
    """Evaluates the full interlock chain.

    Instantiated with the configuration it will judge against; ``evaluate`` is
    otherwise pure.
    """

    def __init__(self, config: Config) -> None:
        self._config = config

    @property
    def config(self) -> Config:
        return self._config

    def evaluate(self, context: GateContext) -> GateDecision:
        """Return the decision, listing every interlock that failed."""
        reasons: list[str] = []

        self._check_master_switches(reasons)
        self._check_mode(reasons)
        self._check_account_identity(context, reasons)
        self._check_application_readiness(context, reasons)
        self._check_contract(context, reasons)
        self._check_reconciliation(context, reasons)
        self._check_market_data(context, reasons)
        self._check_permissions(context, reasons)
        self._check_strategy_and_risk(context, reasons)

        return GateDecision(allowed=not reasons, reasons=tuple(reasons))

    # -- individual interlocks -------------------------------------------

    def _check_master_switches(self, reasons: list[str]) -> None:
        if self._config.kill_switch:
            reasons.append(REASON_KILL_SWITCH)
        if not self._config.allow_order_transmit:
            reasons.append(REASON_TRANSMIT_NOT_ALLOWED)

    def _check_mode(self, reasons: list[str]) -> None:
        mode = self._config.trading_mode
        if mode is TradingMode.DISABLED:
            reasons.append(REASON_MODE_DISABLED)
        elif mode is TradingMode.LIVE and not self._config.live_trading_enabled:
            reasons.append(REASON_LIVE_NOT_ENABLED)

    def _check_account_identity(self, context: GateContext, reasons: list[str]) -> None:
        """Cross-check the configured mode against the *observed* account type.

        This is the interlock that stops a paper-mode process trading a live
        account and vice versa. An unknown account type is always a denial:
        "we could not determine what this is" is never good enough.
        """
        observed = context.broker_account_type
        if observed is AccountType.UNKNOWN:
            reasons.append(REASON_ACCOUNT_TYPE_UNKNOWN)
            return

        expected = _expected_account_type(self._config.trading_mode)
        if expected is None:
            # DISABLED mode; already rejected by _check_mode.
            return
        if observed is not expected:
            reasons.append(_MISMATCH_REASON[expected])

    def _check_application_readiness(self, context: GateContext, reasons: list[str]) -> None:
        if context.app_state is not ApplicationState.READY:
            reasons.append(REASON_APP_NOT_READY)
        if context.connection_state is not ConnectionState.CONNECTED:
            reasons.append(REASON_BROKER_NOT_CONNECTED)
        if not context.account_available:
            reasons.append(REASON_ACCOUNT_UNAVAILABLE)

    def _check_contract(self, context: GateContext, reasons: list[str]) -> None:
        if not context.contract_qualified:
            reasons.append(REASON_CONTRACT_NOT_QUALIFIED)
        if context.contract_is_continuous:
            # A continuous future is an analytics construct. It is not
            # deliverable and must never carry an order.
            reasons.append(REASON_CONTRACT_IS_CONTINUOUS)

    def _check_reconciliation(self, context: GateContext, reasons: list[str]) -> None:
        if not context.positions_reconciled:
            reasons.append(REASON_POSITIONS_NOT_RECONCILED)
        if not context.open_orders_reconciled:
            reasons.append(REASON_ORDERS_NOT_RECONCILED)

    def _check_market_data(self, context: GateContext, reasons: list[str]) -> None:
        max_age = self._config.market_data_max_age_seconds
        if max_age <= 0:
            # Unconfigured means "trading not authorised", not "no limit".
            reasons.append(REASON_MARKET_DATA_AGE_NOT_CONFIGURED)

        if context.market_data_is_delayed:
            # IBKR serves delayed ticks in the same fields as real-time ones,
            # so an unsubscribed or lapsed account looks identical to a healthy
            # one downstream. Observed: code 354 on MSLQ6, "Delayed market data
            # is available". Accepting that offer silently is how a bot ends up
            # pricing orders off quotes from a quarter of an hour ago.
            reasons.append(REASON_MARKET_DATA_DELAYED)

        age = context.market_data_age_seconds
        if age is None:
            reasons.append(REASON_MARKET_DATA_UNAVAILABLE)
            return
        if age < 0:
            # A tick stamped in the future means a clock problem somewhere.
            # Trading through a clock problem is not acceptable.
            reasons.append(REASON_MARKET_DATA_TIMESTAMP_IN_FUTURE)
            return
        if max_age > 0 and age > max_age:
            reasons.append(REASON_MARKET_DATA_STALE)

    def _check_permissions(self, context: GateContext, reasons: list[str]) -> None:
        if self._config.uses_ibkr and not self._config.sol_futures_permission_ready:
            reasons.append(REASON_PERMISSION_CONFIG_NOT_READY)
        if not context.broker_permission_ready:
            reasons.append(REASON_PERMISSION_BROKER_NOT_READY)

    def _check_strategy_and_risk(self, context: GateContext, reasons: list[str]) -> None:
        if not (self._config.strategy_enabled and context.strategy_enabled):
            reasons.append(REASON_STRATEGY_DISABLED)
        if not context.risk_approved:
            reasons.append(REASON_RISK_NOT_APPROVED)


_MISMATCH_REASON: dict[AccountType, str] = {
    AccountType.LIVE: REASON_ACCOUNT_MISMATCH_EXPECTED_LIVE,
    AccountType.PAPER: REASON_ACCOUNT_MISMATCH_EXPECTED_PAPER,
    AccountType.SIMULATED: REASON_ACCOUNT_MISMATCH_EXPECTED_SIMULATED,
}


def _expected_account_type(mode: TradingMode) -> AccountType | None:
    """The one account type each mode is permitted to trade."""
    if mode is TradingMode.MOCK:
        return AccountType.SIMULATED
    if mode is TradingMode.PAPER:
        return AccountType.PAPER
    if mode is TradingMode.LIVE:
        return AccountType.LIVE
    return None


def expected_account_type(mode: TradingMode) -> AccountType | None:
    """Public accessor used by the broker layer to refuse a wrong connection."""
    return _expected_account_type(mode)


def all_reasons() -> Sequence[str]:
    """Every reason string the gate can emit. Used by docs and tests."""
    return tuple(
        value
        for name, value in sorted(globals().items())
        if name.startswith("REASON_") and isinstance(value, str)
    )


__all__ = [
    "GateContext",
    "GateDecision",
    "TransmitGate",
    "all_reasons",
    "expected_account_type",
]
