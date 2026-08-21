"""The passive benchmark, held to its two promises: stay long, roll on schedule.

A benchmark that quietly stopped holding, or rolled more often than it claims,
would flatter or punish every strategy compared against it -- and nothing else
in the suite would notice. So the tests here are about *exposure continuity*
and *roll cadence*, not about profit.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.strategy.hold import SolHoldStrategy
from app.strategy.noop import STRATEGY_REGISTRY, build_strategy

BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


def _bar(day: int, *, close: str = "100", minute: int = 0) -> Bar:
    c = Decimal(close)
    return Bar(
        source="coinbase",
        symbol="SOL-USD",
        interval="1m",
        opened_at=BASE + timedelta(days=day, minutes=minute),
        open=c,
        high=c,
        low=c,
        close=c,
        volume=Decimal("10"),
    )


def _feed(strategy: SolHoldStrategy, bars: Sequence[Bar]) -> list:
    intents = []
    for bar in bars:
        intents.extend(strategy.handle_bar(bar))
    return intents


def _entered(strategy: SolHoldStrategy, day: int = 0) -> list:
    """Drive the first entry to filled and fix the roll calendar.

    The bar after the fill is what anchors the schedule, so the helper feeds
    it: without that the first roll date would depend on whichever bar the
    test happened to send next.
    """
    intents = _feed(strategy, [_bar(day)])
    strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("100"))
    _feed(strategy, [_bar(day, minute=1)])
    return intents


class TestRegistry:
    def test_registered_and_buildable(self) -> None:
        assert "sol-hold" in STRATEGY_REGISTRY
        assert isinstance(build_strategy("sol-hold"), SolHoldStrategy)


class TestEntry:
    def test_it_buys_on_the_first_bar(self) -> None:
        strategy = SolHoldStrategy()
        intents = _feed(strategy, [_bar(0)])
        assert len(intents) == 1
        assert intents[0].direction is Direction.LONG
        assert intents[0].requested_position == 40

    def test_position_comes_only_from_fills(self) -> None:
        strategy = SolHoldStrategy()
        _feed(strategy, [_bar(0)])
        assert strategy.position == 0
        strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("100"))
        assert strategy.position == 40

    def test_an_unfilled_entry_is_retried_not_abandoned(self) -> None:
        """A benchmark that gave up after one cancelled order would silently
        measure a flat account and report it as passive exposure."""
        strategy = SolHoldStrategy()
        _feed(strategy, [_bar(0)])
        retry = _feed(strategy, [_bar(0, minute=1)])
        assert len(retry) == 1
        assert retry[0].requested_position == 40
        assert strategy.describe()["counters"]["entries_cancelled_unfilled"] == 1  # type: ignore[index]


class TestHolding:
    def test_it_does_nothing_while_the_roll_is_not_due(self) -> None:
        strategy = SolHoldStrategy()
        _entered(strategy)
        # Eighty-nine days of wildly varying prices: a benchmark with no view
        # must not react to any of them.
        for day in range(1, 90):
            assert _feed(strategy, [_bar(day, close=str(100 + day))]) == []
        assert strategy.position == 40
        assert strategy.describe()["counters"]["rolls"] == 0  # type: ignore[index]


class TestRolling:
    def test_it_flattens_when_the_roll_falls_due(self) -> None:
        strategy = SolHoldStrategy()
        _entered(strategy)
        assert _feed(strategy, [_bar(89)]) == []
        intents = _feed(strategy, [_bar(90)])
        assert len(intents) == 1
        assert intents[0].requested_position == 0
        assert intents[0].metadata["exit_reason"] == "roll"
        assert strategy.describe()["counters"]["rolls"] == 1  # type: ignore[index]

    def test_it_re_enters_after_the_roll_fills(self) -> None:
        """Exposure resumes on the next bar: a roll is a round trip, not an
        exit. A benchmark that stayed flat after rolling would understate
        passive returns by however long it sat out."""
        strategy = SolHoldStrategy()
        _entered(strategy)
        _feed(strategy, [_bar(90)])
        strategy.on_fill(side=OrderSide.SELL, quantity=40, price=Decimal("120"))
        assert strategy.position == 0
        intents = _feed(strategy, [_bar(90, minute=1)])
        assert len(intents) == 1
        assert intents[0].requested_position == 40
        assert intents[0].metadata["exit_reason"] == "enter"

    def test_an_unfilled_roll_is_re_emitted(self) -> None:
        strategy = SolHoldStrategy()
        _entered(strategy)
        first = _feed(strategy, [_bar(90)])
        assert first[0].requested_position == 0
        again = _feed(strategy, [_bar(90, minute=1)])
        assert len(again) == 1
        assert again[0].requested_position == 0

    def test_the_roll_calendar_does_not_drift_across_rolls(self) -> None:
        strategy = SolHoldStrategy()
        _entered(strategy)
        _feed(strategy, [_bar(90)])
        strategy.on_fill(side=OrderSide.SELL, quantity=40, price=Decimal("120"))
        _feed(strategy, [_bar(90, minute=1)])
        strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("120"))
        # The schedule is the calendar's, not the fill's: the second roll is
        # due on day 180 regardless of which bar the re-entry filled on. A
        # clock restarted from the re-entry would push it to 181+ and lose a
        # roll a year.
        _feed(strategy, [_bar(91)])
        assert _feed(strategy, [_bar(179)]) == []
        assert len(_feed(strategy, [_bar(180)])) == 1

    def test_a_year_produces_four_rolls(self) -> None:
        """The cadence claim in the docstring, measured. Four round trips a
        year is what a passive futures hold actually costs."""
        strategy = SolHoldStrategy()
        _entered(strategy)
        for day in range(1, 366):
            for intent in _feed(strategy, [_bar(day)]):
                side = OrderSide.SELL if intent.requested_position == 0 else OrderSide.BUY
                strategy.on_fill(side=side, quantity=40, price=Decimal("100"))
        assert strategy.describe()["counters"]["rolls"] == 4  # type: ignore[index]


class TestParams:
    def test_unknown_params_are_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy parameter"):
            SolHoldStrategy(params={"roll_dayz": 90})

    @pytest.mark.parametrize("bad", ["0", "6", "-1", "4000", "x"])
    def test_invalid_roll_days_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            SolHoldStrategy(params={"roll_days": bad})

    def test_size_can_only_be_overridden_down(self) -> None:
        assert SolHoldStrategy(params={"position_contracts": 1}).describe()[
            "position_contracts"
        ] == 1
        with pytest.raises(ValueError, match="position_contracts"):
            SolHoldStrategy(params={"position_contracts": 41})
