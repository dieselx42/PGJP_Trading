"""Deployment posture checks.

The regression this file exists for: the first attempt at this feature was a
shell script whose embedded interpreter crashed, produced no output, and
reported "the running container is in the approved state" with exit 0. A safety
verification that fails open manufactures confidence, which is worse than
having none.

So the tests below assert not only that a bad posture fails, but that an
*incomplete* result fails too.
"""

from __future__ import annotations

import pytest

from app.config import Config
from app.safety.posture import (
    APPROVED_ZERO_LIMITS,
    EXPECTED_CHECK_NAMES,
    PostureCheck,
    evaluate_posture,
    failing_checks,
    posture_is_approved,
)
from tests.conftest import default_env, permissive_env

pytestmark = pytest.mark.safety


def _checks(env: dict[str, str], *, kill_switch_engaged: bool | None = None):
    config = Config.from_env(env)
    engaged = config.kill_switch if kill_switch_engaged is None else kill_switch_engaged
    return evaluate_posture(config, kill_switch_engaged=engaged)


class TestApprovedPosture:
    def test_the_deployed_configuration_is_approved(self) -> None:
        checks = _checks(default_env())
        assert posture_is_approved(checks), [c.name for c in failing_checks(checks)]

    def test_every_expected_check_is_produced(self) -> None:
        names = {c.name for c in _checks(default_env())}
        assert names == EXPECTED_CHECK_NAMES

    def test_all_six_risk_limits_are_checked(self) -> None:
        names = {c.name for c in _checks(default_env())}
        assert set(APPROVED_ZERO_LIMITS) <= names
        assert len(APPROVED_ZERO_LIMITS) == 6


class TestPostureFailsClosed:
    def test_an_empty_result_is_not_approved(self) -> None:
        """The exact bug that motivated this module: no checks must not mean pass."""
        assert posture_is_approved(()) is False

    def test_a_truncated_result_is_not_approved(self) -> None:
        """All-passing but incomplete must still fail."""
        partial = tuple(
            PostureCheck(name=name, ok=True, detail="") for name in sorted(EXPECTED_CHECK_NAMES)[:3]
        )
        assert all(c.ok for c in partial)
        assert posture_is_approved(partial) is False

    def test_an_unexpected_check_name_is_not_approved(self) -> None:
        """A result set that does not match exactly is untrustworthy."""
        checks = (*_checks(default_env()), PostureCheck(name="SOMETHING_ELSE", ok=True, detail=""))
        assert posture_is_approved(checks) is False

    def test_a_complete_all_passing_result_is_approved(self) -> None:
        """The control: the fail-closed rules must not reject a genuine pass."""
        complete = tuple(
            PostureCheck(name=name, ok=True, detail="") for name in EXPECTED_CHECK_NAMES
        )
        assert posture_is_approved(complete) is True


class TestPostureDetectsDrift:
    """Each case simulates a value injected outside .env — a control panel, a
    compose override, or a container that predates an .env edit."""

    def test_transmission_enabled_is_detected(self) -> None:
        checks = _checks(default_env(ALLOW_ORDER_TRANSMIT="true"))
        assert not posture_is_approved(checks)
        assert "ALLOW_ORDER_TRANSMIT" in {c.name for c in failing_checks(checks)}

    def test_kill_switch_off_is_detected(self) -> None:
        checks = _checks(default_env(KILL_SWITCH="false"))
        assert not posture_is_approved(checks)
        assert "KILL_SWITCH" in {c.name for c in failing_checks(checks)}

    def test_a_durably_latched_kill_switch_counts_as_engaged(self) -> None:
        """The latch can be engaged on a running process without a redeploy."""
        checks = _checks(default_env(KILL_SWITCH="false"), kill_switch_engaged=True)
        assert "KILL_SWITCH" not in {c.name for c in failing_checks(checks)}

    def test_a_non_mock_mode_is_detected(self) -> None:
        checks = _checks(default_env(TRADING_MODE="paper"))
        assert not posture_is_approved(checks)
        assert "TRADING_MODE" in {c.name for c in failing_checks(checks)}

    def test_permission_flag_flipped_is_detected(self) -> None:
        checks = _checks(default_env(SOL_FUTURES_PERMISSION_READY="true"))
        assert not posture_is_approved(checks)

    @pytest.mark.parametrize("limit", sorted(APPROVED_ZERO_LIMITS))
    def test_any_configured_risk_limit_is_detected(self, limit: str) -> None:
        checks = _checks(default_env(**{limit: "5"}))
        assert not posture_is_approved(checks)
        assert limit in {c.name for c in failing_checks(checks)}

    def test_the_fully_permissive_configuration_is_rejected(self) -> None:
        """The test-only permissive baseline must never be an approved posture."""
        checks = _checks(permissive_env())
        assert not posture_is_approved(checks)
        # Transmission, kill switch, and all six limits differ from approved.
        assert len(failing_checks(checks)) >= 8

    def test_a_live_configuration_is_rejected(self) -> None:
        checks = _checks(permissive_env(TRADING_MODE="live", LIVE_TRADING_ENABLED="true"))
        assert not posture_is_approved(checks)
        failures = {c.name for c in failing_checks(checks)}
        assert "CAN_TRANSMIT_LIVE_ORDERS" in failures
        assert "LIVE_TRADING_ENABLED" in failures


def test_detail_strings_explain_what_was_expected() -> None:
    """An operator reading a failure needs the approved value, not just 'wrong'."""
    checks = _checks(default_env(ALLOW_ORDER_TRANSMIT="true"))
    failure = next(c for c in failing_checks(checks) if c.name == "ALLOW_ORDER_TRANSMIT")
    assert "True" in failure.detail
    assert "approved: False" in failure.detail
