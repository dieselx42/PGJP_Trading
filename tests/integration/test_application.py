"""End-to-end runtime tests against the real asyncio loop and a real database.

These drive :class:`~app.main.TradingApplication` exactly as ``python -m
app.main`` does, including the health server, and assert the properties that
matter operationally: the deployed configuration reaches a safe state, produces
no orders, survives a restart, and recovers from a broker disconnect only by way
of reconciliation.

Two helpers are used deliberately:

* :func:`live_app` keeps the process *running* so state that only exists while
  connected (broker session, market data, health server) can be inspected.
* :func:`run_to_completion` runs the real ``run()`` loop through a clean
  shutdown, for assertions about what was left behind on disk.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.models import BrokerPosition
from app.config import Config
from app.enums import AccountType, ApplicationState, ConnectionState, TradingMode
from app.main import TradingApplication, main
from app.state.database import Database
from app.state.repositories import Repositories
from tests.conftest import CONTRACT_MONTH, default_env, permissive_env

pytestmark = pytest.mark.integration


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """The deployed configuration, redirected at a temporary directory."""
    base = default_env(
        DATABASE_PATH=str(tmp_path / "trading.db"),
        LOG_DIR=str(tmp_path / "logs"),
        DEFAULT_CONTRACT_MONTH=CONTRACT_MONTH,
        MARKET_DATA_POLL_INTERVAL_SECONDS="0.05",
        HEALTH_PORT="0",
        LOG_LEVEL="CRITICAL",
    )
    base.update(overrides)
    return base


@asynccontextmanager
async def live_app(config: Config, *, ticks: int = 3) -> AsyncIterator[TradingApplication]:
    """Start the app, connect, reconcile, run a few ticks, and hold it running."""
    app = TradingApplication(config)
    await app.startup()
    try:
        if config.trading_mode is not TradingMode.DISABLED:
            assert await app._connect_with_backoff()
            await app._after_connect()
            for _ in range(ticks):
                await app._tick()
        yield app
    finally:
        await app.shutdown()


async def run_to_completion(config: Config, *, seconds: float = 0.3) -> TradingApplication:
    """Run the real main loop and let it shut down cleanly."""
    app = TradingApplication(config)
    task = asyncio.create_task(app.run())
    await asyncio.sleep(seconds)
    app.request_stop()
    assert await asyncio.wait_for(task, timeout=10) == 0
    return app


def _repos(config: Config) -> Repositories:
    database = Database(config.database_path)
    database.connect()
    return Repositories(database)


# ---------------------------------------------------------------------------
# The deployed configuration
# ---------------------------------------------------------------------------


class TestDeployedConfiguration:
    async def test_mock_mode_reaches_halted_and_transmits_nothing(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        async with live_app(config) as app:
            # KILL_SWITCH=true is the strongest reason not to trade, so it wins
            # over the other refusals and shows up as the reported state.
            assert app.state is ApplicationState.HALTED
            assert app.reconciliation.succeeded
            assert app.contract is not None
            assert app.contract.con_id > 0

        repos = _repos(config)
        assert repos.orders.recent(limit=100) == (), "an order was created"
        assert repos.fills.count() == 0
        repos.database.close()

    async def test_transmit_gate_refuses_in_the_deployed_state(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        async with live_app(config) as app:
            gate = app.status_payload()["safety"]["transmit_gate"]  # type: ignore[index]
            assert gate["allowed"] is False
            assert "KILL_SWITCH_ENGAGED" in gate["reasons"]
            assert "ORDER_TRANSMIT_NOT_ALLOWED" in gate["reasons"]
            assert "APPLICATION_NOT_READY" in gate["reasons"]

    async def test_status_reports_the_full_picture(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        async with live_app(config) as app:
            status = app.status_payload()
            assert status["application"]["state"] == "HALTED"  # type: ignore[index]
            assert status["application"]["version"]  # type: ignore[index]
            assert status["broker"]["implementation"] == "mock"  # type: ignore[index]
            assert status["broker"]["account_type"] == "simulated"  # type: ignore[index]
            assert status["broker"]["connection_state"] == "connected"  # type: ignore[index]
            assert status["safety"]["can_transmit_live_orders"] is False  # type: ignore[index]
            assert status["strategy"]["name"] == "noop"  # type: ignore[index]
            assert status["database"]["ok"] is True  # type: ignore[index]
            assert status["market_data"]["fresh"] is False  # type: ignore[index]
            assert status["contract"]["symbol"] == "MSL"  # type: ignore[index]
            assert status["portfolio"]["open_orders_count"] == 0  # type: ignore[index]
            assert status["reconciliation"]["succeeded"] is True  # type: ignore[index]

    async def test_market_data_is_never_fresh_when_the_limit_is_unconfigured(
        self, tmp_path
    ) -> None:
        """MARKET_DATA_MAX_AGE_SECONDS=0 in the deployed config."""
        config = Config.from_env(_env(tmp_path))
        async with live_app(config) as app:
            assert app.market_data is not None
            assert app.contract is not None
            # Data is arriving...
            assert app.market_data.latest(app.contract.key()) is not None
            # ...but is never *usable*, because the limit is unset.
            assert app.market_data.is_fresh(app.contract.key()) is False

    async def test_status_never_leaks_an_account_id_or_secret(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        async with live_app(config) as app:
            serialised = json.dumps(app.status_payload(), default=str)
        assert "DUMOCK0001" not in serialised, "an unmasked account id reached /status"
        assert "DU***001" in serialised, "the account id should still be identifiable"
        for forbidden in ("password", "secret", "token", "credential", "api_key"):
            assert forbidden not in serialised.lower()

    async def test_health_is_ok_even_though_trading_is_halted(self, tmp_path) -> None:
        """A deliberately-halted bot is healthy; otherwise Docker would fight it."""
        config = Config.from_env(_env(tmp_path))
        async with live_app(config) as app:
            status_code, payload = app.health_payload()
            assert status_code == 200
            assert payload["status"] == "healthy"
            assert payload["application_state"] == "HALTED"

    async def test_noop_strategy_consumed_market_data(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        # The kill switch stops the strategy being *called*, which is correct.
        # Lift only the kill switch to prove the data path itself works.
        config_live_data = Config.from_env(_env(tmp_path, KILL_SWITCH="false"))
        async with live_app(config_live_data, ticks=5) as app:
            assert app.strategy is not None
            assert app.strategy.quotes_seen > 0, "the data path never delivered a quote"
        assert config.kill_switch is True

    async def test_kill_switch_stops_the_strategy_being_called(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        async with live_app(config, ticks=5) as app:
            assert app.strategy is not None
            assert app.strategy.quotes_seen == 0, (
                "the kill switch must stop strategies producing actionable output"
            )


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------


class TestDisabledMode:
    async def test_disabled_mode_creates_no_broker_at_all(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path, TRADING_MODE="disabled"))
        async with live_app(config) as app:
            assert app.broker is None, "disabled mode must not construct a broker"
            assert app.order_manager is None
            assert app.market_data is None
            assert app.state is ApplicationState.SAFE
            assert app.health_payload()[0] == 200

    async def test_disabled_mode_runs_the_real_loop_without_error(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path, TRADING_MODE="disabled"))
        app = await run_to_completion(config, seconds=0.15)
        assert app.state is ApplicationState.SAFE


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


class TestRestart:
    async def test_state_survives_a_restart_and_no_orders_appear(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))

        first = await run_to_completion(config)
        second = await run_to_completion(config)
        assert second.run_id != first.run_id

        repos = _repos(config)
        state = repos.state.all()
        assert state["last_run_id"] == second.run_id
        assert state["last_application_state"] in {"HALTED", "SAFE"}
        assert repos.orders.recent(limit=100) == ()

        events = repos.events.recent_bot_events(limit=50)
        assert any(e["event"] == "app.starting" for e in events)
        assert any(e["event"] == "app.shutdown" for e in events)
        repos.database.close()

    async def test_contract_metadata_persists_across_restarts(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        async with live_app(config) as app:
            assert app.contract is not None
            con_id = app.contract.con_id

        repos = _repos(config)
        stored = repos.contracts.get(con_id)
        assert stored is not None
        assert stored.symbol == "MSL"
        assert stored.expiration == CONTRACT_MONTH
        assert stored.multiplier == "25"
        repos.database.close()

    async def test_migrations_run_once_across_restarts(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        await run_to_completion(config, seconds=0.1)
        await run_to_completion(config, seconds=0.1)

        repos = _repos(config)
        rows = repos.database.query_all("SELECT version FROM schema_migrations")
        assert len(rows) == len({row["version"] for row in rows})
        repos.database.close()


# ---------------------------------------------------------------------------
# Health server
# ---------------------------------------------------------------------------


class TestHealthServer:
    async def test_endpoints_answer_over_real_http(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        async with live_app(config, ticks=1) as app:
            port = app.health_server.bound_port
            assert port > 0

            health = await _get(f"http://127.0.0.1:{port}/health")
            assert health["status"] == "healthy"

            status = await _get(f"http://127.0.0.1:{port}/status")
            assert status["application"]["run_id"] == app.run_id

            index = await _get(f"http://127.0.0.1:{port}/")
            assert "/status" in index["endpoints"]

    async def test_unknown_paths_and_methods_are_refused(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path))
        async with live_app(config, ticks=0) as app:
            port = app.health_server.bound_port

            with pytest.raises(urllib.error.HTTPError) as not_found:
                await _get(f"http://127.0.0.1:{port}/secrets")
            assert not_found.value.code == 404

            with pytest.raises(urllib.error.HTTPError) as bad_method:
                await _get(f"http://127.0.0.1:{port}/status", method="POST")
            assert bad_method.value.code == 405

    async def test_server_binds_loopback_only(self, tmp_path) -> None:
        """The health service must never be reachable off the host."""
        config = Config.from_env(_env(tmp_path))
        assert config.health_host == "127.0.0.1"
        async with live_app(config, ticks=0) as app:
            server = app.health_server._server
            assert server is not None
            for sock in server.sockets:
                assert sock.getsockname()[0] in ("127.0.0.1", "::1")


# ---------------------------------------------------------------------------
# Disconnect / reconnect / reconciliation
# ---------------------------------------------------------------------------


class TestBrokerDisconnectAndRecovery:
    async def test_disconnect_drops_to_safe_and_clears_market_data(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path, KILL_SWITCH="false", ALLOW_ORDER_TRANSMIT="true"))
        async with live_app(config, ticks=1) as app:
            assert app.state is ApplicationState.READY
            assert app.contract is not None
            assert app.market_data is not None
            contract_key = app.contract.key()
            assert app.market_data.latest(contract_key) is not None

            app.broker.force_disconnect()  # type: ignore[union-attr]
            await app._tick()

            assert app.state is ApplicationState.SAFE
            assert app.contract is None
            assert app.account is None
            assert not app.reconciliation.succeeded
            assert app.market_data.latest(contract_key) is None, (
                "stale quotes must not survive a disconnect"
            )
            gate = app.status_payload()["safety"]["transmit_gate"]  # type: ignore[index]
            assert gate["allowed"] is False

    async def test_reconnect_requires_reconciliation_before_ready(self, tmp_path) -> None:
        config = Config.from_env(_env(tmp_path, KILL_SWITCH="false", ALLOW_ORDER_TRANSMIT="true"))
        async with live_app(config, ticks=1) as app:
            assert app.state is ApplicationState.READY

            app.broker.force_disconnect()  # type: ignore[union-attr]
            await app._tick()
            assert app.state is ApplicationState.SAFE

            # Reconnecting alone is not enough...
            assert await app._connect_with_backoff()
            assert app.state is ApplicationState.CONNECTING
            # ...only reconciliation restores READY.
            await app._after_connect()
            assert app.state is ApplicationState.READY

    async def test_failed_connection_backs_off_without_crashing(self, tmp_path) -> None:
        from app.broker.mock_broker import MockBroker

        config = Config.from_env(
            _env(tmp_path, RECONNECT_INITIAL_SECONDS="0.1", RECONNECT_MAX_SECONDS="0.1")
        )
        app = TradingApplication(config)
        await app.startup()
        try:
            app.broker = MockBroker(fail_connect=True)
            assert await app._connect_with_backoff() is False
            assert app.state is ApplicationState.DISCONNECTED
            assert app.health_payload()[0] == 200, "a retrying bot is not unhealthy"
        finally:
            await app.shutdown()

    async def test_permission_denial_is_not_retried(self, tmp_path) -> None:
        """A permission refusal must stop the retry loop, not feed it."""
        from app.broker.mock_broker import MockBroker

        config = Config.from_env(
            _env(tmp_path, RECONNECT_INITIAL_SECONDS="0.1", RECONNECT_MAX_SECONDS="0.1")
        )
        app = TradingApplication(config)
        await app.startup()
        try:
            broker = MockBroker(permission_denied=True)
            app.broker = broker
            app.resolver.__init__(broker)  # type: ignore[union-attr,misc]
            assert await app._connect_with_backoff()
            await app._after_connect()
            # Contract resolution is refused on permission grounds; the app
            # stays usable but has nothing to trade and never becomes READY.
            assert app.contract is None
            assert app.state is not ApplicationState.READY
        finally:
            await app.shutdown()

    async def test_reconciliation_failure_keeps_the_system_safe(self, tmp_path) -> None:
        """A position the broker knows about that we do not must stop trading."""
        config = Config.from_env(_env(tmp_path, KILL_SWITCH="false", ALLOW_ORDER_TRANSMIT="true"))
        app = TradingApplication(config)
        await app.startup()
        try:
            app.broker.seed_position(  # type: ignore[union-attr]
                BrokerPosition(
                    account_id="DUMOCK0001",
                    con_id=42,
                    symbol="MSL",
                    local_symbol="MSLZ6",
                    sec_type="FUT",
                    exchange="CME",
                    currency="USD",
                    quantity=7,
                    average_cost=Decimal("3750"),
                )
            )
            assert await app._connect_with_backoff()
            await app._after_connect()

            assert not app.reconciliation.succeeded
            assert app.state is ApplicationState.SAFE
            assert app.reconciliation.position_discrepancies[0].broker_quantity == 7
            gate = app.status_payload()["safety"]["transmit_gate"]  # type: ignore[index]
            assert "POSITIONS_NOT_RECONCILED" in gate["reasons"]
        finally:
            await app.shutdown()


# ---------------------------------------------------------------------------
# Fully permissive mode still trades nothing, because NoOpStrategy
# ---------------------------------------------------------------------------


class TestPermissiveModeStillTradesNothing:
    async def test_ready_state_reached_but_no_orders_produced(self, tmp_path) -> None:
        config = Config.from_env(
            permissive_env(
                DATABASE_PATH=str(tmp_path / "t.db"),
                LOG_DIR=str(tmp_path / "logs"),
                MARKET_DATA_POLL_INTERVAL_SECONDS="0.05",
                LOG_LEVEL="CRITICAL",
            )
        )
        async with live_app(config, ticks=5) as app:
            assert app.state is ApplicationState.READY
            assert app._connection_state() is ConnectionState.CONNECTED
            assert app._account_type() is AccountType.SIMULATED
            assert app.strategy is not None
            assert app.strategy.quotes_seen > 0

        repos = _repos(config)
        assert repos.orders.recent(limit=100) == (), "NoOpStrategy produced an order"
        repos.database.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def test_main_refuses_to_start_on_invalid_configuration(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "sort-of-live")
    assert main([]) == 2, "an unparseable configuration must stop the process"


def test_main_refuses_a_half_armed_live_configuration(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "mock")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    assert main([]) == 2


async def _get(url: str, *, method: str = "GET") -> dict:
    request = urllib.request.Request(url, method=method)

    def _fetch() -> dict:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload: dict = json.loads(response.read().decode("utf-8"))
            return payload

    return await asyncio.get_running_loop().run_in_executor(None, _fetch)
