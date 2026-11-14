"""Strict UTC time helpers.

lakeinlet never consults the wall clock. Every temporal judgement is anchored
to the configured ``as_of`` instant, which makes audits and tests repeatable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def parse_utc(value: object, field: str = "timestamp") -> datetime:
    """Parse an ISO-8601 value and require an explicit UTC offset.

    ``Z`` and ``+00:00`` are accepted. Naive values and non-zero offsets are
    rejected: silently interpreting a local time could change an audit result.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} must be a non-empty UTC ISO-8601 string")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{field} is not valid ISO-8601: {value!r}") from exc
    else:
        raise ValueError(f"{field} must be an ISO-8601 string or datetime")

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC timezone offset")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC (offset +00:00), got {value!r}")
    return parsed.astimezone(UTC)


def format_utc(value: datetime) -> str:
    """Render a UTC instant in the canonical report representation."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("value must be timezone-aware UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def shift_seconds(value: datetime, seconds: int) -> datetime:
    """Shift an instant by an integer number of seconds."""
    return value + timedelta(seconds=seconds)


def safe_watermark(max_event_time: datetime, allowed_lateness_seconds: int) -> datetime:
    """Return the largest event time that is still considered on time."""
    return max_event_time - timedelta(seconds=allowed_lateness_seconds)


def exceeds_future_tolerance(
    observed: datetime, as_of: datetime, tolerance_seconds: int
) -> bool:
    """True when an observed instant is later than the audit's future bound."""
    return observed > as_of + timedelta(seconds=tolerance_seconds)


def max_time(*values: datetime | None) -> datetime | None:
    """Return the maximum non-null datetime."""
    present = [value for value in values if value is not None]
    return max(present) if present else None


def min_time(*values: datetime | None) -> datetime | None:
    """Return the minimum non-null datetime."""
    present = [value for value in values if value is not None]
    return min(present) if present else None
