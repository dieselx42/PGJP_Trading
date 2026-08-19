"""Operator-originated orders.

This is the only way a human can cause this system to send an order, and it
exists for one reason: the write path -- ``place_order``, ``orderStatus``,
``execDetails``, ``commissionReport`` -- has never run. The read-only checkout
proved everything the system *reads* from IBKR. Nothing has proved what it
*writes*, and arming a strategy to find out is the wrong first experiment: a
strategy fires on a market tick, at a moment nobody chose, with nobody watching.

What this is NOT
----------------
**It is not a bypass.** The order travels the identical path a strategy order
takes -- :class:`~app.signals.validator.SignalValidator`,
:class:`~app.risk.manager.RiskManager`, :class:`~app.safety.gate.TransmitGate`,
:class:`~app.execution.order_manager.OrderManager` -- by calling the same
method the tick loop calls. Nothing here re-implements the pipeline, so nothing
here can drift out of step with it or quietly omit a check.

**It cannot loosen an interlock.** It sets no configuration, clears no kill
switch, and raises no limit. Every refusal the gate or the risk manager can
produce, it produces here too, and the command reports them rather than
working around them. A refusal is a successful test of the interlock, not a
failure of the command.

**It refuses live mode outright**, before it constructs anything. Whether live
trading is ever driven by hand is a separate decision that nobody should be
able to take by accident at three in the morning.

Two phases, and the safe one is the default
-------------------------------------------
Run without ``--confirm`` and it is a **preview**: it connects, reconciles,
resolves the contract, builds the intent, and asks both approvers what they
would say -- then stops, having persisted nothing and sent nothing. The
preview prints the token that ``--confirm`` requires, and that token is the
broker-reported ``local_symbol``. So the confirmation cannot be typed from
memory or guessed from the runbook; it can only come from a preview that
actually reached IBKR and resolved a real contract.

That is deliberate. A ``--yes`` flag proves the operator can type ``--yes``.
Requiring a value that only the system can tell you proves they looked.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.enums import Direction, OrderType, TradingMode
from app.logging_config import get_logger
from app.signals.models import TradeIntent
from app.signals.validator import SignalValidator
from app.utilities.ids import new_correlation_id
from app.utilities.timeutils import utc_now

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config import Config

_LOG = get_logger("execution.operator")

#: The strategy name recorded against an operator order.
#:
#: Deliberately not the configured strategy's name. Every one of these is
#: durably attributed to a human in the ``signals`` and ``orders`` tables, so a
#: later reader can never mistake one for something a strategy decided.
OPERATOR_STRATEGY_NAME = "operator"

RESULT_REFUSED_LIVE = "REFUSED_LIVE_MODE"
RESULT_PREVIEW = "PREVIEW_ONLY"
RESULT_CONFIRM_MISMATCH = "CONFIRMATION_MISMATCH"
RESULT_NOT_CONNECTED = "BROKER_NOT_CONNECTED"
RESULT_NO_CONTRACT = "NO_QUALIFIED_CONTRACT"
RESULT_SUBMITTED = "SUBMITTED"


@dataclass(frozen=True, slots=True)
class OperatorOrderRequest:
    """What the operator asked for."""

    target_position: int
    """Absolute target in contracts, signed. ``0`` means flat.

    A target rather than a delta, because that is what a
    :class:`~app.signals.models.TradeIntent` is everywhere else in this system.
    Running the same command twice asks for the same *state*, so the second run
    is a no-op rather than a doubled position.
    """

    order_type: OrderType = OrderType.LIMIT
    limit_price: Decimal | None = None
    confirm: str | None = None

    @property
    def direction(self) -> Direction:
        if self.target_position > 0:
            return Direction.LONG
        if self.target_position < 0:
            return Direction.SHORT
        return Direction.FLAT


def _refusal(result: str, **detail: object) -> dict[str, object]:
    return {"result": result, **detail}


async def place_operator_order(config: Config, request: OperatorOrderRequest) -> dict[str, object]:
    """Send one operator order through the real pipeline. Returns a report."""
    # Live mode is refused before a broker, a database or an application object
    # exists. Nothing is constructed that could reach a live account.
    if config.trading_mode is TradingMode.LIVE:
        return _refusal(
            RESULT_REFUSED_LIVE,
            detail=(
                "operator orders are not available in live mode. This command exists to "
                "exercise an unproven write path, and doing that against real money is a "
                "separate decision that is deliberately not available here."
            ),
            trading_mode=config.trading_mode.value,
        )

    if request.order_type is OrderType.LIMIT and request.limit_price is None:
        return _refusal(
            "LIMIT_PRICE_REQUIRED",
            detail="a limit order needs --limit-price; refusing to invent one",
        )

    from app.main import TradingApplication  # noqa: PLC0415 -- avoids a cycle

    app = TradingApplication(
        config,
        run_id=f"operator-{new_correlation_id()}",
        # Runs alongside the trading process, not as it: no health-server
        # bind, and the admin client id so this cannot knock the bot off
        # its IBKR session. See TradingApplication.__init__.
        is_admin_instance=True,
    )
    await app.startup()
    try:
        return await _run(app, request)
    finally:
        await app.shutdown()


async def _run(app: Any, request: OperatorOrderRequest) -> dict[str, object]:
    connected = await app._connect_with_backoff()
    if not connected:
        return _refusal(RESULT_NOT_CONNECTED, detail=app._last_error)

    # Reconcile and resolve the contract exactly as the running system does.
    # An operator order is not a reason to skip reconciliation -- placing one
    # against an unreconciled book is precisely the situation the SAFE state
    # exists to prevent.
    await app._after_connect()

    contract = app.contract
    if contract is None:
        return _refusal(
            RESULT_NO_CONTRACT,
            detail=(
                "no contract was qualified; set DEFAULT_CONTRACT_MONTH or check "
                "reconciliation, which must succeed before a contract is resolved"
            ),
            app_state=app.state.value,
        )

    # Poll once so market-data freshness is a real measurement rather than the
    # absence of one. The gate refuses on stale or missing data either way; the
    # point is that it should refuse for the true reason.
    if app.market_data is not None:
        await app.market_data.poll_all()

    # The validator knows only the configured strategy. Teaching it this one
    # name is a visible act here rather than a permanent widening of what the
    # running system will accept from a signal.
    def _validator() -> SignalValidator:
        validator = SignalValidator(
            configured_symbol=app.config.default_futures_symbol,
            known_strategies=[app.strategy.name, OPERATOR_STRATEGY_NAME],
        )
        validator.seed_seen(app.repositories.signals.known_intent_ids())
        return validator

    app.validator = _validator()

    # A SEPARATE validator answers the preview, because asking must not answer.
    # `SignalValidator.validate` records the intent id whenever it accepts --
    # that is what makes a replayed signal harmless -- so previewing through the
    # real validator marks the intent as seen, and the actual submission is then
    # refused as a duplicate of its own preview. Caught by the control test in
    # `tests/integration/test_operator_order_pipeline.py`, which is exactly the
    # test that exists so the refusal tests cannot pass vacuously.
    preview_validator = _validator()

    correlation_id = new_correlation_id()
    intent = TradeIntent(
        strategy_name=OPERATOR_STRATEGY_NAME,
        symbol=contract.symbol,
        direction=request.direction,
        requested_position=request.target_position,
        created_at=utc_now(),
        correlation_id=correlation_id,
        contract_key=contract.key(),
        metadata={"origin": "operator cli", "order_type": request.order_type.value},
    )

    current = app.position_book.quantity(contract.con_id)
    from app.execution.order_manager import OrderManager  # noqa: PLC0415

    side, quantity = OrderManager.delta_to_side_and_quantity(
        current_position=current, requested_position=request.target_position
    )

    plan: dict[str, object] = {
        "contract": {
            "local_symbol": contract.local_symbol,
            "con_id": contract.con_id,
            "expiration": contract.expiration,
            "multiplier": contract.multiplier,
        },
        "current_position": current,
        "target_position": request.target_position,
        "resulting_order": (
            None
            if quantity == 0
            else {
                "side": side.value,
                "quantity": quantity,
                "order_type": request.order_type.value,
                "limit_price": (None if request.limit_price is None else str(request.limit_price)),
            }
        ),
        "app_state": app.state.value,
    }

    # -- both approvers, asked but not acted on --------------------------
    validation = preview_validator.validate(intent)
    risk_context = app._build_risk_context(intent, contract=contract, side=side, quantity=quantity)
    risk_decision = app.risk_manager.evaluate(risk_context)
    gate_decision = app.gate.evaluate(app._build_gate_context(risk_approved=risk_decision.approved))

    approvals = {
        "signal_validation": {
            "accepted": validation.accepted,
            "reasons": list(validation.reasons),
        },
        "risk": {"approved": risk_decision.approved, "reasons": list(risk_decision.reasons)},
        "gate": {"allowed": gate_decision.allowed, "reasons": list(gate_decision.reasons)},
    }

    if request.confirm != contract.local_symbol:
        return {
            "result": RESULT_PREVIEW if request.confirm is None else RESULT_CONFIRM_MISMATCH,
            "transmitted": False,
            "plan": plan,
            "approvals": approvals,
            "would_be_allowed": bool(
                validation.accepted and risk_decision.approved and gate_decision.allowed
            ),
            "to_send_this_order_add": f"--confirm {contract.local_symbol}",
            "note": (
                "nothing was sent and nothing was persisted. The confirmation token is the "
                "contract's broker-reported local symbol, so it can only come from a preview "
                "that actually resolved a contract at IBKR."
            ),
        }

    _LOG.warning(
        "operator order confirmed; submitting through the full pipeline",
        extra={
            "event": "order.operator_submit",
            "correlation_id": correlation_id,
            "local_symbol": contract.local_symbol,
            "target_position": request.target_position,
            "current_position": current,
        },
    )

    outcome = await app._handle_intent(
        intent, order_type=request.order_type, limit_price=request.limit_price
    )

    return {
        "result": RESULT_SUBMITTED,
        "transmitted": bool(outcome is not None and outcome.order is not None),
        "plan": plan,
        "approvals": approvals,
        "outcome": None if outcome is None else outcome.describe(),
    }


__all__ = [
    "OPERATOR_STRATEGY_NAME",
    "OperatorOrderRequest",
    "place_operator_order",
]
