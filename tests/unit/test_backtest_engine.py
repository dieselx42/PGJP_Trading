"""The replay, and the interlocks it really runs.

Two failure modes shape this file.

The first is the **vacuous refusal suite**. It is trivial to write a backtester
that refuses everything and then prove it refuses correctly under ten different
conditions; every test passes and the engine is useless.
:class:`TestTheReplayActuallyTrades` is the control, and every refusal test
below is only meaningful because it exists. It is the same reasoning as
``test_all_green_baseline_allows_transmission`` in ``test_critical_safety.py``.

The second is **lookahead**. A replay that fills at the close of the bar its
strategy just examined trades on information it did not have, produces an
equity curve that looks good, and gives no sign anything is wrong.
:class:`TestNoLookahead` pins the fill to the *next* bar's open through the full
engine, not just the broker.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import app.risk.manager as risk_manager_module
from app.backtest.broker import FillModel
from app.backtest.engine import (
    BacktestEngine,
    BacktestRun,
    always_tradeable,
    backtest_config,
    cme_liquid_hours,
)
from app.backtest.models import Bar
from app.config import Config, ConfigError
from app.contracts.models import QualifiedContract
from app.enums import Direction, SecurityType, TradingMode
from app.market_data.models import Quote
from app.risk.limits import REASON_ORDER_SIZE_EXCEEDED, REASON_POSITION_EXCEEDED
from app.safety.gate import REASON_KILL_SWITCH, REASON_TRANSMIT_NOT_ALLOWED
from app.signals.models import TradeIntent
from app.signals.validator import REASON_DUPLICATE, REASON_STRATEGY_MISMATCH
from app.strategy.base import Strategy

#: A Monday, inside CME liquid hours (13:30-21:00 UTC), safely in the past so
#: the validator's future-timestamp check is never the thing under test.
T0 = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)

MIN_TICK = Decimal("0.05")
MULTIPLIER = Decimal("25")
COMMISSION = Decimal("3.41")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _contract() -> QualifiedContract:
    return QualifiedContract(
        con_id=987654321,
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


def _config(**overrides: str) -> Config:
    return backtest_config(
        symbol="MSL",
        max_position_contracts=3,
        max_order_size=2,
        max_daily_loss_usd="5000",
        max_orders_per_hour=100,
        max_open_orders=2,
        max_notional_exposure_usd="100000",
        overrides=overrides or None,
    )


def _bars(prices: Sequence[tuple[str, str]], *, start: datetime = T0) -> list[Bar]:
    """One 1-minute bar per (open, close) pair. High/low bracket both."""
    bars = []
    for index, (open_, close) in enumerate(prices):
        pair = [Decimal(open_), Decimal(close)]
        bars.append(
            Bar(
                source="binance",
                symbol="SOLUSDT",
                interval="1m",
                opened_at=start + timedelta(minutes=index),
                open=Decimal(open_),
                high=max(pair),
                low=min(pair),
                close=Decimal(close),
            )
        )
    return bars


def _flat(count: int, price: str = "80", *, start: datetime = T0) -> list[Bar]:
    return _bars([(price, price)] * count, start=start)


# ---------------------------------------------------------------------------
# Strategies. Deliberately trivial: the engine is what is under test.
# ---------------------------------------------------------------------------


class _Target(Strategy):
    """Asks for one fixed target position, on every quote."""

    name = "target"

    def __init__(self, target: int = 1, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._target = target

    def on_quote(self, quote: Quote) -> Sequence[TradeIntent]:
        return (_intent(self.name, self._target, quote.received_at),)


class _Alternating(Strategy):
    """Long on odd quotes, flat on even ones. Produces round trips."""

    name = "alternating"

    def on_quote(self, quote: Quote) -> Sequence[TradeIntent]:
        target = 1 if self.quotes_seen % 2 == 1 else 0
        return (_intent(self.name, target, quote.received_at),)


class _Mislabelled(Strategy):
    """Emits intents attributed to a strategy that is not registered."""

    name = "mislabelled"

    def on_quote(self, quote: Quote) -> Sequence[TradeIntent]:
        return (_intent("ghost", 1, quote.received_at),)


class _Repeater(Strategy):
    """Emits the SAME intent id every time -- a strategy that lost its state."""

    name = "repeater"

    def on_quote(self, quote: Quote) -> Sequence[TradeIntent]:
        return (_intent(self.name, 1, quote.received_at, intent_id="stuck"),)


def _intent(
    strategy: str, target: int, at: datetime, *, intent_id: str | None = None
) -> TradeIntent:
    direction = Direction.LONG if target > 0 else Direction.FLAT if target == 0 else Direction.SHORT
    kwargs: dict[str, object] = {}
    if intent_id is not None:
        kwargs["intent_id"] = intent_id
    return TradeIntent(
        strategy_name=strategy,
        symbol="MSL",
        direction=direction,
        requested_position=target,
        created_at=at,
        **kwargs,  # type: ignore[arg-type]
    )


def _run(
    strategy: Strategy,
    bars: Sequence[Bar],
    *,
    config: Config | None = None,
    session: object = always_tradeable,
    fill_model: FillModel | None = None,
) -> BacktestRun:
    engine = BacktestEngine(
        config=config or _config(),
        contract=_contract(),
        strategy=strategy,
        fill_model=fill_model,
        session=session,  # type: ignore[arg-type]
    )
    return engine.run(bars)


def _reasons(run: BacktestRun, stage: str | None = None) -> set[str]:
    return {
        reason
        for refusal in run.refusals
        if stage is None or refusal.stage == stage
        for reason in refusal.reasons
    }


def _stages(run: BacktestRun) -> set[str]:
    return {refusal.stage for refusal in run.refusals}


# ---------------------------------------------------------------------------
# The control. Without this every refusal test below is vacuous.
# ---------------------------------------------------------------------------


class TestTheReplayActuallyTrades:
    def test_a_permitted_strategy_produces_fills_and_a_position(self) -> None:
        run = _run(_Target(1), _flat(5))

        assert run.fills, "the replay refused everything; every refusal test below is vacuous"
        assert run.final_position == 1
        assert run.bars_seen == 5
        assert run.bars_tradeable == 5

    def test_a_round_trip_is_recorded_with_both_commissions(self) -> None:
        # bar0: intent -> order. bar1 opens 80 -> BUY fills 80.05, then flat
        # intent -> order. bar2 opens 90 -> SELL fills 89.95, closing.
        run = _run(_Alternating(), _bars([("80", "80"), ("80", "80"), ("90", "90")]))

        assert len(run.trades) == 1
        trade = run.trades[0]
        assert trade.side == "LONG"
        assert trade.entry_price == Decimal("80.05")
        assert trade.exit_price == Decimal("89.95")
        assert trade.gross_pnl == (Decimal("89.95") - Decimal("80.05")) * MULTIPLIER
        assert trade.commission == COMMISSION * 2, "entry and exit, not exit alone"
        assert trade.net_pnl == trade.gross_pnl - COMMISSION * 2

    def test_a_flat_replay_attributes_every_commission_to_a_trade(self) -> None:
        """When the book ends flat, the trades account for all of it.

        This is what stops the trade list quietly disagreeing with the headline
        P&L, which is computed from total commission paid.
        """
        run = _run(_Alternating(), _flat(9))

        assert run.final_position == 0
        assert run.trades
        assert sum((t.commission for t in run.trades), Decimal(0)) == run.commission_paid

    def test_realized_pnl_matches_the_sum_of_the_trades(self) -> None:
        run = _run(_Alternating(), _bars([("80", "80"), ("80", "80"), ("90", "90"), ("90", "90")]))

        assert run.realized_pnl == sum((t.gross_pnl for t in run.trades), Decimal(0))


# ---------------------------------------------------------------------------
# Lookahead
# ---------------------------------------------------------------------------


class TestNoLookahead:
    def test_a_fill_uses_the_next_bar_open_not_the_decision_bar_close(self) -> None:
        """The decision bar closes at 100; the next opens at 80.

        A model with lookahead fills near 100 and the equity curve simply looks
        better, with nothing to indicate why.
        """
        run = _run(_Target(1), _bars([("80", "100"), ("80", "80")]))

        assert len(run.fills) == 1
        assert run.fills[0].reference_price == Decimal("80")
        assert run.fills[0].price == Decimal("80.05")

    def test_an_order_on_the_last_bar_never_fills(self) -> None:
        run = _run(_Target(1), _flat(1))

        assert run.fills == ()
        assert run.final_position == 0, "there was no next bar to fill against"

    def test_slippage_is_charged_and_reported(self) -> None:
        run = _run(_Target(1), _flat(2))

        assert run.slippage_paid == MIN_TICK * MULTIPLIER


# ---------------------------------------------------------------------------
# Refusals: the risk manager
# ---------------------------------------------------------------------------


class TestRiskRefusals:
    def test_an_order_above_max_order_size_is_refused_and_never_filled(self) -> None:
        run = _run(_Target(3), _flat(5), config=_config(MAX_ORDER_SIZE="2"))

        assert run.fills == ()
        assert run.final_position == 0
        assert REASON_ORDER_SIZE_EXCEEDED in _reasons(run, "risk")

    def test_a_target_above_max_position_is_refused(self) -> None:
        run = _run(
            _Target(2), _flat(5), config=_config(MAX_POSITION_CONTRACTS="1", MAX_ORDER_SIZE="5")
        )

        assert run.fills == ()
        assert REASON_POSITION_EXCEEDED in _reasons(run, "risk")

    def test_an_unconfigured_limit_prohibits_trading_rather_than_permitting_it(self) -> None:
        """Zero is NOT CONFIGURED, never unlimited -- the same rule as live."""
        run = _run(_Target(1), _flat(5), config=_config(MAX_ORDER_SIZE="0"))

        assert run.fills == ()
        assert "MAX_ORDER_SIZE_NOT_CONFIGURED" in _reasons(run, "risk")

    def test_refusals_are_counted_by_reason(self) -> None:
        run = _run(_Target(3), _flat(4), config=_config(MAX_ORDER_SIZE="2"))

        assert len(run.refusals) == 4, "one per bar; the strategy kept asking"
        assert all(r.stage == "risk" for r in run.refusals)
        assert all(r.requested_position == 3 for r in run.refusals)
        assert all(r.current_position == 0 for r in run.refusals)


# ---------------------------------------------------------------------------
# Refusals: the transmit gate
# ---------------------------------------------------------------------------


class TestGateRefusals:
    """The gate's configuration interlocks are live in a replay.

    The conditions a live system *observes* -- connection, reconciliation,
    market-data age -- are asserted by the replay and cannot fail here. The
    configured ones can, and do.

    Note the stages: a configuration interlock is checked *independently* by
    the risk manager and by the gate, so both name it and the refusal reads
    ``risk+gate``. That is the two-approver design working, not duplication.
    """

    def test_the_kill_switch_stops_a_replay_trading(self) -> None:
        run = _run(_Target(1), _flat(5), config=_config(KILL_SWITCH="true"))

        assert run.fills == ()
        assert run.final_position == 0
        assert REASON_KILL_SWITCH in _reasons(run)
        assert _stages(run) == {"risk+gate"}, "both approvers must object independently"

    def test_transmit_not_allowed_stops_a_replay_trading(self) -> None:
        run = _run(_Target(1), _flat(5), config=_config(ALLOW_ORDER_TRANSMIT="false"))

        assert run.fills == ()
        assert REASON_TRANSMIT_NOT_ALLOWED in _reasons(run)
        assert _stages(run) == {"risk+gate"}

    def test_the_gate_contributes_a_reason_risk_alone_would_miss(self) -> None:
        """Proof the gate is genuinely wired, not decorative.

        Configured as paper, the replay's account is still simulated. Both
        approvers object, but only the gate says *which* mismatch it is: risk
        reports a generic ``TRADING_MODE_ACCOUNT_TYPE_MISMATCH``. If the engine
        stopped at risk, the granular reason could never appear in a replay and
        the gate would be untested by every backtest ever run.
        """
        run = _run(_Target(1), _flat(5), config=_config(TRADING_MODE="paper"))

        gate_only = "ACCOUNT_TYPE_MISMATCH_EXPECTED_PAPER"
        assert run.fills == ()
        assert gate_only in _reasons(run)
        assert gate_only not in {
            value
            for name, value in vars(risk_manager_module).items()
            if name.startswith("REASON_") and isinstance(value, str)
        }, "if risk gained this reason, this test stops proving the gate ran"
        assert _stages(run) == {"risk+gate"}

    def test_a_backtest_cannot_be_configured_for_live_trading_at_all(self) -> None:
        """Refused before the gate ever runs, by the config's own validation.

        ``LIVE_TRADING_ENABLED`` is forced false after overrides are applied, so
        asking for live mode leaves a half-armed configuration, which `Config`
        refuses to construct. The gate's ``LIVE_TRADING_NOT_ENABLED`` is
        unreachable in a replay because nothing gets far enough to be asked.
        """
        with pytest.raises(ConfigError, match="LIVE_TRADING_ENABLED=true"):
            _config(TRADING_MODE="live")

    def test_both_approvers_are_evaluated_not_short_circuited(self) -> None:
        """A risk limit and a gate switch tripped at once report both."""
        run = _run(
            _Target(3),
            _flat(5),
            config=_config(MAX_ORDER_SIZE="2", KILL_SWITCH="true"),
        )

        reasons = _reasons(run)
        assert REASON_ORDER_SIZE_EXCEEDED in reasons, "risk's own finding"
        assert REASON_KILL_SWITCH in reasons, "the interlock both approvers check"
        assert "RISK_CHECKS_NOT_PASSED" not in reasons, (
            "the gate restating risk's verdict is not an independent finding"
        )
        assert run.fills == ()

    def test_a_risk_only_refusal_does_not_claim_the_gate_objected(self) -> None:
        run = _run(_Target(3), _flat(5), config=_config(MAX_ORDER_SIZE="2"))

        assert _stages(run) == {"risk"}
        assert _reasons(run) == {REASON_ORDER_SIZE_EXCEEDED}


# ---------------------------------------------------------------------------
# Refusals: the validator, and the no-op case
# ---------------------------------------------------------------------------


class TestValidatorRefusals:
    def test_an_intent_from_an_unregistered_strategy_is_refused(self) -> None:
        run = _run(_Mislabelled(), _flat(3))

        assert run.fills == ()
        assert REASON_STRATEGY_MISMATCH in _reasons(run, "validator")

    def test_a_repeated_intent_id_is_refused_as_a_duplicate(self) -> None:
        run = _run(_Repeater(), _flat(4))

        # The first is accepted; the rest are duplicates of it.
        assert REASON_DUPLICATE in _reasons(run, "validator")
        assert len([r for r in run.refusals if r.stage == "validator"]) == 3

    def test_historical_timestamps_are_not_treated_as_stale(self) -> None:
        """A bar from a year ago is "now" on the virtual clock.

        The staleness check exists for a live feed. Left on, it would reject
        every historical intent for a reason that says nothing about the
        strategy, and the whole replay would refuse.
        """
        run = _run(_Target(1), _flat(3, start=datetime(2024, 1, 8, 14, 0, tzinfo=UTC)))

        assert "SIGNAL_TIMESTAMP_STALE" not in _reasons(run, "validator")
        assert run.fills


class TestNoChange:
    def test_asking_for_the_position_already_held_places_no_order(self) -> None:
        run = _run(_Target(1), _flat(6))

        assert run.final_position == 1
        assert len(run.fills) == 1, "it bought once and then stopped"
        assert _reasons(run, "no_change") == {"ALREADY_AT_TARGET"}

    def test_closing_a_position_is_expressible(self) -> None:
        """A FLAT/0 intent while long must produce a SELL, not a no-op.

        This is the defect that made closing the first real position
        impossible; asserting it here stops a replay inheriting it.
        """
        run = _run(_Alternating(), _flat(5))

        assert run.final_position == 0
        assert [f.side.value for f in run.fills] == ["BUY", "SELL", "BUY", "SELL"]
        assert len(run.trades) == 2


# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------


class TestSessionFilter:
    def test_cme_liquid_hours_rejects_weekends_and_off_hours(self) -> None:
        assert cme_liquid_hours(datetime(2026, 1, 5, 14, 0, tzinfo=UTC)) is True  # Mon 14:00
        assert cme_liquid_hours(datetime(2026, 1, 5, 13, 29, tzinfo=UTC)) is False
        assert cme_liquid_hours(datetime(2026, 1, 5, 21, 0, tzinfo=UTC)) is False
        assert cme_liquid_hours(datetime(2026, 1, 3, 14, 0, tzinfo=UTC)) is False  # Saturday

    def test_bars_outside_the_session_are_not_traded(self) -> None:
        # 03:00 UTC on a Monday: outside liquid hours for every bar.
        bars = _flat(5, start=datetime(2026, 1, 5, 3, 0, tzinfo=UTC))
        run = _run(_Target(1), bars, session=cme_liquid_hours)

        assert run.bars_seen == 5
        assert run.bars_tradeable == 0
        assert run.fills == ()
        assert run.refusals == (), "the strategy was never consulted at all"
        assert run.session_filtered is True

    def test_an_order_is_cancelled_rather_than_carried_over_a_break(self) -> None:
        """Placed in the last tradeable minute, with the break next.

        A resting order that survives a session break in a replay but would not
        in reality is a way to manufacture fills that never happened.
        """
        bars = _flat(3, start=datetime(2026, 1, 5, 20, 59, tzinfo=UTC))
        run = _run(_Target(1), bars, session=cme_liquid_hours)

        assert run.bars_tradeable == 1
        assert run.fills == (), "the order had no tradeable bar to fill against"
        assert run.final_position == 0

    def test_no_session_filter_is_recorded_as_such(self) -> None:
        run = _run(_Target(1), _flat(3))

        assert run.session_filtered is False


class TestEndOfReplay:
    def test_working_orders_at_the_end_are_cancelled_not_filled(self) -> None:
        run = _run(_Target(1), _flat(1))

        assert run.fills == ()
        assert run.final_position == 0


# ---------------------------------------------------------------------------
# Provenance and isolation
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_spot_data_is_flagged_on_the_run(self) -> None:
        run = _run(_Target(1), _flat(3))

        assert run.source == "binance"
        assert run.is_proxy_data is True, "spot is not the instrument this system trades"
        assert run.interval == "1m"
        assert run.first_bar == T0.isoformat()

    def test_an_empty_replay_reports_nothing_rather_than_guessing(self) -> None:
        run = _run(_Target(1), [])

        assert run.bars_seen == 0
        assert run.source == "none"
        assert run.first_bar is None
        assert run.is_proxy_data is False


class TestIsolationFromTheEnvironment:
    def test_backtest_config_ignores_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server `.env` must not be able to reach a backtest.

        The reverse matters more: a backtest must never inherit a live trading
        mode or live credentials from whatever shell it was started in.
        """
        for name, value in {
            "TRADING_MODE": "live",
            "LIVE_TRADING_ENABLED": "true",
            "KILL_SWITCH": "false",
            "DATABASE_PATH": "/var/lib/trading/production.db",
            "MAX_ORDER_SIZE": "999",
        }.items():
            monkeypatch.setenv(name, value)

        config = _config()

        assert config.trading_mode is TradingMode.MOCK
        assert config.live_trading_enabled is False
        assert config.risk.max_order_size == 2
        assert str(config.database_path) == ":memory:"

    def test_overrides_can_degrade_but_never_escalate(self) -> None:
        config = _config(LIVE_TRADING_ENABLED="true", DATABASE_PATH="/var/lib/trading/prod.db")

        assert config.live_trading_enabled is False
        assert str(config.database_path) == ":memory:"

    def test_the_backtest_package_never_imports_a_real_broker(self) -> None:
        """Structural, not behavioural: nothing here can reach IBKR.

        Asserted by parsing the source rather than by running it, because "it
        did not connect this time" is not the same statement as "it cannot".
        """
        package = Path(__file__).resolve().parents[2] / "app" / "backtest"
        forbidden = {"app.broker.ibkr_broker", "ibapi", "socket"}

        offenders: list[str] = []
        for module in sorted(package.glob("*.py")):
            tree = ast.parse(module.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if any(name == f or name.startswith(f + ".") for f in forbidden):
                        offenders.append(f"{module.name}: {name}")

        assert offenders == []


class TestFillModelIsReported:
    def test_the_run_carries_the_model_it_used(self) -> None:
        model = FillModel(slippage_ticks=3, commission_per_contract=Decimal("5"), spread_ticks=4)
        run = _run(_Target(1), _flat(2), fill_model=model)

        assert run.fill_model is model
        assert run.fills[0].price == Decimal("80") + MIN_TICK * 3
        assert run.fills[0].commission == Decimal("5")


class TestSyntheticSpread:
    def test_the_quote_a_strategy_sees_is_bracketed_around_the_close(self) -> None:
        seen: list[Quote] = []

        class _Recorder(Strategy):
            name = "recorder"

            def on_quote(self, quote: Quote) -> Sequence[TradeIntent]:
                seen.append(quote)
                return ()

        _run(_Recorder(), _bars([("80", "82")]))

        [quote] = seen
        assert quote.last == Decimal("82")
        assert quote.bid == Decimal("82") - MIN_TICK  # half of a 2-tick spread
        assert quote.ask == Decimal("82") + MIN_TICK
        assert quote.is_delayed is False
        assert quote.source == "backtest:binance"
