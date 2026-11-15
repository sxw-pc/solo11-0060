"""Typed domain models shared by the audit pipeline, ledger, and reports."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .timeutil import format_utc

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

#: prev_batch_id links are only valid between batches in one partition.
CHAIN_RULE = (
    "prev_batch_id 仅在同一分区内连续有效；跨分区不得接链，补齐缺口后应将 "
    "prev_batch_id 重接到同分区内序列紧邻的前驱批次。"
)

def partition_label(partition: dict[str, str] | None) -> str:
    """Canonical, stable label for a partition mapping."""
    if not partition:
        return ""
    return ",".join(f"{key}={partition[key]}" for key in sorted(partition))

def _iso(value: datetime | None) -> str | None:
    return format_utc(value) if value is not None else None

@dataclass(slots=True, frozen=True)
class SourceConfig:
    name: str
    partition_keys: tuple[str, ...]
    allowed_lateness_seconds: int
    future_tolerance_seconds: int
    sequence_policy: str = "contiguous"
    manifest_dir: str = ""
    manifest_glob: str = "*.yaml"
    event_time_field: str = "event_time"
    record_id_field: str = "record_id"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "partition_keys": list(self.partition_keys),
            "allowed_lateness_seconds": self.allowed_lateness_seconds,
            "future_tolerance_seconds": self.future_tolerance_seconds,
            "sequence_policy": self.sequence_policy,
            "manifest_dir": self.manifest_dir,
            "manifest_glob": self.manifest_glob,
            "event_time_field": self.event_time_field,
            "record_id_field": self.record_id_field,
        }

@dataclass(slots=True, frozen=True)
class AuditConfig:
    project: str
    lake_root: Path
    as_of: datetime
    sources: tuple[SourceConfig, ...]
    config_path: Path

    @property
    def source_map(self) -> dict[str, SourceConfig]:
        return {source.name: source for source in self.sources}

    def semantic_dict(self) -> dict[str, Any]:
        """Configuration content that affects audit outcomes (no local paths)."""
        return {
            "project": self.project,
            "as_of": format_utc(self.as_of),
            "sources": [source.to_dict() for source in self.sources],
        }

@dataclass(slots=True, frozen=True)
class FileRef:
    path: str
    sha256: str
    bytes: int | None = None
    rows: int | None = None
    event_time_min: datetime | None = None
    event_time_max: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "rows": self.rows,
            "event_time_min": _iso(self.event_time_min),
            "event_time_max": _iso(self.event_time_max),
        }

@dataclass(slots=True)
class BatchManifest:
    manifest_path: Path
    manifest_relpath: str
    batch_id: str
    source: str
    sequence: int
    partition: dict[str, str]
    declared_at: datetime
    files: tuple[FileRef, ...]
    prev_batch_id: str | None = None

    @property
    def partition_label(self) -> str:
        return partition_label(self.partition)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_path": self.manifest_relpath,
            "batch_id": self.batch_id,
            "source": self.source,
            "sequence": self.sequence,
            "partition": dict(sorted(self.partition.items())),
            "declared_at": format_utc(self.declared_at),
            "prev_batch_id": self.prev_batch_id,
            "files": [item.to_dict() for item in self.files],
        }

@dataclass(slots=True, frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    source: str | None = None
    partition: dict[str, str] | None = None
    batch_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"unknown finding severity: {self.severity}")

    @property
    def partition_label(self) -> str:
        return partition_label(self.partition)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "source": self.source,
            "partition": (
                dict(sorted(self.partition.items())) if self.partition else None
            ),
            "batch_id": self.batch_id,
            "message": self.message,
            "evidence": _normalize_json(self.evidence),
        }

    def sort_key(self) -> tuple[Any, ...]:
        return (
            SEVERITY_ORDER[self.severity],
            self.source or "",
            self.code,
            self.batch_id or "",
            self.partition_label,
            self.message,
            _canonical(self.evidence),
        )

def _normalize_json(value: Any) -> Any:
    """Convert dataclass-adjacent values into deterministic JSON values."""
    if isinstance(value, datetime):
        return format_utc(value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(key): _normalize_json(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    return value

def _canonical(value: Any) -> str:
    import json

    return json.dumps(_normalize_json(value), sort_keys=True, ensure_ascii=False)

@dataclass(slots=True, frozen=True)
class Record:
    record_id: str
    event_time: datetime
    file_path: str
    line_number: int

@dataclass(slots=True)
class FileScan:
    ref: FileRef
    relative_path: str
    exists: bool
    actual_sha256: str | None
    actual_bytes: int | None
    header: tuple[str, ...]
    row_count: int
    records: tuple[Record, ...]
    event_time_min: datetime | None
    event_time_max: datetime | None
    issues: tuple[Finding, ...] = ()

    @property
    def effective_digest(self) -> str:
        return self.actual_sha256 or self.ref.sha256

@dataclass(slots=True)
class BatchScan:
    manifest: BatchManifest
    files: tuple[FileScan, ...]
    issues: tuple[Finding, ...] = ()
    record_count: int = 0
    event_time_min: datetime | None = None
    event_time_max: datetime | None = None

    @property
    def source(self) -> str:
        return self.manifest.source

    @property
    def batch_id(self) -> str:
        return self.manifest.batch_id

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
            "error_count": self.error_count,
            "warning_count": self.warning_count,
        }


# ---------------------------------------------------------------------------
# Completion plans
#
# A completion plan turns SEQUENCE_GAP / CHAIN_BROKEN / CHAIN_ORDER_INVALID
# findings into actionable repair guidance: which missing sequence numbers to
# re-ingest, which partition each candidate belongs to (or which partitions a
# human must choose between), and how the prev_batch_id chain should be
# rewired once the data is backfilled.
# ---------------------------------------------------------------------------

#: Basis code for a partition suggested from both surrounding manifests.
PARTITION_BASIS_BOTH_NEIGHBORS = "both_neighbors"
#: Basis code for a partition suggested from the only available neighbor.
PARTITION_BASIS_SINGLE_NEIGHBOR = "single_neighbor"
#: Basis code for an option inside a multi-candidate, human-decision plan.
PARTITION_BASIS_AMBIGUOUS = "ambiguous_neighbors"


@dataclass(slots=True, frozen=True)
class PartitionOption:
    """One suggested partition for a backfilled sequence candidate."""

    partition: dict[str, str]
    basis: str
    needs_manual_decision: bool
    uncertainty_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "partition": dict(sorted(self.partition.items())),
            "partition_label": partition_label(self.partition),
            "basis": self.basis,
            "needs_manual_decision": self.needs_manual_decision,
            "uncertainty_reason": self.uncertainty_reason,
        }


@dataclass(slots=True, frozen=True)
class SequenceCandidate:
    """One missing sequence number that needs to be re-ingested."""

    sequence: int
    prev_sequence: int | None
    next_sequence: int | None
    partition_options: tuple[PartitionOption, ...]
    suggested_prev_batch_id: str | None
    chain_note: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "prev_sequence": self.prev_sequence,
            "next_sequence": self.next_sequence,
            "partition_options": [option.to_dict() for option in self.partition_options],
            "suggested_prev_batch_id": self.suggested_prev_batch_id,
            "chain_note": self.chain_note,
        }


@dataclass(slots=True, frozen=True)
class ChainRequest:
    """Reusable prev_batch_id repair information for one broken chain link."""

    finding_code: str
    batch_id: str
    declared_prev_batch_id: str | None
    partition: dict[str, str]
    manifest_path: str
    sequence: int
    suggested_prev_batch_id: str | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_code": self.finding_code,
            "batch_id": self.batch_id,
            "declared_prev_batch_id": self.declared_prev_batch_id,
            "partition": dict(sorted(self.partition.items())),
            "partition_label": partition_label(self.partition),
            "manifest_path": self.manifest_path,
            "sequence": self.sequence,
            "suggested_prev_batch_id": self.suggested_prev_batch_id,
            "note": self.note,
        }


@dataclass(slots=True, frozen=True)
class CompletionPlan:
    """Actionable backfill plan for one source / gap interval / partition."""

    plan_id: str
    source: str
    gap_range: tuple[int, int] | None
    partition: dict[str, str] | None
    needs_manual_decision: bool
    uncertainty_reason: str | None
    candidates: tuple[SequenceCandidate, ...]
    chain_requests: tuple[ChainRequest, ...]
    chain_rule: str = CHAIN_RULE

    @property
    def partition_label(self) -> str:
        return partition_label(self.partition)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "source": self.source,
            "gap_range": list(self.gap_range) if self.gap_range is not None else None,
            "partition": (
                dict(sorted(self.partition.items())) if self.partition else None
            ),
            "partition_label": self.partition_label,
            "needs_manual_decision": self.needs_manual_decision,
            "uncertainty_reason": self.uncertainty_reason,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "chain_requests": [request.to_dict() for request in self.chain_requests],
            "chain_rule": self.chain_rule,
        }


def build_plan_id(
    source: str,
    gap_range: tuple[int, int] | None,
    partition_identity: str,
) -> str:
    """Derive the stable, replay-safe identity of a completion plan.

    The identity is fully determined by the source, the gap interval, and the
    partition (or the sorted set of candidate partition labels when the
    partition itself is ambiguous). Re-auditing identical inputs therefore
    refreshes the same plan id instead of appending new history.
    """
    digest = hashlib.sha256()
    digest.update(source.encode("utf-8"))
    digest.update(b"\0")
    digest.update(
        ("*" if gap_range is None else f"{gap_range[0]}:{gap_range[1]}").encode(
            "utf-8"
        )
    )
    digest.update(b"\0")
    digest.update(partition_identity.encode("utf-8"))
    return "plan-" + digest.hexdigest()[:12]
