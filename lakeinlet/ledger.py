"""SQLite audit ledger.

The ledger is the durable, queryable record behind the local viewer. A run id
is content-addressed, so re-auditing identical inputs updates one run instead
of creating duplicate history.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import LedgerError
from .models import partition_label
from .audit_result import AuditResult
from .timeutil import format_utc

from .ledger_schema import (
    initialize_ledger,
    readonly_connection,
    verify_readable,
)


def write_run(
    result: AuditResult,
    path: str | Path,
    artifacts: dict[str, str] | None = None,
) -> Path:
    """Insert or idempotently replace one content-addressed audit run."""
    ledger_path = initialize_ledger(path)
    artifacts = artifacts or {}
    try:
        connection = sqlite3.connect(ledger_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("DELETE FROM findings WHERE run_id = ?", (result.run_id,))
            connection.execute(
                "DELETE FROM watermarks WHERE run_id = ?", (result.run_id,)
            )
            connection.execute(
                "DELETE FROM record_convergences WHERE run_id = ?", (result.run_id,)
            )
            connection.execute("DELETE FROM batches WHERE run_id = ?", (result.run_id,))
            summary = result.summary_dict()
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project, status, as_of, recorded_at, config_digest,
                    manifest_count, batch_count, file_count, record_count,
                    raw_record_count,
                    finding_count, error_count, warning_count, info_count,
                    report_json, report_md, report_html
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    project=excluded.project, status=excluded.status,
                    as_of=excluded.as_of, recorded_at=excluded.recorded_at,
                    config_digest=excluded.config_digest,
                    manifest_count=excluded.manifest_count,
                    batch_count=excluded.batch_count,
                    file_count=excluded.file_count,
                    record_count=excluded.record_count,
                    raw_record_count=excluded.raw_record_count,
                    finding_count=excluded.finding_count,
                    error_count=excluded.error_count,
                    warning_count=excluded.warning_count,
                    info_count=excluded.info_count,
                    report_json=excluded.report_json,
                    report_md=excluded.report_md,
                    report_html=excluded.report_html
                """,
                (
                    result.run_id,
                    result.project,
                    result.status,
                    format_utc(result.as_of),
                    format_utc(result.as_of),
                    result.config_digest,
                    summary["manifest_count"],
                    summary["batch_count"],
                    summary["file_count"],
                    summary["record_count"],
                    summary["raw_record_count"],
                    summary["finding_count"],
                    summary["error_count"],
                    summary["warning_count"],
                    summary["info_count"],
                    artifacts.get("json"),
                    artifacts.get("md"),
                    artifacts.get("html"),
                ),
            )
            for ordinal, finding in enumerate(result.findings, start=1):
                connection.execute(
                    """
                    INSERT INTO findings (
                        run_id, ordinal, severity, code, source,
                        partition_label, batch_id, message, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.run_id,
                        ordinal,
                        finding.severity,
                        finding.code,
                        finding.source,
                        finding.partition_label or None,
                        finding.batch_id,
                        finding.message,
                        json.dumps(
                            finding.to_dict()["evidence"],
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ),
                )
            for ordinal, watermark in enumerate(result.watermarks, start=1):
                payload = watermark.to_dict()
                connection.execute(
                    """
                    INSERT INTO watermarks (
                        run_id, ordinal, source, partition_label,
                        max_event_time, safe_watermark, batch_count,
                        late_batch_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.run_id,
                        ordinal,
                        watermark.source,
                        partition_label(payload["partition"]),
                        payload["max_event_time"],
                        payload["safe_watermark"],
                        watermark.batch_count,
                        watermark.late_batch_count,
                    ),
                )
            for ordinal, account in enumerate(result.record_convergences, start=1):
                payload = account.to_dict()
                connection.execute(
                    """
                    INSERT INTO record_convergences (
                        run_id, ordinal, source, record_id, raw_count,
                        effective_count, basis, retained_json, superseded_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.run_id,
                        ordinal,
                        account.source,
                        account.record_id,
                        account.raw_count,
                        account.effective_count,
                        account.basis,
                        (
                            json.dumps(payload["retained"], sort_keys=True,
                                       ensure_ascii=False, separators=(",", ":"))
                            if payload["retained"] is not None
                            else None
                        ),
                        json.dumps(
                            payload["superseded"],
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ),
                )
            for ordinal, batch in enumerate(result.batches, start=1):
                payload = batch.to_dict()
                connection.execute(
                    """
                    INSERT INTO batches (
                        run_id, ordinal, manifest_path, source, batch_id,
                        sequence, partition_label, declared_at, file_count,
                        record_count, raw_record_count, superseded_record_count,
                        event_time_min, event_time_max,
                        watermark_before, watermark_after, late
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.run_id,
                        ordinal,
                        batch.manifest_relpath,
                        batch.source,
                        batch.batch_id,
                        batch.sequence,
                        partition_label(payload["partition"]),
                        payload["declared_at"],
                        batch.file_count,
                        batch.record_count,
                        batch.raw_record_count,
                        batch.superseded_record_count,
                        payload["event_time_min"],
                        payload["event_time_max"],
                        payload["watermark_before"],
                        payload["watermark_after"],
                        1 if batch.late else 0,
                    ),
                )
    except sqlite3.Error as exc:
        raise LedgerError(f"cannot write ledger: {exc}") from exc
    finally:
        connection.close()
    return ledger_path


def read_runs(path: str | Path) -> list[dict[str, Any]]:
    """Read run summaries for the viewer index."""
    ledger_path = Path(path).expanduser().resolve()
    connection = readonly_connection(ledger_path)
    try:
        rows = connection.execute(
            """
            SELECT run_id, project, status, as_of, recorded_at, manifest_count,
                   batch_count, file_count, record_count, raw_record_count,
                   finding_count, error_count, warning_count, info_count
            FROM runs ORDER BY as_of DESC, run_id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def read_run(path: str | Path, run_id: str) -> dict[str, Any] | None:
    """Read one run and all child evidence from the read-only ledger."""
    ledger_path = Path(path).expanduser().resolve()
    connection = readonly_connection(ledger_path)
    try:
        run_row = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            return None
        result = dict(run_row)
        result["findings"] = [
            _with_evidence(dict(row))
            for row in connection.execute(
                "SELECT * FROM findings WHERE run_id = ? ORDER BY ordinal", (run_id,)
            )
        ]
        result["watermarks"] = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM watermarks WHERE run_id = ? ORDER BY ordinal", (run_id,)
            )
        ]
        batches = []
        for row in connection.execute(
            "SELECT * FROM batches WHERE run_id = ? ORDER BY ordinal", (run_id,)
        ):
            item = dict(row)
            item["late"] = bool(item["late"])
            batches.append(item)
        result["batches"] = batches
        result["record_convergences"] = [
            _with_evidence(
                dict(row),
                json_field="superseded_json",
                target="superseded",
                extra_json_field="retained_json",
                extra_target="retained",
            )
            for row in connection.execute(
                "SELECT * FROM record_convergences WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            )
        ]
        return result
    except sqlite3.Error as exc:
        raise LedgerError(f"cannot read ledger run: {exc}") from exc
    finally:
        connection.close()


def _with_evidence(
    row: dict[str, Any],
    json_field: str = "evidence_json",
    target: str = "evidence",
    *,
    extra_json_field: str | None = None,
    extra_target: str | None = None,
) -> dict[str, Any]:
    try:
        row[target] = json.loads(row.pop(json_field))
    except (TypeError, json.JSONDecodeError):
        row[target] = {}
        row.pop(json_field, None)
    if extra_json_field is not None and extra_target is not None:
        raw = row.pop(extra_json_field, None)
        try:
            row[extra_target] = json.loads(raw) if raw is not None else None
        except json.JSONDecodeError:
            row[extra_target] = None
    return row
