"""CSV structure, field, and row-level validation."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import BatchManifest, Finding, Record, SourceConfig
from .timeutil import parse_utc

_MAX_SAMPLES = 3


@dataclass(slots=True)
class CsvInspection:
    header: tuple[str, ...]
    row_count: int
    records: tuple[Record, ...]
    event_time_min: datetime | None
    event_time_max: datetime | None
    issues: tuple[Finding, ...]


def _issue(
    code: str,
    message: str,
    source: SourceConfig,
    manifest: BatchManifest,
    evidence: dict[str, Any],
) -> Finding:
    return Finding(
        "error",
        code,
        message,
        source=source.name,
        partition=dict(manifest.partition),
        batch_id=manifest.batch_id,
        evidence={
            "manifest_path": manifest.manifest_relpath,
            **evidence,
        },
    )


def _sample(line: int, reason: str, **extra: Any) -> dict[str, Any]:
    return {"line": line, "reason": reason, **extra}


def _sample_evidence(
    samples: list[dict[str, Any]], count: int, **extra: Any
) -> dict[str, Any]:
    return {
        "count": count,
        "samples": samples[:_MAX_SAMPLES],
        **extra,
    }


def inspect_csv(
    path: Path,
    relative_path: str,
    manifest: BatchManifest,
    source: SourceConfig,
) -> CsvInspection:
    """Validate one CSV file and collect its parseable record identities."""
    required = [source.event_time_field, source.record_id_field]
    required.extend(key for key in source.partition_keys if key not in required)
    issues: list[Finding] = []
    records: list[Record] = []
    row_count = 0

    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except (OSError, UnicodeError) as exc:
        return CsvInspection(
            (),
            0,
            (),
            None,
            None,
            (
                _issue(
                    "CSV_READ_ERROR",
                    f"cannot open CSV file: {exc}",
                    source,
                    manifest,
                    {"path": relative_path},
                ),
            ),
        )

    with handle:
        try:
            reader = csv.DictReader(handle)
            raw_header = reader.fieldnames
        except (csv.Error, UnicodeError) as exc:
            return CsvInspection(
                (),
                0,
                (),
                None,
                None,
                (
                    _issue(
                        "CSV_READ_ERROR",
                        f"cannot parse CSV header: {exc}",
                        source,
                        manifest,
                        {"path": relative_path},
                    ),
                ),
            )
        if not raw_header:
            return CsvInspection(
                (),
                0,
                (),
                None,
                None,
                (
                    _issue(
                        "CSV_EMPTY",
                        "CSV file has no header row",
                        source,
                        manifest,
                        {"path": relative_path},
                    ),
                ),
            )
        header = tuple(str(name or "").strip() for name in raw_header)
        missing = [name for name in required if name not in header]
        if len(set(header)) != len(header):
            issues.append(
                _issue(
                    "CSV_DUPLICATE_HEADER",
                    "CSV header contains duplicate column names",
                    source,
                    manifest,
                    {"path": relative_path, "header": list(header)},
                )
            )
        if missing:
            # Still count physical rows so the manifest row-count check remains
            # meaningful, but field-level checks cannot run safely.
            try:
                for _ in reader:
                    row_count += 1
            except csv.Error:
                pass
            issues.append(
                _issue(
                    "CSV_MISSING_COLUMNS",
                    f"CSV is missing required columns: {', '.join(missing)}",
                    source,
                    manifest,
                    {
                        "path": relative_path,
                        "missing": missing,
                        "header": list(header),
                        "row_count": row_count,
                    },
                )
            )
            return CsvInspection(header, row_count, (), None, None, tuple(issues))

        malformed: list[dict[str, Any]] = []
        invalid_times: list[dict[str, Any]] = []
        missing_ids: list[dict[str, Any]] = []
        partition_mismatches: list[dict[str, Any]] = []
        malformed_count = invalid_time_count = missing_id_count = mismatch_count = 0

        try:
            for row in reader:
                row_count += 1
                line = reader.line_num
                row = {
                    key.strip() if isinstance(key, str) else key: value
                    for key, value in row.items()
                }
                if None in row or any(value is None for value in row.values()):
                    malformed_count += 1
                    malformed.append(_sample(line, "wrong column count"))
                    continue

                record_id = (row.get(source.record_id_field) or "").strip()
                if not record_id:
                    missing_id_count += 1
                    missing_ids.append(_sample(line, "record id is empty"))
                raw_event = (row.get(source.event_time_field) or "").strip()
                try:
                    event_time = parse_utc(raw_event, source.event_time_field)
                except ValueError:
                    invalid_time_count += 1
                    invalid_times.append(
                        _sample(line, "invalid event time", value=raw_event[:80])
                    )
                    event_time = None

                mismatched = {
                    key: {
                        "expected": manifest.partition.get(key),
                        "actual": (row.get(key) or "").strip(),
                    }
                    for key in source.partition_keys
                    if (row.get(key) or "").strip() != manifest.partition.get(key)
                }
                if mismatched:
                    mismatch_count += 1
                    partition_mismatches.append(
                        _sample(line, "partition values differ", values=mismatched)
                    )

                if record_id and event_time is not None:
                    records.append(
                        Record(
                            record_id=record_id,
                            event_time=event_time,
                            file_path=relative_path,
                            line_number=line,
                        )
                    )
        except csv.Error as exc:
            issues.append(
                _issue(
                    "CSV_READ_ERROR",
                    f"CSV parsing stopped at line {reader.line_num}: {exc}",
                    source,
                    manifest,
                    {"path": relative_path, "rows_before_error": row_count},
                )
            )

        if malformed_count:
            issues.append(
                _issue(
                    "CSV_MALFORMED_ROWS",
                    f"{malformed_count} CSV row(s) have the wrong column count",
                    source,
                    manifest,
                    {
                        "path": relative_path,
                        **_sample_evidence(malformed, malformed_count),
                    },
                )
            )
        if invalid_time_count:
            issues.append(
                _issue(
                    "CSV_INVALID_EVENT_TIME",
                    f"{invalid_time_count} CSV row(s) have an invalid UTC event time",
                    source,
                    manifest,
                    {
                        "path": relative_path,
                        **_sample_evidence(invalid_times, invalid_time_count),
                    },
                )
            )
        if missing_id_count:
            issues.append(
                _issue(
                    "CSV_MISSING_RECORD_ID",
                    f"{missing_id_count} CSV row(s) have an empty record identifier",
                    source,
                    manifest,
                    {
                        "path": relative_path,
                        **_sample_evidence(missing_ids, missing_id_count),
                    },
                )
            )
        if mismatch_count:
            issues.append(
                _issue(
                    "PARTITION_MISMATCH",
                    f"{mismatch_count} CSV row(s) do not match the manifest partition",
                    source,
                    manifest,
                    {
                        "path": relative_path,
                        **_sample_evidence(
                            partition_mismatches, mismatch_count
                        ),
                    },
                )
            )

    event_times = [item.event_time for item in records]
    return CsvInspection(
        header=header,
        row_count=row_count,
        records=tuple(records),
        event_time_min=min(event_times) if event_times else None,
        event_time_max=max(event_times) if event_times else None,
        issues=tuple(issues),
    )
