"""A bar strategy running LIVE: the full path, no seams skipped.

quotes -> BarBuilder -> completed bar -> SolOrbStrategy -> real validator,
risk manager and transmit gate -> OrderManager -> MockBroker fill ->
`on_fill` -> managed trade.

Everything between the quote and the fill is the production code path. The
one substitution is the validator's staleness limit: bars here carry fixed
historical timestamps so the test is deterministic, and a real wall clock
would refuse them for their age -- a fact about the fixture, not the system.
The staleness behaviour itself has its own focused test below
(:class:`TestSignalFreshness`), against the real 60-second limit.

Why these tests are paranoid about ORDERING: the backtest engine guarantees a
fill is reported before the bar that follows it, and the ORB strategy's state
machine is built on that. `_tick_bars` reproduces the guarantee live by
polling fills before feeding any completed bar -- and if fills cannot be
polled, bars WAIT rather than being fed to a strategy that would mistake an
unreported fill for a cancelled order and double up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.models import BrokerError
from app.config import Config
from app.main import TradingApplication
from app.market_data.models import Quote
from app.signals.validator import SignalValidator
from app.strategy.orb import SolOrbStrategy
from tests.conftest import permissive_env
from tests.integration.test_application import live_app

pytestmark = pytest.mark.integration

#: A fixed London session on a past date: deterministic, no DST question, no
#: dependence on when the test suite happens to run.
SESSION = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)


def _config(tmp_path: Path, **overrides: str) -> Config:
    env = permissive_env(
        STRATEGY_NAME="sol-orb",
        # The size override under test: the document says 40, first paper
        # trades run at 1, and permissive_env's MAX_ORDER_SIZE=2 would refuse
        # 40 anyway -- which is itself the deployed-limits story in miniature.
        STRATEGY_POSITION_CONTRACTS="1",
        DATABASE_PATH=str(tmp_path / "t.db"),
        LOG_DIR=str(tmp_path / "logs"),
        MARKET_DATA_POLL_INTERVAL_SECONDS="0.05",
    )
    env.update(overrides)
    return Config.from_env(env)


def _quote(minute: float, price: str, *, seconds: float = 0.0) -> Quote:
    return Quote(
        contract_key="conid:987654321",
        symbol="MSL",
        received_at=SESSION + timedelta(minutes=minute, seconds=seconds),
        source="mock",
        last=Decimal(price),
    )


def _prepare(app: TradingApplication) -> None:
    """Point the app at the fixture's clock instead of the wall's.

    Two adjustments, both about time and neither about behaviour:

    * The harness's startup ticks fed today's MockBroker quotes into the
      builder, so a bucket for the current wall-clock minute is in progress.
      Fixture quotes are stamped in the past and would all be dropped as
      out-of-order behind it. Cleared -- exactly what a disconnect does.
    * The validator's 60-second staleness limit would refuse every fixture
      intent for its age, which is a fact about the fixture's timestamps, not
      the system. Swapped through the public constructor with everything else
      identical. The staleness behaviour itself is tested on the real limit in
      :class:`TestSignalFreshness`.
    """
    assert app.strategy is not None and app.validator is not None
    assert app.bar_builder is not None
    app.bar_builder.clear()
    app._pending_bars.clear()
    replacement = SignalValidator(
        configured_symbol=app.config.default_futures_symbol,
        known_strategies=[app.strategy.name],
        max_signal_age_seconds=float("inf"),
    )
    replacement.seed_seen(app.repositories.signals.known_intent_ids())
    app.validator = replacement
    if app.order_manager is not None:
        app.order_manager.validator = replacement


async def _feed(app: TradingApplication, quotes: list[Quote], *, at_minute: float) -> None:
    """Feed quotes with the virtual clock at SESSION + at_minute."""
    await app._tick_bars(quotes, now=SESSION + timedelta(minutes=at_minute))


#: Five opening-range minutes: high 151.00, low 150.00 -> range $1.00 >= $0.80.
ORB_QUOTES = [
    _quote(0, "150.20"),
    _quote(0, "151.00", seconds=30),
    _quote(1, "150.60"),
    _quote(2, "150.00"),
    _quote(3, "150.40"),
    _quote(4, "150.80"),
]


class TestTheFullLivePath:
    async def test_a_breakout_becomes_a_filled_managed_position(self, tmp_path) -> None:
        """The whole point of the bar feed, end to end."""
        async with live_app(_config(tmp_path)) as app:
            _prepare(app)
            strategy = app.strategy
            assert isinstance(strategy, SolOrbStrategy)
            assert app.contract is not None

            # Opening range forms; nothing may trade during it.
            await _feed(app, ORB_QUOTES, at_minute=5)
            assert strategy.quotes_seen == 5, "the five range bars, nothing else"
            assert strategy.describe()["counters"]["signals_taken"] == 0  # type: ignore[index]

            # Minute 5 closes above the range: the signal.
            await _feed(app, [_quote(5, "151.30")], at_minute=6)
            assert strategy.describe()["counters"]["signals_taken"] == 1  # type: ignore[index]

            # The market order filled at the broker within the same call; the
            # next completed bar is preceded by a fills poll, so the strategy
            # hears about its entry BEFORE it sees minute 6.
            await _feed(app, [_quote(6, "151.40")], at_minute=7)

            assert strategy.position == 1, "the fill reached on_fill"
            assert app.position_book.quantity(app.contract.con_id) == 1
            assert not app.repositories.orders.open_orders(), "market order; nothing resting"

    async def test_the_size_override_reaches_the_order(self, tmp_path) -> None:
        """STRATEGY_POSITION_CONTRACTS=1 means a 1-contract order, not 40.

        Without the override the intent asks for 40, MAX_ORDER_SIZE=2 refuses
        it, and the system sits armed while trading nothing -- the exact shape
        paper trading would have hit on the deployed 1-contract limits.
        """
        async with live_app(_config(tmp_path)) as app:
            _prepare(app)
            strategy = app.strategy
            assert isinstance(strategy, SolOrbStrategy)
            assert app.contract is not None

            await _feed(app, ORB_QUOTES, at_minute=5)
            await _feed(app, [_quote(5, "151.30")], at_minute=6)
            await _feed(app, [_quote(6, "151.40")], at_minute=7)

            assert app.position_book.quantity(app.contract.con_id) == 1, (
                "one contract, as overridden -- not the document's 40"
            )

    async def test_no_bar_no_trade(self, tmp_path) -> None:
        """Quotes alone never reach the strategy; only completed bars do."""
        async with live_app(_config(tmp_path)) as app:
            _prepare(app)
            strategy = app.strategy
            assert isinstance(strategy, SolOrbStrategy)

            # A flood of quotes, all within one minute that has not ended:
            # no bar can complete.
            await _feed(
                app,
                [_quote(0, "150.00", seconds=s) for s in range(0, 59, 5)],
                at_minute=59 / 60,
            )

            assert strategy.quotes_seen == 0, "the strategy saw nothing: no bar completed"


class TestFillBeforeBarOrdering:
    async def test_bars_wait_when_fills_cannot_be_polled(self, tmp_path) -> None:
        """The engine's guarantee, enforced live.

        A strategy fed bar N+1 while its bar-N entry's fill is unreported
        would conclude the entry was cancelled and free the slot -- the
        double-entry bug, silently. So when fills cannot be polled, completed
        bars queue; nothing reaches the strategy.
        """
        async with live_app(_config(tmp_path)) as app:
            _prepare(app)
            strategy = app.strategy
            assert isinstance(strategy, SolOrbStrategy)
            broker = app.broker
            assert broker is not None

            original = broker.get_fills

            async def refuse(*, since_seconds: int = 86_400):
                raise BrokerError("simulated: executions unavailable")

            broker.get_fills = refuse  # type: ignore[method-assign]
            await _feed(app, ORB_QUOTES, at_minute=5)
            assert strategy.quotes_seen == 0, "bars queued, none fed"
            assert len(app._pending_bars) == 5

            broker.get_fills = original  # type: ignore[method-assign]
            await _feed(app, [], at_minute=5)
            assert strategy.quotes_seen == 5, "the backlog drained once fills were readable"
            assert app._pending_bars == []

    async def test_a_runaway_backlog_disables_the_strategy(self, tmp_path) -> None:
        """Hours of queued bars is a market that no longer exists.

        Replaying it at the strategy after fills recover would fire entries
        into prices from another regime. Past the bound, the strategy is
        disabled and the backlog dropped -- fail closed, visibly.
        """
        async with live_app(_config(tmp_path)) as app:
            _prepare(app)
            strategy = app.strategy
            assert isinstance(strategy, SolOrbStrategy)
            broker = app.broker
            assert broker is not None

            async def refuse(*, since_seconds: int = 86_400):
                raise BrokerError("simulated: executions unavailable")

            broker.get_fills = refuse  # type: ignore[method-assign]
            quotes = [_quote(m, "150.00") for m in range(182)]
            await _feed(app, quotes, at_minute=182)

            assert strategy.enabled is False
            assert "backlog" in strategy.params["disabled_reason"]
            assert app._pending_bars == []


class TestPositionMismatchGuard:
    async def test_a_position_the_strategy_did_not_open_disables_it(self, tmp_path) -> None:
        """Restart-mid-trade, in miniature.

        The book holds a contract; the strategy believes it is flat. Its next
        management decision would be computed against a fiction, so the
        reconcile-time invariant disables it -- and deliberately does NOT
        flatten: closing a position is the operator's decision.
        """
        async with live_app(_config(tmp_path)) as app:
            strategy = app.strategy
            assert isinstance(strategy, SolOrbStrategy)
            assert app.contract is not None
            assert strategy.enabled

            app.position_book.apply_fill(
                con_id=app.contract.con_id,
                symbol="MSL",
                local_symbol=app.contract.local_symbol,
                side=__import__("app.enums", fromlist=["OrderSide"]).OrderSide.BUY,
                quantity=1,
                price=Decimal("150"),
                at=SESSION,
            )
            app._check_strategy_position_agrees()

            assert strategy.enabled is False
            assert "did not open" in strategy.params["disabled_reason"]

    async def test_an_agreeing_position_does_not_disable(self, tmp_path) -> None:
        """The control: flat book, flat belief, strategy stays armed."""
        async with live_app(_config(tmp_path)) as app:
            strategy = app.strategy
            assert strategy is not None

            app._check_strategy_position_agrees()

            assert strategy.enabled is True


class TestHaltAndDisconnectHygiene:
    async def test_the_kill_switch_drops_the_bar_in_progress_and_the_backlog(
        self, tmp_path
    ) -> None:
        """Resuming from a halt starts from a gap, never a stale backlog."""
        async with live_app(_config(tmp_path)) as app:
            _prepare(app)
            strategy = app.strategy
            assert isinstance(strategy, SolOrbStrategy)
            assert app.bar_builder is not None

            app.bar_builder.add(_quote(0, "150.00"))
            app._pending_bars.extend(app.bar_builder.add(_quote(1, "150.10")))
            assert app._pending_bars

            app.kill_switch.engage("test halt", engaged_at=SESSION.isoformat())
            await app._tick()

            assert app._pending_bars == []
            assert app.bar_builder.describe()["in_progress"] is None

    async def test_disconnect_clears_the_feed(self, tmp_path) -> None:
        async with live_app(_config(tmp_path)) as app:
            _prepare(app)
            assert app.bar_builder is not None
            app.bar_builder.add(_quote(0, "150.00"))
            app._pending_bars.extend(app.bar_builder.add(_quote(1, "150.10")))

            app._handle_disconnect("test")

            assert app._pending_bars == []
            assert app.bar_builder.describe()["in_progress"] is None


class TestSignalFreshness:
    """The regression the closed_at stamp exists to prevent, on the REAL limit.

    The validator's staleness limit is 60 seconds and a 1-minute bar closes 60
    seconds after it opens. An intent stamped with the bar's OPEN is therefore
    already at the limit the instant the bar completes, and stale one second
    later -- live, every ORB signal would have been refused, silently, while
    the system sat armed. Stamped with the CLOSE, the intent has its full
    budget. The backtest could never catch this: its validator has no clock.
    """

    @pytest.mark.safety
    def test_a_close_stamped_intent_is_fresh_and_an_open_stamped_one_is_stale(self) -> None:
        from datetime import datetime as dt

        from app.backtest.models import Bar
        from app.signals.models import TradeIntent
        from app.utilities.timeutils import utc_now

        now = utc_now()
        # The bar completed one second ago; the runtime is validating its
        # signal now -- the ordinary live case.
        bar = Bar(
            source="ibkr",
            symbol="MSL",
            interval="1m",
            opened_at=(now - timedelta(seconds=61)).replace(microsecond=0),
            open=Decimal("150"),
            high=Decimal("152"),
            low=Decimal("150"),
            close=Decimal("151.5"),
        )
        validator = SignalValidator(configured_symbol="MSL", known_strategies=["sol-orb"])

        def intent(stamp: dt) -> TradeIntent:
            return TradeIntent(
                strategy_name="sol-orb",
                symbol="MSL",
                direction=__import__("app.enums", fromlist=["Direction"]).Direction.LONG,
                requested_position=1,
                created_at=stamp,
            )

        assert validator.validate(intent(bar.closed_at)).accepted is True
        stale = validator.validate(intent(bar.opened_at))
        assert stale.accepted is False
        assert "SIGNAL_TIMESTAMP_STALE" in stale.reasons

    def test_the_strategy_stamps_the_close(self) -> None:
        from app.backtest.models import Bar

        strategy = SolOrbStrategy()
        bar = Bar(
            source="ibkr",
            symbol="MSL",
            interval="1m",
            opened_at=SESSION,
            open=Decimal("150"),
            high=Decimal("150.1"),
            low=Decimal("150"),
            close=Decimal("150"),
        )
        intent = strategy._intent(1, bar)
        assert intent.created_at == bar.closed_at
        assert intent.created_at == SESSION + timedelta(minutes=1)
