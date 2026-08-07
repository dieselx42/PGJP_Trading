"""Database, migrations, and repositories."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import AccountType, OrderSide, OrderStatus, OrderType, TradingMode
from app.execution.order_models import Fill, Order, build_order
from app.portfolio.positions import Position
from app.safety.killswitch import KILL_SWITCH_STATE_KEY, KillSwitch
from app.state.database import MIGRATIONS, Database, DatabaseError
from app.state.models import BotEvent, BrokerEvent, DailyPerformance, SignalRecord
from app.state.repositories import Repositories
from app.utilities.timeutils import utc_now


class TestDatabase:
    def test_migrations_are_applied_and_idempotent(self, tmp_path) -> None:
        db = Database(tmp_path / "a.db")
        assert db.migrate() == MIGRATIONS[-1].version
        assert db.migrate() == MIGRATIONS[-1].version, "re-running must be a no-op"
        db.close()

    def test_wal_is_enabled_for_file_databases(self, tmp_path) -> None:
        db = Database(tmp_path / "b.db")
        db.migrate()
        assert str(db.info()["journal_mode"]).lower() == "wal"
        db.close()

    def test_all_required_tables_exist(self, database: Database) -> None:
        tables = set(database.info()["tables"])  # type: ignore[arg-type]
        required = {
            "orders",
            "fills",
            "positions",
            "signals",
            "risk_decisions",
            "bot_events",
            "broker_events",
            "contract_metadata",
            "daily_performance",
            "application_state",
            "schema_migrations",
        }
        assert required <= tables

    def test_state_survives_reopening_the_file(self, tmp_path) -> None:
        path = tmp_path / "persist.db"
        first = Database(path)
        first.migrate()
        Repositories(first).state.set_state("hello", "world")
        first.close()

        second = Database(path)
        second.connect()
        assert Repositories(second).state.get_state("hello") == "world"
        second.close()

    def test_query_before_connect_raises(self, tmp_path) -> None:
        with pytest.raises(DatabaseError, match="not connected"):
            Database(tmp_path / "c.db").query_one("SELECT 1")

    def test_transaction_rolls_back_on_error(self, database: Database) -> None:
        with pytest.raises(RuntimeError), database.transaction() as connection:
            connection.execute(
                "INSERT INTO application_state (key, value, updated_at) VALUES (?,?,?)",
                ("k", "v", utc_now().isoformat()),
            )
            raise RuntimeError("boom")
        assert database.query_one("SELECT * FROM application_state WHERE key='k'") is None

    def test_info_on_an_unmigrated_database_reports_version_zero(self, tmp_path) -> None:
        """info() is a diagnostic; it must never raise, whatever state the file is in."""
        db = Database(tmp_path / "nested" / "deep" / "x.db")
        info = db.info()
        assert info["ok"] is True  # parent directories are created on demand
        assert info["schema_version"] == 0
        db.close()


class TestOrderRepository:
    def test_insert_then_read_round_trips_every_field(self, repositories, contract) -> None:
        order = _order(contract)
        order.limit_price = Decimal("151.25")
        order.order_type = OrderType.LIMIT
        _, created = repositories.orders.insert_if_absent(order)
        assert created

        loaded = repositories.orders.get(order.internal_order_id)
        assert loaded is not None
        assert loaded.idempotency_key == order.idempotency_key
        assert loaded.limit_price == Decimal("151.25")
        assert loaded.order_type is OrderType.LIMIT
        assert loaded.trading_mode is TradingMode.MOCK
        assert loaded.created_at.tzinfo is not None

    def test_duplicate_idempotency_key_returns_the_existing_row(
        self, repositories, contract
    ) -> None:
        first = _order(contract)
        repositories.orders.insert_if_absent(first)

        duplicate = _order(contract)  # different internal id, same idempotency key
        assert duplicate.internal_order_id != first.internal_order_id
        stored, created = repositories.orders.insert_if_absent(duplicate)

        assert created is False
        assert stored.internal_order_id == first.internal_order_id
        assert len(repositories.orders.recent()) == 1

    def test_open_orders_only_returns_working_orders(self, repositories, contract) -> None:
        working = _order(contract, key="idem_working")
        working.mark_pending_submit()
        repositories.orders.insert_if_absent(working)

        blocked = _order(contract, key="idem_blocked")
        blocked.mark_blocked(("KILL_SWITCH_ENGAGED",))
        repositories.orders.insert_if_absent(blocked)

        open_orders = repositories.orders.open_orders()
        assert [o.internal_order_id for o in open_orders] == [working.internal_order_id]

    def test_prices_survive_as_exact_decimals(self, repositories, contract) -> None:
        order = _order(contract)
        order.order_type = OrderType.LIMIT
        order.limit_price = Decimal("0.1")
        repositories.orders.insert_if_absent(order)
        order.apply_fill(quantity=1, price=Decimal("0.2"))
        repositories.orders.update(order)

        loaded = repositories.orders.get(order.internal_order_id)
        assert loaded is not None
        # 0.1 + 0.2 as binary floats would not compare equal to 0.3.
        assert loaded.limit_price + Decimal("0.2") == Decimal("0.3")

    def test_orders_last_hour_counts_only_transmitted(self, repositories, contract) -> None:
        never_sent = _order(contract, key="idem_a")
        repositories.orders.insert_if_absent(never_sent)
        assert repositories.orders.count_transmitted_last_hour() == 0

        sent = _order(contract, key="idem_b")
        sent.mark_pending_submit()
        repositories.orders.insert_if_absent(sent)
        assert repositories.orders.count_transmitted_last_hour() == 1


class TestOtherRepositories:
    def test_fills_are_idempotent_by_execution_id(self, repositories) -> None:
        fill = Fill(
            execution_id="exec-1",
            internal_order_id="ord-1",
            correlation_id="cor-1",
            run_id="run-1",
            con_id=1,
            symbol="MSL",
            side=OrderSide.BUY,
            quantity=1,
            price=Decimal("150"),
            executed_at=utc_now(),
        )
        assert repositories.fills.record(fill) is True
        assert repositories.fills.record(fill) is False
        assert repositories.fills.count() == 1

    def test_positions_replace_all_is_atomic(self, repositories) -> None:
        repositories.positions.upsert(_position(1, 5))
        repositories.positions.replace_all([_position(2, -3)])
        stored = repositories.positions.all()
        assert len(stored) == 1
        assert stored[0].con_id == 2
        assert stored[0].quantity == -3

    def test_signal_ids_are_recoverable_for_duplicate_detection(self, repositories) -> None:
        repositories.signals.record(
            SignalRecord(
                intent_id="int-1",
                correlation_id="cor-1",
                run_id="run-1",
                strategy_name="noop",
                symbol="MSL",
                direction="LONG",
                requested_position=1,
                created_at=utc_now(),
                accepted=True,
            )
        )
        assert "int-1" in repositories.signals.known_intent_ids()

    def test_risk_decisions_are_recorded(self, repositories) -> None:
        repositories.risk_decisions.record(
            correlation_id="cor-1",
            run_id="run-1",
            approved=False,
            reasons=["KILL_SWITCH_ENGAGED"],
        )
        recent = repositories.risk_decisions.recent()
        assert recent[0]["approved"] is False
        assert recent[0]["reasons"] == ["KILL_SWITCH_ENGAGED"]

    def test_events_round_trip(self, repositories) -> None:
        repositories.events.record_bot_event(
            BotEvent(run_id="run-1", event="app.starting", message="hello")
        )
        repositories.events.record_broker_event(
            BrokerEvent(run_id="run-1", event="broker.connected", message="ok")
        )
        assert repositories.events.recent_bot_events()[0]["event"] == "app.starting"

    def test_contract_metadata_round_trips(self, repositories, contract) -> None:
        repositories.contracts.upsert(contract)
        loaded = repositories.contracts.get(contract.con_id)
        assert loaded is not None
        assert loaded.local_symbol == contract.local_symbol
        assert loaded.min_tick == contract.min_tick
        assert loaded.multiplier == contract.multiplier

    def test_daily_performance_defaults_to_zero(self, repositories) -> None:
        performance = repositories.performance.get()
        assert performance.total_pnl == Decimal("0")

    def test_daily_performance_total_includes_commission(self) -> None:
        performance = DailyPerformance(
            trade_date="2026-01-01",
            realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal("-40"),
            commission=Decimal("10"),
        )
        assert performance.total_pnl == Decimal("50")


class TestKillSwitchPersistence:
    def test_latch_survives_a_reopen(self, tmp_path) -> None:
        path = tmp_path / "ks.db"
        first = Database(path)
        first.migrate()
        KillSwitch(config_engaged=False, store=Repositories(first).state).engage(
            "operator test", engaged_at=utc_now().isoformat()
        )
        first.close()

        second = Database(path)
        second.connect()
        switch = KillSwitch(config_engaged=False, store=Repositories(second).state)
        assert switch.engaged() is True
        assert switch.latched() is True
        second.close()

    def test_config_engaged_wins_without_a_store(self) -> None:
        assert KillSwitch(config_engaged=True).engaged() is True
        assert KillSwitch(config_engaged=False).engaged() is False

    def test_unreadable_store_fails_closed(self) -> None:
        class Broken:
            def get_state(self, key: str) -> str | None:
                raise RuntimeError("store is unavailable")

            def set_state(self, key: str, value: str) -> None:
                raise RuntimeError("store is unavailable")

        switch = KillSwitch(config_engaged=False, store=Broken())
        assert switch.engaged() is True, "an unreadable kill switch must read as engaged"

    def test_there_is_no_disengage_method(self) -> None:
        """Clearing the switch is deliberately a configuration change, not an API."""
        assert not hasattr(KillSwitch, "disengage")
        assert not hasattr(KillSwitch, "clear")

    def test_latch_key_is_stable(self, repositories) -> None:
        KillSwitch(config_engaged=False, store=repositories.state).engage(
            "x", engaged_at=utc_now().isoformat()
        )
        assert repositories.state.get_state(KILL_SWITCH_STATE_KEY) == "true"


def _order(contract, key: str = "idem_fixed") -> Order:
    return build_order(
        contract=contract,
        side=OrderSide.BUY,
        quantity=1,
        order_type=OrderType.MARKET,
        idempotency_key=key,
        correlation_id="cor-1",
        strategy_id="noop",
        run_id="run-1",
        trading_mode=TradingMode.MOCK,
        account_type=AccountType.SIMULATED,
    )


def _position(con_id: int, quantity: int) -> Position:
    return Position(
        con_id=con_id,
        symbol="MSL",
        local_symbol="MSLZ6",
        quantity=quantity,
        average_cost=Decimal("3750"),
        updated_at=utc_now(),
    )


def test_order_status_reached_broker_semantics() -> None:
    assert OrderStatus.NEW.reached_broker is False
    assert OrderStatus.BLOCKED.reached_broker is False
    for status in (
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.SUBMITTED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.ERROR,
    ):
        assert status.reached_broker is True, f"{status} must count as 'may exist at broker'"
