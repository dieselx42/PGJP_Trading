"""The window `bars-import` asks a source for.

A future ``--end`` used to fetch the whole range and then fail on the last
page with HTTP 400 from the venue, reported as FETCH_FAILED after minutes of
successful work. Clamping to now is the fix; these pin it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.cli import _import_window

NOW = datetime(2026, 9, 20, 15, 30, tzinfo=UTC)


class TestImportWindow:
    def test_a_future_end_is_clamped_to_now_and_says_so(self) -> None:
        start, end, clamped = _import_window("2025-07-01", "2026-09-21", 365, now=NOW)
        assert end == NOW
        assert clamped is True
        assert start == datetime(2025, 7, 1, tzinfo=UTC)

    def test_a_past_end_is_left_alone(self) -> None:
        _start, end, clamped = _import_window("2021-07-01", "2023-07-01", 365, now=NOW)
        assert end == datetime(2023, 7, 1, tzinfo=UTC)
        assert clamped is False

    def test_an_omitted_end_is_now_and_is_not_a_clamp(self) -> None:
        _, end, clamped = _import_window("2026-09-19", None, 365, now=NOW)
        assert end == NOW
        assert clamped is False

    def test_an_omitted_start_counts_back_from_the_clamped_end(self) -> None:
        start, end, _ = _import_window(None, "2026-09-21", 30, now=NOW)
        assert end == NOW
        assert start == NOW - timedelta(days=30)

    def test_end_on_today_at_midnight_is_in_the_past_and_untouched(self) -> None:
        _, end, clamped = _import_window("2026-09-19", "2026-09-20", 365, now=NOW)
        assert end == datetime(2026, 9, 20, tzinfo=UTC)
        assert clamped is False
