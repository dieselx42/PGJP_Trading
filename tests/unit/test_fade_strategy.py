"""sol-fade: the failed channel break, on closes, decided once a day.

The channel is 5 days here so a scenario fits in a dozen bars; every rule
the tests pin is independent of the window length.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.strategy.fade import SolFadeStrategy
from app.strategy.noop import STRATEGY_REGISTRY, build_strategy

BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
PARAMS: dict[str, object] = {
    "channel_days": 5,
    "min_extreme_age_days": 2,
    "confirm_days": 2,
    "hold_days": 3,
}


def _bar(at: datetime, *, close: str, high: str | None = None, low: str | None = None) -> Bar:
    c = Decimal(close)
    return Bar(
        source="coinbase",
        symbol="SOL-USD",
        interval="1m",
        opened_at=at,
        open=c,
        high=Decimal(high) if high else c,
        low=Decimal(low) if low else c,
        close=c,
        volume=Decimal("1"),
    )


def _day(day: int, close: str, *, minute: int = 0, **kw: str) -> Bar:
    """A bar of UTC day ``day``; the first one completes day ``day - 1``."""
    return _bar(BASE + timedelta(days=day, minutes=minute), close=close, **kw)


def _feed(s: SolFadeStrategy, bars: Sequence[Bar]) -> list:
    out = []
    for b in bars:
        got = s.handle_bar(b)
        assert len(got) <= 1
        out.extend(got)
    return out


def _strategy(**extra: object) -> SolFadeStrategy:
    return SolFadeStrategy(params={**PARAMS, **extra})


def _counters(s: SolFadeStrategy) -> dict[str, int]:
    c = s.describe()["counters"]
    assert isinstance(c, dict)
    return c


#: Five window days whose closes give H=104, L=98 with the low set on day 2
#: (three days before the break day), then the break and the reclaim.
WINDOW = ["104", "100", "98", "100", "102"]


def _setup_long(s: SolFadeStrategy) -> list:
    """Days 0-4 form the window, day 5 breaks down (97), day 6 reclaims (99).
    Day 7's bar completes day 6 and carries the signal."""
    closes = [*WINDOW, "97", "99"]
    bars = [_day(i, c) for i, c in enumerate(closes)] + [_day(7, "99")]
    return _feed(s, bars)


class TestRegistry:
    def test_registered_and_built_by_name(self) -> None:
        assert "sol-fade" in STRATEGY_REGISTRY
        assert isinstance(build_strategy("sol-fade"), SolFadeStrategy)

    def test_unknown_and_bad_params_are_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy parameter"):
            _strategy(chanel_days=5)
        with pytest.raises(ValueError, match="outside"):
            _strategy(confirm_days=0)
        with pytest.raises(ValueError, match="position_contracts"):
            _strategy(position_contracts="abc")


class TestWarmup:
    def test_nothing_until_the_channel_exists(self) -> None:
        s = _strategy()
        # six bars complete five days: the window needs 5 + 1 to test a break
        assert _feed(s, [_day(i, "100") for i in range(6)]) == []
        assert _counters(s)["days_in_warmup"] == 5
        assert _counters(s)["breaks_down"] == 0


class TestFailedBreakdown:
    def test_a_break_that_closes_back_inside_within_two_days_goes_long(self) -> None:
        s = _strategy()
        [intent] = _setup_long(s)
        assert intent.direction is Direction.LONG
        assert intent.requested_position == 40
        assert intent.metadata["session"] == "long"
        assert intent.metadata["level"] == "98"
        assert intent.metadata["stop"] == "97", "the lowest close of the break"
        assert intent.metadata["take_profit"] == "101", "the channel midpoint (104+98)/2"
        assert intent.created_at == BASE + timedelta(days=7, minutes=1)
        c = _counters(s)
        assert c["breaks_down"] == 1 and c["signals_long"] == 1 and c["orders_emitted"] == 1

    def test_the_stop_follows_the_lowest_close_of_the_break(self) -> None:
        s = _strategy()
        closes = [*WINDOW, "97", "95", "99"]  # deeper on day 6, reclaim on day 7
        [intent] = _feed(s, [_day(i, c) for i, c in enumerate(closes)] + [_day(8, "99")])
        assert intent.metadata["stop"] == "95"

    def test_a_break_not_reclaimed_in_time_is_not_a_trade(self) -> None:
        s = _strategy()
        closes = [*WINDOW, "97", "96", "96", "99"]  # reclaim on day 8 is too late
        assert _feed(s, [_day(i, c) for i, c in enumerate(closes)] + [_day(9, "99")]) == []
        assert _counters(s)["breaks_not_failed"] == 1
        assert _counters(s)["signals_long"] == 0

    def test_a_break_of_a_level_set_yesterday_is_a_slide_not_a_range(self) -> None:
        s = _strategy()
        closes = ["100", "100", "100", "100", "98", "97", "99"]  # L=98 set the day before
        assert _feed(s, [_day(i, c) for i, c in enumerate(closes)] + [_day(7, "99")]) == []
        assert _counters(s)["breaks_too_young"] == 1

    def test_only_closes_are_read(self) -> None:
        """Wild highs and lows on every bar change nothing."""
        s = _strategy()
        closes = [*WINDOW, "97", "99"]
        bars = [_day(i, c, high="150", low="50") for i, c in enumerate(closes)] + [
            _day(7, "99", high="150", low="50")
        ]
        [intent] = _feed(s, bars)
        assert intent.metadata["stop"] == "97" and intent.metadata["take_profit"] == "101"


class TestFailedBreakup:
    def test_the_mirror_goes_short(self) -> None:
        s = _strategy()
        closes = [
            "96",
            "100",
            "102",
            "100",
            "98",
            "103",
            "101",
        ]  # H=102 on day 2; break 103; reclaim 101
        [intent] = _feed(s, [_day(i, c) for i, c in enumerate(closes)] + [_day(7, "101")])
        assert intent.direction is Direction.SHORT
        assert intent.requested_position == -40
        assert intent.metadata["stop"] == "103"
        assert intent.metadata["take_profit"] == "99"  # (102 + 96) / 2
        assert _counters(s)["signals_short"] == 1


class TestExits:
    def _in_long(self) -> SolFadeStrategy:
        s = _strategy()
        _setup_long(s)
        s.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("99.15"))
        assert s.position == 40
        return s

    def test_a_close_at_or_below_the_stop_exits(self) -> None:
        s = self._in_long()
        # Day 8 closes at 96, which the rule reads when day 9's bar completes it.
        assert _feed(s, [_day(8, "96")]) == []
        [exit_] = _feed(s, [_day(9, "96")])
        assert exit_.requested_position == 0
        assert exit_.metadata["exit_reason"] == "stop"
        assert _counters(s)["exits_stop"] == 1

    def test_a_close_at_or_above_the_target_exits(self) -> None:
        s = _strategy()
        closes = [*WINDOW, "97", "99"]
        _feed(s, [_day(i, c) for i, c in enumerate(closes)] + [_day(7, "101")])
        s.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("99.15"))
        [exit_] = _feed(s, [_day(8, "101")])  # completes day 7, close 101 >= 101
        assert exit_.metadata["exit_reason"] == "target"

    def test_time_runs_out(self) -> None:
        s = _strategy()
        closes = [*WINDOW, "97", "99"]
        _feed(s, [_day(i, c) for i, c in enumerate(closes)] + [_day(7, "99.5")])
        s.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("99.15"))
        assert _feed(s, [_day(8, "99.5"), _day(9, "99.5")]) == []  # days 7, 8 held: 1, 2
        [exit_] = _feed(s, [_day(10, "99.5")])  # day 9 completes: held 3 = hold_days
        assert exit_.metadata["exit_reason"] == "time"

    def test_an_exit_is_re_emitted_until_filled_then_state_clears(self) -> None:
        s = self._in_long()
        _feed(s, [_day(8, "96")])
        [first] = _feed(s, [_day(9, "96")])
        [again] = _feed(s, [_day(9, "96.2", minute=1)])
        assert again.metadata["exit_reason"] == "stop"
        assert _counters(s)["orders_reemitted"] == 1
        s.on_fill(side=OrderSide.SELL, quantity=40, price=Decimal("96.1"))
        assert s.position == 0
        assert s.describe()["trade"] is None
        assert first.requested_position == 0

    def test_no_new_break_is_considered_while_in_a_trade(self) -> None:
        s = self._in_long()
        # a fresh window-breaking close while long must not open a pending break
        _feed(s, [_day(8, "99.5"), _day(9, "90")])
        assert s.describe()["pending"] is None
        assert _counters(s)["breaks_down"] == 1


class TestRuntimeContract:
    def test_sizes_and_seed(self) -> None:
        assert _strategy().required_order_size == 40
        assert _strategy(position_contracts=1).required_order_size == 1
        assert _strategy().daily_seed_days == 7

    def test_position_is_not_adoptable(self) -> None:
        s = _strategy()
        s.adopt_position(40)
        assert s.position == 0, "levels come from the break; a restart cannot know them"

    def test_describe_carries_the_effective_rule(self) -> None:
        d = _strategy().describe()
        assert d["params_effective"] == {
            "channel_days": "5",
            "min_extreme_age_days": "2",
            "confirm_days": "2",
            "hold_days": "3",
        }
        assert d["target_position"] == 0 and d["pending"] is None and d["trade"] is None
