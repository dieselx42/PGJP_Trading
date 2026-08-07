"""Environment-driven configuration.

Design rules (all of them are safety rules):

1. **Strict parsing.** An unparseable value raises :class:`ConfigError`. The
   application then refuses to start. A trading process that cannot understand
   its own configuration must not run.

2. **Safe defaults.** Every absent variable falls back to the most restrictive
   value: mode ``disabled``, transmission off, kill switch on, every risk limit
   zero.

3. **Zero means NOT CONFIGURED.** A risk limit of ``0`` disables the
   corresponding activity. It never means "unlimited".

4. **No half-armed configurations.** ``LIVE_TRADING_ENABLED=true`` while
   ``TRADING_MODE`` is not ``live`` is an error, so that going live always
   requires two deliberate, coordinated edits and can never happen by flipping
   a single variable.

5. **No secrets.** Nothing in this module reads or stores a broker password,
   token, or 2FA material. IB Gateway owns authentication.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from app.enums import TradingMode

TRUE_LITERALS: Final = frozenset({"true", "1", "yes", "on"})
FALSE_LITERALS: Final = frozenset({"false", "0", "no", "off"})

VALID_LOG_LEVELS: Final = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
VALID_LOG_FORMATS: Final = frozenset({"json", "text"})

#: Symbols this build is allowed to reference at all. Anything else is refused
#: at configuration time rather than discovered at order time.
SUPPORTED_FUTURES_SYMBOLS: Final = frozenset({"MSL", "SOL"})


class ConfigError(ValueError):
    """Raised when configuration is missing, malformed, or internally unsafe."""


# ---------------------------------------------------------------------------
# Typed parsers
# ---------------------------------------------------------------------------


def _raw(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Parse a boolean flag.

    Unrecognised text raises. It is never coerced to ``False`` "to be safe":
    the operator asked for something we did not understand, and the correct
    response is to stop, not to guess.
    """
    value = _raw(env, name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in TRUE_LITERALS:
        return True
    if lowered in FALSE_LITERALS:
        return False
    raise ConfigError(
        f"{name}={value!r} is not a boolean; "
        f"use one of {sorted(TRUE_LITERALS)} or {sorted(FALSE_LITERALS)}"
    )


def parse_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    value = _raw(env, name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name}={value!r} is not an integer") from exc
    if parsed < minimum:
        raise ConfigError(f"{name}={parsed} is below the minimum of {minimum}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{name}={parsed} is above the maximum of {maximum}")
    return parsed


def parse_decimal(
    env: Mapping[str, str],
    name: str,
    default: Decimal,
    *,
    minimum: Decimal = Decimal("0"),
) -> Decimal:
    value = _raw(env, name)
    if value is None:
        return default
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"{name}={value!r} is not a decimal number") from exc
    if not parsed.is_finite():
        raise ConfigError(f"{name}={value!r} is not finite")
    if parsed < minimum:
        raise ConfigError(f"{name}={parsed} is below the minimum of {minimum}")
    return parsed


def parse_float(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
) -> float:
    value = _raw(env, name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name}={value!r} is not a number") from exc
    if parsed != parsed or parsed in (float("inf"), float("-inf")):  # NaN / inf
        raise ConfigError(f"{name}={value!r} is not finite")
    if parsed < minimum:
        raise ConfigError(f"{name}={parsed} is below the minimum of {minimum}")
    return parsed


def parse_str(env: Mapping[str, str], name: str, default: str) -> str:
    value = _raw(env, name)
    return default if value is None else value


# ---------------------------------------------------------------------------
# Configuration sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Hard numeric ceilings.

    Every field is "not configured" when zero, which the risk manager turns
    into an explicit rejection reason rather than an allowance.
    """

    max_position_contracts: int = 0
    max_order_size: int = 0
    max_daily_loss_usd: Decimal = Decimal("0")
    max_orders_per_hour: int = 0
    max_open_orders: int = 0
    max_notional_exposure_usd: Decimal = Decimal("0")

    @property
    def any_configured(self) -> bool:
        return any(
            (
                self.max_position_contracts,
                self.max_order_size,
                self.max_daily_loss_usd,
                self.max_orders_per_hour,
                self.max_open_orders,
                self.max_notional_exposure_usd,
            )
        )

    @property
    def all_configured(self) -> bool:
        return all(
            (
                self.max_position_contracts > 0,
                self.max_order_size > 0,
                self.max_daily_loss_usd > 0,
                self.max_orders_per_hour > 0,
                self.max_open_orders > 0,
                self.max_notional_exposure_usd > 0,
            )
        )

    def as_dict(self) -> dict[str, str | int]:
        return {
            "MAX_POSITION_CONTRACTS": self.max_position_contracts,
            "MAX_ORDER_SIZE": self.max_order_size,
            "MAX_DAILY_LOSS_USD": str(self.max_daily_loss_usd),
            "MAX_ORDERS_PER_HOUR": self.max_orders_per_hour,
            "MAX_OPEN_ORDERS": self.max_open_orders,
            "MAX_NOTIONAL_EXPOSURE_USD": str(self.max_notional_exposure_usd),
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RiskLimits:
        return cls(
            max_position_contracts=parse_int(env, "MAX_POSITION_CONTRACTS", 0),
            max_order_size=parse_int(env, "MAX_ORDER_SIZE", 0),
            max_daily_loss_usd=parse_decimal(env, "MAX_DAILY_LOSS_USD", Decimal("0")),
            max_orders_per_hour=parse_int(env, "MAX_ORDERS_PER_HOUR", 0),
            max_open_orders=parse_int(env, "MAX_OPEN_ORDERS", 0),
            max_notional_exposure_usd=parse_decimal(env, "MAX_NOTIONAL_EXPOSURE_USD", Decimal("0")),
        )


@dataclass(frozen=True, slots=True)
class IBKRConfig:
    host: str = "127.0.0.1"
    paper_port: int = 4002
    live_port: int = 4001
    client_id: int = 10
    admin_client_id: int = 110
    connect_timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> IBKRConfig:
        host = parse_str(env, "IB_HOST", "127.0.0.1")
        paper_port = parse_int(env, "IB_PAPER_PORT", 4002, minimum=1, maximum=65535)
        live_port = parse_int(env, "IB_LIVE_PORT", 4001, minimum=1, maximum=65535)
        if paper_port == live_port:
            # If the two ports were equal, "connect to paper" and "connect to
            # live" would be the same action and the mode/account cross-check
            # would be the only thing standing between us and the wrong
            # account. Refuse the ambiguity outright.
            raise ConfigError(f"IB_PAPER_PORT and IB_LIVE_PORT must differ (both are {paper_port})")
        client_id = parse_int(env, "IB_CLIENT_ID", 10, minimum=0, maximum=2**31 - 1)
        admin_client_id = parse_int(env, "IB_ADMIN_CLIENT_ID", 110, minimum=0, maximum=2**31 - 1)
        if client_id == admin_client_id:
            raise ConfigError(
                "IB_CLIENT_ID and IB_ADMIN_CLIENT_ID must differ; a duplicate client id "
                "would disconnect the running trading process"
            )
        return cls(
            host=host,
            paper_port=paper_port,
            live_port=live_port,
            client_id=client_id,
            admin_client_id=admin_client_id,
            connect_timeout_seconds=parse_float(
                env, "IB_CONNECT_TIMEOUT_SECONDS", 15.0, minimum=1.0
            ),
        )


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Exponential backoff with a hard ceiling.

    ``max_attempts == 0`` means "retry indefinitely, at the capped interval".
    That is safe because a disconnected broker forces SAFE state and refuses
    orders; it is not a path to hammering IBKR, because the interval grows to
    ``max_seconds`` and stays there.
    """

    initial_seconds: float = 2.0
    max_seconds: float = 300.0
    multiplier: float = 2.0
    max_attempts: int = 0

    def delay_for_attempt(self, attempt: int) -> float:
        """Backoff delay for a 1-based attempt number."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        delay = self.initial_seconds * (self.multiplier ** (attempt - 1))
        return min(delay, self.max_seconds)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ReconnectPolicy:
        initial = parse_float(env, "RECONNECT_INITIAL_SECONDS", 2.0, minimum=0.1)
        maximum = parse_float(env, "RECONNECT_MAX_SECONDS", 300.0, minimum=0.1)
        if maximum < initial:
            raise ConfigError(
                f"RECONNECT_MAX_SECONDS ({maximum}) must be >= "
                f"RECONNECT_INITIAL_SECONDS ({initial})"
            )
        return cls(
            initial_seconds=initial,
            max_seconds=maximum,
            multiplier=parse_float(env, "RECONNECT_MULTIPLIER", 2.0, minimum=1.0),
            max_attempts=parse_int(env, "RECONNECT_MAX_ATTEMPTS", 0),
        )


# ---------------------------------------------------------------------------
# Root configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Config:
    """Complete, validated application configuration."""

    app_env: str = "production"

    # -- safety interlocks ------------------------------------------------
    trading_mode: TradingMode = TradingMode.DISABLED
    allow_order_transmit: bool = False
    live_trading_enabled: bool = False
    kill_switch: bool = True
    sol_futures_permission_ready: bool = False

    # -- broker -----------------------------------------------------------
    ibkr: IBKRConfig = field(default_factory=IBKRConfig)
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)

    # -- instrument -------------------------------------------------------
    default_futures_symbol: str = "MSL"
    default_exchange: str = "CME"
    default_currency: str = "USD"
    default_contract_month: str | None = None

    # -- risk -------------------------------------------------------------
    risk: RiskLimits = field(default_factory=RiskLimits)
    market_data_max_age_seconds: float = 0.0
    market_data_poll_interval_seconds: float = 1.0

    # -- strategy ---------------------------------------------------------
    strategy_name: str = "noop"
    strategy_enabled: bool = True

    # -- storage ----------------------------------------------------------
    database_path: Path = Path("/app/data/trading.db")
    log_dir: Path = Path("/app/logs")
    log_level: str = "INFO"
    log_format: str = "json"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 10

    # -- local health service --------------------------------------------
    health_host: str = "127.0.0.1"
    health_port: int = 8787

    # -- build metadata ---------------------------------------------------
    git_commit: str = "unknown"
    build_timestamp: str = "unknown"

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def ib_port(self) -> int | None:
        """The one port this configuration is permitted to dial.

        In paper mode the live port is not merely discouraged, it is
        unreachable through this property; nothing in the application dials a
        port chosen any other way.
        """
        if self.trading_mode is TradingMode.PAPER:
            return self.ibkr.paper_port
        if self.trading_mode is TradingMode.LIVE:
            return self.ibkr.live_port
        return None

    @property
    def uses_ibkr(self) -> bool:
        return self.trading_mode.uses_ibkr

    def redacted(self) -> dict[str, object]:
        """Configuration snapshot safe to log or serve on /status.

        Contains no credentials because the application holds none; host and
        port are included because they are operational facts, not secrets, and
        the ports are never publicly reachable.
        """
        return {
            "APP_ENV": self.app_env,
            "TRADING_MODE": self.trading_mode.value,
            "ALLOW_ORDER_TRANSMIT": self.allow_order_transmit,
            "LIVE_TRADING_ENABLED": self.live_trading_enabled,
            "KILL_SWITCH": self.kill_switch,
            "SOL_FUTURES_PERMISSION_READY": self.sol_futures_permission_ready,
            "IB_HOST": self.ibkr.host,
            "IB_PORT_IN_USE": self.ib_port,
            "IB_CLIENT_ID": self.ibkr.client_id,
            "DEFAULT_FUTURES_SYMBOL": self.default_futures_symbol,
            "DEFAULT_EXCHANGE": self.default_exchange,
            "DEFAULT_CURRENCY": self.default_currency,
            "DEFAULT_CONTRACT_MONTH": self.default_contract_month,
            "MARKET_DATA_MAX_AGE_SECONDS": self.market_data_max_age_seconds,
            "STRATEGY_NAME": self.strategy_name,
            "STRATEGY_ENABLED": self.strategy_enabled,
            "DATABASE_PATH": str(self.database_path),
            "LOG_DIR": str(self.log_dir),
            "LOG_LEVEL": self.log_level,
            **self.risk.as_dict(),
        }

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build and validate configuration from an environment mapping.

        ``env`` is injectable so tests never mutate the real process
        environment.
        """
        source: Mapping[str, str] = os.environ if env is None else env

        try:
            trading_mode = TradingMode.parse(parse_str(source, "TRADING_MODE", "disabled"))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

        symbol = parse_str(source, "DEFAULT_FUTURES_SYMBOL", "MSL").upper()
        if symbol not in SUPPORTED_FUTURES_SYMBOLS:
            raise ConfigError(
                f"DEFAULT_FUTURES_SYMBOL={symbol!r} is not supported; "
                f"expected one of {sorted(SUPPORTED_FUTURES_SYMBOLS)}"
            )

        log_level = parse_str(source, "LOG_LEVEL", "INFO").upper()
        if log_level not in VALID_LOG_LEVELS:
            raise ConfigError(
                f"LOG_LEVEL={log_level!r} is invalid; expected one of {sorted(VALID_LOG_LEVELS)}"
            )

        log_format = parse_str(source, "LOG_FORMAT", "json").lower()
        if log_format not in VALID_LOG_FORMATS:
            raise ConfigError(
                f"LOG_FORMAT={log_format!r} is invalid; expected one of {sorted(VALID_LOG_FORMATS)}"
            )

        contract_month = _raw(source, "DEFAULT_CONTRACT_MONTH")
        if contract_month is not None:
            _validate_contract_month(contract_month)

        config = cls(
            app_env=parse_str(source, "APP_ENV", "production"),
            trading_mode=trading_mode,
            allow_order_transmit=parse_bool(source, "ALLOW_ORDER_TRANSMIT", False),
            live_trading_enabled=parse_bool(source, "LIVE_TRADING_ENABLED", False),
            kill_switch=parse_bool(source, "KILL_SWITCH", True),
            sol_futures_permission_ready=parse_bool(source, "SOL_FUTURES_PERMISSION_READY", False),
            ibkr=IBKRConfig.from_env(source),
            reconnect=ReconnectPolicy.from_env(source),
            default_futures_symbol=symbol,
            default_exchange=parse_str(source, "DEFAULT_EXCHANGE", "CME").upper(),
            default_currency=parse_str(source, "DEFAULT_CURRENCY", "USD").upper(),
            default_contract_month=contract_month,
            risk=RiskLimits.from_env(source),
            market_data_max_age_seconds=parse_float(source, "MARKET_DATA_MAX_AGE_SECONDS", 0.0),
            market_data_poll_interval_seconds=parse_float(
                source, "MARKET_DATA_POLL_INTERVAL_SECONDS", 1.0, minimum=0.05
            ),
            strategy_name=parse_str(source, "STRATEGY_NAME", "noop").lower(),
            strategy_enabled=parse_bool(source, "STRATEGY_ENABLED", True),
            database_path=Path(parse_str(source, "DATABASE_PATH", "/app/data/trading.db")),
            log_dir=Path(parse_str(source, "LOG_DIR", "/app/logs")),
            log_level=log_level,
            log_format=log_format,
            log_max_bytes=parse_int(source, "LOG_MAX_BYTES", 10 * 1024 * 1024, minimum=1024),
            log_backup_count=parse_int(source, "LOG_BACKUP_COUNT", 10, minimum=1),
            health_host=parse_str(source, "HEALTH_HOST", "127.0.0.1"),
            # 0 is permitted and means "let the OS pick an ephemeral port".
            # Only useful for tests; the health service is loopback-only either way.
            health_port=parse_int(source, "HEALTH_PORT", 8787, minimum=0, maximum=65535),
            git_commit=parse_str(source, "GIT_COMMIT", "unknown"),
            build_timestamp=parse_str(source, "BUILD_TIMESTAMP", "unknown"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        """Cross-field validation. Raises :class:`ConfigError` on any unsafe combination."""
        # A configuration that is one variable away from live trading is
        # itself a hazard. Refuse to run in that state.
        if self.live_trading_enabled and self.trading_mode is not TradingMode.LIVE:
            raise ConfigError(
                "LIVE_TRADING_ENABLED=true requires TRADING_MODE=live. "
                "Refusing to run a half-armed configuration."
            )

        if self.trading_mode is TradingMode.LIVE and not self.live_trading_enabled:
            raise ConfigError(
                "TRADING_MODE=live requires LIVE_TRADING_ENABLED=true. "
                "Refusing to run a half-armed configuration."
            )

        if self.uses_ibkr and not self.sol_futures_permission_ready:
            # Not fatal at startup, because read-only IBKR verification work is
            # exactly how the operator establishes that permission exists. The
            # transmit gate blocks orders independently; see safety/gate.py.
            pass

        if self.health_host not in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):  # noqa: S104
            # 0.0.0.0 is permitted only so that the private-Docker-network
            # deployment described in SECURITY.md remains possible; it is not
            # the default and must never be combined with a published port.
            raise ConfigError(f"HEALTH_HOST={self.health_host!r} is not an allowed bind address")


def _validate_contract_month(value: str) -> None:
    """Accept an explicit expiry as YYYYMM or YYYYMMDD only."""
    if not value.isdigit() or len(value) not in (6, 8):
        raise ConfigError(
            f"DEFAULT_CONTRACT_MONTH={value!r} must be YYYYMM or YYYYMMDD "
            "(an explicit, dated contract; continuous futures are not tradeable)"
        )
    year = int(value[:4])
    month = int(value[4:6])
    if not 2000 <= year <= 2100:
        raise ConfigError(f"DEFAULT_CONTRACT_MONTH year {year} is out of range")
    if not 1 <= month <= 12:
        raise ConfigError(f"DEFAULT_CONTRACT_MONTH month {month} is out of range")
    if len(value) == 8:
        day = int(value[6:8])
        if not 1 <= day <= 31:
            raise ConfigError(f"DEFAULT_CONTRACT_MONTH day {day} is out of range")


__all__ = [
    "SUPPORTED_FUTURES_SYMBOLS",
    "Config",
    "ConfigError",
    "IBKRConfig",
    "ReconnectPolicy",
    "RiskLimits",
    "parse_bool",
    "parse_decimal",
    "parse_float",
    "parse_int",
    "parse_str",
]
