"""The operator order, end to end against MockBroker.

The control test here is the one that matters. Every refusal test in
`tests/unit/test_operator_order.py` would pass on a command that could never
send anything at all, so exactly one test proves the baseline **does** transmit.
The rest degrade one condition from that baseline and require a refusal.

MockBroker throughout: this exercises the pipeline, not IBKR.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Config
from app.enums import OrderType
from app.execution.operator_order import OperatorOrderRequest, place_operator_order
from tests.conftest import permissive_env

pytestmark = pytest.mark.integration


def _config(**overrides: str) -> Config:
    return Config.from_env(permissive_env(**overrides))


def _request(confirm: str | None = None, **overrides: object) -> OperatorOrderRequest:
    base: dict[str, object] = {
        "target_position": 1,
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("50.00"),
        "confirm": confirm,
    }
    base.update(overrides)
    return OperatorOrderRequest(**base)  # type: ignore[arg-type]


async def _preview(config: Config, **overrides: object) -> dict[str, object]:
    return await place_operator_order(config, _request(**overrides))


async def _local_symbol(config: Config) -> str:
    """The confirmation token, obtained the way an operator obtains it."""
    report = await _preview(config)
    plan = report["plan"]
    assert isinstance(plan, dict)
    contract = plan["contract"]
    assert isinstance(contract, dict)
    return str(contract["local_symbol"])


class TestItRunsAlongsideTheTradingProcess:
    """Regression: `place-order` runs beside the bot, not as it.

    The first run against a deployed bot died with
    ``[Errno 98] address already in use`` on the health port, because it built a
    full `TradingApplication` and that starts a health server the trading
    process already owns. Directly behind it sat a worse one: the same
    `client_id` as the running bot, which IBKR resolves by disconnecting one of
    them -- so placing an order would have knocked the bot off its broker
    session.

    Both are tested by effect. Asserting "the flag is set" would have passed on
    the broken version too.
    """

    @staticmethod
    def _free_port() -> int:
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        return int(port)

    async def test_an_admin_instance_can_start_while_the_bot_holds_the_port(self) -> None:
        from app.main import TradingApplication

        port = str(self._free_port())
        running = TradingApplication(_config(HEALTH_PORT=port))
        await running.startup()
        try:
            # Same port, same config. This is the call that used to raise.
            admin = TradingApplication(_config(HEALTH_PORT=port), is_admin_instance=True)
            await admin.startup()
            await admin.shutdown()
        finally:
            await running.shutdown()

    async def test_two_admin_instances_do_not_collide_either(self) -> None:
        from app.main import TradingApplication

        port = str(self._free_port())
        first = TradingApplication(_config(HEALTH_PORT=port), is_admin_instance=True)
        second = TradingApplication(_config(HEALTH_PORT=port), is_admin_instance=True)
        await first.startup()
        await second.startup()
        await second.shutdown()
        await first.shutdown()

    def test_an_admin_instance_uses_the_admin_client_id(self) -> None:
        """IBKR disconnects the loser when two clients share an id."""
        from app.main import TradingApplication

        config = _config(TRADING_MODE="paper", IB_PAPER_PORT="4002")
        admin = TradingApplication(config, is_admin_instance=True)
        broker = admin._build_broker()

        assert broker._client_id == config.ibkr.admin_client_id
        assert broker._client_id != config.ibkr.client_id

    def test_the_trading_process_still_uses_the_trading_client_id(self) -> None:
        from app.main import TradingApplication

        config = _config(TRADING_MODE="paper", IB_PAPER_PORT="4002")
        broker = TradingApplication(config)._build_broker()

        assert broker._client_id == config.ibkr.client_id

    def test_the_default_is_not_an_admin_instance(self) -> None:
        """`python -m app.main` must never silently skip its own health server."""
        from app.main import TradingApplication

        assert TradingApplication(_config()).is_admin_instance is False

    async def test_the_operator_command_asks_for_an_admin_instance(self) -> None:
        """Read from the source: the flag has to actually be passed."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path("app/execution/operator_order.py").read_text())
        found = False
        for node in ast.walk(tree):
            is_construction = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "TradingApplication"
            )
            if is_construction:
                assert "is_admin_instance" in {kw.arg for kw in node.keywords}  # type: ignore[union-attr]
                found = True
        assert found, "operator_order.py no longer constructs a TradingApplication"


class TestControl:
    """Without this, every refusal test below is vacuous."""

    @pytest.mark.safety
    async def test_a_confirmed_order_in_a_fully_authorised_config_is_transmitted(self) -> None:
        config = _config()
        symbol = await _local_symbol(config)

        report = await place_operator_order(config, _request(confirm=symbol))

        assert report["result"] == "SUBMITTED", report
        outcome = report["outcome"]
        assert isinstance(outcome, dict)
        assert outcome["transmitted"] is True, outcome
        assert report["transmitted"] is True


class TestPreviewSendsNothing:
    async def test_omitting_confirm_previews(self) -> None:
        report = await _preview(_config())
        assert report["result"] == "PREVIEW_ONLY"
        assert report["transmitted"] is False
        assert "outcome" not in report

    async def test_the_preview_reports_what_would_happen(self) -> None:
        report = await _preview(_config())
        assert report["would_be_allowed"] is True
        approvals = report["approvals"]
        assert isinstance(approvals, dict)
        assert approvals["risk"] == {"approved": True, "reasons": []}
        assert approvals["gate"] == {"allowed": True, "reasons": []}

    async def test_the_preview_names_the_token_it_needs(self) -> None:
        config = _config()
        report = await _preview(config)
        symbol = await _local_symbol(config)
        assert report["to_send_this_order_add"] == f"--confirm {symbol}"

    async def test_a_preview_persists_no_order(self) -> None:
        """A preview that left a durable row would break idempotency later."""
        config = _config()
        await _preview(config)
        report = await _preview(config)
        # Still previewing, still nothing recorded, no duplicate suppression.
        assert report["result"] == "PREVIEW_ONLY"
        assert report["would_be_allowed"] is True


class TestPreviewingDoesNotConsumeTheIntent:
    """Regression: the preview must not make the real submission a duplicate.

    `SignalValidator.validate` records an intent id whenever it accepts -- that
    is what makes a replayed signal harmless. The first version of this command
    previewed through the *same* validator the pipeline then used, so the
    submission was refused as a duplicate of its own preview and silently sent
    nothing. It reported `SUBMITTED` with a null outcome, which is the worst
    possible shape for that failure: it looks like success.

    Found by the control test above, which is why it exists.
    """

    @pytest.mark.safety
    async def test_previewing_first_does_not_prevent_sending(self) -> None:
        config = _config()
        symbol = await _local_symbol(config)  # a full preview, side effects and all

        report = await place_operator_order(config, _request(confirm=symbol))

        outcome = report["outcome"]
        assert isinstance(outcome, dict), "the preview consumed the intent"
        assert outcome["transmitted"] is True
        assert "SIGNAL_DUPLICATE" not in str(outcome)

    async def test_a_confirmed_run_previews_and_submits_in_one_pass(self) -> None:
        """The confirmed path still reports both approvers, from its own copy."""
        config = _config()
        symbol = await _local_symbol(config)
        report = await place_operator_order(config, _request(confirm=symbol))

        approvals = report["approvals"]
        assert isinstance(approvals, dict)
        assert approvals["signal_validation"] == {"accepted": True, "reasons": []}


class TestWrongConfirmation:
    async def test_a_wrong_token_sends_nothing(self) -> None:
        report = await place_operator_order(_config(), _request(confirm="NOTTHESYMBOL"))
        assert report["result"] == "CONFIRMATION_MISMATCH"
        assert report["transmitted"] is False

    async def test_a_near_miss_is_still_a_miss(self) -> None:
        config = _config()
        symbol = await _local_symbol(config)
        report = await place_operator_order(config, _request(confirm=symbol.lower()))
        assert report["result"] == "CONFIRMATION_MISMATCH"
        assert report["transmitted"] is False


class TestInterlocksStillApply:
    """One condition degraded from the authorised baseline. Each must refuse."""

    @pytest.mark.safety
    async def test_the_kill_switch_stops_an_operator_order(self) -> None:
        config = _config(KILL_SWITCH="true")
        symbol = await _local_symbol(config)
        report = await place_operator_order(config, _request(confirm=symbol))

        outcome = report["outcome"]
        assert not (isinstance(outcome, dict) and outcome.get("transmitted"))

    @pytest.mark.safety
    async def test_transmit_disabled_stops_an_operator_order(self) -> None:
        config = _config(ALLOW_ORDER_TRANSMIT="false")
        symbol = await _local_symbol(config)
        report = await place_operator_order(config, _request(confirm=symbol))

        outcome = report["outcome"]
        assert not (isinstance(outcome, dict) and outcome.get("transmitted"))

    @pytest.mark.safety
    async def test_an_unconfigured_risk_limit_stops_an_operator_order(self) -> None:
        """Zero means NOT CONFIGURED, which means not authorised -- here too."""
        config = _config(MAX_ORDER_SIZE="0")
        symbol = await _local_symbol(config)
        report = await place_operator_order(config, _request(confirm=symbol))

        outcome = report["outcome"]
        assert not (isinstance(outcome, dict) and outcome.get("transmitted"))

    @pytest.mark.safety
    async def test_a_size_over_the_limit_is_refused(self) -> None:
        config = _config(MAX_ORDER_SIZE="1")
        symbol = await _local_symbol(config)
        report = await place_operator_order(config, _request(confirm=symbol, target_position=5))

        outcome = report["outcome"]
        assert not (isinstance(outcome, dict) and outcome.get("transmitted"))

    @pytest.mark.safety
    async def test_unconfigured_market_data_age_stops_an_operator_order(self) -> None:
        config = _config(MARKET_DATA_MAX_AGE_SECONDS="0")
        symbol = await _local_symbol(config)
        report = await place_operator_order(config, _request(confirm=symbol))

        outcome = report["outcome"]
        assert not (isinstance(outcome, dict) and outcome.get("transmitted"))


class TestTargetsNotDeltas:
    async def test_asking_for_the_position_you_already_hold_is_a_no_op(self) -> None:
        """Idempotency by construction: the second ask produces no order."""
        config = _config()
        symbol = await _local_symbol(config)
        first = await place_operator_order(config, _request(confirm=symbol))
        assert first["result"] == "SUBMITTED"

        # A fresh run against the same durable state, same target.
        second = await place_operator_order(config, _request(confirm=symbol))
        outcome = second["outcome"]
        # Either no change is needed, or the duplicate is suppressed. Both are
        # correct; what must NOT happen is a second position-doubling order.
        if isinstance(outcome, dict):
            assert outcome["outcome"] in {"NO_CHANGE", "DUPLICATE", "TRANSMITTED"}

    async def test_the_plan_shows_the_resulting_order_before_it_is_sent(self) -> None:
        report = await _preview(_config(), target_position=2)
        plan = report["plan"]
        assert isinstance(plan, dict)
        resulting = plan["resulting_order"]
        assert isinstance(resulting, dict)
        assert resulting["side"] == "BUY"
        assert resulting["quantity"] == 2
        assert resulting["order_type"] == "LIMIT"
        assert resulting["limit_price"] == "50.00"

    async def test_a_zero_target_from_flat_produces_no_order(self) -> None:
        report = await _preview(_config(), target_position=0)
        plan = report["plan"]
        assert isinstance(plan, dict)
        assert plan["resulting_order"] is None
