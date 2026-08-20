"""Metrics over a completed replay.

Two things are being defended here.

**The numbers are derived, not asserted.** Every figure comes from the
:class:`BacktestRun` and nothing else, so a report cannot drift from what the
replay actually did. Reports are built from hand-made runs below precisely so
the arithmetic is checkable by hand.

**"Not computable" and "zero" are different answers.** A Sharpe of 0.0 says the
strategy earned nothing per unit of risk. ``None`` says there was not enough
data to say anything. Collapsing them into one number is how a backtest ends up
quoted as evidence for something it never measured.

The limitation notes are tested as hard as the arithmetic. A result that does
not carry its caveats gets quoted without them, and with proxy spot data
standing in for CME futures the caveat is the important part.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.backtest.broker import FillModel, SimulatedFill
from app.backtest.engine import BacktestRun, ClosedTrade
from app.backtest.results import build_report
from app.enums import OrderSide

T = "2026-01-05T14:0{}:00+00:00"


def _fill(minute: int, side: OrderSide = OrderSide.BUY, *, quantity: int = 1) -> SimulatedFill:
    return SimulatedFill(
        order_id=f"bt-{minute}",
        side=side,
        quantity=quantity,
        price=Decimal("80"),
        filled_at=T.format(minute),
        commission=Decimal("3.41") * quantity,
        slippage_cost=Decimal("1.25") * quantity,
        reference_price=Decimal("80"),
    )


def _trade(net: str, *, gross: str | None = None, commission: str = "6.82") -> ClosedTrade:
    gross_pnl = Decimal(gross) if gross is not None else Decimal(net) + Decimal(commission)
    return ClosedTrade(
        opened_at=T.format(1),
        closed_at=T.format(2),
        side="LONG",
        quantity=1,
        entry_price=Decimal("80"),
        exit_price=Decimal("81"),
        gross_pnl=gross_pnl,
        commission=Decimal(commission),
        net_pnl=Decimal(net),
    )


def _run(**overrides: object) -> BacktestRun:
    base: dict[str, object] = {
        "symbol": "MSL",
        "source": "binance",
        "interval": "1m",
        "is_proxy_data": True,
        "bars_seen": 5,
        "bars_tradeable": 5,
        "bars_without_trades": 0,
        "volume_reported": True,
        "first_bar": T.format(0),
        "last_bar": T.format(4),
        "fills": (),
        "trades": (),
        "refusals": (),
        "equity_curve": (),
        "final_position": 0,
        "realized_pnl": Decimal(0),
        "commission_paid": Decimal(0),
        "slippage_paid": Decimal(0),
        "fill_model": FillModel(),
        "session_filtered": False,
    }
    base.update(overrides)
    return BacktestRun(**base)  # type: ignore[arg-type]


class TestHeadline:
    def test_net_pnl_is_realized_less_commission(self) -> None:
        report = build_report(_run(realized_pnl=Decimal("500"), commission_paid=Decimal("6.82")))

        assert report.realized_pnl == Decimal("500")
        assert report.net_pnl == Decimal("493.18")

    def test_commission_can_turn_a_profitable_gross_into_a_loss(self) -> None:
        """The whole reason commission is modelled rather than ignored."""
        report = build_report(_run(realized_pnl=Decimal("5"), commission_paid=Decimal("6.82")))

        assert report.net_pnl < 0

    def test_slippage_is_reported_separately_not_folded_in(self) -> None:
        """It is already inside the fill prices; showing it names the cost."""
        report = build_report(_run(realized_pnl=Decimal("100"), slippage_paid=Decimal("2.50")))

        assert report.slippage_paid == Decimal("2.50")
        assert report.net_pnl == Decimal("100"), "not deducted twice"


class TestDrawdown:
    def test_largest_peak_to_trough_fall(self) -> None:
        curve = [Decimal(v) for v in (0, 100, 40, 60, 10)]
        report = build_report(_run(equity_curve=tuple((T.format(0), v) for v in curve)))

        assert report.max_drawdown == Decimal("90")

    def test_a_monotonic_curve_has_no_drawdown(self) -> None:
        curve = tuple((T.format(0), Decimal(v)) for v in (0, 10, 20, 30))
        assert build_report(_run(equity_curve=curve)).max_drawdown == Decimal(0)

    def test_an_empty_curve_is_zero_not_an_error(self) -> None:
        assert build_report(_run()).max_drawdown == Decimal(0)

    def test_drawdown_is_reported_as_a_positive_magnitude(self) -> None:
        curve = tuple((T.format(0), Decimal(v)) for v in (0, -50))
        assert build_report(_run(equity_curve=curve)).max_drawdown == Decimal("50")


class TestSharpe:
    def test_not_computable_is_none_not_zero(self) -> None:
        """A flat curve earned nothing *and* varied by nothing.

        Reporting 0.0 would read as "no edge measured"; the honest answer is
        "there is nothing here to measure".
        """
        flat = tuple((T.format(0), Decimal("5")) for _ in range(10))
        assert build_report(_run(equity_curve=flat)).sharpe is None

    def test_a_single_point_is_none(self) -> None:
        assert build_report(_run(equity_curve=((T.format(0), Decimal("1")),))).sharpe is None

    def test_an_unknown_interval_cannot_be_annualised(self) -> None:
        curve = tuple((T.format(0), Decimal(v)) for v in (0, 1, 3, 4))
        assert build_report(_run(equity_curve=curve, interval="none")).sharpe is None

    def test_a_rising_curve_is_positive_and_its_mirror_is_the_negative(self) -> None:
        rising = tuple((T.format(0), Decimal(v)) for v in (0, 1, 3, 4))
        falling = tuple((T.format(0), Decimal(-v)) for v in (0, 1, 3, 4))

        up = build_report(_run(equity_curve=rising)).sharpe
        down = build_report(_run(equity_curve=falling)).sharpe

        assert up is not None and down is not None
        assert up > 0
        assert down == pytest.approx(-up)

    def test_the_annualisation_basis_is_stated_rather_than_left_implicit(self) -> None:
        curve = tuple((T.format(0), Decimal(v)) for v in (0, 1, 3, 4))
        described = build_report(_run(equity_curve=curve)).describe()

        assert described["performance"]["sharpe_basis"] == "525600 bars per year"  # type: ignore[index]


class TestTrades:
    def test_wins_losses_and_rate(self) -> None:
        report = build_report(_run(trades=(_trade("100"), _trade("-40"), _trade("20"))))

        assert report.trade_count == 3
        assert report.win_count == 2
        assert report.loss_count == 1
        assert report.win_rate == pytest.approx(2 / 3)
        assert report.average_win == Decimal("60")
        assert report.average_loss == Decimal("-40")

    def test_a_breakeven_trade_is_neither_a_win_nor_a_loss(self) -> None:
        report = build_report(_run(trades=(_trade("0"),)))

        assert report.trade_count == 1
        assert report.win_count == 0
        assert report.loss_count == 0

    def test_no_trades_reports_none_not_zero(self) -> None:
        report = build_report(_run())

        assert report.trade_count == 0
        assert report.win_rate is None, "0.0 would claim a measured 0% win rate"
        assert report.average_win is None
        assert report.average_loss is None


class TestExposure:
    def test_fraction_of_bars_holding_a_position(self) -> None:
        run = _run(
            equity_curve=tuple((T.format(i), Decimal(0)) for i in range(4)),
            fills=(_fill(1, OrderSide.BUY), _fill(3, OrderSide.SELL)),
        )

        # Held through bars 1 and 2; flat again at bar 3.
        assert build_report(run).exposure == pytest.approx(0.5)

    def test_several_fills_on_one_bar_are_all_counted(self) -> None:
        """Orders settle together against a single bar open.

        Keeping one fill per timestamp would leave this walking a position the
        replay never held -- here, a phantom short for the rest of the run.
        """
        run = _run(
            equity_curve=tuple((T.format(i), Decimal(0)) for i in range(4)),
            fills=(_fill(1, OrderSide.BUY), _fill(1, OrderSide.SELL)),
        )

        assert build_report(run).exposure == 0.0

    def test_never_trading_is_zero_exposure(self) -> None:
        run = _run(equity_curve=tuple((T.format(i), Decimal(0)) for i in range(4)))
        assert build_report(run).exposure == 0.0

    def test_an_empty_replay_is_zero_not_a_division_error(self) -> None:
        assert build_report(_run(bars_tradeable=0)).exposure == 0.0


class TestRefusals:
    def test_counted_by_reason_most_frequent_first(self) -> None:
        from app.backtest.engine import Refusal

        def refusal(*reasons: str) -> Refusal:
            return Refusal(
                at=T.format(0),
                stage="risk",
                reasons=reasons,
                requested_position=1,
                current_position=0,
            )

        report = build_report(
            _run(
                refusals=(
                    refusal("MAX_ORDER_SIZE_EXCEEDED"),
                    refusal("MAX_ORDER_SIZE_EXCEEDED", "KILL_SWITCH_ENGAGED"),
                    refusal("MAX_ORDER_SIZE_EXCEEDED"),
                )
            )
        )

        assert report.refusal_count == 3
        assert list(report.refusals_by_reason) == [
            "MAX_ORDER_SIZE_EXCEEDED",
            "KILL_SWITCH_ENGAGED",
        ]
        assert report.refusals_by_reason["MAX_ORDER_SIZE_EXCEEDED"] == 3

    def test_the_report_says_what_a_refusal_means(self) -> None:
        """A reader should not have to guess whether refusals were a bug."""
        note = build_report(_run()).describe()["refusals"]["note"]  # type: ignore[index]

        assert "will not perform that way live" in str(note)


class TestLimitations:
    def test_proxy_data_is_named_first_and_in_the_loudest_terms(self) -> None:
        notes = build_report(_run(is_proxy_data=True, source="binance")).describe()["limitations"]

        assert isinstance(notes, list)
        assert "BINANCE SPOT DATA, NOT CME FUTURES" in notes[0]
        assert "not a fill estimate for MSL" in notes[0]

    def test_futures_data_carries_no_proxy_warning(self) -> None:
        notes = build_report(_run(is_proxy_data=False, source="ibkr")).describe()["limitations"]

        assert isinstance(notes, list)
        assert not any("NOT CME FUTURES" in note for note in notes)

    def test_the_bar_and_spread_caveats_are_always_present(self) -> None:
        notes = build_report(_run()).describe()["limitations"]

        assert isinstance(notes, list)
        joined = " ".join(notes)
        assert "Bars are not ticks" in joined
        assert "A bar has no spread" in joined
        assert "Depth is not modelled" in joined

    def test_an_unfiltered_session_is_called_out(self) -> None:
        notes = build_report(_run(session_filtered=False)).describe()["limitations"]

        assert isinstance(notes, list)
        assert any("outside CME trading hours" in note for note in notes)

    def test_a_filtered_session_is_not(self) -> None:
        notes = build_report(_run(session_filtered=True)).describe()["limitations"]

        assert isinstance(notes, list)
        assert not any("outside CME trading hours" in note for note in notes)

    def test_an_open_position_at_the_end_is_flagged_as_unrealised(self) -> None:
        notes = build_report(_run(final_position=2)).describe()["limitations"]

        assert isinstance(notes, list)
        assert any("ended holding 2 contract(s)" in note for note in notes)
        assert any("unrealised" in note for note in notes)

    @pytest.mark.safety
    def test_the_wrong_instrument_outranks_the_wrong_fills(self) -> None:
        """Ordered by how badly a reader would be misled, not by check order.

        A spot result mistaken for a futures one is wrong about *what was
        traded*. That has to lead, even when the fill model also has something
        loud to say.
        """
        notes = build_report(
            _run(is_proxy_data=True, source="binance-us", volume_reported=False)
        ).describe()["limitations"]

        assert isinstance(notes, list)
        assert "NOT CME FUTURES" in notes[0]
        assert "REPORTS NO VOLUME" in notes[1]

    def test_a_source_without_volume_says_fills_may_be_manufactured(self) -> None:
        notes = build_report(_run(is_proxy_data=False, volume_reported=False)).describe()[
            "limitations"
        ]

        assert isinstance(notes, list)
        assert "REPORTS NO VOLUME" in notes[0]
        assert "prices nobody could have got" in notes[0]

    def test_a_few_empty_bars_are_noted_without_alarm(self) -> None:
        notes = build_report(
            _run(is_proxy_data=False, bars_seen=100, bars_without_trades=5)
        ).describe()["limitations"]

        assert isinstance(notes, list)
        joined = " ".join(notes)
        assert "5 of 100 bars (5.0%) had zero volume" in joined
        assert "too thin" not in joined
        assert "zero volume" not in notes[0], "a minor caveat does not lead"

    @pytest.mark.safety
    def test_a_mostly_empty_series_is_called_too_thin_and_leads(self) -> None:
        """Binance.US SOLUSD at 1-minute is largely placeholder bars.

        A result computed over mostly-empty history is mostly fiction, and that
        has to be the first thing a reader sees.
        """
        notes = build_report(
            _run(is_proxy_data=False, bars_seen=100, bars_without_trades=60)
        ).describe()["limitations"]

        assert isinstance(notes, list)
        assert "60 of 100 bars (60.0%)" in notes[0]
        assert "too thin" in notes[0]
        assert "longer bar interval" in notes[0]

    def test_a_fully_traded_series_carries_no_volume_caveat(self) -> None:
        notes = build_report(_run(bars_without_trades=0, volume_reported=True)).describe()[
            "limitations"
        ]

        assert isinstance(notes, list)
        assert not any("zero volume" in note for note in notes)
        assert not any("REPORTS NO VOLUME" in note for note in notes)

    def test_a_flat_ending_is_not_flagged(self) -> None:
        notes = build_report(_run(final_position=0)).describe()["limitations"]

        assert isinstance(notes, list)
        assert not any("ended holding" in note for note in notes)


class TestDescribe:
    def test_provenance_travels_into_the_report(self) -> None:
        described = build_report(_run()).describe()

        assert described["data"] == {
            "symbol": "MSL",
            "source": "binance",
            "interval": "1m",
            "bars": 5,
            "bars_tradeable": 5,
            "bars_without_trades": 0,
            "volume_reported": True,
            "first_bar": T.format(0),
            "last_bar": T.format(4),
            "is_proxy_data": True,
        }

    def test_the_fill_model_is_reported_so_results_are_reproducible(self) -> None:
        model = FillModel(slippage_ticks=2, commission_per_contract=Decimal("5"), spread_ticks=4)
        described = build_report(_run(fill_model=model)).describe()

        assert described["model"]["slippage_ticks"] == 2  # type: ignore[index]
        assert described["model"]["commission_per_contract"] == "5"  # type: ignore[index]
        assert "NEXT bar's open" in str(described["model"]["fills_at"])  # type: ignore[index]

    def test_prices_are_strings_so_no_precision_is_lost_in_serialisation(self) -> None:
        described = build_report(_run(realized_pnl=Decimal("0.1"))).describe()

        assert described["performance"]["realized_pnl"] == "0.1"  # type: ignore[index]
        assert isinstance(described["performance"]["net_pnl"], str)  # type: ignore[index]

    def test_long_lists_are_truncated_and_say_by_how_much(self) -> None:
        described = build_report(_run(trades=tuple(_trade("1") for _ in range(60)))).describe()

        assert len(described["trades"]["detail"]) == 50  # type: ignore[index]
        assert described["trades"]["detail_truncated"] == 10  # type: ignore[index]
        assert described["trades"]["count"] == 60, "the count is never truncated"  # type: ignore[index]

    def test_short_lists_report_nothing_truncated(self) -> None:
        described = build_report(_run(trades=(_trade("1"),))).describe()

        assert described["trades"]["detail_truncated"] == 0  # type: ignore[index]


class TestAttribution:
    """The loss-locator: net is net, and buckets are exact.

    The killer case is a trade with POSITIVE gross and NEGATIVE net -- the
    breakeven exit, +$50 gross, -$322.80 after costs. An attribution that
    quietly summed gross would report the breakeven rule as harmless when it
    is a guaranteed bleed, which is precisely the mistake this section exists
    to expose.
    """

    def _trade_with(
        self, *, gross: str, net: str, exit_reason: str, session: str = "08:00", trade_n: int = 1
    ) -> ClosedTrade:
        return ClosedTrade(
            opened_at=T.format(1),
            closed_at=T.format(2),
            side="LONG",
            quantity=40,
            entry_price=Decimal("200"),
            exit_price=Decimal("200.05"),
            gross_pnl=Decimal(gross),
            commission=Decimal(gross) - Decimal(net),
            net_pnl=Decimal(net),
            session=session,
            trade_n=trade_n,
            exit_reason=exit_reason,
        )

    def test_net_is_net_not_gross(self) -> None:
        """A breakeven trade: +$50 gross, -$322.80 net. The bucket must say net."""
        trades = (
            self._trade_with(gross="50.00", net="-322.80", exit_reason="breakeven"),
            self._trade_with(gross="50.00", net="-322.80", exit_reason="breakeven"),
        )
        described = build_report(_run(trades=trades)).describe()

        [row] = described["attribution"]["by_exit_reason"]  # type: ignore[index]
        assert row["bucket"] == "breakeven"
        assert row["net"] == "-645.60", "net after costs, never gross"
        assert row["gross"] == "100.00", "gross reported alongside, separately"
        assert row["avg_net"] == "-322.80"
        assert row["wins"] == 0, "gross-positive is not a win once costs are paid"

    def test_buckets_are_sorted_worst_first(self) -> None:
        trades = (
            self._trade_with(gross="1500", net="1227.20", exit_reason="target"),
            self._trade_with(gross="-650", net="-1022.80", exit_reason="stop"),
            self._trade_with(gross="50", net="-322.80", exit_reason="breakeven"),
        )
        described = build_report(_run(trades=trades)).describe()

        order = [r["bucket"] for r in described["attribution"]["by_exit_reason"]]  # type: ignore[index]
        assert order == ["stop", "breakeven", "target"], "the bleed leads"

    def test_sessions_and_trade_numbers_slice_independently(self) -> None:
        trades = (
            self._trade_with(
                gross="100", net="-172.80", exit_reason="trail", session="08:00", trade_n=1
            ),
            self._trade_with(
                gross="-650", net="-922.80", exit_reason="stop", session="14:30", trade_n=2
            ),
        )
        described = build_report(_run(trades=trades)).describe()
        attribution = described["attribution"]

        [ny, london] = attribution["by_session"]  # type: ignore[index]
        assert (ny["bucket"], london["bucket"]) == ("14:30", "08:00"), "worst first"
        [second, first] = attribution["by_trade_number"]  # type: ignore[index]
        assert (second["bucket"], first["bucket"]) == ("2", "1")
        assert second["net"] == "-922.80"
