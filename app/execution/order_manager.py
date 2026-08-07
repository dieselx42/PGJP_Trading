"""Order manager.

The single path from a validated trade intent to a broker order. Nothing else
in the application calls ``broker.place_order``.

The sequence is fixed and every step must pass:

1. Work out the position delta the intent implies. No delta, no order.
2. Derive the deterministic idempotency key and persist the order **before**
   any risk or broker work. A crash after this point leaves evidence.
3. If that key already exists and the existing order reached the broker,
   stop -- this is a replay, not a new trade.
4. Risk manager approval.
5. Transmit gate approval.
6. Mark the order PENDING_SUBMIT and flush that to disk *before* calling the
   broker, so a crash between the write and the acknowledgement leaves an order
   that reconciliation will flag rather than one we forget we sent.
7. Transmit.

Steps 4 and 5 are independent and both must approve. The order is persisted
with its block reasons whenever either refuses, so every refusal is auditable
after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.broker.base import Broker
from app.broker.models import (
    BrokerError,
    BrokerPermissionError,
    OrderRequest,
    PlaceOrderResult,
)
from app.config import Config
from app.contracts.models import QualifiedContract
from app.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from app.execution.order_models import Order, OrderValidationError, build_order
from app.logging_config import get_logger
from app.risk.manager import RiskContext, RiskDecision, RiskManager
from app.safety.gate import GateContext, GateDecision, TransmitGate
from app.signals.models import TradeIntent
from app.state.repositories import Repositories
from app.utilities.timeutils import utc_now

_LOG = get_logger("execution.order_manager")

OUTCOME_NO_CHANGE = "NO_POSITION_CHANGE_REQUIRED"
OUTCOME_DUPLICATE = "DUPLICATE_SUPPRESSED_BY_IDEMPOTENCY_KEY"
OUTCOME_RISK_REJECTED = "RISK_REJECTED"
OUTCOME_GATE_REJECTED = "TRANSMIT_GATE_REJECTED"
OUTCOME_BROKER_ERROR = "BROKER_ERROR"
OUTCOME_TRANSMITTED = "TRANSMITTED"
OUTCOME_INVALID = "ORDER_INVALID"


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    """What happened to one intent."""

    outcome: str
    order: Order | None = None
    risk_decision: RiskDecision | None = None
    gate_decision: GateDecision | None = None
    place_result: PlaceOrderResult | None = None
    error: str | None = None

    @property
    def transmitted(self) -> bool:
        return self.outcome == OUTCOME_TRANSMITTED

    @property
    def reasons(self) -> tuple[str, ...]:
        if self.risk_decision is not None and not self.risk_decision.approved:
            return self.risk_decision.reasons
        if self.gate_decision is not None and not self.gate_decision.allowed:
            return self.gate_decision.reasons
        return ()

    def describe(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "transmitted": self.transmitted,
            "reasons": list(self.reasons),
            "error": self.error,
            "order": self.order.describe() if self.order else None,
        }


class OrderManager:
    """Builds, guards, persists, and transmits orders."""

    def __init__(
        self,
        *,
        config: Config,
        broker: Broker,
        repositories: Repositories,
        risk_manager: RiskManager,
        gate: TransmitGate,
        run_id: str,
    ) -> None:
        self._config = config
        self._broker = broker
        self._repos = repositories
        self._risk = risk_manager
        self._gate = gate
        self._run_id = run_id

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    async def submit_intent(
        self,
        intent: TradeIntent,
        *,
        contract: QualifiedContract,
        risk_context: RiskContext,
        gate_context: GateContext,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        time_in_force: TimeInForce = TimeInForce.DAY,
        at: datetime | None = None,
    ) -> SubmissionOutcome:
        """Run the full pipeline for one intent."""
        now = at or utc_now()
        correlation_id = intent.correlation_id or intent.intent_id

        if risk_context.order_quantity <= 0:
            return SubmissionOutcome(outcome=OUTCOME_NO_CHANGE)

        # -- build and persist first -----------------------------------
        try:
            order = build_order(
                contract=contract,
                side=risk_context.order_side,
                quantity=risk_context.order_quantity,
                order_type=order_type,
                idempotency_key=intent.idempotency_key(con_id=contract.con_id),
                correlation_id=correlation_id,
                strategy_id=intent.strategy_name,
                run_id=self._run_id,
                trading_mode=self._config.trading_mode,
                account_type=gate_context.broker_account_type,
                intent_id=intent.intent_id,
                limit_price=limit_price,
                time_in_force=time_in_force,
                at=now,
            )
        except OrderValidationError as exc:
            _LOG.error(
                "order failed structural validation",
                extra={
                    "event": "order.invalid",
                    "correlation_id": correlation_id,
                    "error": str(exc),
                },
            )
            return SubmissionOutcome(outcome=OUTCOME_INVALID, error=str(exc))

        stored, created = self._repos.orders.insert_if_absent(order)
        if not created:
            # A durable record already exists for this exact economic intent.
            if stored.was_transmitted:
                _LOG.warning(
                    "suppressing duplicate order; an order for this intent already reached "
                    "the broker",
                    extra={
                        "event": "order.duplicate_suppressed",
                        "correlation_id": correlation_id,
                        "internal_order_id": stored.internal_order_id,
                        "idempotency_key": stored.idempotency_key,
                        "existing_status": stored.status.value,
                        "broker_order_id": stored.broker_order_id,
                    },
                )
                return SubmissionOutcome(outcome=OUTCOME_DUPLICATE, order=stored)
            # It exists but never reached the broker (BLOCKED or NEW). Carry on
            # with the stored record so we do not create a second row.
            order = stored

        _LOG.info(
            "order created",
            extra={"event": "order.created", **order.describe()},
        )

        # -- risk -------------------------------------------------------
        risk_decision = self._risk.evaluate(risk_context)
        self._repos.risk_decisions.record(
            correlation_id=correlation_id,
            run_id=self._run_id,
            approved=risk_decision.approved,
            reasons=risk_decision.reasons,
            detail=risk_decision.detail,
            intent_id=intent.intent_id,
            decided_at=now,
        )
        if not risk_decision.approved:
            order.mark_blocked(risk_decision.reasons, at=now)
            self._repos.orders.update(order)
            return SubmissionOutcome(
                outcome=OUTCOME_RISK_REJECTED, order=order, risk_decision=risk_decision
            )

        # -- transmit gate ---------------------------------------------
        gate_decision = self._gate.evaluate(gate_context)
        if not gate_decision.allowed:
            order.mark_blocked(gate_decision.reasons, at=now)
            self._repos.orders.update(order)
            _LOG.warning(
                "transmit gate refused an order",
                extra={
                    "event": "order.gate_rejected",
                    "correlation_id": correlation_id,
                    "internal_order_id": order.internal_order_id,
                    "reasons": list(gate_decision.reasons),
                },
            )
            return SubmissionOutcome(
                outcome=OUTCOME_GATE_REJECTED,
                order=order,
                risk_decision=risk_decision,
                gate_decision=gate_decision,
            )

        try:
            order.require_transmittable_type()
        except OrderValidationError as exc:
            order.mark_blocked((str(exc),), at=now)
            self._repos.orders.update(order)
            return SubmissionOutcome(outcome=OUTCOME_INVALID, order=order, error=str(exc))

        # -- transmit ---------------------------------------------------
        # Persisted BEFORE the broker call on purpose: if the process dies
        # between here and the acknowledgement, reconciliation finds an order in
        # PENDING_SUBMIT and refuses to trade until a human resolves it. The
        # alternative -- writing after the call -- can lose an order we actually
        # sent.
        order.mark_pending_submit(at=now)
        self._repos.orders.update(order)

        request = OrderRequest(
            internal_order_id=order.internal_order_id,
            correlation_id=correlation_id,
            contract=contract,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            transmit=True,
        )

        try:
            result = await self._broker.place_order(request)
        except BrokerPermissionError as exc:
            order.mark_rejected("PERMISSION_DENIED", str(exc), at=utc_now())
            self._repos.orders.update(order)
            _LOG.error(
                "broker refused the order for permission reasons; not retrying",
                extra={
                    "event": "order.permission_denied",
                    "correlation_id": correlation_id,
                    "internal_order_id": order.internal_order_id,
                    "error": str(exc),
                    "retryable": False,
                },
            )
            return SubmissionOutcome(
                outcome=OUTCOME_BROKER_ERROR,
                order=order,
                risk_decision=risk_decision,
                gate_decision=gate_decision,
                error=str(exc),
            )
        except BrokerError as exc:
            order.mark_error(type(exc).__name__, str(exc), at=utc_now())
            self._repos.orders.update(order)
            _LOG.error(
                "broker error while placing an order",
                extra={
                    "event": "order.broker_error",
                    "correlation_id": correlation_id,
                    "internal_order_id": order.internal_order_id,
                    "error": str(exc),
                    "retryable": exc.retryable,
                },
            )
            return SubmissionOutcome(
                outcome=OUTCOME_BROKER_ERROR,
                order=order,
                risk_decision=risk_decision,
                gate_decision=gate_decision,
                error=str(exc),
            )

        if not result.accepted or result.broker_order_id is None:
            order.mark_rejected(result.error_code, result.error_message or "rejected by broker")
            self._repos.orders.update(order)
            return SubmissionOutcome(
                outcome=OUTCOME_BROKER_ERROR,
                order=order,
                risk_decision=risk_decision,
                gate_decision=gate_decision,
                place_result=result,
                error=result.error_message,
            )

        order.mark_submitted(result.broker_order_id, at=utc_now())
        if result.status is not OrderStatus.SUBMITTED:
            order.mark_status(result.status)
        self._repos.orders.update(order)
        _LOG.warning(
            "ORDER TRANSMITTED",
            extra={"event": "order.transmitted", **order.describe()},
        )
        return SubmissionOutcome(
            outcome=OUTCOME_TRANSMITTED,
            order=order,
            risk_decision=risk_decision,
            gate_decision=gate_decision,
            place_result=result,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def delta_to_side_and_quantity(
        *, current_position: int, requested_position: int
    ) -> tuple[OrderSide, int]:
        """Convert a target position into a side and a size.

        Intents express targets, not deltas, which is what makes a replayed
        intent harmless.
        """
        delta = requested_position - current_position
        if delta == 0:
            return OrderSide.BUY, 0
        return (OrderSide.BUY if delta > 0 else OrderSide.SELL), abs(delta)

    async def cancel_all_orders(self, *, reason: str) -> int:
        """Emergency cancel of every working order.

        Deliberately does not touch positions. Cancelling resting orders and
        liquidating a book are different decisions with different consequences,
        and only the first one is automated here.
        """
        _LOG.warning(
            "cancel-all requested",
            extra={"event": "emergency.cancel_all", "reason": reason},
        )
        cancelled = await self._broker.cancel_all_orders()
        now = utc_now()
        for order in self._repos.orders.open_orders():
            order.mark_status(OrderStatus.CANCEL_PENDING, at=now)
            self._repos.orders.update(order)
        return cancelled

    def open_order_count(self) -> int:
        return len(self._repos.orders.open_orders())

    def orders_last_hour(self, *, now: datetime | None = None) -> int:
        return self._repos.orders.count_transmitted_last_hour(now=now)


__all__ = [
    "OUTCOME_BROKER_ERROR",
    "OUTCOME_DUPLICATE",
    "OUTCOME_GATE_REJECTED",
    "OUTCOME_INVALID",
    "OUTCOME_NO_CHANGE",
    "OUTCOME_RISK_REJECTED",
    "OUTCOME_TRANSMITTED",
    "OrderManager",
    "SubmissionOutcome",
]
