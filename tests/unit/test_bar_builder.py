"""The tick-to-bar builder that feeds bar strategies live.

The two properties that decide whether a live ORB trade can be trusted:

* **A bar completes only when its minute is over.** A bar handed to the
  strategy early is a close that had not happened yet -- the live equivalent of
  the lookahead the backtest broker was built to prevent.
* **Nothing that is not a current, priced, in-order quote enters a bar.**
  Delayed data especially: the transmit gate refuses to trade on delayed
  quotes, and a bar quietly averaging them in would smuggle the same poison
  into the strategy's decisions instead.

Everything here is fed synthetic quotes and a synthetic clock; the builder is
pure and holds one bucket, so every behaviour is exactly testable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.market_data.bar_builder import BarBuilder
from app.market_data.models import Quote

T0 = datetime(2026, 8, 20, 14, 30, 0, tzinfo=UTC)


def _quote(
    seconds: float,
    price: str,
    *,
    bid: str | None = None,
    ask: str | None = None,
    delayed: bool = False,
    priceless: bool = False,
) -> Quote:
    return Quote(
        contract_key="conid:1",
        symbol="MSL",
        received_at=T0 + timedelta(seconds=seconds),
        source="ibkr",
        bid=None if priceless else Decimal(bid) if bid else None,
        ask=None if priceless else Decimal(ask) if ask else None,
        last=None if priceless else Decimal(price),
        is_delayed=delayed,
    )


@pytest.fixture
def builder() -> BarBuilder:
    return BarBuilder(interval="1m")


class TestCompletion:
    """A bar exists only once its minute has fully passed."""

    def test_quotes_within_one_minute_complete_nothing(self, builder: BarBuilder) -> None:
        assert builder.add(_quote(0, "180.00")) == []
        assert builder.add(_quote(30, "180.50")) == []
        assert builder.add(_quote(59, "180.25")) == []

    def test_a_quote_in_the_next_minute_completes_the_previous_bar(
        self, builder: BarBuilder
    ) -> None:
        builder.add(_quote(0, "180.00"))
        builder.add(_quote(59, "180.25"))

        [bar] = builder.add(_quote(60, "181.00"))

        assert bar.opened_at == T0
        assert bar.close == Decimal("180.25"), "the new minute's price is not in the old bar"

    def test_flush_completes_a_bar_when_no_quote_ever_arrives_to_do_it(
        self, builder: BarBuilder
    ) -> None:
        """The decisive bar before a quiet spell must not sit open forever."""
        builder.add(_quote(10, "180.00"))

        assert builder.flush(T0 + timedelta(seconds=59)) == [], "minute not over yet"
        [bar] = builder.flush(T0 + timedelta(seconds=60))
        assert bar.opened_at == T0
        assert builder.flush(T0 + timedelta(seconds=120)) == [], "not emitted twice"

    @pytest.mark.safety
    def test_a_bar_is_never_emitted_before_its_minute_ends(self, builder: BarBuilder) -> None:
        """The live equivalent of lookahead.

        A strategy given bar N while minute N is still running is acting on a
        close that has not happened. Every completion path must refuse.
        """
        builder.add(_quote(0, "180.00"))

        assert builder.add(_quote(45, "185.00")) == []
        assert builder.flush(T0 + timedelta(seconds=59.9)) == []


class TestAggregation:
    def test_ohlc_comes_from_the_samples_in_order(self, builder: BarBuilder) -> None:
        builder.add(_quote(1, "180.00"))
        builder.add(_quote(20, "182.00"))
        builder.add(_quote(40, "179.50"))
        builder.add(_quote(59, "181.00"))

        [bar] = builder.flush(T0 + timedelta(seconds=60))

        assert bar.open == Decimal("180.00")
        assert bar.high == Decimal("182.00")
        assert bar.low == Decimal("179.50")
        assert bar.close == Decimal("181.00")

    def test_mid_is_preferred_over_last(self, builder: BarBuilder) -> None:
        """A `last` can be minutes old on a quiet contract; the mid cannot.

        Building bars from a stale last would freeze the bar while the market
        moved -- and the ORB range would be measured from a price nobody is
        quoting any more.
        """
        builder.add(_quote(0, "170.00", bid="180.00", ask="180.10"))

        [bar] = builder.flush(T0 + timedelta(seconds=60))

        assert bar.open == Decimal("180.05"), "the bid/ask midpoint, not the stale last"

    def test_the_bar_carries_provenance_and_validates(self, builder: BarBuilder) -> None:
        builder.add(_quote(0, "180.00"))

        [bar] = builder.flush(T0 + timedelta(seconds=60))

        assert bar.source == "ibkr"
        assert bar.symbol == "MSL"
        assert bar.interval == "1m"
        assert bar.is_proxy is False, "these are the instrument's own quotes"
        assert bar.volume == 0
        assert bar.closed_at == T0 + timedelta(seconds=60)

    def test_timestamps_are_floored_to_the_minute(self, builder: BarBuilder) -> None:
        builder.add(_quote(37.2, "180.00"))

        [bar] = builder.flush(T0 + timedelta(seconds=60))

        assert bar.opened_at == T0


class TestWhatNeverEntersABar:
    @pytest.mark.safety
    def test_delayed_quotes_are_dropped_entirely(self, builder: BarBuilder) -> None:
        """The gate refuses to TRADE on delayed data; a bar built from it
        would smuggle the same data in through the strategy's DECISIONS."""
        builder.add(_quote(0, "180.00"))
        builder.add(_quote(30, "999.00", delayed=True))

        [bar] = builder.flush(T0 + timedelta(seconds=60))

        assert bar.high == Decimal("180.00"), "the delayed price left no trace"
        assert builder.describe()["dropped_delayed"] == 1

    def test_a_priceless_quote_is_dropped(self, builder: BarBuilder) -> None:
        builder.add(_quote(0, "180.00"))
        builder.add(_quote(30, "0", priceless=True))

        [bar] = builder.flush(T0 + timedelta(seconds=60))

        assert bar.close == Decimal("180.00")
        assert builder.describe()["dropped_no_price"] == 1

    def test_an_out_of_order_quote_is_never_spliced_into_history(self, builder: BarBuilder) -> None:
        builder.add(_quote(60, "181.00"))  # bar for minute 1 in progress
        builder.add(_quote(30, "170.00"))  # a straggler from minute 0

        [bar] = builder.flush(T0 + timedelta(seconds=120))

        assert bar.opened_at == T0 + timedelta(seconds=60)
        assert bar.low == Decimal("181.00"), "the straggler is not in this bar"
        assert builder.describe()["dropped_out_of_order"] == 1


class TestGaps:
    def test_an_empty_minute_produces_no_bar(self, builder: BarBuilder) -> None:
        """Gaps are facts. An invented flat bar would hand the ORB strategy a
        range that never printed."""
        builder.add(_quote(0, "180.00"))
        # Nothing at all during minute 1; next quote lands in minute 2.
        completed = builder.add(_quote(120, "181.00"))

        assert len(completed) == 1
        assert completed[0].opened_at == T0
        [bar2] = builder.flush(T0 + timedelta(seconds=180))
        assert bar2.opened_at == T0 + timedelta(seconds=120)

    def test_clear_drops_the_bar_in_progress(self, builder: BarBuilder) -> None:
        """On disconnect or halt: a bar straddling an outage is not history."""
        builder.add(_quote(0, "180.00"))
        builder.clear()

        assert builder.flush(T0 + timedelta(seconds=120)) == []


class TestObservability:
    def test_describe_reports_the_feed_health(self, builder: BarBuilder) -> None:
        builder.add(_quote(0, "180.00"))
        builder.add(_quote(61, "181.00"))

        described = builder.describe()

        assert described["bars_emitted"] == 1
        assert described["samples"] == 2
        assert described["in_progress"] == (T0 + timedelta(seconds=60)).isoformat()
        assert "understate" in str(described["note"]), "the sampling caveat is stated"

    def test_an_unknown_interval_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="unknown interval"):
            BarBuilder(interval="2m")
