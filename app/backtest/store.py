"""Bar storage.

Insert is idempotent on ``(source, symbol, interval, opened_at)``, so a re-run
of an import that half-completed does not duplicate or corrupt anything. That
matters more than it sounds: a year of 1-minute data is fetched in hundreds of
pages, and any one of them can fail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.backtest.models import Bar, BarGap, find_gaps, reports_volume, to_decimal
from app.state.database import Database
from app.utilities.timeutils import from_iso, to_iso

#: Above this share of empty bars, a series is too thin to draw conclusions
#: from at this interval. Not a hard refusal -- the data is still stored and
#: still replayable -- but the number is stated rather than left to be noticed.
_THIN_SERIES_SHARE = 0.25


def _liquidity_note(reports: bool, zero_share: float) -> str:
    if not reports:
        return (
            "this source reports no volume, so bars where nothing traded cannot be "
            "identified. A replay will treat every bar as tradeable."
        )
    if zero_share >= _THIN_SERIES_SHARE:
        return (
            f"{zero_share:.1%} of bars had no trades at all. This venue is thin at this "
            "interval; prefer a deeper book or a longer bar interval before replaying it."
        )
    if zero_share > 0:
        return f"{zero_share:.1%} of bars had no trades; a replay will not fill against them."
    return "every bar had trades."


@dataclass(frozen=True, slots=True)
class BarSeriesInfo:
    """What is stored for one source/symbol/interval, and what is missing."""

    source: str
    symbol: str
    interval: str
    count: int
    first_opened_at: datetime | None
    last_opened_at: datetime | None
    gaps: tuple[BarGap, ...] = ()
    zero_volume_bars: int = 0
    """Bars in which nothing traded.

    Reported here so a source can be judged *before* a year of it is imported
    and replayed. A thin venue emits one of these for every quiet interval, and
    a series that is mostly placeholder bars produces a backtest that is mostly
    fiction.
    """

    reports_volume: bool = False

    def describe(self) -> dict[str, object]:
        share = self.zero_volume_bars / self.count if self.count else 0.0
        return {
            "source": self.source,
            "symbol": self.symbol,
            "interval": self.interval,
            "count": self.count,
            "reports_volume": self.reports_volume,
            "zero_volume_bars": self.zero_volume_bars,
            "zero_volume_share": round(share, 4),
            "liquidity_note": _liquidity_note(self.reports_volume, share),
            "first_opened_at": None
            if not self.first_opened_at
            else self.first_opened_at.isoformat(),
            "last_opened_at": None if not self.last_opened_at else self.last_opened_at.isoformat(),
            "gap_count": len(self.gaps),
            "gaps": [g.describe() for g in self.gaps[:20]],
            "gaps_truncated": max(0, len(self.gaps) - 20),
        }


class BarRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    def insert_many(self, bars: Sequence[Bar]) -> int:
        """Store bars, ignoring any already present. Returns the number added.

        ``INSERT OR IGNORE`` rather than ``REPLACE``: a bar already stored is
        the authority. Re-importing must not silently rewrite history under a
        backtest that has already been run against it.
        """
        added = 0
        for bar in bars:
            added += self._db.execute(
                """
                INSERT OR IGNORE INTO bars
                    (source, symbol, interval, opened_at, open, high, low, close, volume)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    bar.source,
                    bar.symbol,
                    bar.interval,
                    to_iso(bar.opened_at),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                ),
            )
        return added

    def load(
        self,
        *,
        source: str,
        symbol: str,
        interval: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[Bar, ...]:
        """Bars in time order. The replay depends on that ordering."""
        sql = [
            "SELECT * FROM bars WHERE source = ? AND symbol = ? AND interval = ?",
        ]
        params: list[object] = [source, symbol, interval]
        if start is not None:
            sql.append("AND opened_at >= ?")
            params.append(to_iso(start))
        if end is not None:
            sql.append("AND opened_at < ?")
            params.append(to_iso(end))
        sql.append("ORDER BY opened_at ASC")
        rows = self._db.query_all(" ".join(sql), tuple(params))
        return tuple(_row_to_bar(row) for row in rows)

    def daily_closes(
        self,
        *,
        source: str,
        symbol: str,
        interval: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[tuple[date, Decimal]]:
        """The last close of each UTC day in the range, oldest first.

        Two indexed range queries rather than one correlated subquery: the
        first finds each day's last bar, the second reads those bars' closes.
        On a series of millions of rows a per-row correlated lookup is the
        difference between a startup that takes a second and one that does
        not finish.
        """
        sql = [
            "SELECT substr(opened_at, 1, 10) AS day, MAX(opened_at) AS last_open FROM bars",
            "WHERE source = ? AND symbol = ? AND interval = ?",
        ]
        params: list[object] = [source, symbol, interval]
        if start is not None:
            sql.append("AND opened_at >= ?")
            params.append(to_iso(start))
        if end is not None:
            sql.append("AND opened_at < ?")
            params.append(to_iso(end))
        sql.append("GROUP BY day ORDER BY day ASC")
        days = self._db.query_all(" ".join(sql), tuple(params))
        if not days:
            return []
        closes: dict[str, Decimal] = {}
        last_opens = [str(row["last_open"]) for row in days]
        for i in range(0, len(last_opens), 500):  # stay under SQLite's variable cap
            chunk = last_opens[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = self._db.query_all(
                # Only "?" placeholders are interpolated; every value is bound.
                f"SELECT opened_at, close FROM bars WHERE source = ? AND symbol = ? "  # noqa: S608
                f"AND interval = ? AND opened_at IN ({marks})",
                (source, symbol, interval, *chunk),
            )
            for row in rows:
                closes[str(row["opened_at"])] = to_decimal(row["close"], field="close")
        return [
            (date.fromisoformat(str(row["day"])), closes[str(row["last_open"])])
            for row in days
            if str(row["last_open"]) in closes
        ]

    def info(self, *, source: str, symbol: str, interval: str) -> BarSeriesInfo:
        bars = self.load(source=source, symbol=symbol, interval=interval)
        return BarSeriesInfo(
            source=source,
            symbol=symbol,
            interval=interval,
            count=len(bars),
            first_opened_at=bars[0].opened_at if bars else None,
            last_opened_at=bars[-1].opened_at if bars else None,
            gaps=find_gaps(bars),
            zero_volume_bars=sum(1 for bar in bars if not bar.had_trades),
            reports_volume=reports_volume(bars),
        )

    def series(self) -> tuple[tuple[str, str, str], ...]:
        """Every (source, symbol, interval) stored."""
        rows = self._db.query_all(
            "SELECT DISTINCT source, symbol, interval FROM bars ORDER BY source, symbol, interval"
        )
        return tuple((r["source"], r["symbol"], r["interval"]) for r in rows)


def _row_to_bar(row: object) -> Bar:
    def field(name: str) -> object:
        return row[name]  # type: ignore[index]

    return Bar(
        source=str(field("source")),
        symbol=str(field("symbol")),
        interval=str(field("interval")),
        opened_at=from_iso(str(field("opened_at"))),
        open=to_decimal(field("open"), field="open"),
        high=to_decimal(field("high"), field="high"),
        low=to_decimal(field("low"), field="low"),
        close=to_decimal(field("close"), field="close"),
        volume=to_decimal(field("volume"), field="volume"),
    )


__all__ = ["BarRepository", "BarSeriesInfo"]
