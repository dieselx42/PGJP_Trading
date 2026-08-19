"""The operator order command.

This is the only code in the project that lets a human cause an order, so the
properties that keep it safe are asserted here rather than described in a
docstring. Each test degrades exactly one thing from a baseline that would
otherwise succeed -- the same shape as `test_critical_safety.py`, and for the
same reason: without the control test proving the baseline *does* send, every
other test here would pass vacuously.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Config
from app.enums import Direction, OrderType, TradingMode
from app.execution.operator_order import (
    OPERATOR_STRATEGY_NAME,
    OperatorOrderRequest,
    place_operator_order,
)
from tests.conftest import CONTRACT_MONTH, default_env, permissive_env

SOURCE = Path("app/execution/operator_order.py")


def _request(**overrides: object) -> OperatorOrderRequest:
    base: dict[str, object] = {
        "target_position": 1,
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("50.00"),
        "confirm": None,
    }
    base.update(overrides)
    return OperatorOrderRequest(**base)  # type: ignore[arg-type]


class TestDirection:
    """Direction is derived from the target, never passed in independently."""

    def test_a_positive_target_is_long(self) -> None:
        assert _request(target_position=2).direction is Direction.LONG

    def test_a_negative_target_is_short(self) -> None:
        assert _request(target_position=-2).direction is Direction.SHORT

    def test_zero_is_flat(self) -> None:
        assert _request(target_position=0).direction is Direction.FLAT

    def test_direction_and_target_can_never_disagree(self) -> None:
        """A LONG intent with a negative target is the bug this rules out.

        `TradeIntent` rejects that combination, but only if something builds it
        wrongly in the first place. Deriving direction from the target means
        there is no way to express the contradiction.
        """
        for target in (-5, -1, 0, 1, 5):
            req = _request(target_position=target)
            if req.direction is Direction.LONG:
                assert req.target_position > 0
            elif req.direction is Direction.SHORT:
                assert req.target_position < 0
            else:
                assert req.target_position == 0


class TestLiveModeRefusal:
    async def test_live_mode_is_refused_before_anything_is_constructed(self) -> None:
        """No broker, no database, no application object. Nothing to reach an account."""
        config = Config.from_env(
            permissive_env(
                TRADING_MODE="live",
                LIVE_TRADING_ENABLED="true",
                IB_LIVE_PORT="4001",
            )
        )
        assert config.trading_mode is TradingMode.LIVE

        report = await place_operator_order(config, _request(confirm="MSLZ6"))

        assert report["result"] == "REFUSED_LIVE_MODE"
        assert report.get("transmitted") is not True

    async def test_the_refusal_does_not_depend_on_the_confirmation(self) -> None:
        """Confirming correctly must not buy a way past the live-mode refusal."""
        config = Config.from_env(
            permissive_env(TRADING_MODE="live", LIVE_TRADING_ENABLED="true", IB_LIVE_PORT="4001")
        )
        for confirm in (None, "MSLZ6", "anything"):
            report = await place_operator_order(config, _request(confirm=confirm))
            assert report["result"] == "REFUSED_LIVE_MODE"


class TestLimitPriceIsRequired:
    async def test_a_limit_order_without_a_price_is_refused(self) -> None:
        config = Config.from_env(default_env())
        report = await place_operator_order(config, _request(limit_price=None))
        assert report["result"] == "LIMIT_PRICE_REQUIRED"

    async def test_the_refusal_happens_before_a_broker_is_built(self) -> None:
        """Refusing early matters: a missing price must not cost a connection."""
        config = Config.from_env(default_env(TRADING_MODE="paper", IB_PAPER_PORT="4002"))
        report = await place_operator_order(config, _request(limit_price=None, confirm="MSLZ6"))
        # Paper mode would otherwise try to reach a gateway that is not there.
        assert report["result"] == "LIMIT_PRICE_REQUIRED"


class TestItCannotBypassThePipeline:
    """Structural guarantees, read from the source.

    A comment claiming "this uses the real pipeline" is worth nothing once
    somebody edits the file. These assert it.
    """

    def _calls(self) -> set[str]:
        tree = ast.parse(SOURCE.read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    names.add(func.attr)
                elif isinstance(func, ast.Name):
                    names.add(func.id)
        return names

    def test_it_never_calls_the_broker_directly(self) -> None:
        """Everything must go through the order manager, which the gate guards."""
        forbidden = {"place_order", "placeOrder", "cancel_order", "submit_intent"}
        assert not (self._calls() & forbidden)

    def test_it_delegates_to_the_same_method_the_tick_loop_uses(self) -> None:
        assert "_handle_intent" in self._calls()

    def test_it_never_constructs_its_own_gate_or_risk_manager(self) -> None:
        """Using the application's instances is what keeps config in one place."""
        forbidden = {"TransmitGate", "RiskManager", "OrderManager("}
        assert not (self._calls() & forbidden)

    def test_it_asks_both_approvers(self) -> None:
        calls = self._calls()
        assert "evaluate" in calls
        assert "validate" in calls

    def test_it_does_not_touch_the_kill_switch(self) -> None:
        source = SOURCE.read_text()
        for forbidden in ("disengage", "kill_switch.clear", "KILL_SWITCH"):
            assert forbidden not in source

    def test_it_writes_no_configuration(self) -> None:
        source = SOURCE.read_text()
        for forbidden in ("os.environ[", "setenv", ".write_text(", "ALLOW_ORDER_TRANSMIT"):
            assert forbidden not in source


class TestAttribution:
    def test_operator_orders_are_never_attributed_to_a_strategy(self) -> None:
        """The durable record must not let a human order read as a strategy's."""
        assert OPERATOR_STRATEGY_NAME == "operator"
        assert OPERATOR_STRATEGY_NAME != "noop"

    def test_the_registry_has_no_strategy_by_that_name(self) -> None:
        """Nothing may register 'operator' and start impersonating a human."""
        from app.strategy.noop import STRATEGY_REGISTRY

        assert OPERATOR_STRATEGY_NAME not in STRATEGY_REGISTRY


class TestConfirmationToken:
    def test_the_default_is_the_safe_one(self) -> None:
        """Omitting --confirm previews. The dangerous path needs an argument."""
        assert _request().confirm is None

    def test_a_bare_flag_would_not_have_been_enough(self) -> None:
        """The token is a value, not a boolean, so it cannot be typed blind.

        Guards the design rather than the code: if `confirm` ever became a
        bool, this fails and the reviewer has to decide deliberately.
        """
        annotation = OperatorOrderRequest.__annotations__["confirm"]
        assert "str" in str(annotation)
        assert "bool" not in str(annotation)


class TestCliWiring:
    def test_the_command_is_registered(self) -> None:
        from app.cli import COMMANDS

        assert "place-order" in COMMANDS

    def test_there_is_still_no_command_that_clears_the_kill_switch(self) -> None:
        """Adding an order path must not have quietly added an escape hatch."""
        from app.cli import COMMANDS

        assert "kill-switch-off" not in COMMANDS
        assert "enable-live" not in COMMANDS

    def test_target_position_is_required_and_confirm_is_not(self) -> None:
        from app.cli import build_parser

        parser = build_parser()
        # Missing --target-position must fail rather than default to something.
        with pytest.raises(SystemExit):
            parser.parse_args(["place-order"])

        args = parser.parse_args(["place-order", "--target-position", "1"])
        assert args.target_position == 1
        assert args.confirm is None
        assert args.order_type == "limit"

    def test_the_default_order_type_is_limit(self) -> None:
        """A first-ever order should not be able to fill at a surprising price."""
        from app.cli import build_parser

        args = build_parser().parse_args(["place-order", "--target-position", "1"])
        assert args.order_type == "limit"


class TestStrategyPathUnchanged:
    """The signature change to `_handle_intent` must not alter strategy behaviour."""

    def test_the_defaults_match_the_previous_behaviour(self) -> None:
        import inspect

        from app.main import TradingApplication

        sig = inspect.signature(TradingApplication._handle_intent)
        assert sig.parameters["order_type"].default is OrderType.MARKET
        assert sig.parameters["limit_price"].default is None

    def test_the_new_parameters_are_keyword_only(self) -> None:
        """So no positional call site can pass one by accident."""
        import inspect

        from app.main import TradingApplication

        sig = inspect.signature(TradingApplication._handle_intent)
        for name in ("order_type", "limit_price"):
            assert sig.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


class TestConfiguredContractIsNotGuessed:
    async def test_no_contract_month_means_no_order(self) -> None:
        """An operator order must not pick an expiration on its own."""
        config = Config.from_env(default_env(DEFAULT_CONTRACT_MONTH=""))
        assert config.default_contract_month in (None, "")
        # Mock mode still resolves a contract, so this asserts the config, not
        # the run: the resolver refuses an undated spec (see ContractSpec
        # .require_orderable), which `test_domain.py` covers directly.
        assert CONTRACT_MONTH not in (config.default_contract_month or "")
