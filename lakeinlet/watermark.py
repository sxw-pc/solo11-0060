"""Event-time watermark reconstruction and lateness/future checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from .models import (
    BatchScan,
    BatchSummary,
    Finding,
    SourceConfig,
    Watermark,
    partition_label,
)
from .timeutil import exceeds_future_tolerance, format_utc, safe_watermark


@dataclass(slots=True)
class _WatermarkState:
    max_event_time: datetime | None = None
    safe_watermark: datetime | None = None
    batch_count: int = 0
    late_batch_count: int = 0


def summarize_batches(scans: Sequence[BatchScan]) -> list[BatchSummary]:
    """Create deterministic per-manifest summaries before rule evaluation."""
    ordered = sorted(
        scans,
        key=lambda item: (
            item.manifest.declared_at,
            item.manifest.sequence,
            item.manifest.manifest_relpath,
        ),
    )
    return [
        BatchSummary(
            source=scan.source,
            batch_id=scan.batch_id,
            sequence=scan.manifest.sequence,
            partition=dict(scan.manifest.partition),
            declared_at=scan.manifest.declared_at,
            manifest_relpath=scan.manifest.manifest_relpath,
            file_count=len(scan.files),
            record_count=scan.record_count,
            event_time_min=scan.event_time_min,
            event_time_max=scan.event_time_max,
        )
        for scan in ordered
    ]


def evaluate_watermarks(
    summaries: list[BatchSummary],
    sources: Mapping[str, SourceConfig],
    as_of: datetime,
) -> tuple[tuple[Watermark, ...], list[Finding]]:
    """Advance per-source/per-partition watermarks in declared arrival order.

    The safe watermark is ``max observed event time - allowed lateness``. A
    batch whose own maximum event time is below the watermark that existed
    before it arrived is late. Events beyond the configured future tolerance
    are findings and are deliberately excluded from watermark advancement, so
    corrupt future timestamps cannot make later legitimate batches look late.
    """
    findings: list[Finding] = []
    states: dict[tuple[str, tuple[tuple[str, str], ...]], _WatermarkState] = {}

    ordered = sorted(
        summaries,
        key=lambda item: (
            item.declared_at,
            item.sequence,
            item.manifest_relpath,
        ),
    )
    for batch in ordered:
        source = sources.get(batch.source)
        if source is None:
            continue
        key = (batch.source, tuple(sorted(batch.partition.items())))
        state = states.setdefault(key, _WatermarkState())
        before = state.safe_watermark
        batch.watermark_before = before

        future = False
        if batch.event_time_max is not None and exceeds_future_tolerance(
            batch.event_time_max, as_of, source.future_tolerance_seconds
        ):
            future = True
            findings.append(
                Finding(
                    "error",
                    "FUTURE_EVENT_TIME",
                    "batch contains event times beyond the configured future "
                    f"tolerance ({source.future_tolerance_seconds}s) relative to as_of",
                    source=batch.source,
                    partition=dict(batch.partition),
                    batch_id=batch.batch_id,
                    evidence={
                        "event_time_max": format_utc(batch.event_time_max),
                        "as_of": format_utc(as_of),
                        "future_tolerance_seconds": source.future_tolerance_seconds,
                    },
                )
            )
        if exceeds_future_tolerance(
            batch.declared_at, as_of, source.future_tolerance_seconds
        ):
            findings.append(
                Finding(
                    "error",
                    "FUTURE_DECLARED_AT",
                    "batch declared_at is beyond the configured future tolerance",
                    source=batch.source,
                    partition=dict(batch.partition),
                    batch_id=batch.batch_id,
                    evidence={
                        "declared_at": format_utc(batch.declared_at),
                        "as_of": format_utc(as_of),
                        "future_tolerance_seconds": source.future_tolerance_seconds,
                    },
                )
            )

        state.batch_count += 1
        if (
            not future
            and before is not None
            and batch.event_time_max is not None
            and batch.event_time_max < before
        ):
            batch.late = True
            state.late_batch_count += 1
            findings.append(
                Finding(
                    "error",
                    "LATE_BATCH",
                    "batch event-time maximum is below the partition's safe watermark",
                    source=batch.source,
                    partition=dict(batch.partition),
                    batch_id=batch.batch_id,
                    evidence={
                        "event_time_max": format_utc(batch.event_time_max),
                        "safe_watermark_before": format_utc(before),
                        "allowed_lateness_seconds": source.allowed_lateness_seconds,
                    },
                )
            )

        if not future and batch.event_time_max is not None:
            if (
                state.max_event_time is None
                or batch.event_time_max > state.max_event_time
            ):
                state.max_event_time = batch.event_time_max
                state.safe_watermark = safe_watermark(
                    state.max_event_time, source.allowed_lateness_seconds
                )
        batch.watermark_after = state.safe_watermark

    watermarks = tuple(
        Watermark(
            source=source_name,
            partition=dict(partition_items),
            max_event_time=state.max_event_time,
            safe_watermark=state.safe_watermark,
            batch_count=state.batch_count,
            late_batch_count=state.late_batch_count,
        )
        for (source_name, partition_items), state in sorted(
            states.items(), key=lambda item: (item[0][0], partition_label(dict(item[0][1])))
        )
    )
    return watermarks, findings
