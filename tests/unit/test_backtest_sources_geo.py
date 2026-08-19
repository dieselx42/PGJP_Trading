"""The sources added because `api.binance.com` geo-blocks the deployed server.

The server answers HTTP 451 from the global Binance endpoint, which is a legal
block on the host's jurisdiction and not something a retry fixes. Two sources
can serve a full year of 1-minute SOL history from a US-hosted box: Coinbase
Exchange, and Binance.US on the same kline format.

The dangerous part of both is **positional parsing**. Neither API returns
objects, so every price is an array index, and an off-by-one produces bars that
pass every structural check and are silently wrong. Coinbase is worse than most
because its ordering is *not* OHLC: `[time, low, high, open, close, volume]`.
That single fact gets its own tests below.

The second risk is **provenance**. `binance-us` is a different order book from
`binance` -- thinner, different prices at the same instant. Storing its bars
under `binance` would interleave two venues into one series that never traded,
and `source` is part of the primary key precisely to stop that.

As with the original Binance source, the network call itself is unverified:
this environment denies outbound access to every exchange host. Everything that
interprets a response is covered; one `urlopen` per source is not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from app.backtest.models import Bar
from app.backtest.sources import (
    BinanceBarSource,
    CoinbaseBarSource,
    HistoricalSourceError,
    _bar_from_coinbase_candle,
)

T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


def _opener(pages: list[Any]) -> Any:
    """Serves each page in turn, then empty pages forever."""
    calls: list[str] = []

    def opener(url: str, timeout: float = 0) -> _Response:
        calls.append(url)
        page = pages.pop(0) if pages else []
        return _Response(json.dumps(page).encode())

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


def _candle(minute: int, *, low: str = "79", high: str = "81") -> list[object]:
    """Coinbase order: time, low, high, open, close, volume."""
    ts = int((T0 + timedelta(minutes=minute)).timestamp())
    return [ts, low, high, "80.10", "80.50", "12.5"]


class TestCoinbaseColumnOrder:
    """The one thing most likely to be wrong, and least likely to be noticed."""

    CANDLE: ClassVar[list[object]] = [
        1787140800,  # time, epoch SECONDS (not milliseconds)
        "79.90",  # low   <- second, not third
        "81.50",  # high
        "80.10",  # open  <- fourth, after both extremes
        "81.20",  # close
        "1234.5",  # volume
    ]

    @pytest.mark.safety
    def test_the_fields_land_in_the_right_places(self) -> None:
        """Coinbase is time/low/high/open/close, NOT time/open/high/low/close.

        Swapping high and low here yields a bar that validates cleanly, because
        both orderings satisfy low <= open,close <= high on a typical candle.
        Nothing downstream can detect it.
        """
        bar = _bar_from_coinbase_candle(self.CANDLE, symbol="SOL-USD", interval="1m")

        assert bar.low == Decimal("79.90")
        assert bar.high == Decimal("81.50")
        assert bar.open == Decimal("80.10")
        assert bar.close == Decimal("81.20")
        assert bar.volume == Decimal("1234.5")

    def test_the_time_is_read_as_seconds_not_milliseconds(self) -> None:
        """Binance sends milliseconds; Coinbase sends seconds.

        Reading one as the other puts every bar ~55,000 years from now, which
        the UTC check would not catch on its own.
        """
        bar = _bar_from_coinbase_candle(self.CANDLE, symbol="SOL-USD", interval="1m")
        assert bar.opened_at == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    def test_provenance_is_stamped_by_the_source_not_the_caller(self) -> None:
        bar = _bar_from_coinbase_candle(self.CANDLE, symbol="SOL-USD", interval="1m")
        assert bar.source == "coinbase"
        assert bar.is_proxy is True

    def test_a_short_candle_is_refused(self) -> None:
        with pytest.raises(HistoricalSourceError, match="too short"):
            _bar_from_coinbase_candle([1787140800, "79"], symbol="SOL-USD", interval="1m")

    def test_an_impossible_candle_is_refused(self) -> None:
        # low 81 above high 79: only catchable because the order is known.
        broken = [1787140800, "81", "79", "80", "80", "1"]
        with pytest.raises(HistoricalSourceError, match="not a valid bar"):
            _bar_from_coinbase_candle(broken, symbol="SOL-USD", interval="1m")


class TestCoinbasePagination:
    def test_newest_first_responses_are_yielded_in_time_order(self) -> None:
        """Coinbase returns descending; a replay depends on ascending.

        Sorting here means nothing downstream has to know that, and a replay
        fed backwards would compute an equity curve that runs in reverse.
        """
        opener = _opener([[_candle(2), _candle(1), _candle(0)]])
        source = CoinbaseBarSource(opener=opener)

        bars = list(
            source.fetch(symbol="SOL-USD", interval="1m", start=T0, end=T0 + timedelta(minutes=3))
        )

        assert [b.opened_at.minute for b in bars] == [0, 1, 2]

    def test_the_request_window_is_capped_at_the_page_limit(self) -> None:
        """Coinbase rejects a wider window outright rather than truncating it."""
        opener = _opener([])
        source = CoinbaseBarSource(opener=opener)

        list(source.fetch(symbol="SOL-USD", interval="1m", start=T0, end=T0 + timedelta(days=1)))

        def _param(url: str, name: str) -> datetime:
            raw = url.split(f"{name}=")[1].split("&")[0]
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))

        first = opener.calls[0]
        assert _param(first, "end") - _param(first, "start") == timedelta(minutes=300)

    @pytest.mark.safety
    def test_an_empty_page_does_not_end_the_import(self) -> None:
        """An outage mid-history is a gap, not the end of the data.

        Stopping at the first empty page would silently truncate a year to
        whatever preceded Coinbase's first missing minute, and the result would
        look like a complete import.
        """
        opener = _opener([[], [_candle(301)]])
        source = CoinbaseBarSource(opener=opener)

        bars = list(
            source.fetch(symbol="SOL-USD", interval="1m", start=T0, end=T0 + timedelta(minutes=600))
        )

        assert [b.opened_at.minute for b in bars] == [1]
        assert len(opener.calls) >= 2, "it kept going past the empty page"

    def test_bars_past_the_end_are_not_returned(self) -> None:
        opener = _opener([[_candle(0), _candle(1), _candle(2)]])
        source = CoinbaseBarSource(opener=opener)

        bars = list(
            source.fetch(symbol="SOL-USD", interval="1m", start=T0, end=T0 + timedelta(minutes=2))
        )

        assert [b.opened_at.minute for b in bars] == [0, 1]

    def test_bars_before_the_cursor_are_not_returned(self) -> None:
        """Coinbase pads a window to the granularity boundary."""
        opener = _opener([[_candle(-2), _candle(0), _candle(1)]])
        source = CoinbaseBarSource(opener=opener)

        bars = list(
            source.fetch(symbol="SOL-USD", interval="1m", start=T0, end=T0 + timedelta(minutes=3))
        )

        assert [b.opened_at.minute for b in bars] == [0, 1]

    @pytest.mark.safety
    def test_a_walk_terminates_rather_than_hammering_the_endpoint(self) -> None:
        opener = _opener([])
        source = CoinbaseBarSource(opener=opener)

        list(source.fetch(symbol="SOL-USD", interval="1m", start=T0, end=T0 + timedelta(days=365)))

        # 365 days of 1-minute bars in 300-minute windows.
        assert len(opener.calls) == 1752

    def test_a_message_payload_is_reported_as_a_refusal(self) -> None:
        """Coinbase answers a bad product id with 200 and a JSON message."""
        opener = _opener([{"message": "NotFound"}])
        source = CoinbaseBarSource(opener=opener)

        with pytest.raises(HistoricalSourceError, match="NotFound"):
            list(
                source.fetch(
                    symbol="NOPE-USD", interval="1m", start=T0, end=T0 + timedelta(minutes=1)
                )
            )

    def test_an_unsupported_interval_is_refused(self) -> None:
        source = CoinbaseBarSource(opener=_opener([]))
        with pytest.raises(HistoricalSourceError, match="unsupported interval"):
            list(
                source.fetch(
                    symbol="SOL-USD", interval="30m", start=T0, end=T0 + timedelta(minutes=1)
                )
            )

    def test_the_url_carries_the_product_and_granularity(self) -> None:
        opener = _opener([])
        source = CoinbaseBarSource(opener=opener)

        list(source.fetch(symbol="SOL-USD", interval="1h", start=T0, end=T0 + timedelta(hours=2)))

        assert "/products/SOL-USD/candles" in opener.calls[0]
        assert "granularity=3600" in opener.calls[0]


class TestBinanceUnitedStates:
    def test_it_targets_a_different_host(self) -> None:
        source = BinanceBarSource.united_states()
        assert source.base_url == "https://api.binance.us"

    @pytest.mark.safety
    def test_its_bars_are_not_stored_under_the_global_venue(self) -> None:
        """A different exchange is a different order book.

        Storing these as `binance` would merge two venues into one series whose
        prices never traded together, and `source` is part of the primary key
        specifically to prevent that.
        """
        opener = _opener([[[int(T0.timestamp() * 1000), "80", "81", "79", "80.5", "10"]]])
        source = BinanceBarSource.united_states(opener=opener)

        [bar] = list(
            source.fetch(symbol="SOLUSD", interval="1m", start=T0, end=T0 + timedelta(minutes=1))
        )

        assert bar.source == "binance-us"
        assert bar.source != BinanceBarSource.name

    def test_an_unrecognised_venue_is_still_flagged_as_a_proxy(self) -> None:
        """Fail closed: only known futures sources are exempt from the warning.

        This is the check that broke when `binance-us` was added -- under an
        allowlist of *proxy* names, a new venue was silently treated as real
        futures data and the result dropped its "NOT CME FUTURES" caveat.
        """
        bar = Bar(
            source="binance-us",
            symbol="SOLUSD",
            interval="1m",
            opened_at=T0,
            open=Decimal("80"),
            high=Decimal("81"),
            low=Decimal("79"),
            close=Decimal("80"),
        )
        assert bar.is_proxy is True

    def test_the_global_source_keeps_its_name(self) -> None:
        assert BinanceBarSource().name == "binance"
        assert BinanceBarSource().base_url == "https://api.binance.com"
