from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from lakeinlet.models import BatchSummary, SourceConfig
from lakeinlet.partition import PartitionScheme
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
    # region is normalized to its canonical upper-case spelling.
    watermark = _watermark(broken_result, "orders", "EU")
    # The upper-case variant and collision EU batch join the same partition and
    # advance its watermark; the future batch still does not contribute.
    assert watermark.max_event_time == parse_utc("2025-03-01T02:02:00Z")
    assert watermark.safe_watermark == parse_utc("2025-03-01T01:52:00Z")
    assert watermark.late_batch_count == 1
    # All eu/EU/emea spellings merge into the one canonical EU partition:
    # eight original/variant batches, excluding the separate US partition.
    assert watermark.batch_count == 8
    us = _watermark(broken_result, "orders", "US")
    assert us.batch_count == 1
    assert us.safe_watermark == parse_utc("2025-03-01T01:54:00Z")

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


def _case_variant_source() -> SourceConfig:
    base = SourceConfig(
        name="s",
        partition_keys=("region",),
        allowed_lateness_seconds=0,
        future_tolerance_seconds=300,
    )
    return replace(
        base,
        partition_scheme=PartitionScheme.build(
            base, {"region": {"case": "upper", "aliases": {"EU": ["emea"]}}}
        ),
    )


def test_case_variant_partitions_share_one_watermark():
    """eu, EU and emea spellings merge onto one canonical EU watermark."""
    source = _case_variant_source()
    as_of = parse_utc("2025-03-01T05:00:00Z")
    summaries = [
        BatchSummary(
            "s", "b1", 1, {"region": "eu"},
            parse_utc("2025-03-01T01:00:00Z"), "m1", 1, 1,
            parse_utc("2025-03-01T00:50:00Z"),
            parse_utc("2025-03-01T01:00:00Z"),
        ),
        BatchSummary(
            "s", "b2", 2, {"region": "EU"},
            parse_utc("2025-03-01T01:10:00Z"), "m2", 1, 1,
            parse_utc("2025-03-01T01:05:00Z"),
            parse_utc("2025-03-01T01:10:00Z"),
        ),
        BatchSummary(
            "s", "b3", 3, {"region": "emea"},
            parse_utc("2025-03-01T01:20:00Z"), "m3", 1, 1,
            parse_utc("2025-03-01T01:15:00Z"),
            parse_utc("2025-03-01T01:20:00Z"),
        ),
    ]
    watermarks, findings = evaluate_watermarks(summaries, {"s": source}, as_of)
    assert len(watermarks) == 1
    assert watermarks[0].partition == {"region": "EU"}
    assert watermarks[0].batch_count == 3
    assert watermarks[0].max_event_time == parse_utc("2025-03-01T01:20:00Z")
    assert findings == []


def test_distinct_canonical_partitions_keep_separate_watermarks():
    source = _case_variant_source()
    as_of = parse_utc("2025-03-01T05:00:00Z")
    summaries = [
        BatchSummary(
            "s", "b1", 1, {"region": "EU"},
            parse_utc("2025-03-01T01:00:00Z"), "m1", 1, 1,
            parse_utc("2025-03-01T01:00:00Z"),
            parse_utc("2025-03-01T01:00:00Z"),
        ),
        BatchSummary(
            "s", "b2", 2, {"region": "US"},
            parse_utc("2025-03-01T01:10:00Z"), "m2", 1, 1,
            parse_utc("2025-03-01T01:10:00Z"),
            parse_utc("2025-03-01T01:10:00Z"),
        ),
    ]
    watermarks, _ = evaluate_watermarks(summaries, {"s": source}, as_of)
    assert {next(iter(wm.partition.items())) for wm in watermarks} == {
        ("region", "EU"),
        ("region", "US"),
    }
    assert all(wm.batch_count == 1 for wm in watermarks)


def test_broken_fixture_merges_case_variants_into_one_watermark(broken_result):
    """End-to-end: eu and EU manifests share the canonical EU watermark."""
    eu = _watermark(broken_result, "orders", "EU")
    assert eu.batch_count == 8
    raw_spellings = {
        batch.partition.get("region") for batch in broken_result.batches
    }
    # Reports only ever expose the canonical spelling.
    assert raw_spellings == {"EU", "US"}


def test_broken_fixture_watermark_advances_across_case_variant_batch(
    broken_result,
):
    upper = next(
        batch
        for batch in broken_result.batches
        if batch.batch_id == "orders-upper-region"
    )
    # It joins the EU watermark state rather than starting a new partition.
    assert upper.watermark_before is not None
    assert upper.partition == {"dt": "2025-03-01", "region": "EU"}
    assert upper.late is False
