"""Physical file inspection for each manifest-declared data file."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .csvcheck import inspect_csv
from .errors import ManifestError
from .manifest import resolve_lake_path
from .models import (
    BatchManifest,
    BatchScan,
    FileRef,
    FileScan,
    Finding,
    SourceConfig,
)
from .timeutil import max_time, min_time

_CHUNK_SIZE = 64 * 1024


def sha256_file(path: Path) -> str:
    """Compute a SHA-256 digest without loading the file into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _finding(
    code: str,
    message: str,
    manifest: BatchManifest,
    source: SourceConfig,
    evidence: dict,
) -> Finding:
    return Finding(
        "error",
        code,
        message,
        source=source.name,
        partition=dict(manifest.partition),
        batch_id=manifest.batch_id,
        evidence={"manifest_path": manifest.manifest_relpath, **evidence},
    )


def _scan_file(
    ref: FileRef,
    manifest: BatchManifest,
    source: SourceConfig,
    lake_root: Path,
) -> FileScan:
    issues: list[Finding] = []
    empty = FileScan(
        ref=ref,
        relative_path=ref.path,
        exists=False,
        actual_sha256=None,
        actual_bytes=None,
        header=(),
        row_count=0,
        records=(),
        event_time_min=None,
        event_time_max=None,
    )
    try:
        path = resolve_lake_path(lake_root, ref.path, "manifest file path")
    except ManifestError as exc:
        empty.issues = (
            _finding(
                "INVALID_FILE_PATH",
                str(exc),
                manifest,
                source,
                {"path": ref.path},
            ),
        )
        return empty

    if not path.is_file():
        empty.issues = (
            _finding(
                "MISSING_DATA_FILE",
                f"declared data file does not exist: {ref.path}",
                manifest,
                source,
                {"path": ref.path},
            ),
        )
        return empty

    try:
        actual_bytes = path.stat().st_size
        actual_digest = sha256_file(path)
    except OSError as exc:
        empty.exists = True
        empty.issues = (
            _finding(
                "FILE_READ_ERROR",
                f"cannot read data file: {exc}",
                manifest,
                source,
                {"path": ref.path},
            ),
        )
        return empty

    if actual_digest != ref.sha256.lower():
        issues.append(
            _finding(
                "FILE_CHECKSUM_MISMATCH",
                "file SHA-256 does not match the manifest",
                manifest,
                source,
                {
                    "path": ref.path,
                    "expected": ref.sha256.lower(),
                    "actual": actual_digest,
                },
            )
        )
    if ref.bytes is not None and actual_bytes != ref.bytes:
        issues.append(
            _finding(
                "FILE_BYTES_MISMATCH",
                "file byte size does not match the manifest",
                manifest,
                source,
                {
                    "path": ref.path,
                    "expected": ref.bytes,
                    "actual": actual_bytes,
                },
            )
        )

    inspection = inspect_csv(path, ref.path, manifest, source)
    issues.extend(inspection.issues)
    if ref.rows is not None and inspection.row_count != ref.rows:
        issues.append(
            _finding(
                "FILE_ROW_COUNT_MISMATCH",
                "parsed data row count does not match the manifest",
                manifest,
                source,
                {
                    "path": ref.path,
                    "expected": ref.rows,
                    "actual": inspection.row_count,
                },
            )
        )
    for label, declared, actual in (
        ("event_time_min", ref.event_time_min, inspection.event_time_min),
        ("event_time_max", ref.event_time_max, inspection.event_time_max),
    ):
        if declared is not None and actual is not None and declared != actual:
            issues.append(
                _finding(
                    "MANIFEST_EVENT_RANGE_MISMATCH",
                    f"{label} does not match the parsed CSV data",
                    manifest,
                    source,
                    {
                        "path": ref.path,
                        "field": label,
                        "expected": declared.isoformat().replace("+00:00", "Z"),
                        "actual": actual.isoformat().replace("+00:00", "Z"),
                    },
                )
            )

    return FileScan(
        ref=ref,
        relative_path=ref.path,
        exists=True,
        actual_sha256=actual_digest,
        actual_bytes=actual_bytes,
        header=inspection.header,
        row_count=inspection.row_count,
        records=inspection.records,
        event_time_min=inspection.event_time_min,
        event_time_max=inspection.event_time_max,
        issues=tuple(issues),
    )


def scan_batch(
    manifest: BatchManifest, source: SourceConfig, lake_root: Path
) -> BatchScan:
    """Inspect all files declared by one manifest.

    The scan keeps every identified record on its ``FileScan`` so downstream
    duplicate convergence, watermark reconstruction, and summary counts share
    one convergence set. ``record_count`` here is the pre-convergence count of
    parseable records; ``raw_record_count`` counts all rows with a record id.
    """
    files = tuple(
        _scan_file(ref, manifest, source, lake_root) for ref in manifest.files
    )
    issues: list[Finding] = []
    for file_scan in files:
        issues.extend(file_scan.issues)
    records = [record for file_scan in files for record in file_scan.records]
    parseable = [record for record in records if record.event_time is not None]
    return BatchScan(
        manifest=manifest,
        files=files,
        issues=tuple(issues),
        record_count=len(parseable),
        event_time_min=min_time(*(item.event_time for item in parseable)),
        event_time_max=max_time(*(item.event_time for item in parseable)),
        raw_record_count=len(records),
    )
