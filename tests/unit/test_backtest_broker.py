"""The simulated broker's fill model.

The property under test throughout is that this broker is **pessimistic in the
same places reality is**. Three of those matter more than the rest:

* An order never fills on the bar that produced it. If it did, the strategy
  would be trading on a price it had already seen when it decided, which is the
  most common way a backtest flatters itself and the hardest to spot afterwards
  -- the equity curve simply looks good.
* Slippage never helps. There is no input to this model that makes a fill
  better than the reference price.
* An order that has no tradeable bar to fill against is cancelled, not carried.
  A resting order that survives a session break here but would not in reality
  is another way to manufacture profit that does not exist.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.broker import BacktestBroker, FillModel
from app.backtest.models import Bar
from app.broker.models import OrderRequest
from app.contracts.models import QualifiedContract
from app.enums import OrderSide, OrderStatus, OrderType, SecurityType

T0 = datetime(2026, 1, 5, 14, 0, tzinfo=UTC)

MIN_TICK = Decimal("0.05")
MULTIPLIER = Decimal("25")


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


def _bar(minute: int = 0, *, open_: str = "80", close: str = "80") -> Bar:
    prices = [Decimal(open_), Decimal(close)]
    return Bar(
        source="binance",
        symbol="SOLUSDT",
        interval="1m",
        opened_at=T0 + timedelta(minutes=minute),
        open=Decimal(open_),
        high=max(prices) + Decimal("1"),
        low=min(prices) - Decimal("1"),
        close=Decimal(close),
    )


def _order(
    side: OrderSide = OrderSide.BUY,
    *,
    quantity: int = 1,
    order_type: OrderType = OrderType.MARKET,
    limit_price: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        internal_order_id="bt-test",
        correlation_id="corr-test",
        contract=_contract(),
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=None if limit_price is None else Decimal(limit_price),
    )


@pytest.fixture
def broker() -> BacktestBroker:
    return BacktestBroker(min_tick=MIN_TICK, multiplier=MULTIPLIER)


class TestNoLookahead:
    """The single property that decides whether any of this is trustworthy."""

    def test_placing_an_order_does_not_fill_it(self, broker: BacktestBroker) -> None:
        result = broker.place_order(_order())

        assert result.accepted is True
        assert result.status is OrderStatus.PENDING_SUBMIT
        assert broker.executed == []
        assert broker.pending_count == 1

    def test_fill_price_derives_from_the_next_open_not_the_current_close(
        self, broker: BacktestBroker
    ) -> None:
        # The decision bar closes at 90; the next bar opens at 80. A model with
        # lookahead would fill near 90.
        broker.place_order(_order(OrderSide.BUY))
        [fill] = broker.settle(_bar(1, open_="80", close="85"))

        assert fill.reference_price == Decimal("80")
        assert fill.price == Decimal("80") + MIN_TICK

    def test_settling_twice_does_not_fill_twice(self, broker: BacktestBroker) -> None:
        broker.place_order(_order())
        first = broker.settle(_bar(1))
        second = broker.settle(_bar(2))

        assert len(first) == 1
        assert second == []
        assert len(broker.executed) == 1


class TestSlippageAlwaysCosts:
    def test_a_buy_pays_more_than_the_open(self, broker: BacktestBroker) -> None:
        broker.place_order(_order(OrderSide.BUY))
        [fill] = broker.settle(_bar(1, open_="80"))

        assert fill.price == Decimal("80.05")
        assert fill.price > fill.reference_price

    def test_a_sell_receives_less_than_the_open(self, broker: BacktestBroker) -> None:
        broker.place_order(_order(OrderSide.SELL))
        [fill] = broker.settle(_bar(1, open_="80"))

        assert fill.price == Decimal("79.95")
        assert fill.price < fill.reference_price

    def test_slippage_cost_is_reported_in_dollars(self, broker: BacktestBroker) -> None:
        broker.place_order(_order(OrderSide.BUY, quantity=2))
        [fill] = broker.settle(_bar(1))

        # One tick, two contracts, 25 SOL per contract.
        assert fill.slippage_cost == MIN_TICK * 2 * MULTIPLIER

    def test_zero_slippage_is_expressible_but_not_the_default(self) -> None:
        assert FillModel().slippage_ticks == 1

        frictionless = BacktestBroker(
            min_tick=MIN_TICK, multiplier=MULTIPLIER, fills=FillModel(slippage_ticks=0)
        )
        frictionless.place_order(_order(OrderSide.BUY))
        [fill] = frictionless.settle(_bar(1, open_="80"))

        assert fill.price == Decimal("80")


class TestCommission:
    def test_commission_scales_with_quantity(self, broker: BacktestBroker) -> None:
        broker.place_order(_order(quantity=3))
        [fill] = broker.settle(_bar(1))

        assert fill.commission == Decimal("3.41") * 3

    def test_default_is_the_rate_ibkr_actually_quoted(self) -> None:
        # Measured from a whatIf preview on MSLQ6, not guessed.
        assert FillModel().commission_per_contract == Decimal("3.41")


class TestSessionBreaks:
    def test_an_untradeable_bar_cancels_rather_than_carries(self, broker: BacktestBroker) -> None:
        broker.place_order(_order())
        settled = broker.settle(_bar(1), tradeable=False)

        assert settled == []
        assert broker.executed == []
        assert broker.pending_count == 0, "the order must be dropped, not held over the break"

    def test_a_later_tradeable_bar_does_not_resurrect_it(self, broker: BacktestBroker) -> None:
        broker.place_order(_order())
        broker.settle(_bar(1), tradeable=False)
        assert broker.settle(_bar(2), tradeable=True) == []


class TestLimitOrders:
    def test_a_marketable_buy_limit_fills(self, broker: BacktestBroker) -> None:
        broker.place_order(_order(OrderSide.BUY, order_type=OrderType.LIMIT, limit_price="81"))
        [fill] = broker.settle(_bar(1, open_="80"))

        assert fill.price == Decimal("80.05")

    def test_marketability_is_judged_at_the_price_the_fill_would_take(
        self, broker: BacktestBroker
    ) -> None:
        """A limit exactly at open+slippage fills; a tick below it does not.

        The check must use the *slipped* price, not the bar open. Judging
        against the open would let an order be accepted at a price it could
        never have received -- filling at 80.05 on an 80.00 limit.
        """
        broker.place_order(_order(OrderSide.BUY, order_type=OrderType.LIMIT, limit_price="80.05"))
        [fill] = broker.settle(_bar(1, open_="80"))
        assert fill.price == Decimal("80.05")

        edge = BacktestBroker(min_tick=MIN_TICK, multiplier=MULTIPLIER)
        edge.place_order(_order(OrderSide.BUY, order_type=OrderType.LIMIT, limit_price="80.00"))
        assert edge.settle(_bar(1, open_="80")) == []

    def test_an_unmarketable_limit_stays_working_and_retries(self, broker: BacktestBroker) -> None:
        broker.place_order(_order(OrderSide.BUY, order_type=OrderType.LIMIT, limit_price="79"))

        assert broker.settle(_bar(1, open_="80")) == []
        assert broker.pending_count == 1, "still working"

        [fill] = broker.settle(_bar(2, open_="78"))
        assert fill.price == Decimal("78.05")

    def test_an_unmarketable_sell_limit_stays_working(self, broker: BacktestBroker) -> None:
        broker.place_order(_order(OrderSide.SELL, order_type=OrderType.LIMIT, limit_price="90"))

        assert broker.settle(_bar(1, open_="80")) == []
        assert broker.pending_count == 1

        [fill] = broker.settle(_bar(2, open_="91"))
        assert fill.price == Decimal("90.95")

    def test_intrabar_high_and_low_are_never_traded_against(self, broker: BacktestBroker) -> None:
        """A limit inside the bar's range but outside its open does not fill.

        The bar reached 79 at some unknown moment. Filling there would require
        inventing the order in which prices arrived within the bar. This is a
        deliberate pessimism: some orders that would have filled in reality do
        not fill here.
        """
        broker.place_order(_order(OrderSide.BUY, order_type=OrderType.LIMIT, limit_price="79"))
        bar = Bar(
            source="binance",
            symbol="SOLUSDT",
            interval="1m",
            opened_at=T0 + timedelta(minutes=1),
            open=Decimal("80"),
            high=Decimal("81"),
            low=Decimal("78"),  # would have filled the 79 limit in reality
            close=Decimal("80"),
        )

        assert broker.settle(bar) == []
        assert broker.pending_count == 1


class TestCancelAll:
    def test_cancel_all_reports_and_drops(self, broker: BacktestBroker) -> None:
        broker.place_order(_order())
        broker.place_order(_order())

        assert broker.cancel_all() == 2
        assert broker.pending_count == 0
        assert broker.settle(_bar(1)) == []

    def test_cancel_all_on_an_empty_book_is_zero(self, broker: BacktestBroker) -> None:
        assert broker.cancel_all() == 0


class TestNotABroker:
    def test_it_cannot_be_mistaken_for_the_real_broker_interface(self) -> None:
        """It is deliberately not a `Broker`, so it cannot be handed to the runtime.

        It also has no connect, no socket and no credentials -- there is nothing
        here that could reach IBKR even by mistake.
        """
        from app.broker.base import Broker

        assert not issubclass(BacktestBroker, Broker)
        for attribute in ("connect", "disconnect", "is_connected", "account_summary"):
            assert not hasattr(BacktestBroker, attribute), attribute
