"""The SOL close-versus-average strategy, held against its own written rules.

Same structure as the trend tests: every rule in the module docstring gets a
test that would fail if the rule were dropped or inverted, and the fill seam
is driven explicitly -- intent on bar N, fill delivered before bar N+1, the
position asserted only ever to move through ``on_fill``.

The window is shrunk to 3 days via the same ``--strategy-params`` path a
replay uses, so every average is checkable by hand: three closes, one sum,
one division. The day helper feeds one 1-minute bar per UTC day; a day with
a single bar is a complete day, and the aggregation tests prove multi-bar
days and gaps compose the way the docstring says.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.strategy.noop import STRATEGY_REGISTRY, build_strategy
from app.strategy.sma import SolSmaStrategy

BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # a Monday

#: Three closes per average, so the arithmetic is one line by hand.
PARAMS: dict[str, object] = {"sma_days": 3}

COUNTERS = {
    "days_completed",
    "days_in_warmup",
    "targets_long",
    "targets_short",
    "ties_held",
    "flips",
    "orders_emitted",
    "orders_reemitted",
    "fills_off_target",
    "exits_unsignalled",
}


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
    close: str = "100",
    minute: int = 0,
    high: str | None = None,
    low: str | None = None,
) -> Bar:
    """One 1-minute bar standing in for UTC day ``day`` (0-based from BASE)."""
    return _bar(BASE + timedelta(days=day, minutes=minute), high=high, low=low, close=close)


def _feed(strategy: SolSmaStrategy, bars: Sequence[Bar]) -> list:
    intents = []
    for bar in bars:
        got = strategy.handle_bar(bar)
        assert len(got) <= 1, "never more than one intent per bar"
        intents.extend(got)
    return intents


def _strategy(**extra: object) -> SolSmaStrategy:
    return SolSmaStrategy(params={**PARAMS, **extra})


def _counters(strategy: SolSmaStrategy) -> dict[str, int]:
    counters = strategy.describe()["counters"]
    assert isinstance(counters, dict)
    return counters


def _long_signal(strategy: SolSmaStrategy) -> list:
    """Days 0-2 close 100, 100, 103: SMA(3) = 303 / 3 = 101 < 103, so the
    target becomes +40 on day 3's first bar, the one that completes day 2."""
    bars = [_day_bar(0), _day_bar(1), _day_bar(2, close="103"), _day_bar(3, close="103")]
    return _feed(strategy, bars)


def _filled_long() -> SolSmaStrategy:
    strategy = _strategy()
    intents = _long_signal(strategy)
    assert len(intents) == 1 and intents[0].requested_position == 40
    strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("103.05"))
    assert strategy.position == 40
    return strategy


class TestRegistry:
    def test_registered_and_buildable(self) -> None:
        assert "sol-sma" in STRATEGY_REGISTRY
        assert isinstance(build_strategy("sol-sma"), SolSmaStrategy)


class TestWarmup:
    def test_nothing_is_emitted_until_the_window_is_full(self) -> None:
        """Two completed days cannot make a 3-day average. Every close here is
        higher than the last, so a strategy willing to guess an average from
        too few days would already be long."""
        strategy = _strategy()
        bars = [_day_bar(0, close="100"), _day_bar(1, close="101"), _day_bar(2, close="102")]
        assert _feed(strategy, bars) == []
        assert _counters(strategy)["days_in_warmup"] == 2
        assert _counters(strategy)["days_completed"] == 2
        assert strategy.describe()["target_position"] == 0

    def test_the_first_full_window_decides_with_the_documented_numbers(self) -> None:
        strategy = _strategy()
        intents = _long_signal(strategy)
        assert len(intents) == 1
        intent = intents[0]
        assert intent.direction is Direction.LONG
        assert intent.requested_position == 40
        assert intent.metadata["session"] == "long"
        assert intent.metadata["trade_n"] == 1
        assert "exit_reason" not in intent.metadata
        assert intent.metadata["sma"] == "101"
        assert intent.metadata["signal_close"] == "103"
        assert intent.metadata["signal_day"] == "2026-01-07"
        assert _counters(strategy)["days_in_warmup"] == 2
        assert _counters(strategy)["targets_long"] == 1
        assert strategy.describe()["target_position"] == 40
        assert strategy.describe()["signal_day"] == "2026-01-07"

    def test_the_decision_waits_for_the_day_to_complete_not_one_bar_early(self) -> None:
        """Day 2 closes at 103 from its first bar onward -- above the average
        it will have -- but nothing may fire until a bar from a LATER day
        proves the day is over. Deciding on the in-progress day is the
        look-ahead this rule must not have."""
        strategy = _strategy()
        bars = [
            _day_bar(0),
            _day_bar(1),
            _day_bar(2, close="103"),
            _day_bar(2, close="103", minute=1),
            _day_bar(2, close="103", minute=1439),
        ]
        assert _feed(strategy, bars) == []
        assert strategy.describe()["target_position"] == 0
        intents = _feed(strategy, [_day_bar(3, close="103")])
        assert len(intents) == 1 and intents[0].requested_position == 40


class TestEntries:
    def test_short_is_symmetric(self) -> None:
        """Closes 100, 100, 97: SMA 99 > 97 -> short."""
        strategy = _strategy()
        bars = [_day_bar(0), _day_bar(1), _day_bar(2, close="97"), _day_bar(3, close="97")]
        intents = _feed(strategy, bars)
        assert len(intents) == 1
        assert intents[0].direction is Direction.SHORT
        assert intents[0].requested_position == -40
        assert intents[0].metadata["session"] == "short"
        assert intents[0].metadata["trade_n"] == 1
        assert "exit_reason" not in intents[0].metadata
        assert intents[0].metadata["sma"] == "99"
        assert _counters(strategy)["targets_short"] == 1

    def test_the_average_reads_closes_not_lows_and_compares_the_close_not_the_high(
        self,
    ) -> None:
        """SHORT scenario: closes 100, 100, 97 -> average 99, and 97 < 99.

        Lows of 50 all week would make a lows-average of 50, below the close,
        and turn this LONG; a 150 high on the decision day would do the same
        if the HIGH were what got compared. Either wrong reading flips the
        sign, so this test fails on both. (It cannot catch highs feeding the
        average: 150s average to 150 and the day is still a short -- that is
        what the LONG twin below is for.)
        """
        strategy = _strategy()
        bars = [
            _day_bar(0, low="50"),
            _day_bar(1, low="50"),
            _day_bar(2, close="97", high="150", low="50"),
            _day_bar(3, close="97"),
        ]
        intents = _feed(strategy, bars)
        assert len(intents) == 1 and intents[0].direction is Direction.SHORT
        assert intents[0].metadata["sma"] == "99"

    def test_the_average_reads_closes_not_highs_and_compares_the_close_not_the_low(
        self,
    ) -> None:
        """LONG scenario, the mirror: closes 100, 100, 103 -> average 101.

        A lows-average is always at or below a closes-average, so no
        SHORT-outcome series can tell highs-in-the-average from closes. This
        one can: highs of 150 average to 150, above the 103 close, and would
        turn it SHORT -- as would comparing the 50 LOW instead of the close.
        Live highs are understated anyway (sampled bars), which is the other
        reason the rule must never read them.
        """
        strategy = _strategy()
        bars = [
            _day_bar(0, high="150"),
            _day_bar(1, high="150"),
            _day_bar(2, close="103", high="150", low="50"),
            _day_bar(3, close="103"),
        ]
        intents = _feed(strategy, bars)
        assert len(intents) == 1 and intents[0].direction is Direction.LONG
        assert intents[0].metadata["sma"] == "101"

    def test_position_comes_only_from_fills(self) -> None:
        strategy = _strategy()
        _long_signal(strategy)
        assert strategy.position == 0
        strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("103.05"))
        assert strategy.position == 40

    def test_intent_plumbing_the_validator_checks(self) -> None:
        strategy = _strategy()
        intent = _long_signal(strategy)[0]
        assert intent.symbol == "MSL"
        assert intent.strategy_name == "sol-sma"
        assert intent.created_at == BASE + timedelta(days=3, minutes=1)
        assert intent.metadata["bar_close"] == "103"


class TestStrictness:
    """A close AT the average is not a signal; an equality mutation fails here."""

    def test_a_close_exactly_at_the_average_holds_flat_before_any_decision(self) -> None:
        """Closes 100, 102, 101: SMA = 303 / 3 = 101 == the close."""
        strategy = _strategy()
        bars = [_day_bar(0), _day_bar(1, close="102"), _day_bar(2, close="101"), _day_bar(3)]
        assert _feed(strategy, bars) == []
        assert _counters(strategy)["ties_held"] == 1
        assert strategy.describe()["target_position"] == 0

    def test_a_hair_above_the_average_is_long(self) -> None:
        """Closes 100, 102, 101.05: SMA = 101.0166..., below the close."""
        strategy = _strategy()
        bars = [_day_bar(0), _day_bar(1, close="102"), _day_bar(2, close="101.05"), _day_bar(3)]
        intents = _feed(strategy, bars)
        assert len(intents) == 1 and intents[0].direction is Direction.LONG
        assert _counters(strategy)["ties_held"] == 0

    def test_a_close_exactly_at_the_average_holds_the_current_side(self) -> None:
        """Long from days 0-2. Day 3 closes at 101.5: SMA(100, 103, 101.5) =
        304.5 / 3 = 101.5, a tie, so the long is held and nothing is emitted.
        Reading a tie as 'not above, therefore short' would flip here."""
        strategy = _filled_long()
        _feed(strategy, [_day_bar(3, close="101.5", minute=1)])
        assert _feed(strategy, [_day_bar(4, close="101.5")]) == []
        assert strategy.describe()["target_position"] == 40
        assert _counters(strategy)["ties_held"] == 1
        assert _counters(strategy)["flips"] == 0


class TestFlip:
    def test_a_close_through_the_average_flips_in_one_intent(self) -> None:
        """Long 40, then day 3 closes at 95: SMA(100, 103, 95) = 99.33 > 95.
        Day 4's first bar must emit ONE intent for -40 -- not a flat and then
        a short -- carrying the entry keys of the new short AND the exit
        reason of the old long, which is what the engine's book reads from a
        flip-through-flat fill."""
        strategy = _filled_long()
        _feed(strategy, [_day_bar(3, close="95", minute=1)])
        intents = _feed(strategy, [_day_bar(4, close="95")])
        assert len(intents) == 1
        intent = intents[0]
        assert intent.direction is Direction.SHORT
        assert intent.requested_position == -40
        assert intent.metadata["session"] == "short"
        assert intent.metadata["trade_n"] == 1
        assert intent.metadata["exit_reason"] == "flip"
        assert intent.metadata["signal_day"] == "2026-01-08"
        counters = _counters(strategy)
        assert counters["flips"] == 1
        assert counters["targets_short"] == 1
        assert counters["orders_emitted"] == 2

    def test_after_the_flip_fills_nothing_more_is_emitted(self) -> None:
        strategy = _filled_long()
        _feed(strategy, [_day_bar(3, close="95", minute=1)])
        _feed(strategy, [_day_bar(4, close="95")])
        # One 80-lot SELL: 40 closes the long, 40 opens the short.
        strategy.on_fill(side=OrderSide.SELL, quantity=80, price=Decimal("94.95"))
        assert strategy.position == -40
        assert _feed(strategy, [_day_bar(4, close="94", minute=i) for i in range(1, 6)]) == []
        assert _counters(strategy)["fills_off_target"] == 0

    def test_a_same_side_day_changes_nothing(self) -> None:
        """Long, and the next day closes above its average again: same
        target, no intent, no counter -- the rule is a state, not a signal
        that re-fires every day."""
        strategy = _filled_long()
        _feed(strategy, [_day_bar(3, close="110", minute=1)])
        assert _feed(strategy, [_day_bar(4, close="110")]) == []
        assert _counters(strategy)["targets_long"] == 1
        assert _counters(strategy)["orders_emitted"] == 1


class TestIdempotence:
    def test_at_target_the_strategy_is_silent(self) -> None:
        """Fifty bars of the same day with the position on target must produce
        nothing: an intent equal to the held position is a no_change refusal
        in the replay and noise in the live log."""
        strategy = _filled_long()
        bars = [_day_bar(3, close=str(100 + i % 7), minute=i) for i in range(1, 51)]
        assert _feed(strategy, bars) == []
        assert _counters(strategy)["orders_emitted"] == 1


class TestCancelledOrder:
    def test_an_unfilled_intent_is_reemitted_identically_until_it_fills(self) -> None:
        """No fill arrives after the first intent (an untradeable bar, or a
        venue that did not fill). The next bar re-emits the same target with
        the same metadata -- the decision has not changed, only the book --
        and counts it, so a replay that keeps missing fills says so."""
        strategy = _strategy()
        first = _long_signal(strategy)[0]
        again = _feed(strategy, [_day_bar(3, close="103", minute=1)])
        assert len(again) == 1
        assert again[0].requested_position == 40
        assert again[0].metadata == first.metadata
        assert _counters(strategy)["orders_reemitted"] == 1
        assert _counters(strategy)["orders_emitted"] == 2
        strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("103.05"))
        assert _feed(strategy, [_day_bar(3, close="103", minute=2)]) == []

    def test_a_cancelled_flip_keeps_its_exit_reason(self) -> None:
        strategy = _filled_long()
        _feed(strategy, [_day_bar(3, close="95", minute=1)])
        first = _feed(strategy, [_day_bar(4, close="95")])
        again = _feed(strategy, [_day_bar(4, close="99", minute=1)])
        assert first[0].metadata["exit_reason"] == "flip"
        assert again[0].metadata["exit_reason"] == "flip"
        assert again[0].requested_position == -40
        assert _counters(strategy)["orders_reemitted"] == 1


class TestPartialFill:
    def _flip_pending(self) -> SolSmaStrategy:
        strategy = _filled_long()
        _feed(strategy, [_day_bar(3, close="95", minute=1)])
        intents = _feed(strategy, [_day_bar(4, close="95")])
        assert intents[0].requested_position == -40
        return strategy

    def test_a_fill_that_only_flattens_is_topped_up_as_a_plain_entry(self) -> None:
        """Half of the 80-lot fills: the long is gone, the short is not on.
        The next bar asks for -40 again, and since nothing is being closed
        the intent carries the short's entry keys and no exit reason."""
        strategy = self._flip_pending()
        strategy.on_fill(side=OrderSide.SELL, quantity=40, price=Decimal("94.95"))
        assert strategy.position == 0
        assert _counters(strategy)["fills_off_target"] == 1
        intents = _feed(strategy, [_day_bar(4, close="95", minute=1)])
        assert len(intents) == 1
        assert intents[0].requested_position == -40
        assert intents[0].metadata["session"] == "short"
        assert "exit_reason" not in intents[0].metadata

    def test_a_fill_that_leaves_some_long_still_reads_as_a_flip(self) -> None:
        strategy = self._flip_pending()
        strategy.on_fill(side=OrderSide.SELL, quantity=30, price=Decimal("94.95"))
        assert strategy.position == 10
        intents = _feed(strategy, [_day_bar(4, close="95", minute=1)])
        assert len(intents) == 1
        assert intents[0].requested_position == -40
        assert intents[0].metadata["exit_reason"] == "flip"
        assert intents[0].metadata["session"] == "short"


class TestFillSafetyNets:
    def test_an_unsignalled_fill_is_flattened_on_the_next_bar(self) -> None:
        """A fill the strategy never asked for, during warm-up: the position
        must not be held unmanaged and must not crash the machine. The next
        bar asks for flat, says why, and keeps asking until the book agrees;
        the counter fires once, not once per re-emit."""
        strategy = _strategy()
        strategy.on_fill(side=OrderSide.BUY, quantity=40, price=Decimal("100"))
        assert strategy.position == 40
        assert _counters(strategy)["fills_off_target"] == 1
        intents = _feed(strategy, [_day_bar(0)])
        assert len(intents) == 1
        assert intents[0].requested_position == 0
        assert intents[0].direction is Direction.FLAT
        assert intents[0].metadata["exit_reason"] == "unsignalled"
        assert "session" not in intents[0].metadata
        again = _feed(strategy, [_day_bar(0, minute=1)])
        assert again[0].metadata["exit_reason"] == "unsignalled"
        assert _counters(strategy)["exits_unsignalled"] == 1
        assert _counters(strategy)["orders_reemitted"] == 1
        strategy.on_fill(side=OrderSide.SELL, quantity=40, price=Decimal("100"))
        assert _feed(strategy, [_day_bar(0, minute=2)]) == []

    def test_a_fill_opposite_to_the_target_is_corrected_not_crashed(self) -> None:
        strategy = _strategy()
        _long_signal(strategy)
        strategy.on_fill(side=OrderSide.SELL, quantity=40, price=Decimal("103.05"))
        assert strategy.position == -40
        assert _counters(strategy)["fills_off_target"] == 1
        intents = _feed(strategy, [_day_bar(3, close="103", minute=1)])
        assert len(intents) == 1
        assert intents[0].requested_position == 40
        assert intents[0].metadata["exit_reason"] == "flip"
        assert intents[0].metadata["session"] == "long"

    def test_an_overfill_is_reduced_to_the_target(self) -> None:
        strategy = _strategy()
        _long_signal(strategy)
        strategy.on_fill(side=OrderSide.BUY, quantity=80, price=Decimal("103.05"))
        assert strategy.position == 80
        intents = _feed(strategy, [_day_bar(3, close="103", minute=1)])
        assert len(intents) == 1
        assert intents[0].requested_position == 40
        assert intents[0].metadata["exit_reason"] == "unsignalled"
        assert _counters(strategy)["exits_unsignalled"] == 1


class TestDayAggregation:
    def test_the_close_used_is_the_days_last_bar(self) -> None:
        """Day 2's first bar closes at 103, its second at 99. The average of
        100, 100, 99 is 99.67, above 99: SHORT. A strategy that took the
        first bar's close would go long here."""
        strategy = _strategy()
        bars = [
            _day_bar(0),
            _day_bar(1),
            _day_bar(2, close="103"),
            _day_bar(2, close="99", minute=600),
            _day_bar(3, close="99"),
        ]
        intents = _feed(strategy, bars)
        assert len(intents) == 1 and intents[0].direction is Direction.SHORT
        assert intents[0].metadata["signal_close"] == "99"

    def test_a_missing_day_is_skipped_not_invented(self) -> None:
        """Days 0, 1 and 3 exist; day 2 does not. The average runs over the
        three days present (100, 100, 103) and the decision fires on day 4's
        bar, which completes day 3. days_completed counts three, not four:
        the gap shows up in that number, never as a fabricated candle."""
        strategy = _strategy()
        bars = [_day_bar(0), _day_bar(1), _day_bar(3, close="103"), _day_bar(4, close="103")]
        intents = _feed(strategy, bars)
        assert len(intents) == 1 and intents[0].direction is Direction.LONG
        assert intents[0].metadata["signal_day"] == "2026-01-08"
        assert _counters(strategy)["days_completed"] == 3

    def test_the_first_bar_after_a_gap_completes_the_day_before_it(self) -> None:
        """Days 0-2 exist, then nothing until day 5. Day 5's first bar is what
        completes day 2, so the decision it carries is day 2's -- late, but
        the same decision, and stamped with day 2's date."""
        strategy = _strategy()
        bars = [_day_bar(0), _day_bar(1), _day_bar(2, close="103"), _day_bar(5, close="103")]
        intents = _feed(strategy, bars)
        assert len(intents) == 1 and intents[0].direction is Direction.LONG
        assert intents[0].metadata["signal_day"] == "2026-01-07"
        assert _counters(strategy)["days_completed"] == 3


class TestWeekendIsOrdinary:
    def test_a_sunday_close_decides_exactly_like_a_weekday_close(self) -> None:
        """BASE is a Monday, so days 4-6 are Friday to Sunday. The Sunday
        close completes on Monday's first bar and decides like any other:
        there is no weekday logic anywhere in the rule. Live, CME's closure
        removes Saturday's bars entirely, and the docstring says what that
        does; the strategy itself does not know what a weekend is."""
        weekend = _strategy()
        bars = [_day_bar(4), _day_bar(5), _day_bar(6, close="103"), _day_bar(7, close="103")]
        intents = _feed(weekend, bars)
        assert len(intents) == 1 and intents[0].direction is Direction.LONG
        assert intents[0].metadata["signal_day"] == "2026-01-11"
        assert _counters(weekend)["days_completed"] == 3

        weekday = _strategy()
        reference = _long_signal(weekday)
        assert reference[0].metadata["sma"] == intents[0].metadata["sma"]
        assert reference[0].requested_position == intents[0].requested_position


class TestParams:
    def test_unknown_params_are_refused_not_ignored(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy parameter"):
            SolSmaStrategy(params={"sma_day": 5})

    def test_session_opens_is_refused(self) -> None:
        """--sessions is an ORB-only diagnostic; the CLI injects it as
        session_opens and a 24/7 daily rule must refuse it, not run on it."""
        with pytest.raises(ValueError, match="session_opens"):
            SolSmaStrategy(params={"session_opens": ["08:00"]})

    @pytest.mark.parametrize("bad", ["0", "1", "-1", "abc", "366", "nan", "2.5"])
    def test_invalid_windows_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            SolSmaStrategy(params={"sma_days": bad})

    @pytest.mark.parametrize("ok", ["2", "365", "25", "100"])
    def test_the_window_range_admits_the_pre_registered_rows(self, ok: str) -> None:
        described = SolSmaStrategy(params={"sma_days": ok}).describe()
        assert described["params_effective"]["sma_days"] == ok  # type: ignore[index]

    def test_size_can_only_be_overridden_down(self) -> None:
        assert (
            SolSmaStrategy(params={"position_contracts": 1}).describe()["position_contracts"] == 1
        )
        assert (
            SolSmaStrategy(params={"position_contracts": "1"}).describe()["position_contracts"] == 1
        )
        with pytest.raises(ValueError, match="position_contracts"):
            SolSmaStrategy(params={"position_contracts": 41})
        with pytest.raises(ValueError, match="position_contracts"):
            SolSmaStrategy(params={"position_contracts": 0})

    def test_a_smaller_size_is_the_target_and_the_flip_is_twice_it(self) -> None:
        strategy = SolSmaStrategy(params={**PARAMS, "position_contracts": 1})
        intents = _long_signal(strategy)
        assert intents[0].requested_position == 1
        strategy.on_fill(side=OrderSide.BUY, quantity=1, price=Decimal("103.05"))
        _feed(strategy, [_day_bar(3, close="95", minute=1)])
        intents = _feed(strategy, [_day_bar(4, close="95")])
        assert intents[0].requested_position == -1

    def test_the_defaults_are_the_pre_registered_rule(self) -> None:
        described = SolSmaStrategy().describe()
        assert described["params_effective"] == {"sma_days": "50"}
        assert described["position_contracts"] == 40
        assert "50-day" in str(described["note"])

    def test_describe_echoes_the_effective_rule_set(self) -> None:
        described = _strategy().describe()
        assert described["name"] == "sol-sma"
        assert described["params_effective"]["sma_days"] == "3"  # type: ignore[index]
        assert described["position_contracts"] == 40
        assert described["target_position"] == 0
        assert described["signal_day"] is None
        counters = described["counters"]
        assert isinstance(counters, dict)
        assert set(counters) == COUNTERS
        assert all(value == 0 for value in counters.values())


class TestSeeding:
    """A restart hands the rule its missed days; it builds state and says nothing."""

    @staticmethod
    def _seed_bars(closes: Sequence[str]) -> list[Bar]:
        from app.strategy.seed import daily_bar

        return [
            daily_bar(
                source="coinbase",
                symbol="SOL-USD",
                day=(BASE + timedelta(days=i)).date(),
                close=Decimal(c),
            )
            for i, c in enumerate(closes)
        ]

    def test_seeding_builds_the_window_and_emits_nothing(self) -> None:
        strategy = _strategy()
        assert strategy.seed(self._seed_bars(["100", "100", "103"])) == 3
        assert not strategy.seeding
        c = _counters(strategy)
        assert c["days_completed"] == 2, "the third seeded day is still in progress"
        assert c["days_in_warmup"] == 2
        assert c["orders_emitted"] == 0, "seeding asks for nothing"
        # The first LIVE bar, on day 3, completes day 2: the window is full,
        # 103 > mean(100, 100, 103) = 101, and the entry goes out at once.
        [intent] = _feed(strategy, [_day_bar(3, close="103")])
        assert intent.requested_position == 40
        assert _counters(strategy)["orders_emitted"] == 1

    def test_the_day_hook_is_silent_while_seeding_and_fires_live(self) -> None:
        strategy = _strategy()
        seen: list = []
        strategy.on_day_completed = seen.append
        strategy.seed(self._seed_bars(["100", "100", "103"]))
        assert seen == [], "seeded days are already in storage"
        _feed(strategy, [_day_bar(3, close="103")])
        assert [d.day for d in seen] == [(BASE + timedelta(days=2)).date()]
        assert seen[0].close == Decimal("103")

    def test_adopting_a_matching_position_asks_for_nothing(self) -> None:
        strategy = _strategy()
        strategy.seed(self._seed_bars(["100", "100", "103"]))
        strategy.adopt_position(40)
        assert strategy.position == 40
        assert _feed(strategy, [_day_bar(3, close="103")]) == []
        assert _counters(strategy)["orders_emitted"] == 0

    def test_adopting_the_wrong_side_flips_on_the_next_bar(self) -> None:
        strategy = _strategy()
        strategy.seed(self._seed_bars(["100", "100", "103"]))
        strategy.adopt_position(-40)
        [intent] = _feed(strategy, [_day_bar(3, close="103")])
        assert intent.requested_position == 40
        assert intent.metadata["exit_reason"] == "flip"

    def test_sizes_the_runtime_checks(self) -> None:
        assert _strategy().required_order_size == 80
        assert _strategy(position_contracts=1).required_order_size == 2
        assert _strategy().daily_seed_days == 3
