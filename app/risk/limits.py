"""Numeric risk limits.

Each check is a small pure function returning a rejection reason or ``None``.
Keeping them separate makes each one individually testable and makes the
"unconfigured means prohibited" rule impossible to miss: every limit has *two*
rejection reasons, one for "you never set this" and one for "you exceeded it".

There is no code path in this module where a limit of zero permits an action.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import RiskLimits
from app.contracts.models import QualifiedContract

# -- rejection reasons ------------------------------------------------------

REASON_ORDER_SIZE_NOT_CONFIGURED = "MAX_ORDER_SIZE_NOT_CONFIGURED"
REASON_ORDER_SIZE_EXCEEDED = "MAX_ORDER_SIZE_EXCEEDED"

REASON_POSITION_NOT_CONFIGURED = "MAX_POSITION_CONTRACTS_NOT_CONFIGURED"
REASON_POSITION_EXCEEDED = "MAX_POSITION_CONTRACTS_EXCEEDED"

REASON_DAILY_LOSS_NOT_CONFIGURED = "MAX_DAILY_LOSS_USD_NOT_CONFIGURED"
REASON_DAILY_LOSS_EXCEEDED = "MAX_DAILY_LOSS_USD_EXCEEDED"

REASON_ORDERS_PER_HOUR_NOT_CONFIGURED = "MAX_ORDERS_PER_HOUR_NOT_CONFIGURED"
REASON_ORDERS_PER_HOUR_EXCEEDED = "MAX_ORDERS_PER_HOUR_EXCEEDED"

REASON_OPEN_ORDERS_NOT_CONFIGURED = "MAX_OPEN_ORDERS_NOT_CONFIGURED"
REASON_OPEN_ORDERS_EXCEEDED = "MAX_OPEN_ORDERS_EXCEEDED"

REASON_NOTIONAL_NOT_CONFIGURED = "MAX_NOTIONAL_EXPOSURE_USD_NOT_CONFIGURED"
REASON_NOTIONAL_EXCEEDED = "MAX_NOTIONAL_EXPOSURE_USD_EXCEEDED"
REASON_NOTIONAL_UNPRICEABLE = "NOTIONAL_EXPOSURE_NOT_COMPUTABLE"


def check_order_size(limits: RiskLimits, quantity: int) -> str | None:
    if limits.max_order_size <= 0:
        return REASON_ORDER_SIZE_NOT_CONFIGURED
    if abs(quantity) > limits.max_order_size:
        return REASON_ORDER_SIZE_EXCEEDED
    return None


def check_position_size(limits: RiskLimits, projected_position: int) -> str | None:
    """Evaluated against the *projected* position, not the current one.

    Checking what the position would become after the fill is the only version
    of this limit that actually prevents a breach.
    """
    if limits.max_position_contracts <= 0:
        return REASON_POSITION_NOT_CONFIGURED
    if abs(projected_position) > limits.max_position_contracts:
        return REASON_POSITION_EXCEEDED
    return None


def check_daily_loss(limits: RiskLimits, daily_pnl: Decimal) -> str | None:
    """``daily_pnl`` is signed; a loss is negative."""
    if limits.max_daily_loss_usd <= 0:
        return REASON_DAILY_LOSS_NOT_CONFIGURED
    if daily_pnl <= -limits.max_daily_loss_usd:
        return REASON_DAILY_LOSS_EXCEEDED
    return None


def check_orders_per_hour(limits: RiskLimits, orders_last_hour: int) -> str | None:
    if limits.max_orders_per_hour <= 0:
        return REASON_ORDERS_PER_HOUR_NOT_CONFIGURED
    if orders_last_hour >= limits.max_orders_per_hour:
        return REASON_ORDERS_PER_HOUR_EXCEEDED
    return None


def check_open_orders(limits: RiskLimits, open_orders: int) -> str | None:
    if limits.max_open_orders <= 0:
        return REASON_OPEN_ORDERS_NOT_CONFIGURED
    if open_orders >= limits.max_open_orders:
        return REASON_OPEN_ORDERS_EXCEEDED
    return None


def check_notional_exposure(
    limits: RiskLimits,
    *,
    contract: QualifiedContract | None,
    projected_position: int,
    reference_price: Decimal | None,
) -> str | None:
    """Projected notional against the configured ceiling.

    If exposure cannot be computed -- no contract or no price -- the answer is
    rejection, not "assume it is fine".
    """
    if limits.max_notional_exposure_usd <= 0:
        return REASON_NOTIONAL_NOT_CONFIGURED
    if contract is None or reference_price is None or reference_price <= 0:
        return REASON_NOTIONAL_UNPRICEABLE
    exposure = contract.notional(projected_position, reference_price)
    if exposure > limits.max_notional_exposure_usd:
        return REASON_NOTIONAL_EXCEEDED
    return None


def unconfigured_limits(limits: RiskLimits) -> tuple[str, ...]:
    """Every limit that is currently unset, for status reporting."""
    reasons: list[str] = []
    if limits.max_order_size <= 0:
        reasons.append(REASON_ORDER_SIZE_NOT_CONFIGURED)
    if limits.max_position_contracts <= 0:
        reasons.append(REASON_POSITION_NOT_CONFIGURED)
    if limits.max_daily_loss_usd <= 0:
        reasons.append(REASON_DAILY_LOSS_NOT_CONFIGURED)
    if limits.max_orders_per_hour <= 0:
        reasons.append(REASON_ORDERS_PER_HOUR_NOT_CONFIGURED)
    if limits.max_open_orders <= 0:
        reasons.append(REASON_OPEN_ORDERS_NOT_CONFIGURED)
    if limits.max_notional_exposure_usd <= 0:
        reasons.append(REASON_NOTIONAL_NOT_CONFIGURED)
    return tuple(reasons)


__all__ = [
    "REASON_DAILY_LOSS_EXCEEDED",
    "REASON_DAILY_LOSS_NOT_CONFIGURED",
    "REASON_NOTIONAL_EXCEEDED",
    "REASON_NOTIONAL_NOT_CONFIGURED",
    "REASON_NOTIONAL_UNPRICEABLE",
    "REASON_OPEN_ORDERS_EXCEEDED",
    "REASON_OPEN_ORDERS_NOT_CONFIGURED",
    "REASON_ORDERS_PER_HOUR_EXCEEDED",
    "REASON_ORDERS_PER_HOUR_NOT_CONFIGURED",
    "REASON_ORDER_SIZE_EXCEEDED",
    "REASON_ORDER_SIZE_NOT_CONFIGURED",
    "REASON_POSITION_EXCEEDED",
    "REASON_POSITION_NOT_CONFIGURED",
    "check_daily_loss",
    "check_notional_exposure",
    "check_open_orders",
    "check_order_size",
    "check_orders_per_hour",
    "check_position_size",
    "unconfigured_limits",
]
