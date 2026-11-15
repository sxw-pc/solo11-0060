"""Idempotency checks across batches, files, and records.

Record-id convergence itself lives in :mod:`lakeinlet.convergence`; this
module turns its accounts into findings and checks the other idempotency
classes.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Sequence

from .convergence import ConvergenceResult, RecordConvergence, converge_records
from .models import BatchScan, Finding


def _location(scan: BatchScan, file_path: str) -> dict[str, str]:
    return {
        "manifest_path": scan.manifest.manifest_relpath,
        "batch_id": scan.batch_id,
        "file": file_path,
    }


def find_duplicate_findings(
    batches: Sequence[BatchScan],
    convergence: ConvergenceResult | None = None,
) -> list[Finding]:
    """Return findings for repeated batch ids, sequences, digests, and records."""
    findings: list[Finding] = []
    convergence = convergence or converge_records(batches)

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

    for account in convergence.duplicate_accounts:
        findings.append(_record_finding(account))
    return findings


def _record_finding(account: RecordConvergence) -> Finding:
    effective = account.effective_count
    message = (
        f"record_id {account.record_id!r} appears {account.raw_count} times in "
        f"source {account.source!r}; {effective} retained"
    )
    return Finding(
        "error",
        "DUPLICATE_RECORD_ID",
        message,
        source=account.source,
        evidence={
            "record_id": account.record_id,
            "count": account.raw_count,
            "raw_count": account.raw_count,
            "effective_count": effective,
            "retained": account.retained.to_dict() if account.retained else None,
            "basis": account.basis,
            "superseded_count": len(account.superseded),
            "superseded": [item.to_dict() for item in account.superseded],
            "locations": [
                {
                    "manifest_path": item.location.manifest_path,
                    "batch_id": item.location.batch_id,
                    "file": item.location.file,
                    "line": item.location.line,
                }
                for item in account.superseded
            ],
        },
    )
