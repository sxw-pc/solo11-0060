from __future__ import annotations

from datetime import datetime, timezone

from lakeinlet.models import SourceConfig
from lakeinlet.summaries import BatchSummary
from lakeinlet.timeutil import parse_utc
from lakeinlet.watermark import evaluate_watermarks

def _watermark(result, source: str, region: str | None = None):
    return next(
        item
        for item in result.watermarks
        if item.source == source
        and (region is None or item.partition.get("region") == region)
    )

def test_clean_demo_has_expected_watermarks(demo_result):
    assert demo_result.status == "passed"
    assert len(demo_result.watermarks) == 3
    eu = _watermark(demo_result, "orders", "eu")
    assert eu.max_event_time == parse_utc("2025-03-01T01:50:00Z")
    assert eu.safe_watermark == parse_utc("2025-03-01T00:50:00Z")
    assert eu.batch_count == 3
    assert eu.late_batch_count == 0
    us = _watermark(demo_result, "orders", "us")
    assert us.max_event_time == parse_utc("2025-03-01T01:30:00Z")
    assert us.safe_watermark == parse_utc("2025-03-01T00:30:00Z")
    clicks = _watermark(demo_result, "clicks")
    assert clicks.safe_watermark == parse_utc("2025-03-01T00:40:00Z")
    assert all(not batch.late for batch in demo_result.batches)

def test_broken_demo_detects_late_batch(broken_result):
    late = next(
        finding for finding in broken_result.findings if finding.code == "LATE_BATCH"
    )
    assert late.batch_id == "orders-late"
    assert late.evidence["event_time_max"] == "2025-03-01T00:20:00Z"
    assert late.evidence["safe_watermark_before"] == "2025-03-01T00:50:00Z"
    batch = next(
        item for item in broken_result.batches if item.batch_id == "orders-late"
    )
    assert batch.late is True
    assert batch.watermark_before == parse_utc("2025-03-01T00:50:00Z")

def test_future_batch_is_flagged_and_does_not_advance_watermark(broken_result):
    future = next(
        finding
        for finding in broken_result.findings
        if finding.code == "FUTURE_EVENT_TIME"
    )
    assert future.batch_id == "orders-future"
    assert future.evidence["event_time_max"] == "2025-03-01T04:05:00Z"
    watermark = _watermark(broken_result, "orders", "eu")
    assert watermark.max_event_time == parse_utc("2025-03-01T01:10:00Z")
    assert watermark.safe_watermark == parse_utc("2025-03-01T01:00:00Z")
    assert watermark.late_batch_count == 1
    assert watermark.batch_count == 6

def test_late_boundary_is_inclusive_and_one_second_below_is_late():
    source = SourceConfig(
        name="s",
        partition_keys=("dt",),
        allowed_lateness_seconds=600,
        future_tolerance_seconds=60,
    )
    as_of = parse_utc("2025-03-01T05:00:00Z")
    summaries = [
        BatchSummary(
            "s",
            "b1",
            1,
            {"dt": "d"},
            parse_utc("2025-03-01T01:00:00Z"),
            "m1",
            1,
            1,
            parse_utc("2025-03-01T00:59:00Z"),
            parse_utc("2025-03-01T01:00:00Z"),
        ),
        BatchSummary(
            "s",
            "b2",
            2,
            {"dt": "d"},
            parse_utc("2025-03-01T01:10:00Z"),
            "m2",
            1,
            1,
            parse_utc("2025-03-01T00:50:00Z"),
            parse_utc("2025-03-01T00:50:00Z"),
        ),
        BatchSummary(
            "s",
            "b3",
            3,
            {"dt": "d"},
            parse_utc("2025-03-01T01:20:00Z"),
            "m3",
            1,
            1,
            parse_utc("2025-03-01T00:49:59Z"),
            parse_utc("2025-03-01T00:49:59Z"),
        ),
    ]
    watermarks, findings = evaluate_watermarks(summaries, {"s": source}, as_of)
    assert summaries[0].watermark_before is None
    assert summaries[1].late is False
    assert summaries[2].late is True
    assert [finding.code for finding in findings] == ["LATE_BATCH"]
    assert watermarks[0].late_batch_count == 1
    assert watermarks[0].safe_watermark == parse_utc("2025-03-01T00:50:00Z")

def test_future_declared_at_is_flagged():
    source = SourceConfig(
        name="s",
        partition_keys=(),
        allowed_lateness_seconds=0,
        future_tolerance_seconds=30,
    )
    as_of = datetime(2025, 3, 1, 3, 0, tzinfo=timezone.utc)
    summaries = [
        BatchSummary(
            "s",
            "b1",
            1,
            {},
            parse_utc("2025-03-01T03:01:00Z"),
            "m1",
            0,
            0,
            None,
            None,
        )
    ]
    _, findings = evaluate_watermarks(summaries, {"s": source}, as_of)
    assert [finding.code for finding in findings] == ["FUTURE_DECLARED_AT"]
