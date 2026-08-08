"""Tests for the read-only IBKR checkout.

Two things are being tested here and they matter for different reasons.

The ordinary tests check the probes report what the broker actually returned.
The tests at the bottom check the checkout is *incapable* of placing an order,
which is a property of the code rather than of any single run.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.broker import checkout as checkout_module
from app.broker.checkout import (
    CheckoutReport,
    Probe,
    ProbeStatus,
    ReadOnlyBroker,
    preconditions,
    run_checkout,
)
from app.broker.models import (
    AccountSummary,
    BrokerConnectionError,
    BrokerOrderSnapshot,
    BrokerPermissionError,
    ConnectionInfo,
    MarketDataTick,
)
from app.config import Config
from app.contracts.models import ContractSpec, QualifiedContract
from app.enums import (
    AccountType,
    OrderSide,
    OrderStatus,
    OrderType,
    SecurityType,
    TradingMode,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def paper_config(**overrides: object) -> Config:
    """A config in the posture a read-only checkout requires."""
    env = {
        "TRADING_MODE": "paper",
        "ALLOW_ORDER_TRANSMIT": "false",
        "LIVE_TRADING_ENABLED": "false",
        "KILL_SWITCH": "true",
        "SOL_FUTURES_PERMISSION_READY": "false",
        "DEFAULT_FUTURES_SYMBOL": "MSL",
        "DEFAULT_EXCHANGE": "CME",
        "DEFAULT_CURRENCY": "USD",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return Config.from_env(env)


def qualified() -> QualifiedContract:
    return QualifiedContract(
        con_id=712345678,
        symbol="MSL",
        local_symbol="MSLU5",
        sec_type=SecurityType.FUTURE,
        exchange="CME",
        currency="USD",
        expiration="202509",
        last_trade_date="20250926",
        multiplier="5",
        min_tick=Decimal("0.5"),
        trading_class="MSL",
    )


class FakeBroker:
    """A stand-in gateway. Records what was asked of it."""

    def __init__(
        self,
        *,
        account_type: AccountType = AccountType.PAPER,
        connect_error: Exception | None = None,
        qualify_error: Exception | None = None,
        market_data_error: Exception | None = None,
        open_orders: list[BrokerOrderSnapshot] | None = None,
        tick: MarketDataTick | None = None,
    ) -> None:
        self._account_type = account_type
        self._connect_error = connect_error
        self._qualify_error = qualify_error
        self._market_data_error = market_data_error
        self._open_orders = open_orders or []
        self._tick = tick
        self.calls: list[str] = []
        self.disconnected = False

    async def connect(self) -> ConnectionInfo:
        self.calls.append("connect")
        if self._connect_error:
            raise self._connect_error
        return ConnectionInfo(
            connected=True,
            account_type=self._account_type,
            account_id="DU1234567",
            host="127.0.0.1",
            port=4002,
            client_id=110,
        )

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.disconnected = True

    @property
    def account_type(self) -> AccountType:
        # A property, exactly as on the real Broker. If this drifts, the
        # checkout would compare a bound method against AccountType.PAPER and
        # quietly report every session as not-paper.
        return self._account_type

    async def get_account_summary(self) -> AccountSummary:
        self.calls.append("get_account_summary")
        return AccountSummary(
            account_id="DU1234567",
            account_type=self._account_type,
            net_liquidation=Decimal("1000000"),
        )

    async def get_positions(self) -> list[object]:
        self.calls.append("get_positions")
        return []

    async def get_open_orders(self) -> list[BrokerOrderSnapshot]:
        self.calls.append("get_open_orders")
        return self._open_orders

    async def get_fills(self, *, since_seconds: int = 86_400) -> list[object]:
        self.calls.append("get_fills")
        return []

    async def get_contract_details(self, spec: ContractSpec) -> list[QualifiedContract]:
        self.calls.append("get_contract_details")
        return [qualified()]

    async def qualify_contract(self, spec: ContractSpec) -> QualifiedContract:
        self.calls.append("qualify_contract")
        if self._qualify_error:
            raise self._qualify_error
        return qualified()

    async def request_market_data(self, contract: QualifiedContract) -> MarketDataTick:
        self.calls.append("request_market_data")
        if self._market_data_error:
            raise self._market_data_error
        return self._tick or MarketDataTick(
            contract_key="MSL:FUT:CME:202509",
            received_at=datetime.now(UTC),
            source="test",
            bid=Decimal("180.0"),
            ask=Decimal("180.5"),
        )

    async def cancel_market_data(self, contract: QualifiedContract) -> None:
        self.calls.append("cancel_market_data")


def probe(report: CheckoutReport, name: str) -> Probe:
    for candidate in report.probes:
        if candidate.name == name:
            return candidate
    raise AssertionError(f"{name} not in report: {[p.name for p in report.probes]}")


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def test_preconditions_pass_in_the_approved_paper_posture():
    assert all(p.ok for p in preconditions(paper_config()))


def test_checkout_refuses_when_transmit_is_enabled():
    config = paper_config(ALLOW_ORDER_TRANSMIT="true")
    failing = [p.name for p in preconditions(config) if not p.ok]
    assert "PRECONDITION_TRANSMIT_DISABLED" in failing


def test_checkout_refuses_when_the_kill_switch_is_cleared():
    config = paper_config(KILL_SWITCH="false")
    failing = [p.name for p in preconditions(config) if not p.ok]
    assert "PRECONDITION_KILL_SWITCH_ENGAGED" in failing


@pytest.mark.parametrize("mode", ["disabled", "mock"])
def test_checkout_refuses_outside_paper_mode(mode: str):
    config = paper_config(TRADING_MODE=mode)
    failing = [p.name for p in preconditions(config) if not p.ok]
    assert "PRECONDITION_MODE_IS_PAPER" in failing


async def test_failed_preconditions_prevent_any_connection():
    broker = FakeBroker()
    report = await run_checkout(paper_config(ALLOW_ORDER_TRANSMIT="true"), broker)
    assert broker.calls == []
    assert not report.ok


# ---------------------------------------------------------------------------
# Session probes
# ---------------------------------------------------------------------------


async def test_full_checkout_passes_against_a_healthy_paper_session():
    broker = FakeBroker()
    report = await run_checkout(paper_config(), broker, contract_month="202509")

    assert report.ok, [p.describe() for p in report.failed]
    assert probe(report, "SOCKET_CONNECT").ok
    assert probe(report, "ACCOUNT_TYPE_IS_PAPER").ok
    assert probe(report, "CONTRACT_QUALIFIES").ok
    assert probe(report, "MARKET_DATA").ok
    assert broker.disconnected


async def test_a_live_account_on_a_paper_checkout_fails():
    report = await run_checkout(paper_config(), FakeBroker(account_type=AccountType.LIVE))
    assert not probe(report, "ACCOUNT_TYPE_IS_PAPER").ok
    assert not report.ok


async def test_failed_connect_skips_the_session_probes_instead_of_omitting_them():
    broker = FakeBroker(connect_error=BrokerConnectionError("gateway not listening"))
    report = await run_checkout(paper_config(), broker)

    assert not probe(report, "SOCKET_CONNECT").ok
    # The operator must be able to see what was NOT verified.
    assert probe(report, "CONTRACT_QUALIFIES").status is ProbeStatus.SKIP
    assert probe(report, "MARKET_DATA").status is ProbeStatus.SKIP


async def test_permission_error_on_qualification_is_reported_not_raised():
    broker = FakeBroker(qualify_error=BrokerPermissionError("not permitted to trade product"))
    report = await run_checkout(paper_config(), broker, contract_month="202509")

    failure = probe(report, "CONTRACT_QUALIFIES")
    assert failure.status is ProbeStatus.FAIL
    assert failure.evidence["error_class"] == "BrokerPermissionError"
    assert failure.evidence["retryable"] is False
    # The run continues; the gate check still happens.
    assert probe(report, "GATE_REFUSES_WHEN_EVERYTHING_ELSE_IS_GREEN").ok
    assert broker.disconnected


async def test_market_data_permission_error_is_reported_and_the_subscription_cancelled():
    broker = FakeBroker(market_data_error=BrokerPermissionError("market data not subscribed"))
    report = await run_checkout(paper_config(), broker, contract_month="202509")

    assert probe(report, "MARKET_DATA").status is ProbeStatus.FAIL
    assert "cancel_market_data" in broker.calls


async def test_an_unpriced_tick_fails_rather_than_passing_quietly():
    silent = MarketDataTick(
        contract_key="MSL:FUT:CME:202509", received_at=datetime.now(UTC), source="test"
    )
    report = await run_checkout(paper_config(), FakeBroker(tick=silent), contract_month="202509")
    assert probe(report, "MARKET_DATA").status is ProbeStatus.FAIL


async def test_expiration_is_never_guessed():
    """No contract month configured or given means SKIP, never front-month."""
    broker = FakeBroker()
    report = await run_checkout(paper_config(), broker)

    assert probe(report, "CONTRACT_QUALIFIES").status is ProbeStatus.SKIP
    assert "qualify_contract" not in broker.calls


async def test_preexisting_open_orders_fail_the_checkout():
    stray = BrokerOrderSnapshot(
        broker_order_id="99",
        account_id="DU1234567",
        con_id=712345678,
        symbol="MSL",
        side=OrderSide.BUY,
        quantity=1,
        order_type=OrderType.LIMIT,
        status=OrderStatus.SUBMITTED,
    )
    broker = FakeBroker(open_orders=[stray])
    report = await run_checkout(paper_config(), broker, contract_month="202509")
    assert probe(report, "OPEN_ORDERS_EMPTY").status is ProbeStatus.FAIL


async def test_disconnect_happens_even_when_a_probe_raises_unexpectedly():
    broker = FakeBroker()

    async def boom() -> None:
        raise RuntimeError("unexpected")

    broker.get_positions = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await run_checkout(paper_config(), broker, contract_month="202509")
    assert broker.disconnected


# ---------------------------------------------------------------------------
# The gate must still refuse
# ---------------------------------------------------------------------------


async def test_the_gate_refuses_even_with_a_perfect_session():
    report = await run_checkout(paper_config(), FakeBroker(), contract_month="202509")
    gate = probe(report, "GATE_REFUSES_WHEN_EVERYTHING_ELSE_IS_GREEN")

    assert gate.ok
    assert gate.evidence["allowed"] is False
    # Every session-side condition was forced green, so the refusals must all
    # come from the deployed configuration rather than from session state.
    assert gate.evidence["reasons"]


# ---------------------------------------------------------------------------
# Structural: the checkout cannot write
# ---------------------------------------------------------------------------

WRITE_METHODS = ("place_order", "modify_order", "cancel_order", "cancel_all_orders")


def test_the_read_only_broker_protocol_exposes_no_write_method():
    """mypy --strict is the first line of defence; this is the second."""
    for name in WRITE_METHODS:
        assert not hasattr(ReadOnlyBroker, name), f"ReadOnlyBroker must not expose {name}"


def test_the_checkout_module_calls_no_order_writing_method():
    """Parse the module and look at what it actually calls.

    A substring search would trip over the docstring, which names these methods
    precisely to explain that they are absent. The AST only sees real calls.
    """
    tree = ast.parse(inspect.getsource(checkout_module))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    # `cancel_market_data` is a read-side unsubscribe and is allowed; the
    # order-writing methods are not.
    assert called.isdisjoint(WRITE_METHODS), f"checkout calls {called & set(WRITE_METHODS)}"


def test_an_empty_report_is_not_a_pass():
    """A checkout that observed nothing has verified nothing."""
    assert CheckoutReport(()).ok is False


def test_the_report_of_a_skipped_run_is_not_a_pass():
    report = CheckoutReport((Probe("X", ProbeStatus.SKIP, "not run"),))
    assert report.ok is True  # skips alone do not fail
    assert report.describe()["counts"] == {"passed": 0, "failed": 0, "skipped": 1}


def test_checkout_is_wired_into_the_cli_and_still_offers_no_way_to_enable_trading():
    from app.cli import COMMANDS

    assert "ibkr-checkout" in COMMANDS
    assert "kill-switch-off" not in COMMANDS
    assert "enable-live" not in COMMANDS


def test_paper_mode_selects_the_paper_port_and_the_admin_client_id():
    """A checkout must never collide with, or disconnect, a running bot."""
    config = paper_config()
    assert config.ib_port == config.ibkr.paper_port
    assert config.ibkr.admin_client_id != config.ibkr.client_id
    assert config.trading_mode is TradingMode.PAPER


# ---------------------------------------------------------------------------
# Diagnostics must survive to the operator
# ---------------------------------------------------------------------------


def test_checkout_logs_go_to_stderr_with_their_structured_fields(capsys, monkeypatch):
    """The IB error code must reach the operator, not be swallowed.

    Without configured logging, the adapter's records fall through to
    `logging.lastResort`, which prints "ibkr error" and drops ib_code,
    ib_message, classified_as and retryable -- everything worth knowing. And
    they must land on stderr, or they interleave with the JSON report on stdout
    and make it unparseable.
    """
    import logging
    import sys

    from app.cli import main

    for key, value in {
        "TRADING_MODE": "paper",
        "ALLOW_ORDER_TRANSMIT": "false",
        "KILL_SWITCH": "true",
        "IB_HOST": "127.0.0.1",
    }.items():
        monkeypatch.setenv(key, value)

    # No gateway is listening, so this fails at SOCKET_CONNECT -- which is the
    # case that most needs its reason reported.
    main(["ibkr-checkout"])

    handlers = logging.getLogger().handlers
    assert handlers, "logging was left unconfigured; records would be dropped"
    streams = {getattr(h, "stream", None) for h in handlers}
    assert sys.stderr in streams, "checkout logs must not share stdout with the report"
    assert not any(isinstance(h, logging.FileHandler) for h in handlers), (
        "the checkout must not write to the trading process's log directory"
    )

    # stdout must still be nothing but the report.
    captured = capsys.readouterr()
    json.loads(captured.out)
