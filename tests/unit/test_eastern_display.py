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

        # The anchors are season-independent, unlike the UTC values: London is
        # fixed to UTC, NY to Eastern wall-clock so it tracks DST.
        anchors = [s["anchor"] for s in described["sessions"]]  # type: ignore[index,union-attr]
        assert anchors == ["08:00 UTC", "09:30 America/New_York"]

        eastern = described["sessions_eastern_today"]
        assert isinstance(eastern, list) and len(eastern) == 2
        assert all(e.endswith(("EST", "EDT")) for e in eastern)
        # The NY session reads 09:30 Eastern in EVERY season -- the whole point
        # of anchoring it to the wall-clock rather than a UTC constant.
        assert eastern[1].startswith("09:30")


class TestGracefulDegradation:
    def test_a_missing_tz_database_degrades_to_text_not_an_exception(self, monkeypatch) -> None:
        """Display must never take down a trading process."""
        monkeypatch.setattr(timeutils, "_eastern_zone", None)

        assert "unavailable" in eastern_display(datetime(2026, 8, 20, 14, 30, tzinfo=UTC))
        assert eastern_hhmm(time(14, 30)) == "unavailable"


class TestUtcStaysTheOnlyInternalRepresentation:
    @pytest.mark.safety
    def test_the_ny_session_tracks_eastern_wall_clock_across_dst(self) -> None:
        """Storage stays UTC, but the NY session is pinned to 9:30 ET.

        The trading HOUR is the Eastern wall-clock, so its UTC value must SHIFT
        with DST -- 14:30 UTC in winter, 13:30 UTC in summer, both 9:30 ET. A
        fixed 14:30 UTC (the old reading) held 9:30 only in winter and ran an
        hour late all summer; this is the test that keeps the anchor honest in
        both seasons.
        """
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

        # 9:30 ET expressed in UTC, per season -- both must open the NY session.
        for opened in (
            datetime(2026, 1, 6, 14, 30, tzinfo=UTC),  # 9:30 EST
            datetime(2026, 7, 6, 13, 30, tzinfo=UTC),  # 9:30 EDT
        ):
            strategy = SolOrbStrategy()
            strategy.handle_bar(bar_at(opened))
            assert strategy.describe()["counters"]["sessions_seen"] == 1, (  # type: ignore[index]
                f"9:30 ET must start the NY session at {opened.isoformat()}"
            )

        # The old fixed hour is now wrong in summer: 14:30 UTC in July is an
        # hour past the 13:30 open, so it must NOT start the NY session.
        strategy = SolOrbStrategy()
        strategy.handle_bar(bar_at(datetime(2026, 7, 6, 14, 30, tzinfo=UTC)))
        assert strategy.describe()["counters"]["sessions_seen"] == 0  # type: ignore[index]
