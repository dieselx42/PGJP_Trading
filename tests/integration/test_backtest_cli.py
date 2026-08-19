"""The `backtest` operator command, end to end through `main`.

The unit tests prove the replay is correct. This file proves the *command* is
honest about what it replayed, which is a different failure mode: a report that
is arithmetically perfect but silently guessed its contract, quietly relaxed a
limit, or produced no trades for a reason it did not mention is worse than no
report, because it will be believed.

So the assertions here are mostly about disclosure:

* the contract came from a stored IBKR qualification and was never invented;
* the limits came from the deployed configuration unless overridden, and the
  output says which;
* a run that produced nothing says *why* it produced nothing;
* proxy provenance survives all the way into the printed result.

Plus the one property every command in this tool has: it changes nothing. A
backtest of a halted server must leave the server halted.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import app.strategy.noop as noop_module
from app.backtest.models import Bar
from app.backtest.store import BarRepository
from app.cli import EXIT_ERROR, EXIT_OK, build_parser, main
from app.contracts.models import QualifiedContract
from app.enums import Direction, SecurityType
from app.market_data.models import Quote
from app.signals.models import TradeIntent
from app.state.database import Database
from app.state.repositories import Repositories
from app.strategy.base import Strategy
from tests.conftest import CONTRACT_MONTH, default_env

pytestmark = pytest.mark.integration

T0 = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)


class _BuyAndHold(Strategy):
    """Registered only inside a test. Asks to be long one contract."""

    name = "test-buy-and-hold"

    def on_quote(self, quote: Quote) -> Sequence[TradeIntent]:
        return (
            TradeIntent(
                strategy_name=self.name,
                symbol="MSL",
                direction=Direction.LONG,
                requested_position=1,
                created_at=quote.received_at,
            ),
        )


@pytest.fixture
def registered_strategy(monkeypatch: pytest.MonkeyPatch) -> type[Strategy]:
    monkeypatch.setitem(noop_module.STRATEGY_REGISTRY, _BuyAndHold.name, _BuyAndHold)
    return _BuyAndHold


@pytest.fixture
def backtest_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A halted deployed configuration, with a contract and bars stored."""
    db_path = tmp_path / "trading.db"
    database = Database(db_path)
    database.connect()
    database.migrate()

    Repositories(database).contracts.upsert(
        QualifiedContract(
            con_id=812345678,
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
    )
    BarRepository(database).insert_many(
        [
            Bar(
                source="binance",
                symbol="SOLUSDT",
                interval="1m",
                opened_at=T0 + timedelta(minutes=i),
                open=Decimal("80") + Decimal(i % 5),
                high=Decimal("86"),
                low=Decimal("79"),
                close=Decimal("80") + Decimal(i % 5),
            )
            for i in range(60)
        ]
    )
    database.close()

    for key, value in default_env(
        DATABASE_PATH=str(db_path),
        LOG_DIR=str(tmp_path / "logs"),
        DEFAULT_CONTRACT_MONTH=CONTRACT_MONTH,
        HEALTH_PORT="0",
    ).items():
        monkeypatch.setenv(key, value)
    return db_path


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code = main(list(argv))
    captured = capsys.readouterr().out
    return code, json.loads(captured) if captured.strip() else {}


#: The deployed configuration has every limit at zero, which means NOT
#: CONFIGURED and therefore prohibited. A replay that is supposed to reach a
#: fill has to supply limits explicitly, exactly as the server would have to.
ARMED = (
    "--max-order-size",
    "1",
    "--max-position",
    "1",
    "--max-orders-per-hour",
    "60",
    "--max-open-orders",
    "2",
    "--max-daily-loss",
    "5000",
    "--max-notional",
    "100000",
)


class TestSurface:
    def test_the_command_is_registered(self) -> None:
        parser = build_parser()
        commands = parser._subparsers._group_actions[0].choices
        assert "backtest" in commands


class TestContractProvenance:
    def test_a_replay_refuses_rather_than_inventing_a_contract(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A guessed multiplier scales every P&L figure and says nothing.

        Refusing is the only honest answer: there is no safe default for a
        contract's tick size, and a fabricated conId would defeat the check
        that exists to make contract identity unambiguous.
        """
        code, payload = _run(capsys, "backtest", "--contract-symbol", "NOSUCH")

        assert code == EXIT_ERROR
        assert payload["result"] == "NO_QUALIFIED_CONTRACT"
        assert "ibkr-checkout" in payload["detail"]

    def test_the_stored_qualification_is_reported_in_full(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, payload = _run(capsys, "backtest")

        assert code == EXIT_OK
        assert payload["contract"]["con_id"] == 812345678
        assert payload["contract"]["multiplier"] == "25"
        assert payload["contract"]["min_tick"] == "0.05"
        assert "not invented" in payload["contract"]["source"]


class TestDisclosure:
    def test_limits_come_from_the_deployed_config_and_say_so(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, payload = _run(capsys, "backtest")

        assert set(payload["limits"]["source"].values()) == {"deployed configuration"}
        # The deployed configuration is halted: zero everywhere.
        assert payload["limits"]["values"]["MAX_ORDER_SIZE"] == 0

    def test_an_unconfigured_deployed_limit_prohibits_a_replay_from_trading(
        self,
        backtest_env: Path,
        registered_strategy: type[Strategy],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Zero means NOT CONFIGURED here exactly as it does live.

        And the result has to *say* that, because "the strategy is flat" and
        "the strategy was never allowed to trade" produce identical numbers and
        mean opposite things.
        """
        code, payload = _run(
            capsys, "backtest", "--strategy", registered_strategy.name, "--session", "all"
        )

        assert code == EXIT_OK
        assert payload["trades"]["count"] == 0
        assert payload["refusals"]["by_reason"]["MAX_ORDER_SIZE_NOT_CONFIGURED"] > 0
        assert "Zero means NOT CONFIGURED, never unlimited" in payload["why_no_trades"]
        assert "does not authorise trading" in payload["why_no_trades"]

    def test_an_overridden_limit_is_marked_as_overridden(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, payload = _run(capsys, "backtest", "--max-order-size", "1")

        assert payload["limits"]["values"]["MAX_ORDER_SIZE"] == 1
        assert payload["limits"]["source"]["MAX_ORDER_SIZE"] == "command line"
        assert payload["limits"]["source"]["MAX_POSITION_CONTRACTS"] == "deployed configuration"

    def test_a_noop_replay_says_why_it_produced_nothing(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Otherwise an empty result reads as "the strategy lost nothing"."""
        _, payload = _run(capsys, "backtest")

        assert payload["strategy"]["name"] == "noop"
        assert payload["trades"]["count"] == 0
        assert "never produces an intent" in payload["why_no_trades"]

    def test_proxy_provenance_survives_into_the_printed_result(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, payload = _run(capsys, "backtest")

        assert payload["data"]["source"] == "binance"
        assert payload["data"]["is_proxy_data"] is True
        assert "NOT CME FUTURES" in payload["limitations"][0]

    def test_the_fill_model_used_is_reported(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, payload = _run(capsys, "backtest", "--slippage-ticks", "4", "--commission", "9.99")

        assert payload["model"]["slippage_ticks"] == 4
        assert payload["model"]["commission_per_contract"] == "9.99"


class TestARealStrategyRuns:
    """Without these, everything above passes on a replay that does nothing."""

    def test_a_trading_strategy_produces_fills_through_the_command(
        self,
        backtest_env: Path,
        registered_strategy: type[Strategy],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code, payload = _run(
            capsys, "backtest", "--strategy", registered_strategy.name, "--session", "all", *ARMED
        )

        assert code == EXIT_OK
        assert payload["trades"]["final_position"] == 1
        assert "why_no_trades" not in payload
        assert payload["performance"]["commission_paid"] != "0"
        assert payload["performance"]["slippage_paid"] != "0"

    def test_one_tightened_limit_stops_it_again(
        self,
        backtest_env: Path,
        registered_strategy: type[Strategy],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The same run, with only the position ceiling lowered to zero."""
        armed = list(ARMED)
        armed[armed.index("--max-position") + 1] = "0"
        code, payload = _run(
            capsys, "backtest", "--strategy", registered_strategy.name, "--session", "all", *armed
        )

        assert code == EXIT_OK
        assert payload["trades"]["count"] == 0
        assert payload["trades"]["final_position"] == 0
        assert payload["refusals"]["by_reason"]["MAX_POSITION_CONTRACTS_NOT_CONFIGURED"] > 0


class TestSwitches:
    def test_by_default_a_halted_server_can_still_answer_the_question(
        self,
        backtest_env: Path,
        registered_strategy: type[Strategy],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _, payload = _run(
            capsys, "backtest", "--strategy", registered_strategy.name, "--session", "all", *ARMED
        )

        assert payload["switches"]["inherited_from_deployed_config"] is False
        assert payload["switches"]["kill_switch"] is False
        assert payload["trades"]["final_position"] == 1

    def test_inherited_switches_make_the_interlocks_bite(
        self,
        backtest_env: Path,
        registered_strategy: type[Strategy],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The deployed configuration is halted, so the replay trades nothing."""
        _, payload = _run(
            capsys,
            "backtest",
            "--strategy",
            registered_strategy.name,
            "--session",
            "all",
            "--inherit-switches",
            *ARMED,
        )

        assert payload["switches"]["inherited_from_deployed_config"] is True
        assert payload["switches"]["kill_switch"] is True
        assert payload["trades"]["count"] == 0
        assert payload["refusals"]["by_reason"]["KILL_SWITCH_ENGAGED"] > 0
        assert payload["refusals"]["by_reason"]["ORDER_TRANSMIT_NOT_ALLOWED"] > 0

    def test_a_backtest_never_changes_the_deployed_kill_switch(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, "backtest", "--inherit-switches")

        code, payload = _run(capsys, "kill-switch-status")
        assert code == EXIT_OK
        assert payload["config_engaged"] is True
        assert payload["safety"]["can_transmit_live_orders"] is False


class TestBadInput:
    def test_missing_bars_is_reported_not_replayed_as_an_empty_year(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, payload = _run(capsys, "backtest", "--symbol", "NOTHING")

        assert code == EXIT_ERROR
        assert payload["result"] == "NO_BARS"
        assert "bars-import" in payload["detail"]

    def test_an_unregistered_strategy_is_refused(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, payload = _run(capsys, "backtest", "--strategy", "does-not-exist")

        assert code == EXIT_ERROR
        assert payload["result"] == "UNKNOWN_STRATEGY"

    def test_an_unparseable_commission_is_refused(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, payload = _run(capsys, "backtest", "--commission", "free")

        assert code == EXIT_ERROR
        assert payload["result"] == "INVALID_COMMISSION"

    def test_a_negative_cost_is_refused(
        self, backtest_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A fill model that pays a strategy to trade is not a pessimistic one."""
        code, payload = _run(capsys, "backtest", "--slippage-ticks", "-2")

        assert code == EXIT_ERROR
        assert payload["result"] == "INVALID_FILL_MODEL"
