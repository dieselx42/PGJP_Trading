"""Bars, storage, and the sources that produce them.

The single most important property here is **provenance**. The price history
available today is Solana *spot*; this system trades CME futures. They are
different instruments, and a result computed from one must never be readable as
a statement about the other. `source` is part of the primary key, travels on
every bar, and is asserted in several places below.

The second is that **gaps are reported and never filled**. A missing hour is a
fact about the data. An invented price is indistinguishable from a real one by
the time it reaches a strategy, and every number downstream of it is then wrong
in a way no test can detect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest

from app.backtest.models import Bar, BarError, find_gaps
from app.backtest.sources import (
    BinanceBarSource,
    CsvBarSource,
    HistoricalSourceError,
    _bar_from_kline,
)
from app.backtest.store import BarRepository
from app.state.database import Database

T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _no_sleep(_seconds: float) -> None:
    """No wall-clock time in the request throttle. See the geo source tests."""


def _bar(minute: int = 0, *, source: str = "binance", close: str = "80") -> Bar:
    return Bar(
        source=source,
        symbol="SOLUSDT",
        interval="1m",
        opened_at=T0 + timedelta(minutes=minute),
        open=Decimal("80"),
        high=Decimal("81"),
        low=Decimal("79"),
        close=Decimal(close),
    )


class TestBarValidity:
    def test_a_bar_with_no_source_is_refused(self) -> None:
        """Provenance is not optional; a bar without it cannot be interpreted."""
        with pytest.raises(BarError, match="source"):
            Bar(
                source="  ",
                symbol="SOLUSDT",
                interval="1m",
                opened_at=T0,
                open=Decimal(80),
                high=Decimal(81),
                low=Decimal(79),
                close=Decimal(80),
            )

    def test_high_below_low_is_refused(self) -> None:
        with pytest.raises(BarError, match="below low"):
            Bar(
                source="binance",
                symbol="SOLUSDT",
                interval="1m",
                opened_at=T0,
                open=Decimal(80),
                high=Decimal(79),
                low=Decimal(81),
                close=Decimal(80),
            )

    def test_a_close_outside_the_range_is_refused(self) -> None:
        """Silently accepting it would put an impossible price into a backtest."""
        with pytest.raises(BarError, match="outside the low-high range"):
            Bar(
                source="binance",
                symbol="SOLUSDT",
                interval="1m",
                opened_at=T0,
                open=Decimal(80),
                high=Decimal(81),
                low=Decimal(79),
                close=Decimal(95),
            )

    def test_a_naive_timestamp_is_refused(self) -> None:
        with pytest.raises(Exception, match=r"(?i)utc|naive|timezone"):
            Bar(
                source="binance",
                symbol="SOLUSDT",
                interval="1m",
                opened_at=datetime(2026, 8, 19, 12, 0),
                open=Decimal(80),
                high=Decimal(81),
                low=Decimal(79),
                close=Decimal(80),
            )

    @pytest.mark.safety
    def test_spot_sources_are_flagged_as_proxies(self) -> None:
        """A run over spot must never read as a statement about futures."""
        assert _bar(source="binance").is_proxy is True
        assert _bar(source="coinbase").is_proxy is True
        assert _bar(source="ibkr").is_proxy is False


class TestGaps:
    def test_a_continuous_series_has_none(self) -> None:
        assert find_gaps([_bar(0), _bar(1), _bar(2)]) == ()

    def test_a_missing_minute_is_reported(self) -> None:
        gaps = find_gaps([_bar(0), _bar(2)])
        assert len(gaps) == 1
        assert gaps[0].missing_bars == 1

    def test_a_long_gap_counts_correctly(self) -> None:
        gaps = find_gaps([_bar(0), _bar(60)])
        assert gaps[0].missing_bars == 59

    @pytest.mark.safety
    def test_gaps_are_reported_not_filled(self) -> None:
        """`find_gaps` returns descriptions. It must never return bars."""
        gaps = find_gaps([_bar(0), _bar(5)])
        assert all(not isinstance(g, Bar) for g in gaps)


class TestStorage:
    def _repo(self) -> tuple[Database, BarRepository]:
        database = Database(":memory:")
        database.connect()
        database.migrate()
        return database, BarRepository(database)

    def test_bars_round_trip(self) -> None:
        database, repo = self._repo()
        try:
            assert repo.insert_many([_bar(0), _bar(1)]) == 2
            loaded = repo.load(source="binance", symbol="SOLUSDT", interval="1m")
            assert [b.opened_at for b in loaded] == [_bar(0).opened_at, _bar(1).opened_at]
            assert loaded[0].close == Decimal("80")
        finally:
            database.close()

    @pytest.mark.safety
    def test_reimporting_the_same_bar_adds_nothing(self) -> None:
        """A year of data is hundreds of pages; any one can fail and be retried."""
        database, repo = self._repo()
        try:
            repo.insert_many([_bar(0)])
            assert repo.insert_many([_bar(0)]) == 0
            assert len(repo.load(source="binance", symbol="SOLUSDT", interval="1m")) == 1
        finally:
            database.close()

    @pytest.mark.safety
    def test_a_stored_bar_is_never_rewritten_by_a_reimport(self) -> None:
        """History must not change under a backtest already run against it."""
        database, repo = self._repo()
        try:
            repo.insert_many([_bar(0, close="80")])
            repo.insert_many([_bar(0, close="80.9")])
            loaded = repo.load(source="binance", symbol="SOLUSDT", interval="1m")
            assert loaded[0].close == Decimal("80")
        finally:
            database.close()

    @pytest.mark.safety
    def test_two_sources_for_the_same_minute_coexist(self) -> None:
        """Spot and futures for one timestamp are different facts, not a clash."""
        database, repo = self._repo()
        try:
            repo.insert_many([_bar(0, source="binance"), _bar(0, source="ibkr")])
            assert len(repo.load(source="binance", symbol="SOLUSDT", interval="1m")) == 1
            assert len(repo.load(source="ibkr", symbol="SOLUSDT", interval="1m")) == 1
        finally:
            database.close()

    def test_a_range_query_is_half_open(self) -> None:
        database, repo = self._repo()
        try:
            repo.insert_many([_bar(0), _bar(1), _bar(2)])
            loaded = repo.load(
                source="binance",
                symbol="SOLUSDT",
                interval="1m",
                start=T0 + timedelta(minutes=1),
                end=T0 + timedelta(minutes=2),
            )
            assert len(loaded) == 1
            assert loaded[0].opened_at == T0 + timedelta(minutes=1)
        finally:
            database.close()

    def test_info_reports_range_and_gaps(self) -> None:
        database, repo = self._repo()
        try:
            repo.insert_many([_bar(0), _bar(1), _bar(5)])
            info = repo.info(source="binance", symbol="SOLUSDT", interval="1m")
            assert info.count == 3
            assert info.first_opened_at == T0
            assert len(info.gaps) == 1
            assert info.gaps[0].missing_bars == 3
        finally:
            database.close()

    def test_info_on_an_empty_series_is_not_an_error(self) -> None:
        database, repo = self._repo()
        try:
            info = repo.info(source="binance", symbol="NOTHING", interval="1m")
            assert info.count == 0
            assert info.first_opened_at is None
        finally:
            database.close()


class TestBinanceParsing:
    """A recorded response shape. The indices are the whole risk here.

    Binance returns an array, not an object, so open/high/low/close are
    positional. An off-by-one swaps high and low and nothing downstream would
    notice -- every bar would still be structurally valid.
    """

    KLINE: ClassVar[list[object]] = [
        1787140800000,  # open time, ms
        "80.10",  # open
        "81.50",  # high
        "79.90",  # low
        "81.20",  # close
        "1234.5",  # volume
        1787140859999,  # close time
        "99999.0",
        500,
    ]

    @pytest.mark.safety
    def test_the_fields_land_in_the_right_places(self) -> None:
        bar = _bar_from_kline(self.KLINE, symbol="SOLUSDT", interval="1m")
        assert bar.open == Decimal("80.10")
        assert bar.high == Decimal("81.50")
        assert bar.low == Decimal("79.90")
        assert bar.close == Decimal("81.20")
        assert bar.volume == Decimal("1234.5")

    def test_the_open_time_is_read_as_utc_milliseconds(self) -> None:
        bar = _bar_from_kline(self.KLINE, symbol="SOLUSDT", interval="1m")
        assert bar.opened_at == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    def test_provenance_is_stamped_by_the_source_not_the_caller(self) -> None:
        bar = _bar_from_kline(self.KLINE, symbol="SOLUSDT", interval="1m")
        assert bar.source == "binance"
        assert bar.is_proxy is True

    def test_a_short_kline_is_refused(self) -> None:
        with pytest.raises(HistoricalSourceError, match="too short"):
            _bar_from_kline([1787140800000, "80"], symbol="SOLUSDT", interval="1m")

    def test_an_impossible_kline_is_refused(self) -> None:
        broken = [1787140800000, "80", "79", "81", "80", "1"]  # high < low
        with pytest.raises(HistoricalSourceError, match="not a valid bar"):
            _bar_from_kline(broken, symbol="SOLUSDT", interval="1m")


class TestBinancePagination:
    """Driven through a fake opener. The network call itself is unverified."""

    class _Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> TestBinancePagination._Response:
            return self

        def __exit__(self, *_: object) -> bool:
            return False

    def _opener(self, pages: list[list[list[object]]]):
        import json

        calls: list[str] = []

        def opener(url: str, timeout: float = 0) -> TestBinancePagination._Response:
            calls.append(url)
            page = pages.pop(0) if pages else []
            return TestBinancePagination._Response(json.dumps(page).encode())

        opener.calls = calls  # type: ignore[attr-defined]
        return opener

    def _kline(self, minute: int) -> list[object]:
        ts = int((T0 + timedelta(minutes=minute)).timestamp() * 1000)
        return [ts, "80", "81", "79", "80.5", "10"]

    def test_it_walks_forward_through_pages(self) -> None:
        opener = self._opener([[self._kline(0), self._kline(1)], [self._kline(2)], []])
        source = BinanceBarSource(opener=opener, sleeper=_no_sleep)
        bars = list(
            source.fetch(symbol="SOLUSDT", interval="1m", start=T0, end=T0 + timedelta(minutes=10))
        )
        assert [b.opened_at.minute for b in bars] == [0, 1, 2]

    @pytest.mark.safety
    def test_an_empty_page_ends_the_walk(self) -> None:
        """Without this it loops forever against a public endpoint."""
        opener = self._opener([[]])
        source = BinanceBarSource(opener=opener, sleeper=_no_sleep)
        bars = list(
            source.fetch(symbol="SOLUSDT", interval="1m", start=T0, end=T0 + timedelta(days=365))
        )
        assert bars == []

    @pytest.mark.safety
    def test_a_repeated_page_cannot_loop_forever(self) -> None:
        """A page that does not advance the cursor is the classic infinite loop."""
        import json

        class Repeating:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> bool:
                return False

        payload = json.dumps([self._kline(0)]).encode()

        def opener(url: str, timeout: float = 0) -> Repeating:
            return Repeating(payload)

        source = BinanceBarSource(opener=opener, sleeper=_no_sleep)
        bars = list(
            source.fetch(symbol="SOLUSDT", interval="1m", start=T0, end=T0 + timedelta(minutes=3))
        )
        # The same bar keeps arriving, but the cursor advances regardless, so
        # the range is exhausted rather than the loop running away.
        assert len(bars) <= 3

    def test_bars_past_the_end_are_not_returned(self) -> None:
        opener = self._opener([[self._kline(0), self._kline(1), self._kline(2)]])
        source = BinanceBarSource(opener=opener, sleeper=_no_sleep)
        bars = list(
            source.fetch(symbol="SOLUSDT", interval="1m", start=T0, end=T0 + timedelta(minutes=2))
        )
        assert [b.opened_at.minute for b in bars] == [0, 1]

    def test_invalid_json_is_reported_clearly(self) -> None:
        class Bad:
            def read(self) -> bytes:
                return b"<html>rate limited</html>"

            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> bool:
                return False

        source = BinanceBarSource(opener=lambda url, timeout=0: Bad(), sleeper=_no_sleep)
        with pytest.raises(HistoricalSourceError, match="invalid JSON"):
            list(
                source.fetch(
                    symbol="SOLUSDT", interval="1m", start=T0, end=T0 + timedelta(minutes=1)
                )
            )


class TestCsvSource:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "bars.csv"
        path.write_text(body, encoding="utf-8")
        return path

    COLUMNS: ClassVar[dict[str, str]] = {
        "opened_at": "time",
        "open": "o",
        "high": "h",
        "low": "l",
        "close": "c",
        "volume": "v",
    }

    @pytest.mark.safety
    def test_a_mapping_without_every_price_is_refused(self) -> None:
        """Guessing which column is which is how high becomes low silently."""
        with pytest.raises(HistoricalSourceError, match="refusing to guess"):
            CsvBarSource(Path("x.csv"), source_name="mine", columns={"opened_at": "time"})

    def test_rows_are_read_with_the_given_mapping(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "time,o,h,l,c,v\n2026-08-19T12:00:00+00:00,80,81,79,80.5,10\n",
        )
        source = CsvBarSource(path, source_name="mine", columns=self.COLUMNS)
        bars = list(
            source.fetch(symbol="MSL", interval="1m", start=T0, end=T0 + timedelta(minutes=5))
        )
        assert len(bars) == 1
        assert bars[0].high == Decimal("81")
        assert bars[0].source == "mine"

    def test_rows_outside_the_range_are_skipped(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "time,o,h,l,c,v\n"
            "2026-08-19T11:00:00+00:00,80,81,79,80.5,10\n"
            "2026-08-19T12:00:00+00:00,80,81,79,80.5,10\n",
        )
        source = CsvBarSource(path, source_name="mine", columns=self.COLUMNS)
        bars = list(
            source.fetch(symbol="MSL", interval="1m", start=T0, end=T0 + timedelta(minutes=5))
        )
        assert len(bars) == 1

    def test_a_bad_row_names_its_line(self, tmp_path: Path) -> None:
        """A 500,000-row file needs the line number, not just 'invalid'."""
        path = self._write(
            tmp_path,
            "time,o,h,l,c,v\n2026-08-19T12:00:00+00:00,80,79,81,80,10\n",
        )
        source = CsvBarSource(path, source_name="mine", columns=self.COLUMNS)
        with pytest.raises(HistoricalSourceError, match=r":2:"):
            list(source.fetch(symbol="MSL", interval="1m", start=T0, end=T0 + timedelta(minutes=5)))
