"""Deployment posture: is the RUNNING process in the approved state?

``scripts/verify_safety.sh`` checks what the ``.env`` file *says*. This module
checks what the process *actually parsed*, which is not the same thing and is
where real incidents live:

* A hosting control panel (Hostinger's Docker Manager, Portainer, ...) can
  inject environment variables that never appear in ``.env``.
* An ``environment:`` entry in a compose override takes precedence over
  ``env_file:``.
* A container started before an ``.env`` edit keeps the old values until it is
  recreated; ``docker compose restart`` alone does not re-read ``env_file``.

In every one of those cases the file-based check reports "safe" and is telling
the truth about a file that is not authoritative.

Fail-closed design
------------------
:func:`evaluate_posture` returns one :class:`PostureCheck` per expectation and
:func:`posture_is_approved` requires that **every** expected check is present
and passing. A missing, empty, or partial result is a failure, not a pass. This
is deliberate: the first version of this feature was a shell script whose
embedded interpreter crashed, produced no output, and reported success because
nothing had recorded a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from app.config import Config
from app.enums import TradingMode

#: The interlock values this phase of the project is approved to run with.
APPROVED_INTERLOCKS: Final[dict[str, object]] = {
    "TRADING_MODE": TradingMode.MOCK,
    "ALLOW_ORDER_TRANSMIT": False,
    "LIVE_TRADING_ENABLED": False,
    "KILL_SWITCH": True,
    "SOL_FUTURES_PERMISSION_READY": False,
}

#: Risk limits must all be zero (= not configured = trading not authorised).
APPROVED_ZERO_LIMITS: Final[tuple[str, ...]] = (
    "MAX_POSITION_CONTRACTS",
    "MAX_ORDER_SIZE",
    "MAX_DAILY_LOSS_USD",
    "MAX_ORDERS_PER_HOUR",
    "MAX_OPEN_ORDERS",
    "MAX_NOTIONAL_EXPOSURE_USD",
)

#: Every check name :func:`evaluate_posture` must produce. If the returned set
#: does not match this exactly, the result is treated as untrustworthy.
EXPECTED_CHECK_NAMES: Final[frozenset[str]] = frozenset(
    {*APPROVED_INTERLOCKS, *APPROVED_ZERO_LIMITS, "CAN_TRANSMIT_LIVE_ORDERS"}
)


@dataclass(frozen=True, slots=True)
class PostureCheck:
    name: str
    ok: bool
    detail: str


def evaluate_posture(config: Config, *, kill_switch_engaged: bool) -> tuple[PostureCheck, ...]:
    """Compare a live :class:`Config` against the approved deployment posture.

    ``kill_switch_engaged`` is the *effective* kill switch (configuration OR the
    durable latch), not just the environment variable, because the latch can be
    engaged on a running process without a redeploy.
    """
    checks: list[PostureCheck] = []

    observed: dict[str, object] = {
        "TRADING_MODE": config.trading_mode,
        "ALLOW_ORDER_TRANSMIT": config.allow_order_transmit,
        "LIVE_TRADING_ENABLED": config.live_trading_enabled,
        "KILL_SWITCH": kill_switch_engaged,
        "SOL_FUTURES_PERMISSION_READY": config.sol_futures_permission_ready,
    }
    for name, expected in APPROVED_INTERLOCKS.items():
        actual = observed[name]
        checks.append(
            PostureCheck(
                name=name,
                ok=actual == expected,
                detail=f"{_render(actual)} (approved: {_render(expected)})",
            )
        )

    limits = config.risk.as_dict()
    for name in APPROVED_ZERO_LIMITS:
        raw = limits.get(name)
        checks.append(
            PostureCheck(
                name=name,
                ok=_is_zero(raw),
                detail=f"{raw} (approved: 0 = not configured)",
            )
        )

    can_transmit = (
        config.trading_mode is TradingMode.LIVE
        and config.live_trading_enabled
        and config.allow_order_transmit
        and not kill_switch_engaged
    )
    checks.append(
        PostureCheck(
            name="CAN_TRANSMIT_LIVE_ORDERS",
            ok=not can_transmit,
            detail=f"{can_transmit} (approved: False)",
        )
    )

    return tuple(checks)


def posture_is_approved(checks: tuple[PostureCheck, ...]) -> bool:
    """True only when every expected check is present and passing.

    Requiring the *exact* expected set is what makes this fail closed: a
    truncated or empty result cannot be mistaken for a clean run.
    """
    names = {check.name for check in checks}
    if names != EXPECTED_CHECK_NAMES:
        return False
    return all(check.ok for check in checks)


def failing_checks(checks: tuple[PostureCheck, ...]) -> tuple[PostureCheck, ...]:
    return tuple(check for check in checks if not check.ok)


def _is_zero(value: object) -> bool:
    if value is None:
        return False
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, ValueError):
        return False


def _render(value: object) -> str:
    if isinstance(value, TradingMode):
        return value.value
    return str(value)


__all__ = [
    "APPROVED_INTERLOCKS",
    "APPROVED_ZERO_LIMITS",
    "EXPECTED_CHECK_NAMES",
    "PostureCheck",
    "evaluate_posture",
    "failing_checks",
    "posture_is_approved",
]
