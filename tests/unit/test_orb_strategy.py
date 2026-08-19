"""The SOL 5-minute ORB strategy, held against its own written specification.

Every rule in the operator's document gets a test that would fail if the rule
were dropped or inverted. The walkthrough test at the bottom replays the
document's own example trade, number for number -- if the implementation and
the document ever disagree about that trade, one of them is wrong and this
fails.

Two structural decisions shape the tests:

* **Fills come from `on_fill`, levels from the real fill price.** The tests
  drive that seam explicitly: signal on bar N, fill delivered before bar N+1,
  levels asserted against the fill, not the signal close.
* **Bars are not ticks.** Where one bar spans both a stop and an upgrade, the
  strategy takes the pessimistic reading (adverse extreme first). Tested with
  a bar constructed to span both.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.models import Bar
from app.enums import Direction, OrderSide
from app.strategy.base import BarStrategy
from app.strategy.noop import STRATEGY_REGISTRY, build_strategy
from app.strategy.orb import SolOrbStrategy

#: A Monday. London session opens 08:00 UTC; NY at 14:30 UTC.
LONDON = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
NY = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


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


def _orb_bars(open_at: datetime = LONDON, *, high: str = "100.50", low: str = "99.50") -> list[Bar]:
    """Five opening-range minutes. Default range $1.00 -- passes the filter."""
    mid = (Decimal(high) + Decimal(low)) / 2
    return [
        _bar(open_at + timedelta(minutes=i), open_=str(mid), high=high, low=low, close=str(mid))
        for i in range(5)
    ]


def _feed(strategy: SolOrbStrategy, bars: Sequence[Bar]) -> list:
    intents = []
    for bar in bars:
        intents.extend(strategy.handle_bar(bar))
    return intents


def _fill(strategy: SolOrbStrategy, side: OrderSide, price: str, quantity: int = 40) -> None:
    strategy.on_fill(side=side, quantity=quantity, price=Decimal(price))


def _entered_long(
    strategy: SolOrbStrategy, *, at: datetime = LONDON, fill_price: str = "100.00"
) -> datetime:
    """Walk a strategy into a long with a known fill. Returns the next bar time."""
    _feed(strategy, _orb_bars(at))
    signal_at = at + timedelta(minutes=5)
    [intent] = _feed(strategy, [_bar(signal_at, close="100.60")])
    assert intent.requested_position == 40
    _fill(strategy, OrderSide.BUY, fill_price)
    return signal_at + timedelta(minutes=1)


class TestOpeningRange:
    def test_the_range_is_measured_from_wicks_not_closes(self) -> None:
        """The document says candle high minus candle low.

        Closes here span only $0.10; the wicks span $1.00. Measured from
        closes the session would be skipped and the breakout below missed.
        """
        strategy = SolOrbStrategy()
        bars = [
            _bar(
                LONDON + timedelta(minutes=i),
                open_="100.00",
                high="100.50",
                low="99.50",
                close="100.05",
            )
            for i in range(5)
        ]
        _feed(strategy, bars)

        [intent] = _feed(strategy, [_bar(LONDON + timedelta(minutes=5), close="100.60")])

        assert intent.requested_position == 40
        assert strategy.describe()["counters"]["sessions_skipped_small_range"] == 0  # type: ignore[index]

    def test_a_range_below_80_cents_skips_the_whole_session(self) -> None:
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars(high="100.39", low="99.60"))  # range 0.79

        intents = _feed(strategy, [_bar(LONDON + timedelta(minutes=5), close="105.00")])

        assert intents == []
        assert strategy.describe()["counters"]["sessions_skipped_small_range"] == 1  # type: ignore[index]

    def test_a_range_of_exactly_80_cents_proceeds(self) -> None:
        """The document: "$0.80 or greater -- proceed"."""
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars(high="100.40", low="99.60"))  # range 0.80

        [intent] = _feed(strategy, [_bar(LONDON + timedelta(minutes=5), close="100.41")])

        assert intent.requested_position == 40

    def test_a_missing_opening_minute_skips_the_session(self) -> None:
        """Four bars are not the opening range, and the fifth is not invented."""
        strategy = SolOrbStrategy()
        bars = _orb_bars()
        del bars[2]
        _feed(strategy, bars)

        intents = _feed(strategy, [_bar(LONDON + timedelta(minutes=5), close="105.00")])

        assert intents == []
        assert strategy.describe()["counters"]["sessions_skipped_gap"] == 1  # type: ignore[index]

    def test_both_documented_sessions_are_recognised(self) -> None:
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars(LONDON))
        _feed(strategy, [_bar(LONDON + timedelta(minutes=5), close="100")])
        _feed(strategy, _orb_bars(NY))

        assert strategy.describe()["counters"]["sessions_seen"] == 2  # type: ignore[index]

    def test_a_bar_outside_any_session_starts_nothing(self) -> None:
        strategy = SolOrbStrategy()
        _feed(strategy, [_bar(datetime(2026, 1, 5, 11, 0, tzinfo=UTC), close="200")])

        assert strategy.describe()["counters"]["sessions_seen"] == 0  # type: ignore[index]


class TestEntries:
    def test_a_close_above_the_range_high_is_a_long_signal(self) -> None:
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars())  # high 100.50

        [intent] = _feed(strategy, [_bar(LONDON + timedelta(minutes=5), close="100.51")])

        assert intent.direction is Direction.LONG
        assert intent.requested_position == 40, "1,000 SOL at 25 SOL per contract"

    def test_a_close_below_the_range_low_is_a_short_signal(self) -> None:
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars())  # low 99.50

        [intent] = _feed(strategy, [_bar(LONDON + timedelta(minutes=5), close="99.49")])

        assert intent.direction is Direction.SHORT
        assert intent.requested_position == -40

    def test_a_wick_through_the_range_is_not_a_signal(self) -> None:
        """The document: "a candle that pokes through and closes back inside
        is not a signal"."""
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars())

        intents = _feed(
            strategy,
            [_bar(LONDON + timedelta(minutes=5), high="101.20", close="100.30")],
        )

        assert intents == []

    def test_a_close_exactly_on_the_range_edge_is_not_a_signal(self) -> None:
        """ "Close above" is strictly above; the edge is still inside."""
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars())

        assert _feed(strategy, [_bar(LONDON + timedelta(minutes=5), close="100.50")]) == []
        assert _feed(strategy, [_bar(LONDON + timedelta(minutes=6), close="99.50")]) == []

    def test_no_entries_after_the_90_minute_window(self) -> None:
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars())

        late = LONDON + timedelta(minutes=90)
        intents = _feed(strategy, [_bar(late, close="100.60")])

        assert intents == []
        assert strategy.describe()["counters"]["signals_skipped_window"] == 1  # type: ignore[index]

    def test_an_entry_in_the_last_window_minute_is_taken(self) -> None:
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars())

        [intent] = _feed(strategy, [_bar(LONDON + timedelta(minutes=89), close="100.60")])

        assert intent.requested_position == 40

    def test_at_most_two_filled_trades_per_session(self) -> None:
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)

        # Trade 1 stops out; fill the exit.
        _feed(strategy, [_bar(after, low="99.30", close="99.40")])
        _fill(strategy, OrderSide.SELL, "99.30")

        # Trade 2: price still above the range -> immediate re-entry signal.
        t2 = after + timedelta(minutes=1)
        [intent] = _feed(strategy, [_bar(t2, close="100.70")])
        assert intent.requested_position == 40
        _fill(strategy, OrderSide.BUY, "100.75")
        _feed(strategy, [_bar(t2 + timedelta(minutes=1), low="100.05", close="100.08")])
        _fill(strategy, OrderSide.SELL, "100.05")

        # A third signal: the session is done.
        intents = _feed(strategy, [_bar(t2 + timedelta(minutes=2), close="100.90")])
        assert intents == []
        assert strategy.describe()["counters"]["signals_skipped_session_full"] == 1  # type: ignore[index]

    def test_one_trade_open_at_a_time(self) -> None:
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)

        # Still long; another breakout close fires a signal -- skipped.
        intents = _feed(strategy, [_bar(after, close="100.90")])

        assert intents == []
        assert strategy.describe()["counters"]["signals_skipped_busy"] == 1  # type: ignore[index]

    def test_an_open_trade_blocks_the_next_sessions_signals(self) -> None:
        """ "Finish one session before starting the next".

        The NY prices sit inside the London trade's neutral band -- above its
        99.35 stop, below its 100.40 break-even trigger -- so the trade is
        still open, unchanged, when the NY signal fires.
        """
        strategy = SolOrbStrategy()
        _entered_long(strategy)  # London trade at 100.00, held into NY

        _feed(strategy, _orb_bars(NY, high="100.30", low="99.40"))  # range 0.90
        intents = _feed(strategy, [_bar(NY + timedelta(minutes=5), close="100.35")])

        assert intents == []
        assert strategy.describe()["counters"]["signals_skipped_busy"] == 1  # type: ignore[index]


class TestLevelsComeFromTheRealFill:
    def test_stop_and_target_are_set_from_the_fill_not_the_signal_close(self) -> None:
        """Signal closed at 100.60; the fill came back at 100.70.

        The document sets levels "from your entry price ... as soon as the
        order fills". A stop at 99.95 (signal-based) and one at 100.05
        (fill-based) are different trades; only the fill-based one is right.
        """
        strategy = SolOrbStrategy()
        after = _entered_long(strategy, fill_price="100.70")

        # 100.06 is below a signal-based stop (100.60-0.65=99.95 -> no exit)
        # but above the fill-based stop (100.70-0.65=100.05 -> exit).
        [intent] = _feed(strategy, [_bar(after, low="100.04", close="100.10")])

        assert intent.requested_position == 0
        assert strategy.describe()["counters"]["exits_stop"] == 1  # type: ignore[index]

    def test_an_unfilled_entry_does_not_consume_a_trade_slot(self) -> None:
        """The order was cancelled (no fill arrived). The machine must not
        wedge waiting, and the session still has both its trades."""
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars())
        signal_at = LONDON + timedelta(minutes=5)
        [_] = _feed(strategy, [_bar(signal_at, close="100.60")])
        # No fill delivered: the next bar arrives with the strategy still flat.

        [intent] = _feed(strategy, [_bar(signal_at + timedelta(minutes=1), close="100.65")])

        assert intent.requested_position == 40, "re-signalled; the slot was not burned"
        counters = strategy.describe()["counters"]
        assert counters["entries_cancelled_unfilled"] == 1  # type: ignore[index]


class TestTradeManagement:
    def test_the_initial_stop_is_65_cents_from_entry(self) -> None:
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)  # fill 100.00 -> stop 99.35

        assert _feed(strategy, [_bar(after, low="99.36", close="99.40")]) == []
        [intent] = _feed(strategy, [_bar(after + timedelta(minutes=1), low="99.35", close="99.40")])
        assert intent.requested_position == 0

    def test_breakeven_at_plus_40_locks_entry_plus_5(self) -> None:
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)  # fill 100.00

        _feed(strategy, [_bar(after, high="100.40", close="100.35")])  # BE trigger touched
        [intent] = _feed(
            strategy, [_bar(after + timedelta(minutes=1), low="100.05", close="100.10")]
        )

        assert intent.requested_position == 0
        assert strategy.describe()["counters"]["exits_breakeven"] == 1  # type: ignore[index]

    def test_below_the_be_trigger_the_stop_stays_at_the_original_level(self) -> None:
        """The control for the test above: +0.39 does not move the stop."""
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)

        _feed(strategy, [_bar(after, high="100.39", close="100.35")])
        intents = _feed(
            strategy, [_bar(after + timedelta(minutes=1), low="100.05", close="100.10")]
        )

        assert intents == [], "100.05 is above the untouched 99.35 stop"

    def test_the_trail_activates_at_plus_65_and_cancels_the_target(self) -> None:
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)

        _feed(strategy, [_bar(after, high="100.65", close="100.60")])  # trail on, peak 100.65
        # Price later reaches what would have been the $1.50 target; with the
        # target cancelled there is no exit -- the trail decides.
        intents = _feed(
            strategy, [_bar(after + timedelta(minutes=1), high="101.60", close="101.55")]
        )
        assert intents == []

        # Pull back $0.40 from the 101.60 peak -> trail exit at 101.20.
        [intent] = _feed(
            strategy, [_bar(after + timedelta(minutes=2), low="101.20", close="101.30")]
        )
        assert intent.requested_position == 0
        assert strategy.describe()["counters"]["exits_trail"] == 1  # type: ignore[index]

    def test_the_trail_never_loosens(self) -> None:
        """A lower high must not move the trail down.

        Every gain here stays below the $1.50 target, so the trail -- not the
        target -- is the only mechanism in play. (An earlier version of this
        test gapped past the target on its first bar and then asserted on a
        re-emitted exit intent: it passed with the trail logic inverted.)
        """
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)  # fill 100.00

        # Peak 101.40 -> trail 101.00.
        assert _feed(strategy, [_bar(after, high="101.40", close="101.30")]) == []
        # Lower high 101.20: a loosening trail would drop to 100.80. Low stays
        # above the correct 101.00 trail, so still no exit either way.
        assert (
            _feed(
                strategy,
                [_bar(after + timedelta(minutes=1), high="101.20", low="101.05", close="101.10")],
            )
            == []
        )
        # 100.95 is through the correct trail (101.00) but above the loosened
        # one (100.80): only a trail that held its ground exits here.
        [intent] = _feed(
            strategy,
            [_bar(after + timedelta(minutes=2), high="101.00", low="100.95", close="101.00")],
        )

        assert intent.requested_position == 0
        assert strategy.describe()["counters"]["exits_trail"] == 1  # type: ignore[index]

    def test_a_gap_to_the_target_fills_the_resting_target_order(self) -> None:
        """One bar leaps from below +0.65 straight past +1.50.

        The $1.50 order was resting at the exchange the whole time; nobody
        cancels it mid-bar. It fills.
        """
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)

        [intent] = _feed(strategy, [_bar(after, high="101.55", close="101.40")])

        assert intent.requested_position == 0
        assert strategy.describe()["counters"]["exits_target"] == 1  # type: ignore[index]

    def test_within_one_bar_the_stop_is_checked_before_any_upgrade(self) -> None:
        """A bar that touches the old stop AND the BE trigger is a stop-out.

        Bars are not ticks; the sequence inside is unknowable. The pessimistic
        reading -- adverse extreme first -- is the one this strategy takes.
        """
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)

        [intent] = _feed(strategy, [_bar(after, low="99.35", high="100.45", close="100.40")])

        assert intent.requested_position == 0
        assert strategy.describe()["counters"]["exits_stop"] == 1  # type: ignore[index]

    def test_a_cancelled_exit_is_re_emitted(self) -> None:
        """An open position with no working exit is never an acceptable state."""
        strategy = SolOrbStrategy()
        after = _entered_long(strategy)

        [first] = _feed(strategy, [_bar(after, low="99.35", close="99.40")])
        assert first.requested_position == 0
        # No fill arrives (the exit was cancelled on an untradeable bar).
        [again] = _feed(strategy, [_bar(after + timedelta(minutes=1), close="99.40")])
        assert again.requested_position == 0

    def test_short_trades_mirror_exactly(self) -> None:
        strategy = SolOrbStrategy()
        _feed(strategy, _orb_bars())
        [intent] = _feed(strategy, [_bar(LONDON + timedelta(minutes=5), close="99.40")])
        assert intent.requested_position == -40
        _fill(strategy, OrderSide.SELL, "99.35")  # entry; stop 100.00, target 97.85
        after = LONDON + timedelta(minutes=6)

        # +0.40 in a short's favour is DOWN: 98.95 -> stop locks to 99.30.
        _feed(strategy, [_bar(after, low="98.95", close="99.00")])
        # +0.65: 98.70 -> trail on, trail = low + 0.40.
        _feed(strategy, [_bar(after + timedelta(minutes=1), low="98.70", close="98.75")])
        # New low 98.00 -> trail 98.40; bounce to 98.40 exits.
        _feed(strategy, [_bar(after + timedelta(minutes=2), low="98.00", close="98.10")])
        [exit_intent] = _feed(
            strategy, [_bar(after + timedelta(minutes=3), high="98.40", close="98.30")]
        )

        assert exit_intent.requested_position == 0
        assert strategy.describe()["counters"]["exits_trail"] == 1  # type: ignore[index]


class TestTheDocumentsOwnWalkthrough:
    def test_the_example_trade_number_for_number(self) -> None:
        """Entry 100.00 -> BE at 100.40 -> trail from 100.65 -> peak 103.00 ->
        exit signalled at 102.60. Gross +$2.60/SOL, the document's own example.
        """
        strategy = SolOrbStrategy()
        after = _entered_long(strategy, fill_price="100.00")
        steps = [
            _bar(after, high="100.40", close="100.38"),
            _bar(after + timedelta(minutes=1), high="100.65", close="100.60"),
            _bar(after + timedelta(minutes=2), high="101.80", close="101.75"),
            _bar(after + timedelta(minutes=3), high="103.00", close="102.90"),
        ]
        for step in steps:
            held = list(strategy.handle_bar(step))
            assert held == [], f"no exit before the pullback: {step.opened_at}"

        [intent] = _feed(
            strategy, [_bar(after + timedelta(minutes=4), low="102.60", close="102.70")]
        )

        assert intent.requested_position == 0
        assert strategy.describe()["counters"]["exits_trail"] == 1  # type: ignore[index]


class TestRegistryAndLiveRefusal:
    def test_the_strategy_is_registered(self) -> None:
        assert "sol-orb" in STRATEGY_REGISTRY
        assert isinstance(build_strategy("sol-orb"), SolOrbStrategy)

    def test_it_is_a_bar_strategy(self) -> None:
        assert issubclass(SolOrbStrategy, BarStrategy)

    def test_feeding_it_quotes_fails_loudly_not_silently(self) -> None:
        """A bar strategy on a quote feed must not appear to be watching."""
        from app.market_data.models import Quote

        strategy = SolOrbStrategy()
        quote = Quote(
            contract_key="x", symbol="MSL", received_at=LONDON, source="t", last=Decimal("1")
        )
        with pytest.raises(NotImplementedError, match="bar strategy"):
            strategy.on_quote(quote)

    @pytest.mark.safety
    def test_the_live_runtime_refuses_to_start_with_it(self, tmp_path) -> None:
        """Backtest first, wire live second -- enforced, not aspirational."""
        import asyncio

        from app.config import Config, ConfigError
        from app.main import TradingApplication
        from tests.conftest import default_env

        config = Config.from_env(
            default_env(
                STRATEGY_NAME="sol-orb",
                DATABASE_PATH=str(tmp_path / "t.db"),
                LOG_DIR=str(tmp_path / "logs"),
                HEALTH_PORT="0",
            )
        )
        app = TradingApplication(config)

        with pytest.raises(ConfigError, match="no bar feed"):
            asyncio.run(app.startup())

        app.database.close()
