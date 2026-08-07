"""Configuration parsing and validation."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app import __version__
from app.config import Config, ConfigError, ReconnectPolicy, parse_bool, parse_int
from app.enums import TradingMode
from tests.conftest import default_env, permissive_env


def test_empty_environment_is_the_safest_possible_configuration() -> None:
    config = Config.from_env({})
    assert config.trading_mode is TradingMode.DISABLED
    assert config.allow_order_transmit is False
    assert config.live_trading_enabled is False
    assert config.kill_switch is True
    assert config.sol_futures_permission_ready is False
    assert config.risk.any_configured is False
    assert config.market_data_max_age_seconds == 0.0
    assert config.default_contract_month is None


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", "  True  "])
def test_bool_true_literals(value: str) -> None:
    assert parse_bool({"X": value}, "X", False) is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_bool_false_literals(value: str) -> None:
    assert parse_bool({"X": value}, "X", True) is False


@pytest.mark.parametrize("value", ["maybe", "yes please", "2", "-1", "null"])
def test_unparseable_boolean_raises_rather_than_guessing(value: str) -> None:
    """An unrecognised flag stops the process; it is never coerced."""
    with pytest.raises(ConfigError, match="not a boolean"):
        parse_bool({"X": value}, "X", False)


def test_unknown_trading_mode_is_refused() -> None:
    with pytest.raises(ConfigError, match="invalid TradingMode"):
        Config.from_env({"TRADING_MODE": "semi-live"})


def test_negative_risk_limit_is_refused() -> None:
    with pytest.raises(ConfigError, match="below the minimum"):
        Config.from_env(default_env(MAX_ORDER_SIZE="-1"))


def test_non_numeric_limit_is_refused() -> None:
    with pytest.raises(ConfigError, match="not an integer"):
        Config.from_env(default_env(MAX_ORDER_SIZE="lots"))


def test_non_numeric_decimal_is_refused() -> None:
    with pytest.raises(ConfigError, match="not a decimal"):
        Config.from_env(default_env(MAX_DAILY_LOSS_USD="loads"))


def test_unsupported_symbol_is_refused() -> None:
    with pytest.raises(ConfigError, match="not supported"):
        Config.from_env(default_env(DEFAULT_FUTURES_SYMBOL="ES"))


def test_invalid_log_level_is_refused() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Config.from_env(default_env(LOG_LEVEL="CHATTY"))


def test_identical_ib_ports_are_refused() -> None:
    """Equal paper/live ports would make 'connect to paper' ambiguous."""
    with pytest.raises(ConfigError, match="must differ"):
        Config.from_env(default_env(IB_PAPER_PORT="4002", IB_LIVE_PORT="4002"))


def test_admin_client_id_must_differ_from_trading_client_id() -> None:
    with pytest.raises(ConfigError, match="must differ"):
        Config.from_env(default_env(IB_CLIENT_ID="10", IB_ADMIN_CLIENT_ID="10"))


@pytest.mark.parametrize("value", ["2026", "20261", "December", "202613", "999901"])
def test_malformed_contract_month_is_refused(value: str) -> None:
    with pytest.raises(ConfigError, match="DEFAULT_CONTRACT_MONTH"):
        Config.from_env(default_env(DEFAULT_CONTRACT_MONTH=value))


@pytest.mark.parametrize("value", ["202612", "20261218"])
def test_valid_contract_month_is_accepted(value: str) -> None:
    config = Config.from_env(default_env(DEFAULT_CONTRACT_MONTH=value))
    assert config.default_contract_month == value


def test_health_host_must_be_a_known_bind_address() -> None:
    with pytest.raises(ConfigError, match="HEALTH_HOST"):
        Config.from_env(default_env(HEALTH_HOST="203.0.113.5"))


def test_redacted_config_contains_no_secret_shaped_keys() -> None:
    config = Config.from_env(permissive_env())
    keys = " ".join(config.redacted()).lower()
    for forbidden in ("password", "secret", "token", "credential", "api_key"):
        assert forbidden not in keys


def test_redacted_config_reports_the_port_actually_in_use() -> None:
    paper = Config.from_env(permissive_env(TRADING_MODE="paper"))
    assert paper.redacted()["IB_PORT_IN_USE"] == paper.ibkr.paper_port
    mock = Config.from_env(permissive_env())
    assert mock.redacted()["IB_PORT_IN_USE"] is None


def test_risk_limits_all_configured_flag() -> None:
    assert Config.from_env(permissive_env()).risk.all_configured is True
    assert Config.from_env(default_env()).risk.all_configured is False


def test_decimal_limits_keep_precision() -> None:
    config = Config.from_env(permissive_env(MAX_DAILY_LOSS_USD="1234.56"))
    assert config.risk.max_daily_loss_usd == Decimal("1234.56")


def test_int_parser_enforces_maximum() -> None:
    with pytest.raises(ConfigError, match="above the maximum"):
        parse_int({"X": "70000"}, "X", 1, minimum=1, maximum=65535)


def test_version_matches_pyproject() -> None:
    """Keeps app.__version__ and the packaging metadata from drifting apart."""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == __version__


class TestReconnectPolicy:
    def test_backoff_grows_and_is_capped(self) -> None:
        policy = ReconnectPolicy(
            initial_seconds=2.0, max_seconds=60.0, multiplier=2.0, max_attempts=0
        )
        assert policy.delay_for_attempt(1) == 2.0
        assert policy.delay_for_attempt(2) == 4.0
        assert policy.delay_for_attempt(3) == 8.0
        assert policy.delay_for_attempt(20) == 60.0

    def test_attempt_is_one_based(self) -> None:
        with pytest.raises(ValueError, match="1-based"):
            ReconnectPolicy().delay_for_attempt(0)

    def test_max_below_initial_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="must be >="):
            Config.from_env(default_env(RECONNECT_INITIAL_SECONDS="30", RECONNECT_MAX_SECONDS="5"))
