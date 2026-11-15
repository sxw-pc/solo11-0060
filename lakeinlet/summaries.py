"""Report-facing summaries derived from scans and watermark evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .timeutil import format_utc


def _iso(value: datetime | None) -> str | None:
    return format_utc(value) if value is not None else None


@dataclass(slots=True)
class BatchSummary:
    source: str
    batch_id: str
    sequence: int
    partition: dict[str, str]
    declared_at: datetime
    manifest_relpath: str
    file_count: int
    record_count: int
    event_time_min: datetime | None
    event_time_max: datetime | None
    watermark_before: datetime | None = None
    watermark_after: datetime | None = None
    late: bool = False
    raw_record_count: int = 0
    superseded_record_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "batch_id": self.batch_id,
            "sequence": self.sequence,
            "partition": dict(sorted(self.partition.items())),
            "declared_at": format_utc(self.declared_at),
            "manifest_path": self.manifest_relpath,
            "file_count": self.file_count,
            "record_count": self.record_count,
            "raw_record_count": self.raw_record_count,
            "superseded_record_count": self.superseded_record_count,
            "event_time_min": _iso(self.event_time_min),
            "event_time_max": _iso(self.event_time_max),
            "watermark_before": _iso(self.watermark_before),
            "watermark_after": _iso(self.watermark_after),
            "late": self.late,
        }


@dataclass(slots=True, frozen=True)
class Watermark:
    source: str
    partition: dict[str, str]
    max_event_time: datetime | None
    safe_watermark: datetime | None
    batch_count: int
    late_batch_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "partition": dict(sorted(self.partition.items())),
            "max_event_time": _iso(self.max_event_time),
            "safe_watermark": _iso(self.safe_watermark),
            "batch_count": self.batch_count,
            "late_batch_count": self.late_batch_count,
        }


@dataclass(slots=True)
class SourceSummary:
    name: str
    partition_keys: tuple[str, ...]
    allowed_lateness_seconds: int
    future_tolerance_seconds: int
    sequence_policy: str
    manifest_count: int
    file_count: int
    record_count: int
    error_count: int = 0
    warning_count: int = 0
    raw_record_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "partition_keys": list(self.partition_keys),
            "allowed_lateness_seconds": self.allowed_lateness_seconds,
            "future_tolerance_seconds": self.future_tolerance_seconds,
            "sequence_policy": self.sequence_policy,
            "manifest_count": self.manifest_count,
            "file_count": self.file_count,
            "record_count": self.record_count,
            "raw_record_count": self.raw_record_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
        }
