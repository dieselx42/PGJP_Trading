"""Bar storage.

Insert is idempotent on ``(source, symbol, interval, opened_at)``, so a re-run
of an import that half-completed does not duplicate or corrupt anything. That
matters more than it sounds: a year of 1-minute data is fetched in hundreds of
pages, and any one of them can fail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.backtest.models import Bar, BarGap, find_gaps, to_decimal
from app.state.database import Database
from app.utilities.timeutils import from_iso, to_iso


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

    def describe(self) -> dict[str, object]:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "interval": self.interval,
            "count": self.count,
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
