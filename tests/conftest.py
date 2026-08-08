"""Shared fixtures.

The most important thing in this file is :func:`permissive_env` together with
:func:`all_green_gate_context`. They construct the *only* state in which an
order is permitted to transmit.

Every critical-safety test then flips exactly one thing and asserts a refusal.
That is why ``test_all_green_baseline_allows_transmission`` exists in
``test_critical_safety.py``: without it, a gate that always refused would pass
all ten safety tests while being useless. The baseline is what makes the other
tests meaningful.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.mock_broker import MockBroker
from app.config import Config
from app.contracts.models import QualifiedContract
from app.enums import (
    AccountType,
    ApplicationState,
    ConnectionState,
    Direction,
    OrderSide,
    SecurityType,
)
from app.risk.manager import RiskContext
from app.safety.gate import GateContext
from app.signals.models import TradeIntent
from app.state.database import Database
from app.state.repositories import Repositories
from app.utilities.timeutils import utc_now

CONTRACT_MONTH = "202612"


def permissive_env(**overrides: str) -> dict[str, str]:
    """An environment in which trading is fully authorised.

    Used as the baseline that safety tests degrade from. Note that this is NOT
    the deployed configuration -- the deployed one has every interlock engaged.
    """
    env = {
        "APP_ENV": "test",
        "TRADING_MODE": "mock",
        "ALLOW_ORDER_TRANSMIT": "true",
        "LIVE_TRADING_ENABLED": "false",
        "KILL_SWITCH": "false",
        "SOL_FUTURES_PERMISSION_READY": "true",
        "DEFAULT_FUTURES_SYMBOL": "MSL",
        "DEFAULT_CONTRACT_MONTH": CONTRACT_MONTH,
        "MAX_POSITION_CONTRACTS": "5",
        "MAX_ORDER_SIZE": "2",
        "MAX_DAILY_LOSS_USD": "1000",
        "MAX_ORDERS_PER_HOUR": "10",
        "MAX_OPEN_ORDERS": "3",
        "MAX_NOTIONAL_EXPOSURE_USD": "1000000",
        "MARKET_DATA_MAX_AGE_SECONDS": "30",
        "DATABASE_PATH": ":memory:",
        "LOG_LEVEL": "CRITICAL",
        "LOG_FORMAT": "json",
        "HEALTH_PORT": "0",
    }
    env.update(overrides)
    return env


def default_env(**overrides: str) -> dict[str, str]:
    """The shipped defaults: everything locked down, every limit unconfigured."""
    env = {
        "APP_ENV": "production",
        "TRADING_MODE": "mock",
        "ALLOW_ORDER_TRANSMIT": "false",
        "LIVE_TRADING_ENABLED": "false",
        "KILL_SWITCH": "true",
        "SOL_FUTURES_PERMISSION_READY": "false",
        "MAX_POSITION_CONTRACTS": "0",
        "MAX_ORDER_SIZE": "0",
        "MAX_DAILY_LOSS_USD": "0",
        "MAX_ORDERS_PER_HOUR": "0",
        "MAX_OPEN_ORDERS": "0",
        "MAX_NOTIONAL_EXPOSURE_USD": "0",
        "MARKET_DATA_MAX_AGE_SECONDS": "0",
        "DATABASE_PATH": ":memory:",
        "LOG_LEVEL": "CRITICAL",
        "HEALTH_PORT": "0",
    }
    env.update(overrides)
    return env


@pytest.fixture
def permissive_config() -> Config:
    return Config.from_env(permissive_env())


@pytest.fixture
def default_config() -> Config:
    return Config.from_env(default_env())


@pytest.fixture
def contract() -> QualifiedContract:
    return QualifiedContract(
        con_id=987654321,
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
        primary_exchange="CME",
        qualified_at=utc_now().isoformat(),
    )


def all_green_gate_context(**overrides: object) -> GateContext:
    """Every interlock satisfied. Flip one field to test one refusal."""
    base: dict[str, object] = {
        "app_state": ApplicationState.READY,
        "connection_state": ConnectionState.CONNECTED,
        "broker_account_type": AccountType.SIMULATED,
        "contract_qualified": True,
        "contract_is_continuous": False,
        "account_available": True,
        "positions_reconciled": True,
        "open_orders_reconciled": True,
        "market_data_age_seconds": 1.0,
        "market_data_is_delayed": False,
        "broker_permission_ready": True,
        "strategy_enabled": True,
        "risk_approved": True,
    }
    base.update(overrides)
    return GateContext(**base)  # type: ignore[arg-type]


def sample_intent(**overrides: object) -> TradeIntent:
    base: dict[str, object] = {
        "strategy_name": "noop",
        "symbol": "MSL",
        "direction": Direction.LONG,
        "requested_position": 1,
        "created_at": utc_now(),
        "correlation_id": "cor_test",
        "intent_id": "int_test_0001",
    }
    base.update(overrides)
    return TradeIntent(**base)  # type: ignore[arg-type]


def all_green_risk_context(contract: QualifiedContract, **overrides: object) -> RiskContext:
    """Every risk control satisfied."""
    base: dict[str, object] = {
        "intent": sample_intent(),
        "order_side": OrderSide.BUY,
        "order_quantity": 1,
        "current_position": 0,
        "contract": contract,
        "reference_price": Decimal("150.00"),
        "market_data_age_seconds": 1.0,
        "daily_pnl": Decimal("0"),
        "open_orders_count": 0,
        "orders_last_hour": 0,
        "app_state": ApplicationState.READY,
        "connection_state": ConnectionState.CONNECTED,
        "account_type": AccountType.SIMULATED,
        "account_available": True,
        "positions_reconciled": True,
        "open_orders_reconciled": True,
        "broker_permission_ready": True,
        "strategy_enabled": True,
        "kill_switch_engaged": False,
        "duplicate_order_exists": False,
    }
    base.update(overrides)
    return RiskContext(**base)  # type: ignore[arg-type]


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "test.db")
    db.connect()
    db.migrate()
    yield db
    db.close()


@pytest.fixture
def repositories(database: Database) -> Repositories:
    return Repositories(database)


@pytest.fixture
def mock_broker() -> MockBroker:
    return MockBroker(seed=42)


def at(seconds_ago: float = 0.0) -> datetime:
    """A UTC timestamp ``seconds_ago`` in the past."""
    from datetime import timedelta

    return datetime.now(UTC) - timedelta(seconds=seconds_ago)
