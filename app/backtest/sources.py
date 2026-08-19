"""Where bars come from.

One protocol, two implementations. The replay engine never sees these -- it
reads from the database -- so a new source is an import concern only and cannot
change how a backtest behaves.

Verification status
-------------------
``CsvBarSource`` is fully tested. ``BinanceBarSource``'s **parsing** is fully
tested against recorded response shapes; its **network call** is not, because
the environment this was written in denies outbound access to
``api.binance.com``.

That distinction is deliberate rather than an excuse. Everything that
interprets a response is covered; what is unproven is one `urlopen` and the
assumption that Binance's payload still looks the way it is documented. The
first real fetch is the verification, which is why ``bars-import`` takes
``--limit`` and prints what it got: fetch ten bars, look at them, then fetch a
year.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from app.backtest.models import INTERVAL_SECONDS, Bar, BarError, to_decimal
from app.logging_config import get_logger
from app.utilities.timeutils import ensure_utc

_LOG = get_logger("backtest.sources")

#: Binance returns at most this many klines per request.
_BINANCE_PAGE_LIMIT: Final = 1000

#: Give up rather than hammer a public endpoint that is refusing.
_MAX_PAGES: Final = 2000


class HistoricalSourceError(RuntimeError):
    """Raised when bars cannot be fetched or parsed."""


class HistoricalSource(Protocol):
    """Anything that can produce bars for a range."""

    name: str

    def fetch(
        self, *, symbol: str, interval: str, start: datetime, end: datetime
    ) -> Iterator[Bar]: ...


class CsvBarSource:
    """Bars from a CSV, with an explicit column mapping.

    The mapping is required rather than guessed. A CSV whose columns are
    inferred is a CSV that silently loads high as low the day somebody exports
    it differently, and every downstream number is then wrong in a way no test
    would catch.
    """

    name = "csv"

    def __init__(
        self,
        path: Path,
        *,
        source_name: str,
        columns: dict[str, str],
        timestamp_format: str | None = None,
    ) -> None:
        self.path = path
        self.source_name = source_name
        self.columns = columns
        self.timestamp_format = timestamp_format
        missing = {"opened_at", "open", "high", "low", "close"} - set(columns)
        if missing:
            raise HistoricalSourceError(
                f"column mapping is missing {sorted(missing)}; refusing to guess which "
                "column holds which price"
            )

    def fetch(self, *, symbol: str, interval: str, start: datetime, end: datetime) -> Iterator[Bar]:
        import csv  # noqa: PLC0415 - only needed for this source

        ensure_utc(start)
        ensure_utc(end)
        with self.path.open(newline="", encoding="utf-8") as handle:
            for line_number, row in enumerate(csv.DictReader(handle), start=2):
                try:
                    opened_at = self._timestamp(row[self.columns["opened_at"]])
                except (KeyError, ValueError) as exc:
                    raise HistoricalSourceError(f"{self.path}:{line_number}: {exc}") from exc
                if not start <= opened_at < end:
                    continue
                try:
                    yield Bar(
                        source=self.source_name,
                        symbol=symbol,
                        interval=interval,
                        opened_at=opened_at,
                        open=to_decimal(row[self.columns["open"]], field="open"),
                        high=to_decimal(row[self.columns["high"]], field="high"),
                        low=to_decimal(row[self.columns["low"]], field="low"),
                        close=to_decimal(row[self.columns["close"]], field="close"),
                        volume=to_decimal(
                            row.get(self.columns.get("volume", ""), 0) or 0, field="volume"
                        ),
                    )
                except (BarError, KeyError) as exc:
                    raise HistoricalSourceError(f"{self.path}:{line_number}: {exc}") from exc

    def _timestamp(self, raw: str) -> datetime:
        from app.utilities.timeutils import from_iso  # noqa: PLC0415

        if self.timestamp_format:
            parsed = datetime.strptime(raw, self.timestamp_format)  # noqa: DTZ007
            from datetime import UTC  # noqa: PLC0415

            return parsed.replace(tzinfo=UTC)
        return from_iso(raw)


class BinanceBarSource:
    """Public SOL spot klines.

    **This is not the instrument this system trades.** Bars carry
    ``source="binance"``, which `Bar.is_proxy` reports and every backtest
    result repeats, so a run over this data cannot later be read as a statement
    about CME futures.
    """

    name = "binance"

    #: The system's interval names are already Binance's. Mapped explicitly
    #: anyway, so a future divergence is a KeyError rather than a wrong request.
    _INTERVALS: Final[dict[str, str]] = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "1h": "1h",
        "1d": "1d",
    }

    def __init__(
        self,
        *,
        base_url: str = "https://api.binance.com",
        opener: Any = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Injected so the parsing can be tested without a network. The default
        # is the real thing; nothing here silently runs against a fake.
        self._opener = opener or urllib.request.urlopen
        self._timeout = timeout_seconds

    def fetch(self, *, symbol: str, interval: str, start: datetime, end: datetime) -> Iterator[Bar]:
        ensure_utc(start)
        ensure_utc(end)
        if interval not in self._INTERVALS:
            raise HistoricalSourceError(f"unsupported interval {interval!r}")
        step = timedelta(seconds=INTERVAL_SECONDS[interval])

        cursor = start
        pages = 0
        while cursor < end:
            pages += 1
            if pages > _MAX_PAGES:
                raise HistoricalSourceError(
                    f"stopped after {_MAX_PAGES} pages without reaching {end.isoformat()}; "
                    "refusing to keep hammering a public endpoint"
                )
            rows = self._page(symbol=symbol, interval=interval, start=cursor, end=end)
            if not rows:
                return
            for row in rows:
                bar = _bar_from_kline(row, symbol=symbol, interval=interval)
                if bar.opened_at >= end:
                    return
                yield bar
            last_open = _kline_open_time(rows[-1])
            # Advance past the last bar received. Without this a short final
            # page loops forever on the same timestamp.
            cursor = max(last_open + step, cursor + step)

    def _page(
        self, *, symbol: str, interval: str, start: datetime, end: datetime
    ) -> list[list[Any]]:
        params = (
            f"symbol={symbol}&interval={self._INTERVALS[interval]}"
            f"&startTime={int(start.timestamp() * 1000)}"
            f"&endTime={int(end.timestamp() * 1000)}"
            f"&limit={_BINANCE_PAGE_LIMIT}"
        )
        url = f"{self.base_url}/api/v3/klines?{params}"
        try:
            with self._opener(url, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HistoricalSourceError(f"could not reach {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise HistoricalSourceError(f"{self.base_url} returned invalid JSON: {exc}") from exc
        if not isinstance(payload, list):
            raise HistoricalSourceError(f"expected a list of klines, got {type(payload).__name__}")
        _LOG.info(
            "fetched a page of klines",
            extra={"event": "bars.page", "count": len(payload), "start": start.isoformat()},
        )
        return payload


def _kline_open_time(row: Sequence[Any]) -> datetime:
    from datetime import UTC  # noqa: PLC0415

    try:
        return datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
    except (IndexError, TypeError, ValueError) as exc:
        raise HistoricalSourceError(f"kline has no usable open time: {row!r}") from exc


def _bar_from_kline(row: Sequence[Any], *, symbol: str, interval: str) -> Bar:
    """Binance kline -> Bar.

    Positional by necessity: the API returns an array, not an object. The
    indices are pinned by a test against a recorded response, because an
    off-by-one here swaps high and low and nothing downstream would notice.
    """
    try:
        return Bar(
            source="binance",
            symbol=symbol,
            interval=interval,
            opened_at=_kline_open_time(row),
            open=to_decimal(row[1], field="open"),
            high=to_decimal(row[2], field="high"),
            low=to_decimal(row[3], field="low"),
            close=to_decimal(row[4], field="close"),
            volume=to_decimal(row[5], field="volume"),
        )
    except IndexError as exc:
        raise HistoricalSourceError(f"kline is too short: {row!r}") from exc
    except BarError as exc:
        raise HistoricalSourceError(f"kline is not a valid bar: {exc}") from exc


__all__ = [
    "BinanceBarSource",
    "CsvBarSource",
    "HistoricalSource",
    "HistoricalSourceError",
]
