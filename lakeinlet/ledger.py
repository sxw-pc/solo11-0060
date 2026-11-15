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
            connection.execute("DELETE FROM batches WHERE run_id = ?", (result.run_id,))
            connection.execute(
                "DELETE FROM completion_plans WHERE run_id = ?", (result.run_id,)
            )
            summary = result.summary_dict()
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project, status, as_of, recorded_at, config_digest,
                    manifest_count, batch_count, file_count, record_count,
                    finding_count, error_count, warning_count, info_count,
                    report_json, report_md, report_html
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    project=excluded.project, status=excluded.status,
                    as_of=excluded.as_of, recorded_at=excluded.recorded_at,
                    config_digest=excluded.config_digest,
                    manifest_count=excluded.manifest_count,
                    batch_count=excluded.batch_count,
                    file_count=excluded.file_count,
                    record_count=excluded.record_count,
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
            for ordinal, batch in enumerate(result.batches, start=1):
                payload = batch.to_dict()
                connection.execute(
                    """
                    INSERT INTO batches (
                        run_id, ordinal, manifest_path, source, batch_id,
                        sequence, partition_label, declared_at, file_count,
                        record_count, event_time_min, event_time_max,
                        watermark_before, watermark_after, late
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        payload["event_time_min"],
                        payload["event_time_max"],
                        payload["watermark_before"],
                        payload["watermark_after"],
                        1 if batch.late else 0,
                    ),
                )
            for ordinal, plan in enumerate(result.completion_plans, start=1):
                payload = plan.to_dict()
                gap_range = (
                    f"{plan.gap_range[0]}:{plan.gap_range[1]}"
                    if plan.gap_range is not None
                    else None
                )
                connection.execute(
                    """
                    INSERT INTO completion_plans (
                        run_id, ordinal, plan_id, source, gap_range,
                        partition_label, needs_manual_decision,
                        uncertainty_reason, candidate_count,
                        chain_request_count, plan_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.run_id,
                        ordinal,
                        plan.plan_id,
                        plan.source,
                        gap_range,
                        plan.partition_label or None,
                        1 if plan.needs_manual_decision else 0,
                        plan.uncertainty_reason,
                        len(plan.candidates),
                        len(plan.chain_requests),
                        json.dumps(
                            payload,
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
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
                   batch_count, file_count, record_count, finding_count,
                   error_count, warning_count, info_count
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
        result["completion_plans"] = [
            _with_plan_payload(dict(row))
            for row in connection.execute(
                "SELECT * FROM completion_plans WHERE run_id = ? ORDER BY ordinal",
                (run_id,),
            )
        ]
        return result
    except sqlite3.Error as exc:
        raise LedgerError(f"cannot read ledger run: {exc}") from exc
    finally:
        connection.close()


def _with_evidence(row: dict[str, Any]) -> dict[str, Any]:
    try:
        row["evidence"] = json.loads(row.pop("evidence_json"))
    except (TypeError, json.JSONDecodeError):
        row["evidence"] = {}
        row.pop("evidence_json", None)
    return row


def _with_plan_payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        row["plan"] = json.loads(row.pop("plan_json"))
    except (TypeError, json.JSONDecodeError):
        row["plan"] = {}
        row.pop("plan_json", None)
    row["needs_manual_decision"] = bool(row["needs_manual_decision"])
    return row


def read_completion_plan_ids(path: str | Path, source: str) -> list[dict[str, Any]]:
    """Return recorded plan ids for one source across all audit runs.

    Plan ids are content-addressed from (source, gap interval, partition), so
    the same backfill gap refreshes the same row on every re-audit instead of
    appending duplicates; callers can use this history to tell whether a gap
    seen in the current run was already planned (and possibly repaired).
    """
    ledger_path = Path(path).expanduser().resolve()
    connection = readonly_connection(ledger_path)
    try:
        rows = connection.execute(
            """
            SELECT cp.plan_id, cp.source, cp.gap_range, cp.partition_label,
                   cp.needs_manual_decision, cp.uncertainty_reason,
                   cp.candidate_count, cp.chain_request_count,
                   r.run_id, r.as_of
            FROM completion_plans cp
            JOIN runs r ON r.run_id = cp.run_id
            WHERE cp.source = ?
            ORDER BY cp.plan_id, r.as_of DESC
            """,
            (source,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error as exc:
        raise LedgerError(f"cannot read completion plans: {exc}") from exc
    finally:
        connection.close()
