"""Builds 1-minute bars from the live quote stream.

This is the piece that lets a :class:`~app.strategy.base.BarStrategy` run
against a real broker. The backtest engine reads bars from the database; live,
there is no bar until something builds one, and until this existed the runtime
refused to start bar strategies at all rather than feed them nothing.

Sampled, not exchange bars -- stated, not hidden
------------------------------------------------
The live runtime polls quotes every ``MARKET_DATA_POLL_INTERVAL_SECONDS``
(deployed: about one second), so a 1-minute bar here is built from dozens of
*samples* of the market, not from every trade. Two consequences:

* **Highs and lows are understated.** The true extreme may fall between
  samples. For the ORB strategy that means the opening range measures slightly
  narrow and the trail's peak slightly low -- both conservative in the filter,
  slightly loose in the trail, and neither silently fixable. The honest
  alternative is exchange-built bars, which the IBKR adapter does not stream
  yet.
* **The close is the last sample**, taken within a second of the minute
  boundary at the deployed poll rate. For a strategy that acts on closes, that
  is the number that matters most, and it is the most accurate one here.

Prices are the quote's **mid** where bid and ask exist, else the last trade.
Mid is always current; a ``last`` can be minutes old on a quiet contract and
would freeze the bar while the market moved.

What never enters a bar
-----------------------
* **Delayed quotes.** A fifteen-minute-old price is not a price. The transmit
  gate already refuses to trade on delayed data; a bar quietly built from it
  would smuggle the same poison in through the strategy's decisions instead.
* **Price-less quotes.** A tick with no bid, ask, or last says the feed is
  alive and nothing else.
* **Out-of-order quotes.** A quote stamped before the bar under construction
  is dropped and counted, never spliced into history.

Empty minutes produce **no bar** -- a gap, exactly as a data outage looks in
the historical store. The ORB strategy already treats a gap in the opening
range as "skip the session"; inventing a flat bar instead would manufacture a
range that never printed.

Completion needs a clock, not just quotes: with no tick in the new minute the
old bar would never close. ``flush(now)`` exists for that and is called every
poll tick by the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.backtest.models import INTERVAL_SECONDS, Bar
from app.logging_config import get_logger
from app.market_data.models import Quote
from app.utilities.timeutils import ensure_utc

_LOG = get_logger("market_data.bar_builder")


@dataclass
class _Bucket:
    """The bar under construction."""

    opened_at: datetime
    source: str
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    samples: int = 1


class BarBuilder:
    """Aggregates polled quotes into completed, validated bars.

    Pure with respect to the outside world: it is fed quotes and a clock and
    returns bars. It never reaches a broker, never sleeps, and holds at most
    one bucket, so it cannot leak memory across a long session.
    """

    def __init__(self, *, interval: str = "1m") -> None:
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"unknown interval {interval!r}")
        self.interval = interval
        self._span = timedelta(seconds=INTERVAL_SECONDS[interval])
        self._bucket: _Bucket | None = None
        self._counts: dict[str, int] = {
            "samples": 0,
            "bars_emitted": 0,
            "dropped_delayed": 0,
            "dropped_no_price": 0,
            "dropped_out_of_order": 0,
        }

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def add(self, quote: Quote) -> list[Bar]:
        """Feed one quote. Returns the bar it completed, if it completed one.

        A quote in a *new* minute is what proves the previous minute is over,
        so completion usually happens here; :meth:`flush` covers the minute in
        which no quote arrives at all.
        """
        if quote.is_delayed:
            # Never averaged in, never "just this once". A bar that is 5%
            # delayed data is a delayed bar that does not say so.
            self._counts["dropped_delayed"] += 1
            return []

        price = quote.mid  # bid/ask midpoint, falling back to last
        if price is None:
            self._counts["dropped_no_price"] += 1
            return []

        minute = _floor_to(quote.received_at, self._span)
        bucket = self._bucket

        if bucket is not None and minute < bucket.opened_at:
            self._counts["dropped_out_of_order"] += 1
            return []

        completed: list[Bar] = []
        if bucket is not None and minute > bucket.opened_at:
            completed.append(self._finalize())
            bucket = None

        if bucket is None:
            self._bucket = _Bucket(
                opened_at=minute,
                source=quote.source,
                symbol=quote.symbol,
                open=price,
                high=price,
                low=price,
                close=price,
            )
        else:
            bucket.high = max(bucket.high, price)
            bucket.low = min(bucket.low, price)
            bucket.close = price
            bucket.samples += 1
        self._counts["samples"] += 1
        return completed

    def flush(self, now: datetime) -> list[Bar]:
        """Complete the in-progress bar if its minute has fully passed.

        Called every poll tick. Without it, the last bar before a quiet spell
        would sit unfinished forever, and a session's decisive bar could be
        exactly that bar.
        """
        ensure_utc(now)
        bucket = self._bucket
        if bucket is not None and now >= bucket.opened_at + self._span:
            return [self._finalize()]
        return []

    def clear(self) -> None:
        """Drop the in-progress bar, e.g. on disconnect or halt.

        A bar built from quotes that straddle an outage is not a minute of
        market history; the honest output is a gap.
        """
        self._bucket = None

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _finalize(self) -> Bar:
        bucket = self._bucket
        assert bucket is not None
        self._bucket = None
        self._counts["bars_emitted"] += 1
        bar = Bar(
            source=bucket.source,
            symbol=bucket.symbol,
            interval=self.interval,
            opened_at=bucket.opened_at,
            open=bucket.open,
            high=bucket.high,
            low=bucket.low,
            close=bucket.close,
            # Sampled quotes carry no traded volume. Zero is the honest value;
            # nothing downstream of the LIVE path reads volume, and these bars
            # are never written to the historical store.
            volume=Decimal(0),
        )
        _LOG.debug(
            "bar completed",
            extra={
                "event": "bar_builder.completed",
                "opened_at": bar.opened_at.isoformat(),
                "samples": bucket.samples,
                **{k: str(v) for k, v in (("close", bar.close), ("high", bar.high))},
            },
        )
        return bar

    def describe(self) -> dict[str, object]:
        bucket = self._bucket
        return {
            "interval": self.interval,
            "in_progress": None if bucket is None else bucket.opened_at.isoformat(),
            "in_progress_samples": 0 if bucket is None else bucket.samples,
            **dict(self._counts),
            "note": (
                "bars are sampled from polled quotes: highs and lows understate the true "
                "extremes; closes are accurate to the poll interval"
            ),
        }


def _floor_to(at: datetime, span: timedelta) -> datetime:
    ensure_utc(at)
    seconds = int(span.total_seconds())
    epoch = int(at.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=at.tzinfo)


__all__ = ["BarBuilder"]
