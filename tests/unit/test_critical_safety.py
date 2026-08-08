"""The ten critical safety tests from the project specification.

Each one starts from a fully-authorised baseline and breaks exactly one thing,
so a passing test proves that *that specific condition* blocks transmission --
not merely that something, somewhere, refused.

``test_00_all_green_baseline_allows_transmission`` is the control. If it ever
fails, every other test in this file becomes meaningless, because a gate that
refuses unconditionally would satisfy all of them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.mock_broker import MockBroker
from app.broker.models import BrokerOrderRejectedError, OrderRequest
from app.config import Config
from app.enums import (
    AccountType,
    ApplicationState,
    ConnectionState,
    OrderSide,
    OrderType,
    TradingMode,
)
from app.execution.order_manager import (
    OUTCOME_DUPLICATE,
    OUTCOME_GATE_REJECTED,
    OUTCOME_RISK_REJECTED,
    OUTCOME_TRANSMITTED,
    OrderManager,
)
from app.risk.manager import RiskManager
from app.safety.gate import (
    REASON_ACCOUNT_MISMATCH_EXPECTED_LIVE,
    REASON_ACCOUNT_MISMATCH_EXPECTED_PAPER,
    REASON_ACCOUNT_TYPE_UNKNOWN,
    REASON_KILL_SWITCH,
    REASON_MARKET_DATA_STALE,
    REASON_ORDERS_NOT_RECONCILED,
    REASON_POSITIONS_NOT_RECONCILED,
    REASON_TRANSMIT_NOT_ALLOWED,
    TransmitGate,
)
from app.state.database import Database
from app.state.repositories import Repositories
from tests.conftest import (
    all_green_gate_context,
    all_green_risk_context,
    default_env,
    permissive_env,
    sample_intent,
)

pytestmark = pytest.mark.safety


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


def test_00_all_green_baseline_allows_transmission() -> None:
    """The control. Without this the other ten tests prove nothing."""
    config = Config.from_env(permissive_env())
    decision = TransmitGate(config).evaluate(all_green_gate_context())
    assert decision.allowed, f"baseline must permit; refused for {decision.reasons}"
    assert decision.reasons == ()


def test_00_all_green_baseline_passes_risk(contract) -> None:
    config = Config.from_env(permissive_env())
    decision = RiskManager(config).evaluate(all_green_risk_context(contract))
    assert decision.approved, f"baseline must approve; refused for {decision.reasons}"


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 1 -- default configuration cannot transmit
# ---------------------------------------------------------------------------


def test_01_default_configuration_cannot_transmit() -> None:
    """With the shipped defaults, an order cannot be transmitted."""
    config = Config.from_env(default_env())
    decision = TransmitGate(config).evaluate(all_green_gate_context())
    assert not decision.allowed
    # The defaults fail for several independent reasons, which is the point:
    # fixing one of them is not enough to start trading.
    assert REASON_KILL_SWITCH in decision.reasons
    assert REASON_TRANSMIT_NOT_ALLOWED in decision.reasons


def test_01_default_configuration_with_no_env_at_all_cannot_transmit() -> None:
    """An entirely empty environment is also safe, not merely the documented one."""
    config = Config.from_env({})
    assert config.trading_mode is TradingMode.DISABLED
    assert config.kill_switch is True
    assert config.allow_order_transmit is False
    assert config.live_trading_enabled is False
    assert config.sol_futures_permission_ready is False
    assert not TransmitGate(config).evaluate(all_green_gate_context()).allowed


def test_01_default_risk_configuration_rejects_everything(contract) -> None:
    config = Config.from_env(default_env())
    decision = RiskManager(config).evaluate(all_green_risk_context(contract))
    assert not decision.approved


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 2 -- paper mode + live account
# ---------------------------------------------------------------------------


def test_02_paper_mode_connected_to_live_account_rejects_every_order() -> None:
    config = Config.from_env(permissive_env(TRADING_MODE="paper"))
    decision = TransmitGate(config).evaluate(
        all_green_gate_context(broker_account_type=AccountType.LIVE)
    )
    assert not decision.allowed
    assert REASON_ACCOUNT_MISMATCH_EXPECTED_PAPER in decision.reasons


def test_02_paper_mode_rejects_simulated_account_too() -> None:
    """Paper mode requires a *paper* account, not merely 'not live'."""
    config = Config.from_env(permissive_env(TRADING_MODE="paper"))
    decision = TransmitGate(config).evaluate(
        all_green_gate_context(broker_account_type=AccountType.SIMULATED)
    )
    assert not decision.allowed
    assert REASON_ACCOUNT_MISMATCH_EXPECTED_PAPER in decision.reasons


def test_02_paper_mode_risk_manager_also_rejects_live_account(contract) -> None:
    config = Config.from_env(permissive_env(TRADING_MODE="paper"))
    decision = RiskManager(config).evaluate(
        all_green_risk_context(contract, account_type=AccountType.LIVE)
    )
    assert not decision.approved


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 3 -- live mode + paper account
# ---------------------------------------------------------------------------


def test_03_live_mode_connected_to_paper_account_rejects_every_order() -> None:
    config = Config.from_env(permissive_env(TRADING_MODE="live", LIVE_TRADING_ENABLED="true"))
    decision = TransmitGate(config).evaluate(
        all_green_gate_context(broker_account_type=AccountType.PAPER)
    )
    assert not decision.allowed
    assert REASON_ACCOUNT_MISMATCH_EXPECTED_LIVE in decision.reasons


def test_03_live_mode_requires_live_trading_enabled() -> None:
    """A half-armed live configuration is refused at parse time, not at order time."""
    from app.config import ConfigError

    with pytest.raises(ConfigError, match="LIVE_TRADING_ENABLED"):
        Config.from_env(permissive_env(TRADING_MODE="live", LIVE_TRADING_ENABLED="false"))


def test_03_live_trading_enabled_without_live_mode_is_refused() -> None:
    """And the reverse: you cannot pre-arm the live flag while in another mode."""
    from app.config import ConfigError

    with pytest.raises(ConfigError, match="TRADING_MODE=live"):
        Config.from_env(permissive_env(TRADING_MODE="mock", LIVE_TRADING_ENABLED="true"))


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 4 -- kill switch
# ---------------------------------------------------------------------------


def test_04_kill_switch_rejects_every_order() -> None:
    config = Config.from_env(permissive_env(KILL_SWITCH="true"))
    decision = TransmitGate(config).evaluate(all_green_gate_context())
    assert not decision.allowed
    assert REASON_KILL_SWITCH in decision.reasons


def test_04_kill_switch_rejects_in_every_mode() -> None:
    for mode, extra in (
        ("mock", {}),
        ("paper", {}),
        ("live", {"LIVE_TRADING_ENABLED": "true"}),
    ):
        config = Config.from_env(permissive_env(TRADING_MODE=mode, KILL_SWITCH="true", **extra))
        account = {
            "mock": AccountType.SIMULATED,
            "paper": AccountType.PAPER,
            "live": AccountType.LIVE,
        }[mode]
        decision = TransmitGate(config).evaluate(
            all_green_gate_context(broker_account_type=account)
        )
        assert not decision.allowed, f"kill switch failed to block in {mode} mode"
        assert REASON_KILL_SWITCH in decision.reasons


def test_04_kill_switch_rejects_at_risk_layer_too(contract) -> None:
    config = Config.from_env(permissive_env())
    decision = RiskManager(config).evaluate(
        all_green_risk_context(contract, kill_switch_engaged=True)
    )
    assert not decision.approved


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 5 -- ALLOW_ORDER_TRANSMIT=false
# ---------------------------------------------------------------------------


def test_05_allow_order_transmit_false_rejects_every_order() -> None:
    config = Config.from_env(permissive_env(ALLOW_ORDER_TRANSMIT="false"))
    decision = TransmitGate(config).evaluate(all_green_gate_context())
    assert not decision.allowed
    assert REASON_TRANSMIT_NOT_ALLOWED in decision.reasons


def test_05_allow_order_transmit_false_rejects_at_risk_layer_too(contract) -> None:
    config = Config.from_env(permissive_env(ALLOW_ORDER_TRANSMIT="false"))
    decision = RiskManager(config).evaluate(all_green_risk_context(contract))
    assert not decision.approved


async def test_05_broker_adapter_refuses_untransmittable_request(contract) -> None:
    """The broker adapter is an independent second barrier."""
    broker = MockBroker()
    await broker.connect()
    request = OrderRequest(
        internal_order_id="ord_x",
        correlation_id="cor_x",
        contract=contract,
        side=OrderSide.BUY,
        quantity=1,
        order_type=OrderType.MARKET,
        transmit=False,
    )
    with pytest.raises(BrokerOrderRejectedError, match="transmit=False"):
        await broker.place_order(request)


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 6 -- unknown account type
# ---------------------------------------------------------------------------


def test_06_unknown_broker_account_type_rejects_every_order() -> None:
    config = Config.from_env(permissive_env())
    decision = TransmitGate(config).evaluate(
        all_green_gate_context(broker_account_type=AccountType.UNKNOWN)
    )
    assert not decision.allowed
    assert REASON_ACCOUNT_TYPE_UNKNOWN in decision.reasons


def test_06_unknown_account_type_rejects_at_risk_layer_too(contract) -> None:
    config = Config.from_env(permissive_env())
    decision = RiskManager(config).evaluate(
        all_green_risk_context(contract, account_type=AccountType.UNKNOWN)
    )
    assert not decision.approved


def test_06_disconnected_broker_reports_unknown_account_type() -> None:
    """A disconnected adapter must not keep claiming to know the account."""
    broker = MockBroker()
    assert broker.get_connection_info().account_type is AccountType.UNKNOWN


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 7 -- failed reconciliation
# ---------------------------------------------------------------------------


def test_07_failed_position_reconciliation_rejects_every_order() -> None:
    config = Config.from_env(permissive_env())
    decision = TransmitGate(config).evaluate(all_green_gate_context(positions_reconciled=False))
    assert not decision.allowed
    assert REASON_POSITIONS_NOT_RECONCILED in decision.reasons


def test_07_failed_order_reconciliation_rejects_every_order() -> None:
    config = Config.from_env(permissive_env())
    decision = TransmitGate(config).evaluate(all_green_gate_context(open_orders_reconciled=False))
    assert not decision.allowed
    assert REASON_ORDERS_NOT_RECONCILED in decision.reasons


def test_07_reconciliation_failure_rejects_at_risk_layer_too(contract) -> None:
    config = Config.from_env(permissive_env())
    for field in ("positions_reconciled", "open_orders_reconciled"):
        decision = RiskManager(config).evaluate(all_green_risk_context(contract, **{field: False}))
        assert not decision.approved, f"{field}=False must reject"


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 8 -- stale market data
# ---------------------------------------------------------------------------


def test_08_stale_market_data_rejects_every_order() -> None:
    config = Config.from_env(permissive_env(MARKET_DATA_MAX_AGE_SECONDS="30"))
    decision = TransmitGate(config).evaluate(all_green_gate_context(market_data_age_seconds=31.0))
    assert not decision.allowed
    assert REASON_MARKET_DATA_STALE in decision.reasons


def test_08_missing_market_data_rejects_every_order() -> None:
    config = Config.from_env(permissive_env())
    decision = TransmitGate(config).evaluate(all_green_gate_context(market_data_age_seconds=None))
    assert not decision.allowed


def test_08_market_data_stamped_in_the_future_rejects_every_order() -> None:
    """A negative age means a clock problem; trading through it is not acceptable."""
    config = Config.from_env(permissive_env())
    decision = TransmitGate(config).evaluate(all_green_gate_context(market_data_age_seconds=-5.0))
    assert not decision.allowed


def test_08_boundary_exactly_at_limit_is_still_fresh() -> None:
    config = Config.from_env(permissive_env(MARKET_DATA_MAX_AGE_SECONDS="30"))
    assert (
        TransmitGate(config).evaluate(all_green_gate_context(market_data_age_seconds=30.0)).allowed
    )
    assert (
        not TransmitGate(config)
        .evaluate(all_green_gate_context(market_data_age_seconds=30.001))
        .allowed
    )


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 9 -- a restart must not duplicate an order
# ---------------------------------------------------------------------------


async def _submit_once(
    config: Config, repositories: Repositories, broker: MockBroker, contract, intent
):
    manager = OrderManager(
        config=config,
        broker=broker,
        repositories=repositories,
        risk_manager=RiskManager(config),
        gate=TransmitGate(config),
        run_id="run_test",
    )
    return await manager.submit_intent(
        intent,
        contract=contract,
        risk_context=all_green_risk_context(contract),
        gate_context=all_green_gate_context(),
    )


async def test_09_restart_does_not_duplicate_an_existing_order(tmp_path, contract) -> None:
    """Simulates a full process restart against the same durable database."""
    config = Config.from_env(permissive_env(DATABASE_PATH=str(tmp_path / "trading.db")))
    intent = sample_intent()

    # -- first "process" --------------------------------------------------
    db1 = Database(tmp_path / "trading.db")
    db1.connect()
    db1.migrate()
    repos1 = Repositories(db1)
    broker1 = MockBroker()
    await broker1.connect()
    first = await _submit_once(config, repos1, broker1, contract, intent)
    assert first.outcome == OUTCOME_TRANSMITTED
    first_order_id = first.order.internal_order_id
    db1.close()

    # -- restart: brand new database handle, brand new broker -------------
    db2 = Database(tmp_path / "trading.db")
    db2.connect()
    db2.migrate()
    repos2 = Repositories(db2)
    broker2 = MockBroker()
    await broker2.connect()
    second = await _submit_once(config, repos2, broker2, contract, intent)

    assert second.outcome == OUTCOME_DUPLICATE
    assert second.order.internal_order_id == first_order_id
    assert len(repos2.orders.recent(limit=100)) == 1, "a restart created a second order"
    db2.close()


async def test_09_repeated_signal_in_one_process_does_not_duplicate(repositories, contract) -> None:
    config = Config.from_env(permissive_env())
    broker = MockBroker()
    await broker.connect()
    intent = sample_intent()

    first = await _submit_once(config, repositories, broker, contract, intent)
    second = await _submit_once(config, repositories, broker, contract, intent)

    assert first.outcome == OUTCOME_TRANSMITTED
    assert second.outcome == OUTCOME_DUPLICATE
    assert len(repositories.orders.recent(limit=100)) == 1


def test_09_idempotency_key_is_deterministic(contract) -> None:
    intent = sample_intent()
    assert intent.idempotency_key(con_id=contract.con_id) == intent.idempotency_key(
        con_id=contract.con_id
    )
    # ...and genuinely distinguishes different economic content.
    other = sample_intent(intent_id="int_test_0002")
    assert other.idempotency_key(con_id=contract.con_id) != intent.idempotency_key(
        con_id=contract.con_id
    )
    assert intent.idempotency_key(con_id=1) != intent.idempotency_key(con_id=2)


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TEST 10 -- zero limits disable, never mean unlimited
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "expected_reason"),
    [
        ("MAX_ORDER_SIZE", "MAX_ORDER_SIZE_NOT_CONFIGURED"),
        ("MAX_POSITION_CONTRACTS", "MAX_POSITION_CONTRACTS_NOT_CONFIGURED"),
        ("MAX_DAILY_LOSS_USD", "MAX_DAILY_LOSS_USD_NOT_CONFIGURED"),
        ("MAX_ORDERS_PER_HOUR", "MAX_ORDERS_PER_HOUR_NOT_CONFIGURED"),
        ("MAX_OPEN_ORDERS", "MAX_OPEN_ORDERS_NOT_CONFIGURED"),
        ("MAX_NOTIONAL_EXPOSURE_USD", "MAX_NOTIONAL_EXPOSURE_USD_NOT_CONFIGURED"),
    ],
)
def test_10_zero_limit_disables_rather_than_unlimits(
    variable: str, expected_reason: str, contract
) -> None:
    """Zeroing any single limit must reject with that limit's own reason."""
    config = Config.from_env(permissive_env(**{variable: "0"}))
    decision = RiskManager(config).evaluate(all_green_risk_context(contract))
    assert not decision.approved, f"{variable}=0 was treated as unlimited"
    assert expected_reason in decision.reasons


def test_10_zero_market_data_age_disables_trading() -> None:
    config = Config.from_env(permissive_env(MARKET_DATA_MAX_AGE_SECONDS="0"))
    decision = TransmitGate(config).evaluate(all_green_gate_context())
    assert not decision.allowed
    assert "MARKET_DATA_MAX_AGE_NOT_CONFIGURED" in decision.reasons


def test_10_a_huge_order_under_zero_limits_is_still_refused(contract) -> None:
    """The classic failure mode: 0 read as 'no ceiling' would let this through."""
    config = Config.from_env(default_env())
    decision = RiskManager(config).evaluate(all_green_risk_context(contract, order_quantity=10_000))
    assert not decision.approved


# ---------------------------------------------------------------------------
# End-to-end: the deployed configuration cannot transmit through the real path
# ---------------------------------------------------------------------------


async def test_deployed_configuration_blocks_the_full_order_pipeline(
    repositories, contract
) -> None:
    """The exact configuration this project deploys, driven through OrderManager."""
    config = Config.from_env(default_env())
    broker = MockBroker()
    await broker.connect()

    manager = OrderManager(
        config=config,
        broker=broker,
        repositories=repositories,
        risk_manager=RiskManager(config),
        gate=TransmitGate(config),
        run_id="run_deployed",
    )
    outcome = await manager.submit_intent(
        sample_intent(),
        contract=contract,
        risk_context=all_green_risk_context(contract),
        gate_context=all_green_gate_context(),
    )

    assert outcome.outcome == OUTCOME_RISK_REJECTED
    assert not outcome.transmitted
    assert outcome.order.status.value == "BLOCKED"
    assert outcome.order.broker_order_id is None
    assert outcome.order.transmitted_at is None
    # Nothing reached the simulated broker.
    assert len(await broker.get_open_orders()) == 0
    assert len(await broker.get_fills()) == 0


async def test_gate_rejection_is_recorded_but_never_transmitted(repositories, contract) -> None:
    """Risk passes, the gate refuses: the order must be persisted and blocked."""
    config = Config.from_env(permissive_env())
    broker = MockBroker()
    await broker.connect()
    manager = OrderManager(
        config=config,
        broker=broker,
        repositories=repositories,
        risk_manager=RiskManager(config),
        gate=TransmitGate(config),
        run_id="run_gate",
    )
    outcome = await manager.submit_intent(
        sample_intent(),
        contract=contract,
        risk_context=all_green_risk_context(contract),
        gate_context=all_green_gate_context(app_state=ApplicationState.SAFE),
    )
    assert outcome.outcome == OUTCOME_GATE_REJECTED
    assert "APPLICATION_NOT_READY" in outcome.order.block_reasons
    assert outcome.order.broker_order_id is None
    assert len(await broker.get_open_orders()) == 0


def test_disconnected_broker_rejects_every_order() -> None:
    config = Config.from_env(permissive_env())
    decision = TransmitGate(config).evaluate(
        all_green_gate_context(connection_state=ConnectionState.DISCONNECTED)
    )
    assert not decision.allowed


def test_continuous_future_can_never_carry_an_order() -> None:
    config = Config.from_env(permissive_env())
    decision = TransmitGate(config).evaluate(all_green_gate_context(contract_is_continuous=True))
    assert not decision.allowed
    assert "CONTRACT_IS_CONTINUOUS_FUTURE" in decision.reasons


def test_paper_and_live_modes_require_permission_flag() -> None:
    config = Config.from_env(
        permissive_env(TRADING_MODE="paper", SOL_FUTURES_PERMISSION_READY="false")
    )
    decision = TransmitGate(config).evaluate(
        all_green_gate_context(broker_account_type=AccountType.PAPER)
    )
    assert not decision.allowed
    assert "SOL_FUTURES_PERMISSION_NOT_CONFIGURED_READY" in decision.reasons


def test_paper_mode_never_dials_the_live_port() -> None:
    config = Config.from_env(permissive_env(TRADING_MODE="paper"))
    assert config.ib_port == config.ibkr.paper_port
    assert config.ib_port != config.ibkr.live_port


def test_disabled_and_mock_modes_expose_no_ib_port() -> None:
    for mode in ("disabled", "mock"):
        config = Config.from_env(permissive_env(TRADING_MODE=mode))
        assert config.ib_port is None


def test_notional_limit_blocks_an_oversized_position(contract) -> None:
    """MSL multiplier 25 x 4 contracts x $150 = $15,000 notional."""
    config = Config.from_env(permissive_env(MAX_NOTIONAL_EXPOSURE_USD="10000", MAX_ORDER_SIZE="10"))
    decision = RiskManager(config).evaluate(
        all_green_risk_context(contract, order_quantity=4, reference_price=Decimal("150.00"))
    )
    assert not decision.approved
    assert "MAX_NOTIONAL_EXPOSURE_USD_EXCEEDED" in decision.reasons


@pytest.mark.safety
def test_11_delayed_market_data_is_refused_however_fresh_it_looks() -> None:
    """A delayed tick is not a price an automated system may act on.

    IBKR serves delayed prices in the *same* tick fields as real-time ones
    (types 66/67/68 versus 1/2/4), so an unsubscribed or lapsed account produces
    data that is indistinguishable downstream and looks perfectly fresh -- the
    tick genuinely did arrive a second ago; the quote in it is fifteen minutes
    old. Observed against a real gateway: code 354 on MSLQ6, "Delayed market
    data is available".

    There is deliberately no configuration that permits this.
    """
    config = Config.from_env(permissive_env(MARKET_DATA_MAX_AGE_SECONDS="30"))
    context = all_green_gate_context(market_data_is_delayed=True, market_data_age_seconds=0.5)

    decision = TransmitGate(config).evaluate(context)

    assert decision.allowed is False
    assert "MARKET_DATA_IS_DELAYED" in decision.reasons


@pytest.mark.safety
def test_11_a_caller_that_does_not_know_is_treated_as_delayed() -> None:
    """The default is the refusing one, as with every other field on GateContext."""
    from app.safety.gate import GateContext

    assert GateContext().market_data_is_delayed is True
