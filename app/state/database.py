"""SQLite persistence with forward-only migrations.

Portability to PostgreSQL
-------------------------
Callers never touch ``sqlite3``. They use :class:`Database`, which exposes
``execute``/``query_all``/``query_one``/``transaction``. Replacing the backend
means writing a second :class:`Database` implementation and a second migration
list; no repository or application code changes.

The schema is written to keep that promise:

* Timestamps are stored as ISO-8601 UTC **text**, not as SQLite ``REAL`` julian
  days or unix ints. The same strings load into a PostgreSQL ``timestamptz``.
* Money and prices are stored as **text decimal strings**, not floats. Binary
  floating point is the wrong representation for prices, and text maps cleanly
  onto ``NUMERIC``.
* No SQLite-specific SQL (no ``INSERT OR REPLACE`` in the hot paths, no
  ``rowid`` dependence) outside this module.

Concurrency
-----------
WAL is enabled so a reader (the operator CLI) never blocks the writer (the
trading process). The connection is guarded by a re-entrant lock and shared
across the asyncio loop; at this transaction volume that is far simpler than an
async driver and has no measurable cost.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.logging_config import get_logger
from app.utilities.timeutils import utc_now

_LOG = get_logger("state.database")

SCHEMA_VERSION = 1


class DatabaseError(RuntimeError):
    """Raised when the database cannot be opened, migrated, or written."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        statements=(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                applied_at  TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS orders (
                internal_order_id   TEXT PRIMARY KEY,
                idempotency_key     TEXT NOT NULL UNIQUE,
                correlation_id      TEXT NOT NULL,
                intent_id           TEXT,
                strategy_id         TEXT NOT NULL,
                run_id              TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                transmitted_at      TEXT,
                symbol              TEXT NOT NULL,
                con_id              INTEGER NOT NULL,
                local_symbol        TEXT NOT NULL DEFAULT '',
                exchange            TEXT NOT NULL DEFAULT '',
                currency            TEXT NOT NULL DEFAULT 'USD',
                side                TEXT NOT NULL,
                quantity            INTEGER NOT NULL,
                order_type          TEXT NOT NULL,
                time_in_force       TEXT NOT NULL DEFAULT 'DAY',
                limit_price         TEXT,
                stop_price          TEXT,
                status              TEXT NOT NULL,
                broker_order_id     TEXT,
                filled_quantity     INTEGER NOT NULL DEFAULT 0,
                average_fill_price  TEXT,
                commission          TEXT,
                commission_currency TEXT,
                error_code          TEXT,
                error_message       TEXT,
                block_reasons       TEXT NOT NULL DEFAULT '[]',
                trading_mode        TEXT NOT NULL,
                account_type        TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
            "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_orders_correlation ON orders(correlation_id)",
            "CREATE INDEX IF NOT EXISTS idx_orders_broker_id ON orders(broker_order_id)",
            """
            CREATE TABLE IF NOT EXISTS fills (
                execution_id        TEXT PRIMARY KEY,
                internal_order_id   TEXT,
                broker_order_id     TEXT,
                correlation_id      TEXT NOT NULL DEFAULT '',
                run_id              TEXT NOT NULL DEFAULT '',
                con_id              INTEGER NOT NULL,
                symbol              TEXT NOT NULL,
                side                TEXT NOT NULL,
                quantity            INTEGER NOT NULL,
                price               TEXT NOT NULL,
                commission          TEXT,
                commission_currency TEXT,
                executed_at         TEXT NOT NULL,
                recorded_at         TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(internal_order_id)",
            "CREATE INDEX IF NOT EXISTS idx_fills_executed_at ON fills(executed_at)",
            """
            CREATE TABLE IF NOT EXISTS positions (
                con_id          INTEGER PRIMARY KEY,
                account_id      TEXT NOT NULL DEFAULT '',
                symbol          TEXT NOT NULL,
                local_symbol    TEXT NOT NULL DEFAULT '',
                quantity        INTEGER NOT NULL,
                average_cost    TEXT NOT NULL,
                source          TEXT NOT NULL DEFAULT 'local',
                updated_at      TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS signals (
                intent_id           TEXT PRIMARY KEY,
                correlation_id      TEXT NOT NULL,
                run_id              TEXT NOT NULL,
                strategy_name       TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                direction           TEXT NOT NULL,
                requested_position  INTEGER NOT NULL,
                confidence          TEXT,
                contract_key        TEXT,
                metadata            TEXT NOT NULL DEFAULT '{}',
                accepted            INTEGER NOT NULL,
                rejection_reasons   TEXT NOT NULL DEFAULT '[]',
                created_at          TEXT NOT NULL,
                recorded_at         TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at)",
            """
            CREATE TABLE IF NOT EXISTS risk_decisions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id  TEXT NOT NULL,
                intent_id       TEXT,
                run_id          TEXT NOT NULL,
                approved        INTEGER NOT NULL,
                reasons         TEXT NOT NULL DEFAULT '[]',
                detail          TEXT NOT NULL DEFAULT '{}',
                decided_at      TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_risk_decided_at ON risk_decisions(decided_at)",
            """
            CREATE TABLE IF NOT EXISTS bot_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT NOT NULL,
                correlation_id  TEXT,
                level           TEXT NOT NULL,
                event           TEXT NOT NULL,
                message         TEXT NOT NULL DEFAULT '',
                data            TEXT NOT NULL DEFAULT '{}',
                occurred_at     TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_bot_events_occurred ON bot_events(occurred_at)",
            """
            CREATE TABLE IF NOT EXISTS broker_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT NOT NULL,
                correlation_id  TEXT,
                event           TEXT NOT NULL,
                code            TEXT,
                message         TEXT NOT NULL DEFAULT '',
                data            TEXT NOT NULL DEFAULT '{}',
                occurred_at     TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_broker_events_occurred ON broker_events(occurred_at)",
            """
            CREATE TABLE IF NOT EXISTS contract_metadata (
                con_id              INTEGER PRIMARY KEY,
                symbol              TEXT NOT NULL,
                local_symbol        TEXT NOT NULL,
                sec_type            TEXT NOT NULL,
                exchange            TEXT NOT NULL,
                primary_exchange    TEXT,
                currency            TEXT NOT NULL,
                expiration          TEXT NOT NULL,
                last_trade_date     TEXT NOT NULL,
                multiplier          TEXT NOT NULL,
                min_tick            TEXT NOT NULL,
                trading_class       TEXT NOT NULL,
                trading_hours       TEXT,
                liquid_hours        TEXT,
                time_zone_id        TEXT,
                long_name           TEXT,
                raw                 TEXT NOT NULL DEFAULT '{}',
                qualified_at        TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS daily_performance (
                trade_date      TEXT PRIMARY KEY,
                realized_pnl    TEXT NOT NULL DEFAULT '0',
                unrealized_pnl  TEXT NOT NULL DEFAULT '0',
                commission      TEXT NOT NULL DEFAULT '0',
                order_count     INTEGER NOT NULL DEFAULT 0,
                fill_count      INTEGER NOT NULL DEFAULT 0,
                updated_at      TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS application_state (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
            """,
        ),
    ),
)


class Database:
    """Thin, thread-safe wrapper around a SQLite connection."""

    def __init__(self, path: Path | str, *, timeout_seconds: float = 30.0) -> None:
        self._path = Path(path)
        self._timeout = timeout_seconds
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_memory(self) -> bool:
        return str(self._path) == ":memory:"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        if self._connection is not None:
            return
        if not self.is_memory:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DatabaseError(
                    f"cannot create database directory {self._path.parent}: {exc}"
                ) from exc
        try:
            connection = sqlite3.connect(
                str(self._path),
                timeout=self._timeout,
                check_same_thread=False,
                isolation_level=None,  # explicit transactions only
            )
        except sqlite3.Error as exc:
            raise DatabaseError(f"cannot open database {self._path}: {exc}") from exc

        connection.row_factory = sqlite3.Row
        # WAL keeps the operator CLI's reads from blocking the trading writer.
        # It is a no-op for :memory: databases, which is why the result is not
        # asserted on.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _require(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DatabaseError("database is not connected; call connect() first")
        return self._connection

    # ------------------------------------------------------------------
    # Migrations
    # ------------------------------------------------------------------

    def migrate(self) -> int:
        """Apply outstanding migrations. Returns the resulting schema version."""
        self.connect()
        connection = self._require()
        with self._lock:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL,
                    applied_at  TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                try:
                    connection.execute("BEGIN")
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) "
                        "VALUES (?, ?, ?)",
                        (migration.version, migration.name, utc_now().isoformat()),
                    )
                    connection.execute("COMMIT")
                except sqlite3.Error as exc:
                    connection.execute("ROLLBACK")
                    raise DatabaseError(
                        f"migration {migration.version} ({migration.name}) failed: {exc}"
                    ) from exc
                _LOG.info(
                    "applied database migration",
                    extra={
                        "event": "database.migrated",
                        "version": migration.version,
                        # Not "name": logging reserves that attribute on LogRecord
                        # and raises KeyError if an `extra` tries to overwrite it.
                        "migration_name": migration.name,
                    },
                )
        return self.schema_version()

    def schema_version(self) -> int:
        """Applied schema version. ``0`` means the database has never been migrated."""
        try:
            row = self.query_one("SELECT MAX(version) AS version FROM schema_migrations")
        except DatabaseError:
            # The migrations table does not exist yet; that is version 0, not a fault.
            return 0
        if row is None or row["version"] is None:
            return 0
        return int(row["version"])

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        with self._lock:
            connection = self._require()
            try:
                cursor = connection.execute(sql, params)
            except sqlite3.Error as exc:
                raise DatabaseError(f"query failed: {exc}\nSQL: {sql.strip()[:200]}") from exc
            return cursor.rowcount

    def query_all(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[sqlite3.Row]:
        with self._lock:
            connection = self._require()
            try:
                return list(connection.execute(sql, params))
            except sqlite3.Error as exc:
                raise DatabaseError(f"query failed: {exc}\nSQL: {sql.strip()[:200]}") from exc

    def query_one(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> sqlite3.Row | None:
        rows = self.query_all(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction. Rolls back on any exception."""
        with self._lock:
            connection = self._require()
            connection.execute("BEGIN")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def info(self) -> dict[str, object]:
        """Summary used by ``make db-info`` and the /status endpoint."""
        try:
            self.connect()
            journal_row = self.query_one("PRAGMA journal_mode")
            journal_mode = journal_row[0] if journal_row else "unknown"
            tables = [
                str(row["name"])
                for row in self.query_all(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            counts: dict[str, int] = {}
            for table in tables:
                if table.startswith("sqlite_"):
                    continue
                row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608
                counts[table] = int(row["n"]) if row else 0
            size_bytes = (
                self._path.stat().st_size if not self.is_memory and self._path.exists() else 0
            )
            return {
                "path": str(self._path),
                "ok": True,
                "schema_version": self.schema_version(),
                "journal_mode": journal_mode,
                "size_bytes": size_bytes,
                "tables": counts,
            }
        except (DatabaseError, OSError) as exc:
            return {"path": str(self._path), "ok": False, "error": str(exc)}

    def healthy(self) -> bool:
        try:
            row = self.query_one("SELECT 1 AS ok")
            return row is not None and int(row["ok"]) == 1
        except DatabaseError:
            return False


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "Database", "DatabaseError", "Migration"]
