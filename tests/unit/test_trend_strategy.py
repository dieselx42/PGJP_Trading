"""The SOL daily trend strategy, held against its own written rules.

Same structure as the ORB tests: every rule in the module docstring gets a
test that would fail if the rule were dropped or inverted, and the fill seam
is driven explicitly -- signal on bar N, fill delivered before bar N+1, levels
asserted against the fill price.

The tests run with small windows (3-day entry channel, 2-day exit channel,
3-day ATR) via the same ``--strategy-params`` path a replay uses, so the
arithmetic stays small enough to check by hand. The day helper feeds one
1-minute bar per UTC day: a day with a single bar is a complete day, and the
aggregation test proves multi-bar days compose correctly.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.strategy.noop import STRATEGY_REGISTRY, build_strategy
from app.strategy.trend import SolTrendStrategy

BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)

#: Small windows so channel and ATR values are checkable by hand.
PARAMS: dict[str, object] = {"entry_channel_days": 3, "exit_channel_days": 2, "atr_days": 3}


def _bar(
    at: datetime,
    *,
    open_: str | None = None,
    high: str | None = None,
    low: str | None = None,
    close: str = "100",
) -> Bar:
    c = Decimal(close)
    o = Decimal(open_) if open_ is not None else c
    return Bar(
        source="coinbase",
        symbol="SOL-USD",
        interval="1m",
        opened_at=at,
        open=o,
        high=Decimal(high) if high is not None else max(o, c),
        low=Decimal(low) if low is not None else min(o, c),
        close=c,
        volume=Decimal("10"),
    )


def _day_bar(
    day: int,
    *,
    high: str = "101",
    low: str = "99",
    close: str = "100",
    minute: int = 0,
) -> Bar:
    """One 1-minute bar standing in for UTC day ``day`` (0-based from BASE)."""
    return _bar(
        BASE + timedelta(days=day, minutes=minute),
        open_=close,
        high=high,
        low=low,
        close=close,
    )


def _feed(strategy: SolTrendStrategy, bars: Sequence[Bar]) -> list:
    intents = []
    for bar in bars:
        intents.extend(strategy.handle_bar(bar))
    return intents


def _strategy(**extra: object) -> SolTrendStrategy:
    return SolTrendStrategy(params={**PARAMS, **extra})


def _warmup_days() -> list[Bar]:
    """Days 0-2: high 101, low 99, close 100. TR = 2 for every pair."""
    return [_day_bar(i) for i in range(3)]


def _long_entry(strategy: SolTrendStrategy) -> list:
    """Days 0-2 flat, day 3 closes at 103 above the 101 channel with TR 5
    (high 105, low 100), so ATR(3) = (2 + 2 + 5) / 3 = 3 exactly. The signal
    fires on day 4's first bar, which completes day 3. That completing bar
    holds a 103 low so day 4's aggregate never drags the later exit-channel
    tests down to the warmup lows."""
    bars = [
        *_warmup_days(),
        _day_bar(3, high="105", low="100", close="103"),
        _day_bar(4, high="104", low="103", close="103.5"),
    ]
    return _feed(strategy, bars)


class TestRegistry:
    def test_registered_and_buildable(self) -> None:
        assert "sol-trend" in STRATEGY_REGISTRY
        strategy = build_strategy("sol-trend")
        assert isinstance(strategy, SolTrendStrategy)


class TestDayAggregation:
    def test_a_days_high_is_the_max_across_its_bars(self) -> None:
        """Two bars per warmup day; the second extends the high to 101. The
        entry channel must therefore be 101: a day-3 close of 100.8 breaks
        the first-bar-only reading (100.5) but not the real channel, so a
        signal here would prove aggregation dropped the second bar."""
        strategy = _strategy()
        bars = []
        for day in range(3):
            bars.append(_day_bar(day, high="100.5", low="99", close="100"))
            bars.append(_day_bar(day, high="101", low="99.5", close="100", minute=600))
        bars.append(_day_bar(3, high="100.9", low="99", close="100.8"))
        bars.append(_day_bar(4))
        assert _feed(strategy, bars) == []

        # And the mirror: a close above the aggregated 101 does signal.
        strategy = _strategy()
        bars[-2] = _day_bar(3, high="105", low="100", close="101.5")
        intents = _feed(strategy, bars)
        assert len(intents) == 1 and intents[0].direction is Direction.LONG


class TestWarmup:
    def test_no_entries_before_the_channel_has_enough_days(self) -> None:
        """Every warmup day closes higher than everything before it -- each
        would be a breakout if the strategy were willing to guess a channel
        from too few days."""
        strategy = _strategy()
        bars = [
            _day_bar(i, high=str(100 + 2 * i + 1), low=str(100 + 2 * i - 1), close=str(100 + 2 * i))
            for i in range(3)
        ]
        bars.append(_day_bar(3, high="111", low="109", close="110"))
        assert _feed(strategy, bars) == []
        counters = strategy.describe()["counters"]
        assert counters["days_in_warmup"] > 0  # type: ignore[index]
        assert counters["entries_long"] == 0  # type: ignore[index]


class TestEntries:
    def test_long_breakout_signals_with_the_documented_levels(self) -> None:
        strategy = _strategy()
        intents = _long_entry(strategy)
        assert len(intents) == 1
        intent = intents[0]
        assert intent.direction is Direction.LONG
        assert intent.requested_position == 40
        assert intent.metadata["session"] == "long"
        assert intent.metadata["atr"] == "3"

    def test_short_breakout_is_symmetric(self) -> None:
        strategy = _strategy()
        bars = [*_warmup_days(), _day_bar(3, high="100", low="95", close="97"), _day_bar(4)]
        intents = _feed(strategy, bars)
        assert len(intents) == 1
        assert intents[0].direction is Direction.SHORT
        assert intents[0].requested_position == -40
        assert intents[0].metadata["session"] == "short"

    def test_a_close_inside_the_channel_is_no_signal(self) -> None:
        strategy = _strategy()
        bars = [*_warmup_days(), _day_bar(3, high="100.9", low="99.1", close="100.5"), _day_bar(4)]
        assert _feed(strategy, bars) == []

    def test_a_wick_through_the_channel_is_no_signal(self) -> None:
        """Day 3 trades to 105 but CLOSES back inside. Close-based, like the
        ORB: a wick is not a breakout."""
        strategy = _strategy()
        bars = [*_warmup_days(), _day_bar(3, high="105", low="99", close="100.5"), _day_bar(4)]
        assert _feed(strategy, bars) == []

    def test_position_comes_only_from_fills(self) -> None:
        strategy = _strategy()
        _long_entry(strategy)
        assert strategy.position == 0
        strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("103.50"))
        assert strategy.position == 40


class TestExits:
    def _filled_long(self) -> SolTrendStrategy:
        """A long filled at 103.50 with frozen ATR 3: initial stop 97.50,
        trail 3 x 3 = 9 behind the peak."""
        strategy = _strategy()
        _long_entry(strategy)
        strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("103.50"))
        return strategy

    def test_initial_stop_is_two_atr_from_the_fill(self) -> None:
        strategy = self._filled_long()
        # Low touches 97.50 exactly: stopped, pessimistically.
        intents = _feed(strategy, [_day_bar(4, high="104", low="97.50", close="98", minute=1)])
        assert len(intents) == 1
        assert intents[0].requested_position == 0
        assert intents[0].metadata["exit_reason"] == "stop"

    def test_a_low_above_the_stop_does_not_exit(self) -> None:
        strategy = self._filled_long()
        intents = _feed(strategy, [_day_bar(4, high="104", low="97.51", close="98", minute=1)])
        assert intents == []

    def test_trail_ratchets_and_reports_trail_not_stop(self) -> None:
        strategy = self._filled_long()
        # Peak reaches 110 -> trailed stop 101, above the 97.50 initial.
        assert _feed(strategy, [_day_bar(4, high="110", low="103", close="109", minute=1)]) == []
        # A drop through 101 is a TRAIL exit -- the stop no longer sits at
        # its initial level, and the attribution must say so.
        intents = _feed(strategy, [_day_bar(4, high="109", low="100.9", close="101", minute=2)])
        assert len(intents) == 1
        assert intents[0].metadata["exit_reason"] == "trail"

    def test_trail_never_loosens(self) -> None:
        strategy = self._filled_long()
        _feed(strategy, [_day_bar(4, high="110", low="103", close="109", minute=1)])
        # A quieter bar with a lower high must not lower the trailed stop.
        _feed(strategy, [_day_bar(4, high="105", low="102", close="104", minute=2)])
        intents = _feed(strategy, [_day_bar(4, high="104", low="100.9", close="101", minute=3)])
        assert len(intents) == 1
        assert intents[0].metadata["exit_reason"] == "trail"

    def test_channel_exit_on_a_completed_day_close(self) -> None:
        strategy = self._filled_long()
        # Days 4-6 hold above the stop; day 6 closes at 102, below the
        # 2-day exit channel (days 4-5 lows = 103). The exit fires on day
        # 7's first bar, which completes day 6.
        _feed(strategy, [_day_bar(4, high="104", low="103", close="103.5", minute=1)])
        _feed(strategy, [_day_bar(5, high="104", low="103", close="103.5")])
        _feed(strategy, [_day_bar(6, high="103.5", low="102", close="102")])
        intents = _feed(strategy, [_day_bar(7, high="102.5", low="101.8", close="102")])
        assert len(intents) == 1
        assert intents[0].requested_position == 0
        assert intents[0].metadata["exit_reason"] == "channel"

    def test_stop_is_checked_before_the_channel_exit(self) -> None:
        """Day 7's first bar both completes a channel-breaking day AND trades
        through the stop. Pessimistic reading: the stop fired first."""
        strategy = self._filled_long()
        _feed(strategy, [_day_bar(4, high="104", low="103", close="103.5", minute=1)])
        _feed(strategy, [_day_bar(5, high="104", low="103", close="103.5")])
        _feed(strategy, [_day_bar(6, high="103.5", low="102", close="102")])
        intents = _feed(strategy, [_day_bar(7, high="102", low="97", close="97.2")])
        assert len(intents) == 1
        assert intents[0].metadata["exit_reason"] == "stop"

    def test_a_cancelled_exit_reemits_with_the_original_reason(self) -> None:
        strategy = self._filled_long()
        first = _feed(strategy, [_day_bar(4, high="104", low="97.50", close="98", minute=1)])
        assert first[0].metadata["exit_reason"] == "stop"
        # No fill arrives (untradeable bar); the re-emit keeps the reason even
        # though the next bar's prices would tell a different story.
        again = _feed(strategy, [_day_bar(4, high="110", low="105", close="109", minute=2)])
        assert len(again) == 1
        assert again[0].requested_position == 0
        assert again[0].metadata["exit_reason"] == "stop"

    def test_after_an_exit_fill_the_book_is_flat_and_reentry_is_possible(self) -> None:
        strategy = self._filled_long()
        _feed(strategy, [_day_bar(4, high="104", low="97.50", close="98", minute=1)])
        strategy.on_fill(side=OrderSide.SELL, quantity=40, price=Decimal("97.40"))
        assert strategy.position == 0
        # Day 4 closes back above its 3-day channel -> a fresh signal fires
        # when day 5's first bar completes it.
        _feed(strategy, [_day_bar(4, high="107", low="97", close="106", minute=3)])
        intents = _feed(strategy, [_day_bar(5, high="106", low="105", close="105.5")])
        assert len(intents) == 1
        assert intents[0].direction is Direction.LONG


class TestOneAtATime:
    def test_a_breakout_while_in_a_trade_is_counted_not_taken(self) -> None:
        strategy = _strategy()
        _long_entry(strategy)
        strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("103.50"))
        # Day 4 closes above its channel again; day 5's first bar completes it
        # while the trade is open. No intent, one counter.
        _feed(strategy, [_day_bar(4, high="108", low="103", close="107", minute=1)])
        intents = _feed(strategy, [_day_bar(5, high="107", low="106", close="106.5")])
        assert intents == []
        assert strategy.describe()["counters"]["signals_while_in_trade"] == 1  # type: ignore[index]


class TestCancelledEntry:
    def test_an_unfilled_entry_is_counted_and_the_machine_recovers(self) -> None:
        strategy = _strategy()
        intents = _long_entry(strategy)
        assert len(intents) == 1
        # No fill arrives. The next bar clears the pending state.
        _feed(strategy, [_day_bar(4, minute=1)])
        assert strategy.describe()["counters"]["entries_cancelled_unfilled"] == 1  # type: ignore[index]
        # Day 4 itself closes above ITS channel -- which now contains day 3's
        # 105 high -- so the fresh signal needs a close beyond 105, on day 5.
        _feed(strategy, [_day_bar(4, high="106.5", low="99", close="106", minute=2)])
        intents = _feed(strategy, [_day_bar(5, high="105", low="104", close="104.5")])
        assert len(intents) == 1


class TestParams:
    def test_unknown_params_are_refused_not_ignored(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy parameter"):
            SolTrendStrategy(params={"entry_chanel_days": 5})

    @pytest.mark.parametrize("bad", ["0", "-1", "abc", "201"])
    def test_invalid_windows_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            SolTrendStrategy(params={"entry_channel_days": bad})

    @pytest.mark.parametrize("bad", ["0", "-2", "21", "x"])
    def test_invalid_multiples_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            SolTrendStrategy(params={"stop_atr_mult": bad})

    def test_size_can_only_be_overridden_down(self) -> None:
        assert SolTrendStrategy(params={"position_contracts": 1}).describe()[
            "position_contracts"
        ] == 1
        with pytest.raises(ValueError, match="position_contracts"):
            SolTrendStrategy(params={"position_contracts": 41})
        with pytest.raises(ValueError, match="position_contracts"):
            SolTrendStrategy(params={"position_contracts": 0})

    def test_describe_echoes_the_effective_rule_set(self) -> None:
        described = SolTrendStrategy(params={"stop_atr_mult": "2.5", **PARAMS}).describe()
        effective = described["params_effective"]
        assert effective["stop_atr_mult"] == "2.5"  # type: ignore[index]
        assert effective["entry_channel_days"] == "3"  # type: ignore[index]
        assert effective["atr_days"] == "3"  # type: ignore[index]
