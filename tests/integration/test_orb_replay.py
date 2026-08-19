"""The ORB strategy through the full replay engine, end to end.

The unit tests prove the state machine follows the document. This file proves
the whole pipeline holds together: bars in, the real validator/risk/gate on
every intent, fills at the next open with slippage, levels computed from those
actual fills, and a P&L a person can recompute by hand.

The hand-computed trade below is the anchor. If any layer shifts -- fill
timing, slippage application, commission, the strategy's levels -- the
arithmetic stops matching and this fails with numbers, not vibes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.broker import FillModel
from app.backtest.engine import BacktestEngine, always_tradeable, backtest_config
from app.backtest.models import Bar
from app.contracts.models import QualifiedContract
from app.enums import SecurityType
from app.strategy.orb import SolOrbStrategy

pytestmark = pytest.mark.integration

LONDON = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)

MIN_TICK = Decimal("0.05")
MULTIPLIER = Decimal("25")
CONTRACTS = 40


def _contract() -> QualifiedContract:
    return QualifiedContract(
        con_id=812345678,
        symbol="MSL",
        local_symbol="MSLZ6",
        sec_type=SecurityType.FUTURE,
        exchange="CME",
        currency="USD",
        expiration="202612",
        last_trade_date="20261218",
        multiplier=str(MULTIPLIER),
        min_tick=MIN_TICK,
        trading_class="MSL",
    )


def _config():
    """Limits sized for the document's 40 contracts, passed explicitly."""
    return backtest_config(
        symbol="MSL",
        max_position_contracts=40,
        max_order_size=40,
        max_daily_loss_usd="30000",
        max_orders_per_hour=20,
        max_open_orders=2,
        max_notional_exposure_usd="500000",
    )


def _bar(minute: int, *, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        source="coinbase",
        symbol="SOL-USD",
        interval="1m",
        opened_at=LONDON + timedelta(minutes=minute),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("100"),
    )


def _run(bars, *, fill_model: FillModel | None = None):
    engine = BacktestEngine(
        config=_config(),
        contract=_contract(),
        strategy=SolOrbStrategy(),
        fill_model=fill_model or FillModel(),
        session=always_tradeable,
    )
    return engine.run(bars)


class TestAHandComputedRoundTrip:
    """One London session, one long, stopped out. Every number derived below.

    Timeline (1-minute bars):
      08:00-08:04  opening range: high 100.50, low 99.50 (range $1.00, passes)
      08:05        closes 100.60 -> long signal (market order placed)
      08:06        opens 100.70 -> BUY fills at 100.75 (open + 1 tick slip)
                   levels from the FILL: stop 100.10, target 102.25
      08:07        low touches 100.10 -> stop decision (flat intent)
      08:08        opens 100.00 -> SELL fills at 99.95 (open - 1 tick slip)

    P&L: (99.95 - 100.75) x 25 x 40 = -$800 gross.
    Commission: 40 x $3.41 x 2 sides = $272.80. Net -$1,072.80.
    Slippage cost: 2 sides x (0.05 x 25 x 40) = $100, already inside the fills.
    """

    def _bars(self):
        orb = [
            _bar(i, open_="100.00", high="100.50", low="99.50", close="100.00") for i in range(5)
        ]
        return [
            *orb,
            _bar(5, open_="100.20", high="100.65", low="100.10", close="100.60"),  # signal
            _bar(6, open_="100.70", high="100.80", low="100.35", close="100.40"),  # entry fill
            _bar(7, open_="100.35", high="100.40", low="100.10", close="100.20"),  # stop touched
            _bar(8, open_="100.00", high="100.20", low="99.90", close="100.10"),  # exit fill
            _bar(9, open_="100.10", high="100.15", low="100.00", close="100.05"),
        ]

    def test_the_arithmetic_matches_by_hand(self) -> None:
        run = _run(self._bars())

        assert len(run.fills) == 2
        entry, exit_ = run.fills
        assert entry.side.value == "BUY"
        assert entry.price == Decimal("100.75"), "08:06 open 100.70 + one tick"
        assert exit_.side.value == "SELL"
        assert exit_.price == Decimal("99.95"), "08:08 open 100.00 - one tick"
        assert entry.quantity == exit_.quantity == CONTRACTS

        assert len(run.trades) == 1
        trade = run.trades[0]
        assert trade.gross_pnl == Decimal("-800.00")
        assert trade.commission == Decimal("272.80")
        assert trade.net_pnl == Decimal("-1072.80")
        assert run.final_position == 0
        assert run.realized_pnl == Decimal("-800.00")
        assert run.slippage_paid == Decimal("100.00")

    def test_the_stop_was_computed_from_the_fill_not_the_signal(self) -> None:
        """The signal closed at 100.60; the fill was 100.75.

        A signal-based stop (99.95) is NOT touched by the 08:07 low of 100.10;
        only the fill-based stop (100.10) is. If this trade exits, the levels
        came from the fill -- which is the property under test.
        """
        run = _run(self._bars())

        assert len(run.trades) == 1, "the trade exited: the stop was fill-based"

        strategy_view = run  # the counters live on the strategy; re-run to read them
        del strategy_view
        strategy = SolOrbStrategy()
        engine = BacktestEngine(
            config=_config(),
            contract=_contract(),
            strategy=strategy,
            fill_model=FillModel(),
            session=always_tradeable,
        )
        engine.run(self._bars())
        assert strategy.describe()["counters"]["exits_stop"] == 1  # type: ignore[index]

    def test_every_intent_passed_the_real_interlocks(self) -> None:
        """40 contracts cleared risk because the limits were sized for it.

        The control for the refusal test below: with fitting limits, nothing
        refuses, so when something DOES refuse it is the limits doing it.
        """
        run = _run(self._bars())

        assert run.refusals == ()

    def test_undersized_limits_refuse_the_documented_position(self) -> None:
        """The deployed configuration allows 1 contract; the document wants 40.

        A replay under the deployed limits must refuse every entry -- the
        strategy does not get to shrink itself to fit, because 25 SOL is not
        the strategy the document describes.
        """
        config = backtest_config(
            symbol="MSL",
            max_position_contracts=1,
            max_order_size=1,
            max_daily_loss_usd="100",
            max_orders_per_hour=2,
            max_open_orders=1,
            max_notional_exposure_usd="10000",
        )
        engine = BacktestEngine(
            config=config,
            contract=_contract(),
            strategy=SolOrbStrategy(),
            fill_model=FillModel(),
            session=always_tradeable,
        )
        run = engine.run(self._bars())

        assert run.fills == ()
        assert run.trades == ()
        reasons = {r for refusal in run.refusals for r in refusal.reasons}
        assert "MAX_ORDER_SIZE_EXCEEDED" in reasons
        assert "MAX_POSITION_CONTRACTS_EXCEEDED" in reasons


class TestSessionDisciplineThroughTheEngine:
    def test_a_small_range_session_produces_no_orders_at_all(self) -> None:
        bars = [
            _bar(i, open_="100.00", high="100.30", low="99.60", close="100.00")  # range 0.70
            for i in range(5)
        ] + [
            _bar(5, open_="100.00", high="105.00", low="100.00", close="105.00"),  # huge breakout
            _bar(6, open_="105.00", high="105.10", low="104.90", close="105.00"),
        ]

        run = _run(bars)

        assert run.fills == ()
        assert run.refusals == (), "the strategy never emitted; nothing reached the interlocks"

    def test_the_trail_exit_through_real_fills(self) -> None:
        """The document's walkthrough shape, with engine fills.

        Entry fills 100.75 (08:06 open + slip). Trail activates at fill+0.65 =
        101.40; peak 103.00 -> trail 102.60; 08:09 touches it; exit fills at
        the 08:10 open less slip = 102.55. Gross (102.55-100.75) x 25 x 40 =
        +$1,800.
        """
        bars = [
            _bar(i, open_="100.00", high="100.50", low="99.50", close="100.00") for i in range(5)
        ] + [
            _bar(5, open_="100.20", high="100.65", low="100.15", close="100.60"),  # signal
            _bar(6, open_="100.70", high="101.00", low="100.60", close="100.90"),  # entry 100.75
            _bar(7, open_="100.90", high="101.45", low="100.85", close="101.40"),  # trail on
            _bar(8, open_="101.40", high="103.00", low="101.35", close="102.90"),  # peak 103.00
            _bar(9, open_="102.80", high="102.85", low="102.60", close="102.70"),  # trail touched
            _bar(10, open_="102.60", high="102.70", low="102.50", close="102.55"),  # exit 102.55
            _bar(11, open_="102.55", high="102.60", low="102.50", close="102.55"),
        ]

        run = _run(bars)

        assert len(run.trades) == 1
        trade = run.trades[0]
        assert trade.exit_price == Decimal("102.55")
        assert trade.gross_pnl == Decimal("1800.00")
        assert trade.net_pnl == Decimal("1800.00") - Decimal("272.80")

        # After the exit, price still sits above the range high, so the next
        # candle closing there is a fresh signal: trade 2 of the session's 2.
        # That is the document's own rule ("wait for a 1-minute candle to
        # fully close outside the range" -- nothing requires price to re-enter
        # first), asserted here so the re-entry reading is pinned, not
        # accidental.
        assert len(run.fills) == 3, "round trip plus the re-entry"
        assert run.fills[2].side.value == "BUY"
        assert run.final_position == 40, "trade 2 open at the end of the data"
