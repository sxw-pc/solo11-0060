"""Idempotency checks across batches, files, and records."""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from .models import BatchScan, Finding


def _location(scan: BatchScan, file_path: str) -> dict[str, str]:
    return {
        "manifest_path": scan.manifest.manifest_relpath,
        "batch_id": scan.batch_id,
        "file": file_path,
    }


def find_duplicate_findings(batches: Sequence[BatchScan]) -> list[Finding]:
    """Return findings for repeated batch ids, sequences, digests, and records."""
    findings: list[Finding] = []

    by_batch: dict[str, list[BatchScan]] = defaultdict(list)
    for scan in batches:
        by_batch[scan.batch_id].append(scan)
    for batch_id in sorted(by_batch):
        group = by_batch[batch_id]
        if len(group) < 2:
            continue
        group.sort(key=lambda item: item.manifest.manifest_relpath)
        findings.append(
            Finding(
                "error",
                "DUPLICATE_BATCH_ID",
                f"batch_id {batch_id!r} appears in {len(group)} manifests",
                source=group[0].source,
                batch_id=batch_id,
                evidence={
                    "count": len(group),
                    "manifests": [item.manifest.manifest_relpath for item in group],
                },
            )
        )

    by_source_sequence: dict[tuple[str, int], list[BatchScan]] = defaultdict(list)
    for scan in batches:
        by_source_sequence[(scan.source, scan.manifest.sequence)].append(scan)
    for source, sequence in sorted(by_source_sequence):
        group = by_source_sequence[(source, sequence)]
        if len(group) < 2:
            continue
        group.sort(key=lambda item: item.manifest.manifest_relpath)
        findings.append(
            Finding(
                "error",
                "DUPLICATE_SEQUENCE",
                f"source {source!r} declares sequence {sequence} "
                f"in {len(group)} manifests",
                source=source,
                evidence={
                    "sequence": sequence,
                    "count": len(group),
                    "batch_ids": [item.batch_id for item in group],
                    "manifests": [item.manifest.manifest_relpath for item in group],
                },
            )
        )

    by_digest: dict[str, list[tuple[BatchScan, str]]] = defaultdict(list)
    for scan in batches:
        for file_scan in scan.files:
            if file_scan.exists and file_scan.actual_sha256:
                by_digest[file_scan.actual_sha256].append(
                    (scan, file_scan.relative_path)
                )
    for digest in sorted(by_digest):
        group = by_digest[digest]
        if len(group) < 2:
            continue
        group.sort(key=lambda item: (item[1], item[0].manifest.manifest_relpath))
        findings.append(
            Finding(
                "error",
                "DUPLICATE_FILE_DIGEST",
                f"SHA-256 {digest[:12]}... is shared by {len(group)} data files",
                source=group[0][0].source,
                evidence={
                    "sha256": digest,
                    "count": len(group),
                    "locations": [
                        _location(scan, file_path) for scan, file_path in group
                    ],
                },
            )
        )

    by_record: dict[tuple[str, str], list[tuple[BatchScan, str, int]]] = defaultdict(list)
    for scan in batches:
        for file_scan in scan.files:
            for record in file_scan.records:
                by_record[(scan.source, record.record_id)].append(
                    (scan, record.file_path, record.line_number)
                )
    for source, record_id in sorted(by_record):
        group = by_record[(source, record_id)]
        if len(group) < 2:
            continue
        group.sort(key=lambda item: (item[1], item[2], item[0].batch_id))
        findings.append(
            Finding(
                "error",
                "DUPLICATE_RECORD_ID",
                f"record_id {record_id!r} appears {len(group)} times in source "
                f"{source!r}",
                source=source,
                evidence={
                    "record_id": record_id,
                    "count": len(group),
                    "locations": [
                        {
                            "manifest_path": scan.manifest.manifest_relpath,
                            "batch_id": scan.batch_id,
                            "file": path,
                            "line": line,
                        }
                        for scan, path, line in group[:10]
                    ],
                },
            )
        )
    return findings
