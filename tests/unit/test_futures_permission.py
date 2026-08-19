"""Futures permission: observed, declared, or unknown.

The interlock this covers was, until 2026-08-19, **impossible to satisfy**. The
TWS API exposes no "may this account trade CME futures" flag, so the IBKR
adapter reported `None` for every account, and `None` was treated as "not
permitted". No configuration and no account could pass it.

It went unnoticed for the whole life of the project because `MockBroker` reports
`True`. Every test passed against a system that could not have transmitted an
order under any circumstances. That is the failure mode these tests exist to
prevent recurring: a check whose only passing path is the fake.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.models import (
    AccountSummary,
    BrokerContractError,
    BrokerPermissionError,
    PermissionProbe,
)
from app.config import Config
from app.enums import AccountType
from app.main import TradingApplication
from tests.conftest import permissive_env


def _app(*, declared: bool, observed: bool | None, has_account: bool = True) -> TradingApplication:
    config = Config.from_env(
        permissive_env(SOL_FUTURES_PERMISSION_READY="true" if declared else "false")
    )
    app = TradingApplication(config, is_admin_instance=True)
    if has_account:
        app.account = AccountSummary(
            account_id="DU111111",
            account_type=AccountType.PAPER,
            futures_permission=observed,
        )
    return app


class TestThreeStateSemantics:
    def test_observed_permitted_authorises(self) -> None:
        assert _app(declared=False, observed=True)._broker_permission_ready() is True

    @pytest.mark.safety
    def test_an_observed_refusal_overrides_an_operator_declaration(self) -> None:
        """The broker decides. A human who believes otherwise is wrong."""
        app = _app(declared=True, observed=False)
        assert app._broker_permission_ready() is False
        assert "NOT permitted" in app._permission_source()

    def test_unknown_falls_back_to_the_declaration(self) -> None:
        """The only case that changed, and the one that unblocked IBKR."""
        assert _app(declared=True, observed=None)._broker_permission_ready() is True
        assert _app(declared=False, observed=None)._broker_permission_ready() is False

    @pytest.mark.safety
    def test_no_account_is_never_permitted(self) -> None:
        """Not even with the declaration set: unknown account, no authority."""
        app = _app(declared=True, observed=None, has_account=False)
        assert app._broker_permission_ready() is False

    def test_the_source_distinguishes_observed_from_declared(self) -> None:
        """Both authorise. They mean very different things, so say which."""
        assert "observed" in _app(declared=False, observed=True)._permission_source()
        assert "declared" in _app(declared=True, observed=None)._permission_source()


class TestTheInterlockIsNowSatisfiable:
    """The regression itself: with IBKR, could this EVER pass?"""

    @pytest.mark.safety
    def test_an_ibkr_style_none_report_can_authorise(self) -> None:
        """`None` is what every real IBKR account reports. It must be reachable."""
        app = _app(declared=True, observed=None)
        assert app._broker_permission_ready() is True, (
            "the IBKR adapter reports None for every account; if None can never "
            "authorise, this interlock is impossible to satisfy in production"
        )

    def test_the_adapter_still_reports_none(self) -> None:
        """Confirms the premise rather than assuming it stayed true."""
        source = Path("app/broker/ibkr_broker.py").read_text()
        assert "futures_permission=None," in source


class TestProbeResultMeaning:
    def test_a_priced_preview_means_permitted(self) -> None:
        probe = PermissionProbe(permitted=True, detail="priced", commission=Decimal("3.41"))
        assert probe.permitted is True
        assert probe.describe()["commission"] == "3.41"

    def test_a_permission_refusal_means_not_permitted(self) -> None:
        from app.broker.ibkr_broker import _permission_probe_from_error
        from app.utilities.timeutils import utc_now

        probe = _permission_probe_from_error(
            BrokerPermissionError("IBKR 10187: not permitted to trade this product"),
            probed_at=utc_now(),
        )
        assert probe.permitted is False

    @pytest.mark.safety
    def test_a_non_permission_error_is_undetermined_not_denied(self) -> None:
        """Reporting a contract error as 'not permitted' states an unobserved fact."""
        from app.broker.ibkr_broker import _permission_probe_from_error
        from app.utilities.timeutils import utc_now

        probe = _permission_probe_from_error(
            BrokerContractError("IBKR 200: no security definition"), probed_at=utc_now()
        )
        assert probe.permitted is None
        assert "not the same as denied" in probe.detail

    def test_undetermined_never_authorises_on_its_own(self) -> None:
        assert _app(declared=False, observed=None)._broker_permission_ready() is False


class TestTheProbeCannotPlaceAnOrder:
    """Structural, read from the source. A comment would not survive an edit."""

    SOURCE = Path("app/broker/ibkr_broker.py")

    def _probe_function(self) -> ast.AsyncFunctionDef:
        tree = ast.parse(self.SOURCE.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "probe_futures_permission":
                return node
        raise AssertionError("probe_futures_permission is gone")

    @pytest.mark.safety
    def test_what_if_is_assigned_a_literal_true(self) -> None:
        """Not a parameter, not a variable: no caller can reach it with False."""
        assignments = [
            node
            for node in ast.walk(self._probe_function())
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Attribute) and t.attr == "whatIf" for t in node.targets)
        ]
        assert assignments, "the whatIf flag is no longer set"
        for node in assignments:
            assert isinstance(node.value, ast.Constant), "whatIf must be a literal"
            assert node.value.value is True

    @pytest.mark.safety
    def test_what_if_is_not_a_parameter_of_the_probe(self) -> None:
        args = self._probe_function().args
        names = {a.arg for a in [*args.args, *args.kwonlyargs, *args.posonlyargs]}
        assert "what_if" not in names
        assert "whatIf" not in names
        assert "transmit" not in names

    def test_the_limit_price_is_far_from_any_market(self) -> None:
        """Belt and braces if IBKR ever ignored the flag: rest, do not fill."""
        from app.broker.ibkr_broker import _PERMISSION_PROBE_LIMIT_PRICE

        assert Decimal("10") > _PERMISSION_PROBE_LIMIT_PRICE

    def test_the_read_only_checkout_still_cannot_write(self) -> None:
        """The probe deliberately lives outside the checkout's read-only surface."""
        checkout = Path("app/broker/checkout.py").read_text()
        assert "probe_futures_permission" not in checkout
        assert "placeOrder" not in checkout


class TestCliWiring:
    def test_check_permission_is_registered(self) -> None:
        from app.cli import COMMANDS

        assert "check-permission" in COMMANDS

    def test_it_accepts_a_contract_month_for_one_run(self) -> None:
        from app.cli import build_parser

        args = build_parser().parse_args(["check-permission", "--contract-month", "20260828"])
        assert args.contract_month == "20260828"

    def test_the_contract_month_is_optional_and_not_guessed(self) -> None:
        from app.cli import build_parser

        args = build_parser().parse_args(["check-permission"])
        assert args.contract_month is None
