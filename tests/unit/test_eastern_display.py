"""Eastern-time display, and the rule it must never break.

The operating team reads Eastern time; the system stores and computes in UTC.
These tests pin the boundary: Eastern appears ALONGSIDE UTC as text, labelled
EST or EDT honestly by date -- and nothing else changes. A strategy session
defined at 14:30 UTC still fires at 14:30 UTC in January and July alike; only
the label beside it moves.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

import app.utilities.timeutils as timeutils
from app.strategy.orb import SolOrbStrategy
from app.utilities.timeutils import eastern_display, eastern_hhmm


class TestEasternDisplay:
    def test_summer_is_edt_minus_four(self) -> None:
        assert (
            eastern_display(datetime(2026, 8, 20, 14, 30, tzinfo=UTC)) == "2026-08-20 10:30:00 EDT"
        )

    def test_winter_is_est_minus_five(self) -> None:
        """The honest label. "EST" year-round would be wrong for two-thirds of
        the year, which is exactly the class of mistake local time invites."""
        assert (
            eastern_display(datetime(2026, 1, 20, 14, 30, tzinfo=UTC)) == "2026-01-20 09:30:00 EST"
        )

    def test_the_date_can_roll_backwards_across_midnight_utc(self) -> None:
        assert eastern_display(datetime(2026, 8, 20, 2, 0, tzinfo=UTC)).startswith("2026-08-19 22:")

    def test_naive_datetimes_are_still_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            eastern_display(datetime(2026, 8, 20, 14, 30))


class TestEasternHhmm:
    def test_a_fixed_utc_session_reads_differently_by_season(self) -> None:
        """14:30 UTC is 09:30 EST in winter and 10:30 EDT in summer.

        This display existing is the point: the strategy trades fixed UTC, and
        a team thinking in Eastern sees exactly which local hour that lands on
        today, instead of assuming.
        """
        winter = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
        summer = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

        assert eastern_hhmm(time(14, 30), on=winter) == "09:30 EST"
        assert eastern_hhmm(time(14, 30), on=summer) == "10:30 EDT"

    def test_the_strategy_reports_both_clocks(self) -> None:
        described = SolOrbStrategy().describe()

        assert described["sessions_utc"] == ["08:00:00", "14:30:00"]
        eastern = described["sessions_eastern_today"]
        assert isinstance(eastern, list) and len(eastern) == 2
        assert all(e.endswith(("EST", "EDT")) for e in eastern)


class TestGracefulDegradation:
    def test_a_missing_tz_database_degrades_to_text_not_an_exception(self, monkeypatch) -> None:
        """Display must never take down a trading process."""
        monkeypatch.setattr(timeutils, "_eastern_zone", None)

        assert "unavailable" in eastern_display(datetime(2026, 8, 20, 14, 30, tzinfo=UTC))
        assert eastern_hhmm(time(14, 30)) == "unavailable"


class TestUtcStaysTheOnlyInternalRepresentation:
    @pytest.mark.safety
    def test_session_behaviour_is_identical_across_the_dst_boundary(self) -> None:
        """The display changes with the season; the TRADING HOUR must not.

        If someone ever "helpfully" redefines sessions in local time, a 14:30
        UTC winter bar and a 14:30 UTC summer bar would stop both starting a
        session -- this is the test that catches it.
        """
        from datetime import timedelta
        from decimal import Decimal

        from app.backtest.models import Bar

        def bar_at(when: datetime) -> Bar:
            return Bar(
                source="coinbase",
                symbol="SOL-USD",
                interval="1m",
                opened_at=when,
                open=Decimal("100"),
                high=Decimal("100"),
                low=Decimal("100"),
                close=Decimal("100"),
            )

        for month in (1, 7):  # EST and EDT
            strategy = SolOrbStrategy()
            session_open = datetime(2026, month, 6, 14, 30, tzinfo=UTC)
            strategy.handle_bar(bar_at(session_open))
            assert strategy.describe()["counters"]["sessions_seen"] == 1, (  # type: ignore[index]
                f"the 14:30 UTC session must start in month {month} exactly as in any other"
            )
            strategy.handle_bar(bar_at(session_open + timedelta(hours=3)))
            assert strategy.describe()["counters"]["sessions_seen"] == 1  # type: ignore[index]
