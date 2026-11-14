from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lakeinlet.timeutil import (
    exceeds_future_tolerance,
    format_utc,
    max_time,
    min_time,
    parse_utc,
    safe_watermark,
    shift_seconds,
)

def test_parse_z_and_zero_offset_are_equivalent():
    first = parse_utc("2025-03-01T10:20:30Z")
    second = parse_utc("2025-03-01T10:20:30+00:00")
    assert first == second
    assert first.tzinfo == timezone.utc

def test_parse_accepts_aware_utc_datetime():
    value = datetime(2025, 3, 1, 10, tzinfo=timezone.utc)
    assert parse_utc(value) == value

@pytest.mark.parametrize(
    "value",
    [
        "2025-03-01T10:20:30",
        "2025-03-01",
        "2025-03-01T10:20:30+08:00",
        "",
        "not-a-time",
        123,
    ],
)
def test_parse_rejects_ambiguous_or_invalid_values(value):
    with pytest.raises(ValueError):
        parse_utc(value)

def test_parse_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone"):
        parse_utc(datetime(2025, 3, 1, 10, 0))

def test_format_is_canonical_and_stable():
    value = parse_utc("2025-03-01T10:20:30Z")
    assert format_utc(value) == "2025-03-01T10:20:30Z"
    assert format_utc(value) == format_utc(value)

def test_format_rejects_naive_values():
    with pytest.raises(ValueError):
        format_utc(datetime(2025, 3, 1))

def test_future_tolerance_uses_strict_boundary():
    as_of = parse_utc("2025-03-01T03:00:00Z")
    assert not exceeds_future_tolerance(
        as_of + timedelta(seconds=60), as_of, 60
    )
    assert exceeds_future_tolerance(
        as_of + timedelta(seconds=61), as_of, 60
    )

def test_safe_watermark_subtracts_lateness():
    observed = parse_utc("2025-03-01T01:00:00Z")
    assert safe_watermark(observed, 600) == parse_utc("2025-03-01T00:50:00Z")
    assert safe_watermark(observed, 0) == observed

def test_shift_and_min_max_helpers():
    first = parse_utc("2025-03-01T00:00:00Z")
    second = parse_utc("2025-03-01T01:00:00Z")
    assert shift_seconds(first, 30) == parse_utc("2025-03-01T00:00:30Z")
    assert min_time(first, second, None) == first
    assert max_time(first, None, second) == second
    assert min_time(None, None) is None
    assert max_time() is None
