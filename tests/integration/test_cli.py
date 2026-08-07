"""Operator CLI.

The two properties worth guarding here are negative ones: the CLI must expose
no way to clear the kill switch and no way to enable live trading. The rest is
read-only reporting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import EXIT_CONFIG, EXIT_ERROR, EXIT_OK, build_parser, main
from app.state.database import Database
from app.state.repositories import Repositories
from tests.conftest import CONTRACT_MONTH, default_env

pytestmark = pytest.mark.integration


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch) -> Path:
    """Install the deployed configuration into the process environment."""
    db_path = tmp_path / "trading.db"
    database = Database(db_path)
    database.migrate()
    database.close()

    for key, value in default_env(
        DATABASE_PATH=str(db_path),
        LOG_DIR=str(tmp_path / "logs"),
        DEFAULT_CONTRACT_MONTH=CONTRACT_MONTH,
        HEALTH_PORT="8799",
    ).items():
        monkeypatch.setenv(key, value)
    return db_path


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = main(list(argv))
    captured = capsys.readouterr().out
    return code, json.loads(captured) if captured.strip() else {}


class TestSurface:
    def test_there_is_no_way_to_clear_the_kill_switch(self) -> None:
        parser = build_parser()
        commands = parser._subparsers._group_actions[0].choices
        assert "kill-switch-off" not in commands
        assert "enable-live" not in commands
        assert "kill-switch-on" in commands

    def test_every_advertised_command_exists(self) -> None:
        parser = build_parser()
        commands = set(parser._subparsers._group_actions[0].choices)
        required = {
            "status",
            "broker-status",
            "positions",
            "open-orders",
            "contract-info",
            "kill-switch-on",
            "kill-switch-status",
            "cancel-all-orders",
        }
        assert required <= commands


class TestReadOnlyCommands:
    def test_kill_switch_status_reports_engaged_in_the_deployed_config(
        self, cli_env, capsys
    ) -> None:
        code, payload = _run(capsys, "kill-switch-status")
        assert code == EXIT_OK
        assert payload["engaged"] is True
        assert payload["config_engaged"] is True
        assert payload["safety"]["can_transmit_live_orders"] is False

    def test_positions_and_open_orders_start_empty(self, cli_env, capsys) -> None:
        code, payload = _run(capsys, "positions")
        assert code == EXIT_OK
        assert payload["count"] == 0

        code, payload = _run(capsys, "open-orders")
        assert code == EXIT_OK
        assert payload["count"] == 0

    def test_broker_status_reports_the_mock_broker_and_no_ib_port(self, cli_env, capsys) -> None:
        code, payload = _run(capsys, "broker-status")
        assert code == EXIT_OK
        assert payload["trading_mode"] == "mock"
        assert payload["broker_implementation"] == "mock"
        assert payload["ib_port_in_use"] is None

    def test_contract_info_explains_the_qualification_rule(self, cli_env, capsys) -> None:
        code, payload = _run(capsys, "contract-info")
        assert code == EXIT_OK
        assert payload["configured_symbol"] == "MSL"
        assert payload["configured_contract_month"] == CONTRACT_MONTH

    def test_db_info_reports_a_migrated_database(self, cli_env, capsys) -> None:
        code, payload = _run(capsys, "db-info")
        assert code == EXIT_OK
        assert payload["ok"] is True
        assert payload["schema_version"] >= 1

    def test_config_output_carries_no_secrets(self, cli_env, capsys) -> None:
        code, payload = _run(capsys, "config")
        assert code == EXIT_OK
        text = json.dumps(payload).lower()
        for forbidden in ("password", "secret", "token", "credential"):
            assert forbidden not in text

    def test_status_falls_back_to_durable_state_when_nothing_is_running(
        self, cli_env, capsys
    ) -> None:
        code, payload = _run(capsys, "status")
        assert code == EXIT_ERROR  # non-zero: the bot is genuinely not answering
        assert payload["live_status"] == "unavailable"
        assert "database" in payload


class TestKillSwitchEngage:
    def test_engaging_latches_durably(self, cli_env, capsys) -> None:
        code, payload = _run(capsys, "kill-switch-on", "--reason", "integration test")
        assert code == EXIT_OK
        assert payload["result"] == "KILL_SWITCH_ENGAGED"
        assert payload["latched"] is True

        database = Database(cli_env)
        database.connect()
        assert Repositories(database).state.get_state("kill_switch_latched") == "true"
        database.close()

    def test_a_reason_is_mandatory(self, cli_env) -> None:
        with pytest.raises(SystemExit):
            main(["kill-switch-on"])


class TestCancelAllOrders:
    def test_confirmation_is_required(self, cli_env, capsys) -> None:
        code, payload = _run(capsys, "cancel-all-orders")
        assert code == EXIT_ERROR
        assert payload["result"] == "CONFIRMATION_REQUIRED"

    def test_confirmed_cancel_runs_and_leaves_positions_alone(self, cli_env, capsys) -> None:
        code, payload = _run(capsys, "cancel-all-orders", "--confirm")
        assert code == EXIT_OK
        assert payload["result"] == "CANCEL_ALL_REQUESTED"
        assert "positions are NOT affected" in payload["note"]


def test_invalid_configuration_exits_with_the_config_code(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRADING_MODE", "almost-live")
    assert main(["kill-switch-status"]) == EXIT_CONFIG
