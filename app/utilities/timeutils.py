"""Time handling.

Rules for this project, enforced here and by ruff's DTZ ruleset:

* Every trading timestamp is timezone-aware.
* UTC is the only internal representation.
* Naive datetimes are rejected, never "assumed to be UTC".
* Exchange-local time is a presentation concern only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


__all__ = [
    "age_seconds",
    "ensure_utc",
    "from_iso",
    "hours_ago",
    "to_iso",
    "utc_date_string",
    "utc_now",
]
