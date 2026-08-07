"""Repositories.

One class per table family. All SQL lives here so the rest of the application
has no opinion about the storage engine.

The most important method in this module is
:meth:`OrderRepository.insert_if_absent`. It is the durable half of the
idempotency guarantee: the unique index on ``idempotency_key`` makes a
duplicate insert impossible, and this method turns that constraint violation
into "here is the order you already created" rather than an error.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from app.contracts.models import QualifiedContract
from app.enums import (
    AccountType,
    OrderSide,
    OrderStatus,
    OrderType,
    SecurityType,
    TimeInForce,
    TradingMode,
)
from app.execution.order_models import Fill, Order
from app.logging_config import get_logger
from app.portfolio.positions import Position
from app.state.database import Database, DatabaseError
from app.state.models import BotEvent, BrokerEvent, DailyPerformance, SignalRecord
from app.utilities.timeutils import from_iso, hours_ago, to_iso, utc_date_string, utc_now

_LOG = get_logger("state.repositories")


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _decimal_or_zero(value: object) -> Decimal:
    return _decimal(value) or Decimal("0")


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class OrderRepository:
    """Durable order state, including the idempotency guard."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def insert_if_absent(self, order: Order) -> tuple[Order, bool]:
        """Persist ``order`` unless its idempotency key already exists.

        Returns ``(order, created)``. When ``created`` is ``False`` the returned
        order is the one already in the database -- including whatever status
        and broker order id it has accumulated -- so a caller that restarted
        mid-flight sees the real state rather than a fresh copy.

        This relies on the UNIQUE constraint rather than a read-then-write
        check, so two concurrent attempts cannot both succeed.
        """
        existing = self.get_by_idempotency_key(order.idempotency_key)
        if existing is not None:
            return existing, False
        try:
            self._insert(order)
        except DatabaseError as exc:
            if "UNIQUE" not in str(exc).upper():
                raise
            # Lost the race; the winner's row is authoritative.
            existing = self.get_by_idempotency_key(order.idempotency_key)
            if existing is None:  # pragma: no cover - would mean the constraint lied
                raise
            return existing, False
        return order, True

    def _insert(self, order: Order) -> None:
        self._db.execute(
            """
            INSERT INTO orders (
                internal_order_id, idempotency_key, correlation_id, intent_id, strategy_id,
                run_id, created_at, updated_at, transmitted_at, symbol, con_id, local_symbol,
                exchange, currency, side, quantity, order_type, time_in_force, limit_price,
                stop_price, status, broker_order_id, filled_quantity, average_fill_price,
                commission, commission_currency, error_code, error_message, block_reasons,
                trading_mode, account_type
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order.internal_order_id,
                order.idempotency_key,
                order.correlation_id,
                order.intent_id,
                order.strategy_id,
                order.run_id,
                to_iso(order.created_at),
                to_iso(order.updated_at),
                to_iso(order.transmitted_at) if order.transmitted_at else None,
                order.symbol,
                order.con_id,
                order.local_symbol,
                order.exchange,
                order.currency,
                order.side.value,
                order.quantity,
                order.order_type.value,
                order.time_in_force.value,
                _text(order.limit_price),
                _text(order.stop_price),
                order.status.value,
                order.broker_order_id,
                order.filled_quantity,
                _text(order.average_fill_price),
                _text(order.commission),
                order.commission_currency,
                order.error_code,
                order.error_message,
                order.block_reasons_json(),
                order.trading_mode.value,
                order.account_type.value,
            ),
        )

    def update(self, order: Order) -> None:
        self._db.execute(
            """
            UPDATE orders SET
                updated_at = ?, transmitted_at = ?, status = ?, broker_order_id = ?,
                filled_quantity = ?, average_fill_price = ?, commission = ?,
                commission_currency = ?, error_code = ?, error_message = ?,
                block_reasons = ?, account_type = ?
            WHERE internal_order_id = ?
            """,
            (
                to_iso(order.updated_at),
                to_iso(order.transmitted_at) if order.transmitted_at else None,
                order.status.value,
                order.broker_order_id,
                order.filled_quantity,
                _text(order.average_fill_price),
                _text(order.commission),
                order.commission_currency,
                order.error_code,
                order.error_message,
                order.block_reasons_json(),
                order.account_type.value,
                order.internal_order_id,
            ),
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, internal_order_id: str) -> Order | None:
        row = self._db.query_one(
            "SELECT * FROM orders WHERE internal_order_id = ?", (internal_order_id,)
        )
        return None if row is None else _row_to_order(row)

    def get_by_idempotency_key(self, key: str) -> Order | None:
        row = self._db.query_one("SELECT * FROM orders WHERE idempotency_key = ?", (key,))
        return None if row is None else _row_to_order(row)

    def get_by_broker_order_id(self, broker_order_id: str) -> Order | None:
        row = self._db.query_one(
            "SELECT * FROM orders WHERE broker_order_id = ?", (broker_order_id,)
        )
        return None if row is None else _row_to_order(row)

    def open_orders(self) -> Sequence[Order]:
        placeholders = ",".join("?" for _ in _OPEN_STATUS_VALUES)
        rows = self._db.query_all(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY created_at",  # noqa: S608
            _OPEN_STATUS_VALUES,
        )
        return tuple(_row_to_order(row) for row in rows)

    def recent(self, limit: int = 50) -> Sequence[Order]:
        rows = self._db.query_all("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,))
        return tuple(_row_to_order(row) for row in rows)

    def count_since(self, since: datetime) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM orders WHERE transmitted_at IS NOT NULL "
            "AND transmitted_at >= ?",
            (to_iso(since),),
        )
        return int(row["n"]) if row else 0

    def count_transmitted_last_hour(self, *, now: datetime | None = None) -> int:
        return self.count_since(hours_ago(1, now=now))

    def open_order_tuples(self) -> Sequence[tuple[str, str | None, str]]:
        """``(internal_order_id, broker_order_id, symbol)`` for reconciliation."""
        return tuple(
            (order.internal_order_id, order.broker_order_id, order.symbol)
            for order in self.open_orders()
        )


_OPEN_STATUS_VALUES: tuple[str, ...] = (
    OrderStatus.PENDING_SUBMIT.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.ACKNOWLEDGED.value,
    OrderStatus.PARTIALLY_FILLED.value,
    OrderStatus.CANCEL_PENDING.value,
)


def _row_to_order(row: sqlite3.Row) -> Order:
    order = Order(
        idempotency_key=str(row["idempotency_key"]),
        correlation_id=str(row["correlation_id"]),
        strategy_id=str(row["strategy_id"]),
        run_id=str(row["run_id"]),
        internal_order_id=str(row["internal_order_id"]),
        intent_id=row["intent_id"],
        symbol=str(row["symbol"]),
        con_id=int(row["con_id"]),
        local_symbol=str(row["local_symbol"]),
        exchange=str(row["exchange"]),
        currency=str(row["currency"]),
        side=OrderSide(row["side"]),
        quantity=int(row["quantity"]),
        order_type=OrderType(row["order_type"]),
        time_in_force=TimeInForce(row["time_in_force"]),
        limit_price=_decimal(row["limit_price"]),
        stop_price=_decimal(row["stop_price"]),
        status=OrderStatus(row["status"]),
        broker_order_id=row["broker_order_id"],
        filled_quantity=int(row["filled_quantity"]),
        average_fill_price=_decimal(row["average_fill_price"]),
        commission=_decimal(row["commission"]),
        commission_currency=row["commission_currency"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        block_reasons=tuple(json.loads(row["block_reasons"] or "[]")),
        trading_mode=TradingMode(row["trading_mode"]),
        account_type=AccountType(row["account_type"]),
        created_at=from_iso(str(row["created_at"])),
        updated_at=from_iso(str(row["updated_at"])),
        transmitted_at=from_iso(str(row["transmitted_at"])) if row["transmitted_at"] else None,
    )
    return order


class FillRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def record(self, fill: Fill) -> bool:
        """Insert a fill. Returns ``False`` if this execution was already known.

        Execution ids are unique at the broker, so this makes fill ingestion
        idempotent across reconnects and reconciliation passes.
        """
        existing = self._db.query_one(
            "SELECT execution_id FROM fills WHERE execution_id = ?", (fill.execution_id,)
        )
        if existing is not None:
            return False
        self._db.execute(
            """
            INSERT INTO fills (
                execution_id, internal_order_id, broker_order_id, correlation_id, run_id,
                con_id, symbol, side, quantity, price, commission, commission_currency,
                executed_at, recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                fill.execution_id,
                fill.internal_order_id,
                fill.broker_order_id,
                fill.correlation_id,
                fill.run_id,
                fill.con_id,
                fill.symbol,
                fill.side.value,
                fill.quantity,
                str(fill.price),
                _text(fill.commission),
                fill.commission_currency,
                to_iso(fill.executed_at),
                to_iso(utc_now()),
            ),
        )
        return True

    def recent(self, limit: int = 50) -> Sequence[Fill]:
        rows = self._db.query_all("SELECT * FROM fills ORDER BY executed_at DESC LIMIT ?", (limit,))
        return tuple(
            Fill(
                execution_id=str(row["execution_id"]),
                internal_order_id=str(row["internal_order_id"] or ""),
                correlation_id=str(row["correlation_id"]),
                run_id=str(row["run_id"]),
                con_id=int(row["con_id"]),
                symbol=str(row["symbol"]),
                side=OrderSide(row["side"]),
                quantity=int(row["quantity"]),
                price=_decimal_or_zero(row["price"]),
                executed_at=from_iso(str(row["executed_at"])),
                commission=_decimal(row["commission"]),
                commission_currency=row["commission_currency"],
                broker_order_id=row["broker_order_id"],
            )
            for row in rows
        )

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM fills")
        return int(row["n"]) if row else 0


class PositionRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert(self, position: Position) -> None:
        self._db.execute(
            """
            INSERT INTO positions (
                con_id, account_id, symbol, local_symbol, quantity, average_cost,
                source, updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(con_id) DO UPDATE SET
                account_id = excluded.account_id,
                symbol = excluded.symbol,
                local_symbol = excluded.local_symbol,
                quantity = excluded.quantity,
                average_cost = excluded.average_cost,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                position.con_id,
                position.account_id,
                position.symbol,
                position.local_symbol,
                position.quantity,
                str(position.average_cost),
                position.source,
                to_iso(position.updated_at),
            ),
        )

    def delete(self, con_id: int) -> None:
        self._db.execute("DELETE FROM positions WHERE con_id = ?", (con_id,))

    def replace_all(self, positions: Sequence[Position]) -> None:
        """Adopt a new set of positions atomically, after a successful reconcile."""
        with self._db.transaction() as connection:
            connection.execute("DELETE FROM positions")
            for position in positions:
                connection.execute(
                    """
                    INSERT INTO positions (
                        con_id, account_id, symbol, local_symbol, quantity, average_cost,
                        source, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        position.con_id,
                        position.account_id,
                        position.symbol,
                        position.local_symbol,
                        position.quantity,
                        str(position.average_cost),
                        position.source,
                        to_iso(position.updated_at),
                    ),
                )

    def all(self) -> Sequence[Position]:
        rows = self._db.query_all("SELECT * FROM positions ORDER BY symbol")
        return tuple(
            Position(
                con_id=int(row["con_id"]),
                symbol=str(row["symbol"]),
                local_symbol=str(row["local_symbol"]),
                quantity=int(row["quantity"]),
                average_cost=_decimal_or_zero(row["average_cost"]),
                updated_at=from_iso(str(row["updated_at"])),
                account_id=str(row["account_id"]),
                source=str(row["source"]),
            )
            for row in rows
        )


class SignalRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def record(self, signal: SignalRecord) -> None:
        self._db.execute(
            """
            INSERT INTO signals (
                intent_id, correlation_id, run_id, strategy_name, symbol, direction,
                requested_position, confidence, contract_key, metadata, accepted,
                rejection_reasons, created_at, recorded_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(intent_id) DO NOTHING
            """,
            (
                signal.intent_id,
                signal.correlation_id,
                signal.run_id,
                signal.strategy_name,
                signal.symbol,
                signal.direction,
                signal.requested_position,
                None if signal.confidence is None else str(signal.confidence),
                signal.contract_key,
                signal.metadata_json(),
                1 if signal.accepted else 0,
                signal.reasons_json(),
                to_iso(signal.created_at),
                to_iso(utc_now()),
            ),
        )

    def known_intent_ids(self, limit: int = 10_000) -> Sequence[str]:
        """Intent ids already processed, used to seed duplicate detection."""
        rows = self._db.query_all(
            "SELECT intent_id FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return tuple(str(row["intent_id"]) for row in rows)

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM signals")
        return int(row["n"]) if row else 0


class RiskDecisionRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def record(
        self,
        *,
        correlation_id: str,
        run_id: str,
        approved: bool,
        reasons: Sequence[str],
        detail: dict[str, object] | None = None,
        intent_id: str | None = None,
        decided_at: datetime | None = None,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO risk_decisions (
                correlation_id, intent_id, run_id, approved, reasons, detail, decided_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                correlation_id,
                intent_id,
                run_id,
                1 if approved else 0,
                json.dumps(list(reasons)),
                json.dumps(detail or {}, default=str, sort_keys=True),
                to_iso(decided_at or utc_now()),
            ),
        )

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM risk_decisions")
        return int(row["n"]) if row else 0

    def recent(self, limit: int = 20) -> Sequence[dict[str, object]]:
        rows = self._db.query_all(
            "SELECT * FROM risk_decisions ORDER BY decided_at DESC LIMIT ?", (limit,)
        )
        return tuple(
            {
                "decided_at": str(row["decided_at"]),
                "correlation_id": str(row["correlation_id"]),
                "approved": bool(row["approved"]),
                "reasons": json.loads(row["reasons"] or "[]"),
            }
            for row in rows
        )


class EventRepository:
    """Bot and broker events."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def record_bot_event(self, event: BotEvent) -> None:
        self._db.execute(
            """
            INSERT INTO bot_events (run_id, correlation_id, level, event, message, data,
                                    occurred_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                event.run_id,
                event.correlation_id,
                event.level,
                event.event,
                event.message,
                event.data_json(),
                to_iso(event.occurred_at),
            ),
        )

    def record_broker_event(self, event: BrokerEvent) -> None:
        self._db.execute(
            """
            INSERT INTO broker_events (run_id, correlation_id, event, code, message, data,
                                       occurred_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                event.run_id,
                event.correlation_id,
                event.event,
                event.code,
                event.message,
                event.data_json(),
                to_iso(event.occurred_at),
            ),
        )

    def recent_bot_events(self, limit: int = 50) -> Sequence[dict[str, object]]:
        rows = self._db.query_all(
            "SELECT * FROM bot_events ORDER BY occurred_at DESC LIMIT ?", (limit,)
        )
        return tuple(
            {
                "occurred_at": str(row["occurred_at"]),
                "level": str(row["level"]),
                "event": str(row["event"]),
                "message": str(row["message"]),
            }
            for row in rows
        )


class ContractRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def upsert(self, contract: QualifiedContract) -> None:
        self._db.execute(
            """
            INSERT INTO contract_metadata (
                con_id, symbol, local_symbol, sec_type, exchange, primary_exchange, currency,
                expiration, last_trade_date, multiplier, min_tick, trading_class, trading_hours,
                liquid_hours, time_zone_id, long_name, raw, qualified_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(con_id) DO UPDATE SET
                local_symbol = excluded.local_symbol,
                expiration = excluded.expiration,
                last_trade_date = excluded.last_trade_date,
                multiplier = excluded.multiplier,
                min_tick = excluded.min_tick,
                trading_class = excluded.trading_class,
                trading_hours = excluded.trading_hours,
                liquid_hours = excluded.liquid_hours,
                time_zone_id = excluded.time_zone_id,
                long_name = excluded.long_name,
                raw = excluded.raw,
                qualified_at = excluded.qualified_at
            """,
            (
                contract.con_id,
                contract.symbol,
                contract.local_symbol,
                contract.sec_type.value,
                contract.exchange,
                contract.primary_exchange,
                contract.currency,
                contract.expiration,
                contract.last_trade_date,
                contract.multiplier,
                str(contract.min_tick),
                contract.trading_class,
                contract.trading_hours,
                contract.liquid_hours,
                contract.time_zone_id,
                contract.long_name,
                contract.raw_json(),
                contract.qualified_at or to_iso(utc_now()),
            ),
        )

    def get(self, con_id: int) -> QualifiedContract | None:
        row = self._db.query_one("SELECT * FROM contract_metadata WHERE con_id = ?", (con_id,))
        if row is None:
            return None
        return QualifiedContract(
            con_id=int(row["con_id"]),
            symbol=str(row["symbol"]),
            local_symbol=str(row["local_symbol"]),
            sec_type=SecurityType(row["sec_type"]),
            exchange=str(row["exchange"]),
            currency=str(row["currency"]),
            expiration=str(row["expiration"]),
            last_trade_date=str(row["last_trade_date"]),
            multiplier=str(row["multiplier"]),
            min_tick=_decimal_or_zero(row["min_tick"]),
            trading_class=str(row["trading_class"]),
            primary_exchange=row["primary_exchange"],
            trading_hours=row["trading_hours"],
            liquid_hours=row["liquid_hours"],
            time_zone_id=row["time_zone_id"],
            long_name=row["long_name"],
            qualified_at=str(row["qualified_at"]),
            raw=json.loads(row["raw"] or "{}"),
        )

    def all(self) -> Sequence[dict[str, object]]:
        rows = self._db.query_all("SELECT * FROM contract_metadata ORDER BY expiration")
        # `for key in row` would iterate sqlite3.Row *values*, not keys.
        return tuple({key: row[key] for key in row.keys()} for row in rows)  # noqa: SIM118


class PerformanceRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def get(self, trade_date: str | None = None) -> DailyPerformance:
        """Today's totals, or an all-zero record when nothing has happened yet."""
        date_key = trade_date or utc_date_string()
        row = self._db.query_one(
            "SELECT * FROM daily_performance WHERE trade_date = ?", (date_key,)
        )
        if row is None:
            return DailyPerformance(trade_date=date_key)
        return DailyPerformance(
            trade_date=date_key,
            realized_pnl=_decimal_or_zero(row["realized_pnl"]),
            unrealized_pnl=_decimal_or_zero(row["unrealized_pnl"]),
            commission=_decimal_or_zero(row["commission"]),
            order_count=int(row["order_count"]),
            fill_count=int(row["fill_count"]),
            updated_at=from_iso(str(row["updated_at"])),
        )

    def upsert(self, performance: DailyPerformance) -> None:
        self._db.execute(
            """
            INSERT INTO daily_performance (
                trade_date, realized_pnl, unrealized_pnl, commission, order_count,
                fill_count, updated_at
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(trade_date) DO UPDATE SET
                realized_pnl = excluded.realized_pnl,
                unrealized_pnl = excluded.unrealized_pnl,
                commission = excluded.commission,
                order_count = excluded.order_count,
                fill_count = excluded.fill_count,
                updated_at = excluded.updated_at
            """,
            (
                performance.trade_date,
                str(performance.realized_pnl),
                str(performance.unrealized_pnl),
                str(performance.commission),
                performance.order_count,
                performance.fill_count,
                to_iso(performance.updated_at),
            ),
        )


class StateRepository:
    """Durable key/value state, including the kill-switch latch."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def get_state(self, key: str) -> str | None:
        row = self._db.query_one("SELECT value FROM application_state WHERE key = ?", (key,))
        return None if row is None else str(row["value"])

    def set_state(self, key: str, value: str) -> None:
        self._db.execute(
            """
            INSERT INTO application_state (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at
            """,
            (key, value, to_iso(utc_now())),
        )

    def all(self) -> dict[str, str]:
        rows = self._db.query_all("SELECT key, value FROM application_state ORDER BY key")
        return {str(row["key"]): str(row["value"]) for row in rows}


class Repositories:
    """Convenience bundle so callers take one dependency rather than eight."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.orders = OrderRepository(database)
        self.fills = FillRepository(database)
        self.positions = PositionRepository(database)
        self.signals = SignalRepository(database)
        self.risk_decisions = RiskDecisionRepository(database)
        self.events = EventRepository(database)
        self.contracts = ContractRepository(database)
        self.performance = PerformanceRepository(database)
        self.state = StateRepository(database)


__all__ = [
    "ContractRepository",
    "EventRepository",
    "FillRepository",
    "OrderRepository",
    "PerformanceRepository",
    "PositionRepository",
    "Repositories",
    "RiskDecisionRepository",
    "SignalRepository",
    "StateRepository",
]
