"""Assemble the daily history a strategy is fed before live data starts.

A daily rule with a 50-day window is silent for 50 days after every process
start -- and a restart is how every ``.env`` change is applied -- unless it is
handed the days it missed. Two sources exist, and they are combined here in
plain code so the policy is testable without a database:

* **Live days**: the strategy's own completed UTC days, written to the ``bars``
  table as ``1d`` bars (source ``live-sampled``) as each one completes. After
  50 live days these are the whole seed and nothing else is consulted.
* **Spot days**: for days the bot never saw -- a cold start, or a gap while it
  was down -- the last close of each UTC day from the stored spot series
  (Coinbase SOL-USD, the same data every replay ran on).

A live day always beats a spot day for the same date. The seed is the most
recent ``want`` days that exist; missing days are left missing, never
invented, matching the aggregator's own rule.

**What this blends, said plainly.** Spot and the front-month future differ by
the basis -- typically under a percent, a dollar or two at these prices. For
the first ``want`` live days the average is partly spot while the close it is
compared against is the future, so a flip near the line can land a day early
or late. It fades as live days replace spot ones and is gone after ``want``
days. The alternative -- 50 days of silence on every restart -- is worse.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal

from app.backtest.models import Bar

#: Source stamped on the 1d bars a running strategy writes for its own days.
LIVE_DAILY_SOURCE = "live-sampled"


def daily_bar(
    *,
    source: str,
    symbol: str,
    day: date,
    close: Decimal,
    open_: Decimal | None = None,
    high: Decimal | None = None,
    low: Decimal | None = None,
) -> Bar:
    """One ``1d`` bar for a UTC day. Only ``close`` is required: a seed built
    from spot closes has no honest open, high or low, and a rule that reads
    only closes never needs them, so they default to the close."""
    return Bar(
        source=source,
        symbol=symbol,
        interval="1d",
        opened_at=datetime.combine(day, time(0, 0), tzinfo=UTC),
        open=close if open_ is None else open_,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=Decimal(0),
    )


def assemble_daily_seed(
    *,
    live_days: Iterable[Bar],
    spot_closes: Iterable[tuple[date, Decimal]],
    want: int,
    spot_source: str,
    spot_symbol: str,
) -> list[Bar]:
    """The most recent ``want`` daily bars, live beating spot per day, oldest first."""
    if want < 1:
        return []
    by_day: dict[date, Bar] = {}
    for day, close in spot_closes:
        by_day[day] = daily_bar(source=spot_source, symbol=spot_symbol, day=day, close=close)
    for bar in live_days:
        if bar.interval != "1d":
            raise ValueError(f"live seed bars must be 1d, got {bar.interval!r}")
        by_day[bar.opened_at.date()] = bar
    days: Sequence[date] = sorted(by_day)[-want:]
    return [by_day[d] for d in days]


def describe_seed(bars: Sequence[Bar]) -> dict[str, object]:
    """What was fed, for the startup log: counts by source and the span."""
    by_source: dict[str, int] = {}
    for bar in bars:
        by_source[bar.source] = by_source.get(bar.source, 0) + 1
    return {
        "days": len(bars),
        "by_source": by_source,
        "first_day": bars[0].opened_at.date().isoformat() if bars else None,
        "last_day": bars[-1].opened_at.date().isoformat() if bars else None,
    }


__all__ = ["LIVE_DAILY_SOURCE", "assemble_daily_seed", "daily_bar", "describe_seed"]
