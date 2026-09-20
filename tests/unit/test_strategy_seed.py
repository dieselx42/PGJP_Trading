"""Assembling the daily seed: live beats spot, most recent N, gaps stay gaps."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.models import Bar
from app.strategy.seed import LIVE_DAILY_SOURCE, assemble_daily_seed, daily_bar, describe_seed

D0 = date(2026, 1, 5)


def _d(i: int) -> date:
    return D0 + timedelta(days=i)


def _assemble(**kw: object) -> list[Bar]:
    base: dict[str, object] = {
        "live_days": [],
        "spot_closes": [],
        "want": 3,
        "spot_source": "coinbase",
        "spot_symbol": "SOL-USD",
    }
    return assemble_daily_seed(**{**base, **kw})  # type: ignore[arg-type]


class TestAssemble:
    def test_live_beats_spot_on_the_same_day_and_the_result_is_the_last_n(self) -> None:
        spot = [(_d(i), Decimal(100 + i)) for i in range(6)]
        live = [daily_bar(source=LIVE_DAILY_SOURCE, symbol="MSL", day=_d(4), close=Decimal("999"))]
        seed = _assemble(live_days=live, spot_closes=spot, want=3)
        assert [b.opened_at.date() for b in seed] == [_d(3), _d(4), _d(5)]
        assert [b.source for b in seed] == ["coinbase", LIVE_DAILY_SOURCE, "coinbase"]
        assert seed[1].close == Decimal("999")

    def test_missing_days_stay_missing(self) -> None:
        seed = _assemble(spot_closes=[(_d(0), Decimal(1)), (_d(3), Decimal(2))], want=5)
        assert [b.opened_at.date() for b in seed] == [_d(0), _d(3)]

    def test_want_zero_is_empty(self) -> None:
        assert _assemble(spot_closes=[(_d(0), Decimal(1))], want=0) == []

    def test_a_non_daily_live_bar_is_refused(self) -> None:
        bad = Bar(
            source=LIVE_DAILY_SOURCE,
            symbol="MSL",
            interval="1m",
            opened_at=datetime(2026, 1, 5, tzinfo=UTC),
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
        )
        with pytest.raises(ValueError, match="1d"):
            _assemble(live_days=[bad])

    def test_daily_bar_defaults_open_high_low_to_the_close(self) -> None:
        b = daily_bar(source="coinbase", symbol="SOL-USD", day=_d(0), close=Decimal("5"))
        assert (b.open, b.high, b.low, b.close) == (Decimal(5),) * 4
        assert b.interval == "1d"
        assert b.opened_at == datetime(2026, 1, 5, tzinfo=UTC)

    def test_describe_counts_by_source_and_gives_the_span(self) -> None:
        spot = [(_d(i), Decimal(1)) for i in range(3)]
        live = [daily_bar(source=LIVE_DAILY_SOURCE, symbol="MSL", day=_d(2), close=Decimal(2))]
        d = describe_seed(_assemble(live_days=live, spot_closes=spot, want=3))
        assert d == {
            "days": 3,
            "by_source": {"coinbase": 2, LIVE_DAILY_SOURCE: 1},
            "first_day": _d(0).isoformat(),
            "last_day": _d(2).isoformat(),
        }
        assert describe_seed([]) == {
            "days": 0,
            "by_source": {},
            "first_day": None,
            "last_day": None,
        }
