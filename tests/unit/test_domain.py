"""Contracts, market data, signals, strategy, orders, positions, reconciliation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.broker.mock_broker import MockBroker
from app.broker.models import BrokerFill, BrokerOrderSnapshot, BrokerPosition
from app.contracts.models import ContractError, ContractSpec, QualifiedContract
from app.contracts.resolver import ContractResolutionError, ContractResolver
from app.contracts.solana import DEFAULT_PRODUCT, PRODUCTS, get_product
from app.enums import (
    Direction,
    OrderSide,
    OrderStatus,
    OrderType,
    SecurityType,
)
from app.execution.order_models import (
    BracketOrder,
    OrderValidationError,
    build_order,
)
from app.market_data.manager import MarketDataManager
from app.market_data.models import Quote
from app.portfolio.positions import Position, PositionBook
from app.portfolio.reconciliation import Reconciler
from app.signals.models import SignalError, TradeIntent
from app.signals.validator import SignalValidator
from app.strategy.noop import NoOpStrategy, build_strategy
from app.utilities.timeutils import ensure_utc, utc_now
from tests.conftest import CONTRACT_MONTH, sample_intent

# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------


class TestTimeHandling:
    def test_naive_datetimes_are_rejected(self) -> None:
        from datetime import datetime

        with pytest.raises(ValueError, match="naive datetime"):
            ensure_utc(datetime(2026, 1, 1, 12, 0, 0))

    def test_quotes_reject_naive_timestamps(self) -> None:
        from datetime import datetime

        with pytest.raises(ValueError, match="naive datetime"):
            Quote(
                contract_key="k",
                symbol="MSL",
                received_at=datetime(2026, 1, 1),
                source="test",
                last=Decimal("1"),
            )

    def test_intents_reject_naive_timestamps(self) -> None:
        from datetime import datetime

        with pytest.raises(ValueError, match="naive datetime"):
            sample_intent(created_at=datetime(2026, 1, 1))


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class TestContracts:
    def test_default_product_is_the_micro_contract(self) -> None:
        assert DEFAULT_PRODUCT.symbol == "MSL"
        assert set(PRODUCTS) == {"MSL", "SOL"}

    def test_unknown_symbol_raises_rather_than_substituting(self) -> None:
        with pytest.raises(KeyError, match="unsupported futures symbol"):
            get_product("ES")

    def test_qualified_contract_requires_a_positive_con_id(self) -> None:
        with pytest.raises(ContractError, match="positive conId"):
            QualifiedContract(
                con_id=0,
                symbol="MSL",
                local_symbol="MSLZ6",
                sec_type=SecurityType.FUTURE,
                exchange="CME",
                currency="USD",
                expiration=CONTRACT_MONTH,
                last_trade_date="20261218",
                multiplier="25",
                min_tick=Decimal("0.05"),
                trading_class="MSL",
            )

    def test_qualified_contract_refuses_continuous_security_type(self) -> None:
        with pytest.raises(ContractError, match="only FUT"):
            QualifiedContract(
                con_id=1,
                symbol="MSL",
                local_symbol="MSL",
                sec_type=SecurityType.CONTINUOUS_FUTURE,
                exchange="CME",
                currency="USD",
                expiration=CONTRACT_MONTH,
                last_trade_date="20261218",
                multiplier="25",
                min_tick=Decimal("0.05"),
                trading_class="MSL",
            )

    def test_undated_spec_is_not_orderable(self) -> None:
        with pytest.raises(ContractError, match="explicit expiration"):
            ContractSpec(symbol="MSL").require_orderable()

    def test_continuous_spec_is_not_orderable(self) -> None:
        spec = ContractSpec(
            symbol="MSL",
            sec_type=SecurityType.CONTINUOUS_FUTURE,
            last_trade_date_or_contract_month=CONTRACT_MONTH,
        )
        with pytest.raises(ContractError, match="analytics-only"):
            spec.require_orderable()

    def test_notional_uses_the_multiplier(self, contract: QualifiedContract) -> None:
        assert contract.notional(2, Decimal("150")) == Decimal("7500")  # 2 x 25 x 150

    async def test_resolver_requires_an_explicit_month(self) -> None:
        broker = MockBroker()
        await broker.connect()
        with pytest.raises(ContractResolutionError, match="explicit expiration"):
            await ContractResolver(broker).resolve("MSL", None)

    async def test_resolver_caches_and_returns_a_qualified_contract(self) -> None:
        broker = MockBroker()
        await broker.connect()
        resolver = ContractResolver(broker)
        first = await resolver.resolve("MSL", CONTRACT_MONTH)
        second = await resolver.resolve("MSL", CONTRACT_MONTH)
        assert first is second
        assert first.con_id > 0
        assert first.expiration == CONTRACT_MONTH

    async def test_resolver_reports_permission_denials_without_retrying(self) -> None:
        broker = MockBroker(permission_denied=True)
        await broker.connect()
        with pytest.raises(ContractResolutionError, match="permission"):
            await ContractResolver(broker).resolve("MSL", CONTRACT_MONTH)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


class TestMarketData:
    def test_freshness_requires_a_configured_limit(self) -> None:
        quote = _quote(age_seconds=0.0)
        assert quote.is_fresh(0) is False, "unconfigured limit must not mean fresh"
        assert quote.is_fresh(30) is True

    def test_stale_quotes_are_not_fresh(self) -> None:
        assert _quote(age_seconds=45).is_fresh(30) is False

    def test_future_quotes_are_not_fresh(self) -> None:
        assert _quote(age_seconds=-10).is_fresh(30) is False

    def test_mid_falls_back_to_last(self) -> None:
        quote = Quote(
            contract_key="k",
            symbol="MSL",
            received_at=utc_now(),
            source="t",
            last=Decimal("150"),
        )
        assert quote.mid == Decimal("150")

    async def test_manager_tracks_age_and_freshness(self, contract) -> None:
        broker = MockBroker()
        await broker.connect()
        manager = MarketDataManager(broker, max_age_seconds=30)
        await manager.subscribe(contract)

        assert manager.is_fresh(contract.key()) is True
        age = manager.age_seconds(contract.key())
        assert age is not None and age >= 0

    async def test_manager_reports_no_data_before_any_tick(self, contract) -> None:
        broker = MockBroker()
        await broker.connect()
        manager = MarketDataManager(broker, max_age_seconds=30)
        assert manager.age_seconds(contract.key()) is None
        assert manager.is_fresh(contract.key()) is False

    async def test_clear_drops_cached_quotes_on_disconnect(self, contract) -> None:
        """Keeping quotes across a disconnect would fake freshness."""
        broker = MockBroker()
        await broker.connect()
        manager = MarketDataManager(broker, max_age_seconds=30)
        await manager.subscribe(contract)
        assert manager.is_fresh(contract.key())

        manager.clear()
        assert manager.is_fresh(contract.key()) is False
        assert manager.age_seconds(contract.key()) is None

    async def test_permission_denial_is_recorded_once(self, contract) -> None:
        broker = MockBroker(permission_denied=True)
        await broker.connect()
        manager = MarketDataManager(broker, max_age_seconds=30)
        assert await manager.poll(contract) is None
        assert await manager.poll(contract) is None
        assert contract.key() in manager.permission_denials()

    async def test_zero_max_age_never_reports_fresh(self, contract) -> None:
        broker = MockBroker()
        await broker.connect()
        manager = MarketDataManager(broker, max_age_seconds=0)
        await manager.subscribe(contract)
        assert manager.is_fresh(contract.key()) is False


def _quote(*, age_seconds: float) -> Quote:
    return Quote(
        contract_key="k",
        symbol="MSL",
        received_at=utc_now() - timedelta(seconds=age_seconds),
        source="test",
        bid=Decimal("149"),
        ask=Decimal("151"),
    )


# ---------------------------------------------------------------------------
# Signals and strategy
# ---------------------------------------------------------------------------


class TestSignals:
    def test_direction_and_target_must_agree(self) -> None:
        with pytest.raises(SignalError, match="LONG intent"):
            sample_intent(direction=Direction.LONG, requested_position=-1)
        with pytest.raises(SignalError, match="SHORT intent"):
            sample_intent(direction=Direction.SHORT, requested_position=1)
        with pytest.raises(SignalError, match="FLAT intent"):
            sample_intent(direction=Direction.FLAT, requested_position=2)

    def test_confidence_bounds(self) -> None:
        with pytest.raises(SignalError, match="confidence"):
            sample_intent(confidence=1.5)
        assert sample_intent(confidence=0.5).confidence == 0.5

    def test_validator_accepts_a_good_intent(self) -> None:
        validator = _validator()
        assert validator.validate(sample_intent()).accepted

    def test_validator_rejects_a_duplicate_intent_id(self) -> None:
        validator = _validator()
        intent = sample_intent()
        assert validator.validate(intent).accepted
        result = validator.validate(intent)
        assert not result.accepted
        assert "DUPLICATE_SIGNAL" in result.reasons

    def test_validator_seeds_duplicates_from_durable_state(self) -> None:
        """A restart must not re-admit an intent that was already processed."""
        validator = _validator()
        validator.seed_seen(["int_test_0001"])
        result = validator.validate(sample_intent(intent_id="int_test_0001"))
        assert "DUPLICATE_SIGNAL" in result.reasons

    def test_validator_rejects_a_different_symbol(self) -> None:
        result = _validator().validate(
            sample_intent(symbol="SOL", direction=Direction.LONG, requested_position=1)
        )
        assert "SIGNAL_SYMBOL_NOT_CONFIGURED_INSTRUMENT" in result.reasons

    def test_validator_rejects_an_unsupported_symbol(self) -> None:
        # Bypass TradeIntent's own checks is unnecessary -- ES is structurally
        # valid as an intent, it is simply not a symbol this build supports.
        result = _validator().validate(sample_intent(symbol="ES"))
        assert "SIGNAL_SYMBOL_NOT_SUPPORTED" in result.reasons

    def test_validator_rejects_a_stale_intent(self) -> None:
        result = _validator().validate(sample_intent(created_at=utc_now() - timedelta(minutes=5)))
        assert "SIGNAL_TIMESTAMP_STALE" in result.reasons

    def test_validator_rejects_a_future_intent(self) -> None:
        result = _validator().validate(sample_intent(created_at=utc_now() + timedelta(minutes=5)))
        assert "SIGNAL_TIMESTAMP_IN_FUTURE" in result.reasons

    def test_validator_rejects_an_unknown_strategy(self) -> None:
        result = _validator().validate(sample_intent(strategy_name="martingale"))
        assert "SIGNAL_FROM_UNKNOWN_STRATEGY" in result.reasons

    def test_validator_rejects_an_absurd_size(self) -> None:
        result = _validator().validate(sample_intent(requested_position=99_999))
        assert "SIGNAL_REQUESTED_POSITION_UNREASONABLE" in result.reasons


def _validator() -> SignalValidator:
    return SignalValidator(configured_symbol="MSL", known_strategies=["noop"])


class TestStrategy:
    def test_noop_never_produces_an_intent(self) -> None:
        strategy = NoOpStrategy()
        for _ in range(200):
            assert strategy.handle_quote(_quote(age_seconds=0)) == ()
        assert strategy.quotes_seen == 200

    def test_disabled_strategy_produces_nothing(self) -> None:
        strategy = NoOpStrategy(enabled=False)
        assert strategy.handle_quote(_quote(age_seconds=0)) == ()
        assert strategy.enabled is False

    def test_registry_refuses_an_unknown_strategy(self) -> None:
        with pytest.raises(KeyError, match="unknown strategy"):
            build_strategy("moon-phase")

    def test_registry_builds_the_noop_strategy(self) -> None:
        assert isinstance(build_strategy("noop"), NoOpStrategy)


# ---------------------------------------------------------------------------
# Order models
# ---------------------------------------------------------------------------


class TestOrderModels:
    def test_market_order_rejects_a_limit_price(self, contract) -> None:
        with pytest.raises(OrderValidationError, match="must not carry"):
            build_order(
                contract=contract,
                side=OrderSide.BUY,
                quantity=1,
                order_type=OrderType.MARKET,
                idempotency_key="k",
                correlation_id="c",
                strategy_id="noop",
                run_id="r",
                trading_mode=_mode(),
                account_type=_account(),
                limit_price=Decimal("100"),
            )

    def test_limit_order_requires_a_limit_price(self, contract) -> None:
        with pytest.raises(OrderValidationError, match="limit order requires"):
            build_order(
                contract=contract,
                side=OrderSide.BUY,
                quantity=1,
                order_type=OrderType.LIMIT,
                idempotency_key="k",
                correlation_id="c",
                strategy_id="noop",
                run_id="r",
                trading_mode=_mode(),
                account_type=_account(),
            )

    def test_zero_quantity_is_refused(self, contract) -> None:
        with pytest.raises(OrderValidationError, match="quantity must be positive"):
            build_order(
                contract=contract,
                side=OrderSide.BUY,
                quantity=0,
                order_type=OrderType.MARKET,
                idempotency_key="k",
                correlation_id="c",
                strategy_id="noop",
                run_id="r",
                trading_mode=_mode(),
                account_type=_account(),
            )

    def test_stop_and_bracket_are_modelled_but_not_transmittable(self, contract) -> None:
        order = build_order(
            contract=contract,
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.STOP,
            idempotency_key="k",
            correlation_id="c",
            strategy_id="noop",
            run_id="r",
            trading_mode=_mode(),
            account_type=_account(),
            stop_price=Decimal("140"),
        )
        with pytest.raises(OrderValidationError, match="not transmitted in this phase"):
            order.require_transmittable_type()

    def test_bracket_children_must_oppose_the_parent(self, contract) -> None:
        parent = _simple(contract, OrderSide.BUY)
        same_side_child = _simple(contract, OrderSide.BUY, key="k2")
        with pytest.raises(OrderValidationError, match="opposite side"):
            BracketOrder(parent=parent, stop_loss=same_side_child).validate()

    def test_bracket_requires_a_protective_child(self, contract) -> None:
        with pytest.raises(OrderValidationError, match="at least one protective"):
            BracketOrder(parent=_simple(contract, OrderSide.BUY)).validate()

    def test_average_fill_price_is_volume_weighted(self, contract) -> None:
        order = _simple(contract, OrderSide.BUY, quantity=3)
        order.apply_fill(quantity=1, price=Decimal("100"))
        order.apply_fill(quantity=2, price=Decimal("130"))
        assert order.average_fill_price == Decimal("120")
        assert order.status is OrderStatus.FILLED

    def test_partial_fill_status(self, contract) -> None:
        order = _simple(contract, OrderSide.BUY, quantity=3)
        order.apply_fill(quantity=1, price=Decimal("100"))
        assert order.status is OrderStatus.PARTIALLY_FILLED

    def test_over_filling_is_refused(self, contract) -> None:
        order = _simple(contract, OrderSide.BUY, quantity=1)
        with pytest.raises(OrderValidationError, match="over-fill"):
            order.apply_fill(quantity=2, price=Decimal("100"))

    def test_blocked_orders_never_count_as_transmitted(self, contract) -> None:
        order = _simple(contract, OrderSide.BUY)
        order.mark_blocked(("KILL_SWITCH_ENGAGED",))
        assert order.was_transmitted is False

    def test_pending_submit_counts_as_transmitted(self, contract) -> None:
        """A crash after this point must not lead to a second send."""
        order = _simple(contract, OrderSide.BUY)
        order.mark_pending_submit()
        assert order.was_transmitted is True


def _simple(contract, side: OrderSide, *, quantity: int = 1, key: str = "k"):
    return build_order(
        contract=contract,
        side=side,
        quantity=quantity,
        order_type=OrderType.MARKET,
        idempotency_key=key,
        correlation_id="c",
        strategy_id="noop",
        run_id="r",
        trading_mode=_mode(),
        account_type=_account(),
    )


def _mode():
    from app.enums import TradingMode

    return TradingMode.MOCK


def _account():
    from app.enums import AccountType

    return AccountType.SIMULATED


# ---------------------------------------------------------------------------
# Positions and reconciliation
# ---------------------------------------------------------------------------


class TestPositions:
    def test_average_cost_recalculated_when_adding(self) -> None:
        book = PositionBook()
        book.apply_fill(
            con_id=1,
            symbol="MSL",
            local_symbol="MSLZ6",
            side=OrderSide.BUY,
            quantity=1,
            price=Decimal("100"),
        )
        book.apply_fill(
            con_id=1,
            symbol="MSL",
            local_symbol="MSLZ6",
            side=OrderSide.BUY,
            quantity=1,
            price=Decimal("200"),
        )
        position = book.get(1)
        assert position is not None
        assert position.quantity == 2
        assert position.average_cost == Decimal("150")

    def test_closing_removes_the_position(self) -> None:
        book = PositionBook()
        book.apply_fill(
            con_id=1,
            symbol="MSL",
            local_symbol="MSLZ6",
            side=OrderSide.BUY,
            quantity=1,
            price=Decimal("100"),
        )
        book.apply_fill(
            con_id=1,
            symbol="MSL",
            local_symbol="MSLZ6",
            side=OrderSide.SELL,
            quantity=1,
            price=Decimal("110"),
        )
        assert book.quantity(1) == 0
        assert len(book) == 0

    def test_short_positions_are_negative(self) -> None:
        book = PositionBook()
        book.apply_fill(
            con_id=1,
            symbol="MSL",
            local_symbol="MSLZ6",
            side=OrderSide.SELL,
            quantity=2,
            price=Decimal("100"),
        )
        assert book.quantity(1) == -2


class TestReconciliation:
    def test_matching_state_reconciles(self) -> None:
        book = PositionBook([_pos(1, 2)])
        result = Reconciler().reconcile(
            book=book,
            broker_positions=[_broker_pos(1, 2)],
            broker_open_orders=[],
            local_open_orders=[],
            broker_fills=[],
            account_available=True,
        )
        assert result.succeeded

    def test_a_position_difference_fails_reconciliation(self) -> None:
        result = Reconciler().reconcile(
            book=PositionBook([_pos(1, 2)]),
            broker_positions=[_broker_pos(1, 3)],
            broker_open_orders=[],
            local_open_orders=[],
            broker_fills=[],
            account_available=True,
        )
        assert not result.succeeded
        assert result.position_discrepancies[0].delta == 1

    def test_a_position_we_do_not_know_about_fails_reconciliation(self) -> None:
        result = Reconciler().reconcile(
            book=PositionBook(),
            broker_positions=[_broker_pos(1, 1)],
            broker_open_orders=[],
            local_open_orders=[],
            broker_fills=[],
            account_available=True,
        )
        assert not result.succeeded

    def test_an_order_working_at_the_broker_that_we_do_not_know_fails(self) -> None:
        result = Reconciler().reconcile(
            book=PositionBook(),
            broker_positions=[],
            broker_open_orders=[_broker_order("B-1")],
            local_open_orders=[],
            broker_fills=[],
            account_available=True,
        )
        assert not result.succeeded
        assert result.order_discrepancies[0].kind == "unknown_locally"

    def test_a_local_order_the_broker_lost_fails(self) -> None:
        result = Reconciler().reconcile(
            book=PositionBook(),
            broker_positions=[],
            broker_open_orders=[],
            local_open_orders=[("ord-1", "B-1", "MSL")],
            broker_fills=[],
            account_available=True,
        )
        assert not result.succeeded
        assert result.order_discrepancies[0].kind == "unknown_at_broker"

    def test_a_local_order_with_no_broker_id_fails(self) -> None:
        """An order stuck in PENDING_SUBMIT after a crash must stop trading."""
        result = Reconciler().reconcile(
            book=PositionBook(),
            broker_positions=[],
            broker_open_orders=[],
            local_open_orders=[("ord-1", None, "MSL")],
            broker_fills=[],
            account_available=True,
        )
        assert not result.succeeded

    def test_missing_account_fails_reconciliation(self) -> None:
        result = Reconciler().reconcile(
            book=PositionBook(),
            broker_positions=[],
            broker_open_orders=[],
            local_open_orders=[],
            broker_fills=[],
            account_available=False,
        )
        assert not result.succeeded

    def test_fills_are_counted(self) -> None:
        result = Reconciler().reconcile(
            book=PositionBook(),
            broker_positions=[],
            broker_open_orders=[],
            local_open_orders=[],
            broker_fills=[_fill()],
            account_available=True,
        )
        assert result.fills_seen == 1


def _pos(con_id: int, quantity: int) -> Position:
    return Position(
        con_id=con_id,
        symbol="MSL",
        local_symbol="MSLZ6",
        quantity=quantity,
        average_cost=Decimal("3750"),
        updated_at=utc_now(),
    )


def _broker_pos(con_id: int, quantity: int) -> BrokerPosition:
    return BrokerPosition(
        account_id="DU1",
        con_id=con_id,
        symbol="MSL",
        local_symbol="MSLZ6",
        sec_type="FUT",
        exchange="CME",
        currency="USD",
        quantity=quantity,
        average_cost=Decimal("3750"),
    )


def _broker_order(order_id: str) -> BrokerOrderSnapshot:
    return BrokerOrderSnapshot(
        broker_order_id=order_id,
        account_id="DU1",
        con_id=1,
        symbol="MSL",
        side=OrderSide.BUY,
        quantity=1,
        order_type=OrderType.LIMIT,
        status=OrderStatus.SUBMITTED,
    )


def _fill() -> BrokerFill:
    return BrokerFill(
        execution_id="e-1",
        broker_order_id="B-1",
        account_id="DU1",
        con_id=1,
        symbol="MSL",
        side=OrderSide.BUY,
        quantity=1,
        price=Decimal("150"),
        executed_at=utc_now(),
    )


def test_trade_intent_flat_is_not_actionable() -> None:
    intent = TradeIntent(
        strategy_name="noop",
        symbol="MSL",
        direction=Direction.FLAT,
        requested_position=0,
        created_at=utc_now(),
    )
    assert intent.is_actionable is False
