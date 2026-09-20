"""A strategy that cannot get its own flip through the risk ceiling must not start."""

from __future__ import annotations

import pytest

from app.config import ConfigError, RiskLimits
from app.main import _check_order_size_fits
from app.strategy.noop import build_strategy


def _sma():
    return build_strategy("sol-sma", params={"position_contracts": 1})


class TestOrderSizeGuard:
    def test_too_small_a_ceiling_refuses_to_start(self) -> None:
        with pytest.raises(ConfigError, match="MAX_ORDER_SIZE >= 2"):
            _check_order_size_fits(_sma(), RiskLimits(max_order_size=1))

    def test_a_sufficient_ceiling_passes(self) -> None:
        _check_order_size_fits(_sma(), RiskLimits(max_order_size=2))

    def test_an_unconfigured_ceiling_is_left_to_the_risk_manager(self) -> None:
        """Zero is 'not configured', which the risk manager already refuses loudly."""
        _check_order_size_fits(_sma(), RiskLimits(max_order_size=0))

    def test_a_strategy_with_no_requirement_never_trips(self) -> None:
        _check_order_size_fits(build_strategy("noop"), RiskLimits(max_order_size=1))
