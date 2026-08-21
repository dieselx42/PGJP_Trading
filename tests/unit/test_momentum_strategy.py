"""The momentum strategy, held to the three rules that distinguish it.

sol-momentum exists because of three diagnosed failures, and each has a test
that would fail if the rule were dropped:

* **risk sizing** -- a fixed contract count means risk swings with volatility;
  here size must fall as ATR rises, and the same dollars must be at risk.
* **the cost gate** -- the ORB paid $0.373/SOL to chase $0.40 moves; a stop
  too small relative to that toll must be refused, not merely noted.
* **the regime filter** -- a breakout against the long-term trend must not be
  taken.

Everything else (stop-before-trail pessimism, strict channel comparisons, exit
re-emission keeping its original reason, no same-day re-entry) is inherited
behaviour and tested here too, because inherited is not the same as verified.

The windows are shrunk via the same ``--strategy-params`` path a replay uses,
so the arithmetic stays checkable by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.strategy.momentum import COST_PER_SOL_ROUND_TRIP, SolMomentumStrategy
from app.strategy.noop import STRATEGY_REGISTRY, build_strategy

BASE = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)

#: Small windows, and the regime filter off unless a test is about it.
PARAMS: dict[str, object] = {
    "entry_channel_days": 3,
    "exit_channel_days": 2,
    "atr_days": 3,
    "regime_days": 0,
}


def _bar(at: datetime, *, high: str, low: str, close: str) -> Bar:
    return Bar(
        source="coinbase",
        symbol="SOL-USD",
        interval="1m",
        opened_at=at,
        open=Decimal(close),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
    )


def _day(day: int, *, high: str, low: str, close: str, minute: int = 0) -> Bar:
    return _bar(BASE + timedelta(days=day, minutes=minute), high=high, low=low, close=close)


def _feed(strategy: SolMomentumStrategy, bars: Sequence[Bar]) -> list:
    intents = []
    for bar in bars:
        intents.extend(strategy.handle_bar(bar))
    return intents


def _strategy(**extra: object) -> SolMomentumStrategy:
    return SolMomentumStrategy(params={**PARAMS, **extra})


def _warmup(atr: int) -> list[Bar]:
    """Three days around 100 whose consecutive true range is exactly ``atr``.

    high/low are +/- atr/2 and every close is 100, so
    TR = max(high-low, |high-prev_close|, |low-prev_close|) = atr.
    """
    half = Decimal(atr) / 2
    return [_day(i, high=str(100 + half), low=str(100 - half), close="100") for i in range(3)]


def _entry_price(atr: int) -> Decimal:
    """Where :func:`_breakout` fills: the breakout day's close."""
    return Decimal(100 + atr)


def _breakout(strategy: SolMomentumStrategy, *, atr: int) -> list:
    """Warm up, then break the channel high WITHOUT disturbing the ATR.

    The breakout day is high=close=100+atr, low=100, so its own true range is
    also exactly ``atr`` -- otherwise the breakout's range inflates ATR(3) and
    every stop and size derived from it stops being checkable by hand. It
    closes at 100+atr, above the warmup channel high of 100+atr/2.

    Day 4's bar is what completes day 3, which is when the signal decides.
    """
    close = str(_entry_price(atr))
    bars = [
        *_warmup(atr),
        _day(3, high=close, low="100", close=close),
        _day(4, high=close, low=close, close=close),
    ]
    return _feed(strategy, bars)


class TestRegistry:
    def test_registered_and_buildable(self) -> None:
        assert "sol-momentum" in STRATEGY_REGISTRY
        assert isinstance(build_strategy("sol-momentum"), SolMomentumStrategy)


class TestVolatilitySizing:
    """The headline change: same dollars at risk, whatever the volatility."""

    def test_size_halves_when_volatility_doubles(self) -> None:
        quiet = _strategy(risk_budget="5000")
        loud = _strategy(risk_budget="5000")
        # ATR 10 -> stop $20/SOL -> $500/contract -> 10 contracts.
        quiet_intents = _breakout(quiet, atr=10)
        # ATR 20 -> stop $40/SOL -> $1,000/contract -> 5 contracts.
        loud_intents = _breakout(loud, atr=20)

        assert quiet_intents[0].requested_position == 10
        assert loud_intents[0].requested_position == 5

    def test_the_dollars_at_risk_are_the_same_either_way(self) -> None:
        """The point of the exercise, stated as arithmetic rather than trust."""
        for atr, expected_contracts in ((10, 10), (20, 5)):
            strategy = _strategy(risk_budget="5000")
            intents = _breakout(strategy, atr=atr)
            contracts = intents[0].requested_position
            stop_per_sol = Decimal(str(intents[0].metadata["stop_per_sol"]))
            assert contracts == expected_contracts
            risked = stop_per_sol * Decimal(25) * contracts
            assert risked == Decimal("5000")

    def test_size_is_capped_by_position_contracts(self) -> None:
        """A tiny stop would size enormous; the cap is what stops it."""
        strategy = _strategy(risk_budget="100000", position_contracts=40)
        intents = _breakout(strategy, atr=10)
        assert intents[0].requested_position == 40

    def test_a_signal_that_sizes_below_one_contract_is_skipped(self) -> None:
        """Rounding up to one contract would take more risk than the budget
        allows -- the moment a risk framework becomes a suggestion."""
        # Budget $100, stop $40/SOL -> $1,000/contract -> 0 contracts.
        strategy = _strategy(risk_budget="100")
        assert _breakout(strategy, atr=20) == []
        assert strategy.describe()["counters"]["skipped_size_below_one"] == 1  # type: ignore[index]

    def test_describe_reports_the_sizes_actually_requested(self) -> None:
        strategy = _strategy(risk_budget="5000")
        _breakout(strategy, atr=10)
        sizing = strategy.describe()["sizing"]
        assert sizing["sizes_requested"] == [10]  # type: ignore[index]
        assert sizing["max"] == 10  # type: ignore[index]


class TestCostGate:
    def test_a_stop_below_the_cost_multiple_is_refused(self) -> None:
        """ATR 2 -> stop $4/SOL, under the 25x floor of $9.32."""
        strategy = _strategy()
        assert _breakout(strategy, atr=2) == []
        assert strategy.describe()["counters"]["skipped_cost_gate"] == 1  # type: ignore[index]

    def test_a_stop_above_the_cost_multiple_is_taken(self) -> None:
        """ATR 10 -> stop $20/SOL, over the floor."""
        strategy = _strategy()
        assert len(_breakout(strategy, atr=10)) == 1

    def test_the_gate_moves_with_the_multiple(self) -> None:
        # Same market, gate raised: stop $20/SOL now needs 100 x 0.3728 = $37.28.
        strategy = _strategy(min_stop_cost_multiple="100")
        assert _breakout(strategy, atr=10) == []
        assert strategy.describe()["counters"]["skipped_cost_gate"] == 1  # type: ignore[index]

    def test_the_gate_is_derived_from_the_measured_cost(self) -> None:
        """A hardcoded dollar floor would drift out of step with the fill
        model; the gate must be a multiple of the measured round trip."""
        described = SolMomentumStrategy().describe()["cost_gate"]
        assert described["cost_per_sol_round_trip"] == str(COST_PER_SOL_ROUND_TRIP)  # type: ignore[index]
        assert Decimal(str(described["min_stop_per_sol"])) == (  # type: ignore[index]
            Decimal("25") * COST_PER_SOL_ROUND_TRIP
        )


class TestRegimeFilter:
    def _bars_below_trend(self) -> list[Bar]:
        """A decline, then a bounce that breaks the 3-day channel HIGH while
        still far below the 10-day mean -- the against-the-trend long the
        filter exists to refuse.

        The decline itself fires short signals on the way down. They are never
        filled, so each is simply cancelled and counted; an unfilled entry
        blocks nothing, and the strategy is still flat when the bounce comes.
        Only the LAST bar -- the one completing the bounce day -- is asserted
        on, which is what isolates the regime rule from everything else.
        """
        bars = [
            _day(i, high=str(200 - 10 * i), low=str(190 - 10 * i), close=str(195 - 10 * i))
            for i in range(10)
        ]
        # Day 10 closes at 138: above the prior 3 days' highs (max 130) but
        # below the 10-day mean close of 144.3.
        bars.append(_day(10, high="140", low="100", close="138"))
        bars.append(_day(11, high="138", low="138", close="138"))
        return bars

    def test_a_breakout_against_the_regime_is_refused(self) -> None:
        strategy = _strategy(regime_days=10)
        bars = self._bars_below_trend()
        _feed(strategy, bars[:-1])
        assert _feed(strategy, bars[-1:]) == []
        assert strategy.describe()["counters"]["skipped_regime"] == 1  # type: ignore[index]

    def test_the_same_breakout_is_taken_with_the_filter_off(self) -> None:
        """Proves the refusal above was the REGIME rule, and not some other
        gate quietly rejecting the same bar."""
        strategy = _strategy(regime_days=0)
        bars = self._bars_below_trend()
        _feed(strategy, bars[:-1])
        final = _feed(strategy, bars[-1:])
        assert len(final) == 1
        assert final[0].direction is Direction.LONG
        assert strategy.describe()["counters"]["skipped_regime"] == 0  # type: ignore[index]

    def test_a_breakout_with_the_regime_is_taken(self) -> None:
        strategy = _strategy(regime_days=3)
        assert len(_breakout(strategy, atr=10)) == 1


class TestEntriesAndExits:
    def _filled(self, atr: int = 10) -> tuple[SolMomentumStrategy, int]:
        """Long, filled at 100+atr. At atr=10: 10 contracts, entry 110,
        initial stop 110 - 2*10 = 90, trail 3*10 = 30 behind the peak."""
        strategy = _strategy()
        intents = _breakout(strategy, atr=atr)
        contracts = intents[0].requested_position
        strategy.on_fill(side=OrderSide.BUY, quantity=contracts, price=_entry_price(atr))
        return strategy, contracts

    def test_a_wick_through_the_channel_is_no_signal(self) -> None:
        strategy = _strategy()
        bars = [
            *_warmup(10),
            _day(3, high="150", low="95", close="100"),  # trades through, closes inside
            _day(4, high="100", low="100", close="100"),
        ]
        assert _feed(strategy, bars) == []

    def test_a_close_exactly_at_the_channel_is_no_signal(self) -> None:
        strategy = _strategy()
        bars = [
            *_warmup(10),
            _day(3, high="105", low="95", close="105"),
            _day(4, high="105", low="105", close="105"),
        ]
        assert _feed(strategy, bars) == []

    def test_short_breakouts_are_symmetric(self) -> None:
        strategy = _strategy()
        bars = [
            *_warmup(10),
            _day(3, high="105", low="70", close="72"),
            _day(4, high="72", low="72", close="72"),
        ]
        intents = _feed(strategy, bars)
        assert len(intents) == 1
        assert intents[0].direction is Direction.SHORT
        assert intents[0].requested_position < 0

    def test_initial_stop_is_two_atr_from_the_fill(self) -> None:
        strategy, _ = self._filled(atr=10)
        # Entry 110, ATR 10 -> stop at 90. A low touching 90 exits.
        intents = _feed(strategy, [_day(4, high="111", low="90", close="92", minute=1)])
        assert len(intents) == 1
        assert intents[0].requested_position == 0
        assert intents[0].metadata["exit_reason"] == "stop"

    def test_the_stop_uses_the_previous_bars_level_not_this_bars_trail(self) -> None:
        """One bar makes new highs AND dips to where the trail would sit if it
        advanced off the same bar. Advancing first would manufacture an exit
        from a peak that had not been banked when the dip happened."""
        strategy, _ = self._filled(atr=10)
        # High 140 implies a 110 trail (3xATR = 30 back) -- but only from the
        # NEXT bar. This bar's low of 110 must not trip it; the stop is 90.
        assert _feed(strategy, [_day(4, high="140", low="110", close="138", minute=1)]) == []
        # Now armed: the same low exits, and reports trail rather than stop.
        intents = _feed(strategy, [_day(4, high="138", low="109", close="110", minute=2)])
        assert len(intents) == 1
        assert intents[0].metadata["exit_reason"] == "trail"

    def test_a_cancelled_exit_reemits_with_the_original_reason(self) -> None:
        strategy, _ = self._filled(atr=10)
        first = _feed(strategy, [_day(4, high="111", low="90", close="92", minute=1)])
        assert first[0].metadata["exit_reason"] == "stop"
        again = _feed(strategy, [_day(4, high="200", low="150", close="190", minute=2)])
        assert len(again) == 1
        assert again[0].metadata["exit_reason"] == "stop"

    def test_reentry_waits_for_the_next_completed_day(self) -> None:
        strategy, contracts = self._filled(atr=10)
        _feed(strategy, [_day(4, high="111", low="90", close="92", minute=1)])
        strategy.on_fill(side=OrderSide.SELL, quantity=contracts, price=Decimal("91"))
        assert strategy.position == 0
        # Day 4 itself closes above its channel; suppressed, because the exit
        # was decided on day 4.
        _feed(strategy, [_day(4, high="180", low="90", close="175", minute=3)])
        assert _feed(strategy, [_day(5, high="175", low="175", close="175")]) == []
        assert strategy.describe()["counters"]["entries_suppressed_post_exit"] == 1  # type: ignore[index]

    def test_a_breakout_while_in_a_trade_is_counted_not_taken(self) -> None:
        strategy, _ = self._filled(atr=10)
        _feed(strategy, [_day(4, high="180", low="109", close="175", minute=1)])
        assert _feed(strategy, [_day(5, high="175", low="174", close="175")]) == []
        assert strategy.describe()["counters"]["signals_while_in_trade"] == 1  # type: ignore[index]


class TestFillSafetyNets:
    def test_an_unsignalled_fill_gets_an_emergency_stop_at_entry(self) -> None:
        strategy = _strategy()
        strategy.on_fill(side=OrderSide.BUY, quantity=5, price=Decimal("100"))
        assert strategy.position == 5
        intents = _feed(strategy, [_day(0, high="101", low="99", close="100")])
        assert len(intents) == 1
        assert intents[0].requested_position == 0

    def test_a_fill_opposite_to_the_signal_is_managed_not_crashed(self) -> None:
        strategy = _strategy()
        intents = _breakout(strategy, atr=10)
        strategy.on_fill(
            side=OrderSide.SELL, quantity=intents[0].requested_position, price=_entry_price(10)
        )
        assert strategy.position == -intents[0].requested_position
        # Managed as the short it actually is: entry 110, stop 110 + 20 = 130,
        # so a move up through 130 stops it.
        exits = _feed(strategy, [_day(4, high="131", low="110", close="130", minute=1)])
        assert len(exits) == 1
        assert exits[0].requested_position == 0


class TestParams:
    def test_unknown_params_are_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy parameter"):
            SolMomentumStrategy(params={"risk_budgets": "5000"})

    @pytest.mark.parametrize("bad", ["0", "-1", "nan", "abc", "200000"])
    def test_invalid_risk_budgets_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            SolMomentumStrategy(params={"risk_budget": bad})

    def test_a_trail_tighter_than_the_stop_is_refused(self) -> None:
        with pytest.raises(ValueError, match="trail_atr_mult"):
            SolMomentumStrategy(params={"stop_atr_mult": "3", "trail_atr_mult": "2"})

    def test_an_exit_window_longer_than_the_entry_window_is_refused(self) -> None:
        with pytest.raises(ValueError, match="exit_channel_days"):
            SolMomentumStrategy(params={"entry_channel_days": 5, "exit_channel_days": 6})

    def test_regime_days_zero_is_allowed_as_an_experiment(self) -> None:
        """Turning the most opinionated rule OFF is exactly the check a
        skeptic should be able to run, so it must not be a config error."""
        strategy = SolMomentumStrategy(params={"regime_days": 0})
        assert strategy.describe()["params_effective"]["regime_days"] == "0"  # type: ignore[index]

    def test_size_can_only_be_overridden_down(self) -> None:
        assert (
            SolMomentumStrategy(params={"position_contracts": 1}).describe()["position_contracts"]
            == 1
        )
        with pytest.raises(ValueError, match="position_contracts"):
            SolMomentumStrategy(params={"position_contracts": 41})
