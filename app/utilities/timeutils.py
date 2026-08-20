"""Time handling.

Rules for this project, enforced here and by ruff's DTZ ruleset:

* Every trading timestamp is timezone-aware.
* UTC is the only internal representation.
* Naive datetimes are rejected, never "assumed to be UTC".
* Exchange-local time is a presentation concern only.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, tzinfo

ISO_FORMAT_NOTE = "ISO-8601 with explicit UTC offset, microsecond precision"


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as UTC, rejecting naive datetimes.

    A naive datetime in trading code is a latent correctness bug (it silently
    adopts whatever the host timezone happens to be), so it is an error rather
    than something to paper over.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "naive datetime rejected: all trading timestamps must be timezone-aware UTC"
        )
    return value.astimezone(UTC)


def to_iso(value: datetime) -> str:
    """Serialise a datetime for storage/logging (always UTC, always explicit offset)."""
    return ensure_utc(value).isoformat()


def from_iso(value: str) -> datetime:
    """Parse a stored ISO-8601 timestamp back into an aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    return ensure_utc(parsed)


def age_seconds(value: datetime, *, now: datetime | None = None) -> float:
    """Age of ``value`` in seconds.

    A future timestamp yields a negative age; callers that treat "fresh" as
    ``age <= limit`` must also reject implausible negative ages themselves.
    """
    reference = ensure_utc(now) if now is not None else utc_now()
    return (reference - ensure_utc(value)).total_seconds()


def utc_date_string(value: datetime | None = None) -> str:
    """UTC calendar date (YYYY-MM-DD) used to key daily performance rows."""
    reference = ensure_utc(value) if value is not None else utc_now()
    return reference.date().isoformat()


def hours_ago(hours: float, *, now: datetime | None = None) -> datetime:
    reference = ensure_utc(now) if now is not None else utc_now()
    return reference - timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Eastern-time DISPLAY. Presentation only, per the rules at the top of this
# module: nothing is ever stored, compared, or scheduled in local time.
# ---------------------------------------------------------------------------

#: The operating team reads Eastern time. Shown ALONGSIDE UTC, never instead
#: of it, and labelled EST or EDT honestly by date -- "EST" year-round would
#: be wrong for two-thirds of the year, which is exactly the class of mistake
#: local time invites.
_EASTERN_ZONE_NAME = "America/New_York"

_eastern_zone: object = False  # False = not yet resolved; None = unavailable


def _eastern() -> tzinfo | None:
    """The Eastern zone, or ``None`` where no tz database exists.

    Display must never take down a trading process, so a missing tzdata
    degrades to "unavailable" text rather than an exception. The Docker image
    installs tzdata; this guard covers everywhere else the code might run.
    """
    global _eastern_zone
    if _eastern_zone is False:
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            _eastern_zone = ZoneInfo(_EASTERN_ZONE_NAME)
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError or missing key
            _eastern_zone = None
    return _eastern_zone  # type: ignore[return-value]


def eastern_display(value: datetime) -> str:
    """``2026-08-20 07:50:09 EDT`` for a UTC instant. Presentation only."""
    zone = _eastern()
    if zone is None:
        return "eastern time unavailable (no tz database)"
    local = ensure_utc(value).astimezone(zone)
    return local.strftime("%Y-%m-%d %H:%M:%S %Z")


def eastern_hhmm(utc_hhmm: time, *, on: datetime | None = None) -> str:
    """What a fixed UTC clock time reads as in Eastern, on a given date.

    The date matters: 14:30 UTC is 09:30 EST in January and 10:30 EDT in
    August. Callers display this next to the UTC value so a team that thinks
    in Eastern can see, rather than assume, which hour a UTC-fixed schedule
    lands on today.
    """
    zone = _eastern()
    if zone is None:
        return "unavailable"
    reference = ensure_utc(on) if on is not None else utc_now()
    at = reference.replace(
        hour=utc_hhmm.hour, minute=utc_hhmm.minute, second=0, microsecond=0
    ).astimezone(zone)
    return at.strftime("%H:%M %Z")


__all__ = [
    "age_seconds",
    "eastern_display",
    "eastern_hhmm",
    "ensure_utc",
    "from_iso",
    "hours_ago",
    "to_iso",
    "utc_date_string",
    "utc_now",
]
