"""Typed domain models shared by the audit pipeline, ledger, and reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .timeutil import format_utc

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

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
    """One CSV row carrying a record identity.

    ``event_time`` is ``None`` when the field cannot be parsed as UTC. Such a
    record still joins duplicate grouping and evidence, but is never a
    candidate for the retained slot and never contributes to event ranges.
    """

    record_id: str
    event_time: datetime | None
    file_path: str
    line_number: int
    raw_event_time: str = ""

    @property
    def event_time_parseable(self) -> bool:
        return self.event_time is not None

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

    @property
    def raw_record_count(self) -> int:
        """Physical rows that carry a non-empty record id (parseable or not)."""
        return len(self.records)

@dataclass(slots=True)
class BatchScan:
    manifest: BatchManifest
    files: tuple[FileScan, ...]
    issues: tuple[Finding, ...] = ()
    record_count: int = 0
    event_time_min: datetime | None = None
    event_time_max: datetime | None = None
    raw_record_count: int = 0
    superseded_record_count: int = 0
    effective_records: tuple[Record, ...] = ()

    @property
    def source(self) -> str:
        return self.manifest.source

    @property
    def batch_id(self) -> str:
        return self.manifest.batch_id

    @property
    def records(self) -> tuple[Record, ...]:
        """All identified rows in physical order, parseable or not."""
        return tuple(
            record for file_scan in self.files for record in file_scan.records
        )
