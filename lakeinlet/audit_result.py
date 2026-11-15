"""Top-level audit result and serialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .convergence import RecordConvergence
from .models import Finding
from .summaries import BatchSummary, SourceSummary, Watermark
from .timeutil import format_utc


@dataclass(slots=True)
class AuditResult:
    project: str
    as_of: datetime
    sources: tuple[SourceSummary, ...]
    findings: list[Finding]
    watermarks: tuple[Watermark, ...]
    batches: tuple[BatchSummary, ...]
    manifest_count: int
    file_count: int
    record_count: int
    config_digest: str
    raw_record_count: int = 0
    run_id: str = ""
    status: str = "passed"
    record_convergences: tuple[RecordConvergence, ...] = ()

    @property
    def error_count(self) -> int:
        return sum(1 for item in self.findings if item.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for item in self.findings if item.severity == "warning")

    @property
    def info_count(self) -> int:
        return sum(1 for item in self.findings if item.severity == "info")

    @property
    def finding_count(self) -> int:
        return len(self.findings)

    def summary_dict(self) -> dict[str, int]:
        return {
            "manifest_count": self.manifest_count,
            "batch_count": len(self.batches),
            "file_count": self.file_count,
            "record_count": self.record_count,
            "raw_record_count": self.raw_record_count,
            "finding_count": self.finding_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "project": self.project,
            "as_of": format_utc(self.as_of),
            "run_id": self.run_id,
            "status": self.status,
            "config_digest": self.config_digest,
            "summary": self.summary_dict(),
            "sources": [source.to_dict() for source in self.sources],
            "watermarks": [watermark.to_dict() for watermark in self.watermarks],
            "batches": [batch.to_dict() for batch in self.batches],
            "record_convergences": [
                account.to_dict() for account in self.record_convergences
            ],
            "findings": [finding.to_dict() for finding in self.findings],
        }
