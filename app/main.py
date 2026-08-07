"""Application entry point and runtime.

Wires the components together and owns the lifecycle:

    STARTING -> CONNECTING -> RECONCILING -> SAFE -> READY
                     ^                         |
                     +--- disconnect ----------+

The state machine is the point, not decoration. READY is reachable only through
successful reconciliation, and every path that loses confidence in our view of
the account -- a disconnect, a failed reconcile, a permission refusal -- drops
back to SAFE, where the transmit gate refuses everything.

Run with ``python -m app.main``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Sequence

from app.broker.base import Broker
from app.broker.mock_broker import MockBroker
from app.broker.models import (
    AccountSummary,
    BrokerError,
    BrokerPermissionError,
)
from app.config import Config, ConfigError
from app.contracts.models import QualifiedContract
from app.contracts.resolver import ContractResolutionError, ContractResolver
from app.enums import AccountType, ApplicationState, ConnectionState, OrderSide, TradingMode
from app.execution.order_manager import OrderManager, SubmissionOutcome
from app.logging_config import (
    configure_logging,
    correlation_scope,
    get_logger,
    mask_account_id,
)
from app.market_data.manager import MarketDataManager
from app.monitoring.health import evaluate_health
from app.monitoring.server import HealthServer
from app.monitoring.status import build_info, safety_summary
from app.portfolio.positions import PositionBook
from app.portfolio.reconciliation import Reconciler, ReconciliationResult
from app.risk.manager import RiskContext, RiskManager
from app.safety.gate import GateContext, TransmitGate
from app.safety.killswitch import KillSwitch
from app.signals.models import TradeIntent
from app.signals.validator import SignalValidator
from app.state.database import Database
from app.state.models import BotEvent, BrokerEvent, SignalRecord
from app.state.repositories import Repositories
from app.strategy.base import Strategy
from app.strategy.noop import build_strategy
from app.utilities.ids import new_correlation_id, new_run_id
from app.utilities.timeutils import utc_now

_LOG = get_logger("main")

STATE_KEY_LAST_STATE = "last_application_state"
STATE_KEY_LAST_RUN_ID = "last_run_id"
STATE_KEY_LAST_SHUTDOWN = "last_shutdown_at"


class TradingApplication:
    """The running trading system."""

    def __init__(self, config: Config, *, run_id: str | None = None) -> None:
        self.config = config
        self.run_id = run_id or new_run_id()

        self.state = ApplicationState.STARTING
        self._stop_event = asyncio.Event()
        self._shutting_down = False

        self.database = Database(config.database_path)
        self.repositories = Repositories(self.database)
        self.kill_switch = KillSwitch(
            config_engaged=config.kill_switch, store=self.repositories.state
        )
        self.gate = TransmitGate(config)
        self.risk_manager = RiskManager(config)

        self.broker: Broker | None = None
        self.resolver: ContractResolver | None = None
        self.market_data: MarketDataManager | None = None
        self.order_manager: OrderManager | None = None
        self.strategy: Strategy | None = None
        self.validator: SignalValidator | None = None

        self.position_book = PositionBook()
        self.reconciler = Reconciler()
        self.reconciliation: ReconciliationResult = ReconciliationResult()
        self.account: AccountSummary | None = None
        self.contract: QualifiedContract | None = None

        self.health_server = HealthServer(
            host=config.health_host,
            port=config.health_port,
            health_provider=self.health_payload,
            status_provider=self.status_payload,
        )

        self._connect_attempt = 0
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        self._set_state(ApplicationState.STARTING)
        self.database.connect()
        version = self.database.migrate()
        self._record_event(
            "app.starting",
            f"starting {self.config.trading_mode.value} mode",
            data={"schema_version": version, **build_info(self.config)},
        )
        _LOG.info(
            "application starting",
            extra={
                "event": "app.starting",
                "run_id": self.run_id,
                **build_info(self.config),
                **safety_summary(self.config, kill_switch_engaged=self.kill_switch.engaged()),
            },
        )
        self.repositories.state.set_state(STATE_KEY_LAST_RUN_ID, self.run_id)

        self.strategy = build_strategy(
            self.config.strategy_name, enabled=self.config.strategy_enabled
        )
        self.validator = SignalValidator(
            configured_symbol=self.config.default_futures_symbol,
            known_strategies=[self.strategy.name],
        )
        self.validator.seed_seen(self.repositories.signals.known_intent_ids())

        self.position_book.replace_all(self.repositories.positions.all())

        await self.health_server.start()

        if self.config.trading_mode is TradingMode.DISABLED:
            # No broker is constructed at all. There is nothing to place an
            # order through, which is a stronger guarantee than a flag.
            self._set_state(ApplicationState.SAFE)
            _LOG.warning(
                "TRADING_MODE=disabled: no broker will be created and no orders are possible",
                extra={"event": "app.disabled"},
            )
            return

        self.broker = self._build_broker()
        self.resolver = ContractResolver(self.broker)
        self.market_data = MarketDataManager(
            self.broker, max_age_seconds=self.config.market_data_max_age_seconds
        )
        self.order_manager = OrderManager(
            config=self.config,
            broker=self.broker,
            repositories=self.repositories,
            risk_manager=self.risk_manager,
            gate=self.gate,
            run_id=self.run_id,
        )

    def _build_broker(self) -> Broker:
        mode = self.config.trading_mode
        if mode is TradingMode.MOCK:
            return MockBroker()
        # Imported here so that DISABLED and MOCK runs never load IBKR code.
        from app.broker.ibkr_broker import IBKRBroker  # noqa: PLC0415
        from app.safety.gate import expected_account_type  # noqa: PLC0415

        port = self.config.ib_port
        if port is None:  # pragma: no cover - unreachable for paper/live
            raise ConfigError(f"no IB port resolved for trading mode {mode.value}")
        return IBKRBroker(
            host=self.config.ibkr.host,
            port=port,
            client_id=self.config.ibkr.client_id,
            connect_timeout_seconds=self.config.ibkr.connect_timeout_seconds,
            expected_account_type=expected_account_type(mode),
        )

    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        _LOG.info("application shutting down", extra={"event": "app.shutdown"})
        self._record_event("app.shutdown", "graceful shutdown")
        with contextlib.suppress(Exception):
            self.repositories.state.set_state(STATE_KEY_LAST_STATE, self.state.value)
            self.repositories.state.set_state(STATE_KEY_LAST_SHUTDOWN, utc_now().isoformat())
        with contextlib.suppress(Exception):
            await self.health_server.stop()
        if self.broker is not None:
            with contextlib.suppress(Exception):
                await self.broker.disconnect()
        self.database.close()

    def request_stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> int:
        await self.startup()
        try:
            await self._main_loop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- top-level guard
            self._last_error = str(exc)
            self._set_state(ApplicationState.ERROR)
            _LOG.exception("unhandled error in main loop", extra={"event": "app.fatal"})
            self._record_event("app.fatal", str(exc), level="ERROR")
            return 1
        finally:
            await self.shutdown()
        return 0

    async def _main_loop(self) -> None:
        interval = self.config.market_data_poll_interval_seconds
        while not self._stop_event.is_set():
            if self.config.trading_mode is TradingMode.DISABLED:
                await self._sleep(interval)
                continue

            broker = self.broker
            assert broker is not None

            if not broker.is_connected():
                connected = await self._connect_with_backoff()
                if not connected:
                    continue
                await self._after_connect()

            await self._tick()
            await self._sleep(interval)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake immediately on shutdown."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def _connect_with_backoff(self) -> bool:
        """Connect, backing off exponentially. Returns False when we should not retry."""
        broker = self.broker
        assert broker is not None
        policy = self.config.reconnect

        self._set_state(ApplicationState.CONNECTING)
        self._connect_attempt += 1
        attempt = self._connect_attempt

        try:
            info = await broker.connect()
        except BrokerPermissionError as exc:
            # Permission problems never resolve by retrying. Stop, stay SAFE,
            # and make the reason obvious.
            self._last_error = str(exc)
            self._set_state(ApplicationState.SAFE)
            _LOG.error(
                "broker refused the connection for permission reasons; not retrying",
                extra={
                    "event": "broker.permission_denied",
                    "error": str(exc),
                    "retryable": False,
                },
            )
            self._record_broker_event("broker.permission_denied", str(exc))
            await self._sleep(policy.max_seconds)
            return False
        except BrokerError as exc:
            self._last_error = str(exc)
            self._set_state(ApplicationState.DISCONNECTED)
            delay = policy.delay_for_attempt(attempt)
            _LOG.warning(
                "broker connection failed; backing off",
                extra={
                    "event": "broker.connect_failed",
                    "attempt": attempt,
                    "retry_in_seconds": delay,
                    "error": str(exc),
                },
            )
            self._record_broker_event("broker.connect_failed", str(exc), data={"attempt": attempt})
            if policy.max_attempts and attempt >= policy.max_attempts:
                _LOG.error(
                    "giving up on broker connection after the configured maximum attempts",
                    extra={"event": "broker.connect_abandoned", "attempts": attempt},
                )
                self.request_stop()
                return False
            await self._sleep(delay)
            return False

        self._connect_attempt = 0
        self._last_error = None
        self._record_broker_event("broker.connected", "connected", data=info.describe())
        return True

    async def _after_connect(self) -> None:
        """Reconcile, resolve the contract, and subscribe to data."""
        await self._reconcile()

        if not self.reconciliation.succeeded:
            # SAFE, and it stays SAFE. Trading resumes only after a human
            # resolves the discrepancy.
            self._set_state(ApplicationState.SAFE)
            return

        await self._resolve_contract()
        self._set_state(self._compute_ready_state())

    async def _reconcile(self) -> None:
        broker = self.broker
        assert broker is not None
        self._set_state(ApplicationState.RECONCILING)

        account_available = False
        try:
            self.account = await broker.get_account_summary()
            account_available = True
        except BrokerError as exc:
            self.account = None
            _LOG.error(
                "could not retrieve account summary",
                extra={"event": "reconciliation.account_failed", "error": str(exc)},
            )

        try:
            broker_positions = await broker.get_positions()
            broker_orders = await broker.get_open_orders()
            broker_fills = await broker.get_fills()
        except BrokerError as exc:
            self.reconciliation = ReconciliationResult(errors=(str(exc),))
            _LOG.error(
                "reconciliation could not read broker state",
                extra={"event": "reconciliation.read_failed", "error": str(exc)},
            )
            self._record_event("reconciliation.failed", str(exc), level="ERROR")
            return

        self.reconciliation = self.reconciler.reconcile(
            book=self.position_book,
            broker_positions=broker_positions,
            broker_open_orders=broker_orders,
            local_open_orders=self.repositories.orders.open_order_tuples(),
            broker_fills=broker_fills,
            account_available=account_available,
        )

        if self.reconciliation.succeeded:
            self.position_book.replace_all(self.reconciliation.broker_positions)
            self.repositories.positions.replace_all(self.reconciliation.broker_positions)
            self._record_event(
                "reconciliation.succeeded",
                "broker and local state agree",
                data=self.reconciliation.describe(),
            )
        else:
            self._record_event(
                "reconciliation.failed",
                "broker and local state disagree; human intervention required",
                level="ERROR",
                data=self.reconciliation.describe(),
            )

    async def _resolve_contract(self) -> None:
        resolver = self.resolver
        market_data = self.market_data
        if resolver is None or market_data is None:
            return
        try:
            contract = await resolver.resolve(
                self.config.default_futures_symbol, self.config.default_contract_month
            )
        except ContractResolutionError as exc:
            self.contract = None
            self._last_error = str(exc)
            _LOG.warning(
                "no tradeable contract is selected; market data and strategy stay inactive",
                extra={"event": "contract.unresolved", "error": str(exc)},
            )
            self._record_event("contract.unresolved", str(exc), level="WARNING")
            return

        self.contract = contract
        self.repositories.contracts.upsert(contract)
        await market_data.subscribe(contract)

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        broker = self.broker
        market_data = self.market_data
        strategy = self.strategy
        if broker is None or market_data is None or strategy is None:
            return

        if not broker.is_connected():
            self._handle_disconnect("broker reported disconnected during tick")
            return

        if self.contract is None:
            return

        quotes = await market_data.poll_all()

        # Kill switch stops strategies producing actionable output. Monitoring,
        # data collection, and status remain fully available.
        if self.kill_switch.engaged():
            if self.state is not ApplicationState.HALTED:
                self._set_state(ApplicationState.HALTED)
            return

        for quote in quotes:
            intents = strategy.handle_quote(quote)
            for intent in intents:
                await self._handle_intent(intent)

    def _handle_disconnect(self, reason: str) -> None:
        """Drop to SAFE and forget cached data."""
        self._set_state(ApplicationState.SAFE)
        self.reconciliation = ReconciliationResult()
        self.account = None
        if self.market_data is not None:
            self.market_data.clear()
        self.contract = None
        _LOG.warning(
            "broker disconnected; entering SAFE state and refusing orders",
            extra={"event": "broker.disconnected", "reason": reason},
        )
        self._record_broker_event("broker.disconnected", reason)

    # ------------------------------------------------------------------
    # Intent handling
    # ------------------------------------------------------------------

    async def _handle_intent(self, intent: TradeIntent) -> SubmissionOutcome | None:
        validator = self.validator
        order_manager = self.order_manager
        if validator is None or order_manager is None:
            return None

        correlation_id = intent.correlation_id or new_correlation_id()
        with correlation_scope(correlation_id):
            validation = validator.validate(intent)
            self.repositories.signals.record(
                SignalRecord(
                    intent_id=intent.intent_id,
                    correlation_id=correlation_id,
                    run_id=self.run_id,
                    strategy_name=intent.strategy_name,
                    symbol=intent.symbol,
                    direction=intent.direction.value,
                    requested_position=intent.requested_position,
                    created_at=intent.created_at,
                    accepted=validation.accepted,
                    confidence=intent.confidence,
                    contract_key=intent.contract_key,
                    metadata=intent.metadata,
                    rejection_reasons=validation.reasons,
                )
            )
            if not validation.accepted:
                return None

            contract = self.contract
            if contract is None:
                return None

            side, quantity = OrderManager.delta_to_side_and_quantity(
                current_position=self.position_book.quantity(contract.con_id),
                requested_position=intent.requested_position,
            )
            if quantity == 0:
                return None

            risk_context = self._build_risk_context(
                intent, contract=contract, side=side, quantity=quantity
            )
            gate_context = self._build_gate_context(risk_approved=True)

            return await order_manager.submit_intent(
                intent,
                contract=contract,
                risk_context=risk_context,
                gate_context=gate_context,
            )

    def _build_risk_context(
        self,
        intent: TradeIntent,
        *,
        contract: QualifiedContract,
        side: OrderSide,
        quantity: int,
    ) -> RiskContext:
        market_data = self.market_data
        quote = market_data.latest(contract.key()) if market_data else None
        performance = self.repositories.performance.get()
        order_manager = self.order_manager
        assert order_manager is not None

        return RiskContext(
            intent=intent,
            order_side=side,
            order_quantity=quantity,
            current_position=self.position_book.quantity(contract.con_id),
            contract=contract,
            reference_price=quote.mid if quote else None,
            market_data_age_seconds=(
                market_data.age_seconds(contract.key()) if market_data else None
            ),
            daily_pnl=performance.total_pnl,
            open_orders_count=order_manager.open_order_count(),
            orders_last_hour=order_manager.orders_last_hour(),
            app_state=self.state,
            connection_state=self._connection_state(),
            account_type=self._account_type(),
            account_available=self.account is not None,
            positions_reconciled=self.reconciliation.positions_reconciled,
            open_orders_reconciled=self.reconciliation.orders_reconciled,
            broker_permission_ready=self._broker_permission_ready(),
            strategy_enabled=self.strategy.enabled if self.strategy else False,
            kill_switch_engaged=self.kill_switch.engaged(),
            duplicate_order_exists=False,
        )

    def _build_gate_context(self, *, risk_approved: bool) -> GateContext:
        market_data = self.market_data
        contract = self.contract
        age = (
            market_data.age_seconds(contract.key())
            if market_data is not None and contract is not None
            else None
        )
        return GateContext(
            app_state=self.state,
            connection_state=self._connection_state(),
            broker_account_type=self._account_type(),
            contract_qualified=contract is not None and contract.con_id > 0,
            contract_is_continuous=contract.is_continuous if contract is not None else True,
            account_available=self.account is not None,
            positions_reconciled=self.reconciliation.positions_reconciled,
            open_orders_reconciled=self.reconciliation.orders_reconciled,
            market_data_age_seconds=age,
            broker_permission_ready=self._broker_permission_ready(),
            strategy_enabled=self.strategy.enabled if self.strategy else False,
            risk_approved=risk_approved,
        )

    def _broker_permission_ready(self) -> bool:
        """Broker-reported permission.

        ``None`` from the adapter means "we could not determine this", which is
        treated as not permitted. The IBKR adapter always reports ``None`` today
        because the TWS API exposes no such flag -- see ``ibkr_broker.py``.
        """
        if self.account is None:
            return False
        return self.account.futures_permission is True

    def _connection_state(self) -> ConnectionState:
        if self.broker is None:
            return ConnectionState.DISCONNECTED
        return self.broker.get_connection_state()

    def _account_type(self) -> AccountType:
        if self.broker is None:
            return AccountType.UNKNOWN
        return self.broker.get_connection_info().account_type

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _compute_ready_state(self) -> ApplicationState:
        """Decide the post-reconciliation state.

        Order matters: the strongest reason not to trade wins, so an operator
        reading /status sees the real cause rather than a generic SAFE.
        """
        if not self.reconciliation.succeeded:
            return ApplicationState.SAFE
        if self.kill_switch.engaged():
            return ApplicationState.HALTED
        if self.config.trading_mode is TradingMode.DISABLED:
            return ApplicationState.SAFE
        if not self.config.allow_order_transmit:
            return ApplicationState.SAFE
        return ApplicationState.READY

    def _set_state(self, state: ApplicationState) -> None:
        if state is self.state:
            return
        previous = self.state
        self.state = state
        _LOG.info(
            "application state changed",
            extra={
                "event": "app.state_changed",
                "previous_state": previous.value,
                "state": state.value,
            },
        )
        with contextlib.suppress(Exception):
            self.repositories.state.set_state(STATE_KEY_LAST_STATE, state.value)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def health_payload(self) -> tuple[int, dict[str, object]]:
        result = evaluate_health(
            app_state=self.state,
            database_ok=self.database.healthy(),
            shutting_down=self._shutting_down,
        )
        return result.http_status, result.as_dict()

    def status_payload(self) -> dict[str, object]:
        market_data = self.market_data
        contract_key = self.contract.key() if self.contract else None
        broker_info = (
            self.broker.get_connection_info().describe() if self.broker is not None else None
        )
        return {
            "application": {
                "status": "running",
                "state": self.state.value,
                "run_id": self.run_id,
                "app_env": self.config.app_env,
                "last_error": self._last_error,
                **build_info(self.config),
            },
            "safety": {
                **safety_summary(self.config, kill_switch_engaged=self.kill_switch.engaged()),
                "kill_switch_detail": self.kill_switch.describe(),
                "transmit_gate": self.gate.evaluate(
                    self._build_gate_context(risk_approved=False)
                ).as_dict(),
            },
            "broker": {
                "implementation": self.broker.name if self.broker else "none",
                "connection_state": self._connection_state().value,
                "account_type": self._account_type().value,
                "account_id": mask_account_id(self.account.account_id if self.account else None),
                "last_heartbeat_age_seconds": (
                    self.broker.last_heartbeat_age_seconds() if self.broker else None
                ),
                "connection": broker_info,
            },
            "contract": self.contract.describe() if self.contract else None,
            "market_data": (market_data.status(contract_key).describe() if market_data else None),
            "portfolio": {
                "positions_count": len(self.position_book),
                "positions": self.position_book.describe(),
                "open_orders_count": len(self.repositories.orders.open_orders()),
            },
            "reconciliation": self.reconciliation.describe(),
            "risk": self.risk_manager.describe(),
            "strategy": self.strategy.describe() if self.strategy else None,
            "database": self.database.info(),
            "performance": self.repositories.performance.get().describe(),
            "generated_at": utc_now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _record_event(
        self,
        event: str,
        message: str,
        *,
        level: str = "INFO",
        data: dict[str, object] | None = None,
    ) -> None:
        with contextlib.suppress(Exception):
            self.repositories.events.record_bot_event(
                BotEvent(
                    run_id=self.run_id,
                    event=event,
                    level=level,
                    message=message,
                    data=data or {},
                )
            )

    def _record_broker_event(
        self, event: str, message: str, *, data: dict[str, object] | None = None
    ) -> None:
        with contextlib.suppress(Exception):
            self.repositories.events.record_broker_event(
                BrokerEvent(run_id=self.run_id, event=event, message=message, data=data or {})
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_application(config: Config) -> int:
    app = TradingApplication(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, app.request_stop)
    return await app.run()


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point. Returns a process exit code."""
    del argv  # configuration comes from the environment, never from flags

    try:
        config = Config.from_env()
    except ConfigError as exc:
        # No logging configured yet, and a configuration we cannot parse is
        # exactly the case where refusing to start is the safe behaviour.
        sys.stderr.write(f"FATAL: invalid configuration: {exc}\n")
        return 2

    run_id = new_run_id()
    configure_logging(config, run_id=run_id)

    if config.trading_mode is TradingMode.LIVE:
        _LOG.critical(
            "TRADING_MODE=live -- every live interlock must pass for any order to transmit",
            extra={"event": "app.live_mode_selected"},
        )

    try:
        return asyncio.run(run_application(config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
