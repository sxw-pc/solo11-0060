from __future__ import annotations

import sqlite3

import pytest

from lakeinlet.errors import LedgerError
from lakeinlet.ledger import (
    initialize_ledger,
    read_completion_plan_ids,
    read_run,
    read_runs,
    verify_readable,
    write_run,
)

def test_write_run_creates_queryable_ledger(tmp_path, demo_result):
    ledger = tmp_path / "nested" / "ledger.sqlite"
    write_run(
        demo_result,
        ledger,
        {"json": "report.json", "md": "report.md", "html": "report.html"},
    )
    assert ledger.is_file()

    runs = read_runs(ledger)
    assert len(runs) == 1
    assert runs[0]["run_id"] == demo_result.run_id
    assert runs[0]["project"] == "lakeinlet-demo"
    assert runs[0]["status"] == "passed"
    assert runs[0]["batch_count"] == 6
    assert runs[0]["record_count"] == 17

    detail = read_run(ledger, demo_result.run_id)
    assert detail is not None
    assert len(detail["batches"]) == 6
    assert len(detail["watermarks"]) == 3
    assert detail["findings"] == []
    assert detail["report_json"] == "report.json"

def test_ledger_schema_version_and_tables(tmp_path, demo_result):
    ledger = initialize_ledger(tmp_path / "ledger.sqlite")
    connection = sqlite3.connect(ledger)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "runs",
            "findings",
            "watermarks",
            "batches",
            "completion_plans",
        }.issubset(tables)
    finally:
        connection.close()
    write_run(demo_result, ledger)
    assert len(read_runs(ledger)) == 1

def test_rewrite_is_idempotent_for_same_run(tmp_path, broken_result):
    ledger = tmp_path / "ledger.sqlite"
    write_run(broken_result, ledger)
    first_runs = read_runs(ledger)
    first_detail = read_run(ledger, broken_result.run_id)
    write_run(broken_result, ledger)
    second_runs = read_runs(ledger)
    second_detail = read_run(ledger, broken_result.run_id)

    assert len(first_runs) == len(second_runs) == 1
    assert len(second_detail["findings"]) == broken_result.finding_count
    # Row ids may differ after replacement; the evidence payload must not.
    first_evidence = [
        {key: value for key, value in item.items() if key != "id"}
        for item in first_detail["findings"]
    ]
    second_evidence = [
        {key: value for key, value in item.items() if key != "id"}
        for item in second_detail["findings"]
    ]
    assert first_evidence == second_evidence
    connection = sqlite3.connect(ledger)
    try:
        count = connection.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
        assert count == broken_result.finding_count
    finally:
        connection.close()

def test_broken_run_persists_findings_and_late_batches(tmp_path, broken_result):
    ledger = tmp_path / "ledger.sqlite"
    write_run(broken_result, ledger)
    detail = read_run(ledger, broken_result.run_id)
    assert detail["status"] == "failed"
    codes = {item["code"] for item in detail["findings"]}
    assert "LATE_BATCH" in codes
    assert "DUPLICATE_RECORD_ID" in codes
    late_batches = [item for item in detail["batches"] if item["late"]]
    assert [item["batch_id"] for item in late_batches] == ["orders-late"]
    evidence = next(
        item for item in detail["findings"] if item["code"] == "LATE_BATCH"
    )["evidence"]
    assert evidence["allowed_lateness_seconds"] == 600

def test_unknown_run_returns_none(tmp_path, demo_result):
    ledger = tmp_path / "ledger.sqlite"
    write_run(demo_result, ledger)
    assert read_run(ledger, "does-not-exist") is None

def test_missing_ledger_is_rejected(tmp_path):
    with pytest.raises(LedgerError, match="not found"):
        read_runs(tmp_path / "missing.sqlite")

def test_incompatible_schema_is_rejected(tmp_path):
    ledger = tmp_path / "bad.sqlite"
    connection = sqlite3.connect(ledger)
    connection.execute("CREATE TABLE unrelated (id INTEGER)")
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(LedgerError, match="incompatible schema"):
        verify_readable(ledger)


def test_v1_ledger_is_upgraded_without_losing_runs(tmp_path, broken_result):
    """Ledgers written before completion plans migrate in place on next write."""
    ledger = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(ledger)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, project TEXT NOT NULL, status TEXT NOT NULL,
            as_of TEXT NOT NULL, recorded_at TEXT NOT NULL, config_digest TEXT NOT NULL,
            manifest_count INTEGER NOT NULL, batch_count INTEGER NOT NULL,
            file_count INTEGER NOT NULL, record_count INTEGER NOT NULL,
            finding_count INTEGER NOT NULL, error_count INTEGER NOT NULL,
            warning_count INTEGER NOT NULL, info_count INTEGER NOT NULL,
            report_json TEXT, report_md TEXT, report_html TEXT
        );
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL, severity TEXT NOT NULL, code TEXT NOT NULL,
            source TEXT, partition_label TEXT, batch_id TEXT,
            message TEXT NOT NULL, evidence_json TEXT NOT NULL
        );
        CREATE TABLE watermarks (
            run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, source TEXT NOT NULL,
            partition_label TEXT NOT NULL, max_event_time TEXT,
            safe_watermark TEXT, batch_count INTEGER NOT NULL,
            late_batch_count INTEGER NOT NULL
        );
        CREATE TABLE batches (
            run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            manifest_path TEXT NOT NULL, source TEXT NOT NULL, batch_id TEXT NOT NULL,
            sequence INTEGER NOT NULL, partition_label TEXT NOT NULL,
            declared_at TEXT NOT NULL, file_count INTEGER NOT NULL,
            record_count INTEGER NOT NULL, event_time_min TEXT,
            event_time_max TEXT, watermark_before TEXT, watermark_after TEXT,
            late INTEGER NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO runs VALUES ("
        "'legacy','p','passed','2025-01-01T00:00:00Z','2025-01-01T00:00:00Z',"
        "'d',0,0,0,0,0,0,0,0,NULL,NULL,NULL)"
    )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()

    with pytest.raises(LedgerError, match="incompatible schema"):
        verify_readable(ledger)

    write_run(broken_result, ledger)
    assert verify_readable(ledger) == ledger.resolve()
    runs = read_runs(ledger)
    assert {row["run_id"] for row in runs} == {"legacy", broken_result.run_id}
    detail = read_run(ledger, broken_result.run_id)
    assert len(detail["completion_plans"]) == len(broken_result.completion_plans)

def test_empty_initialized_ledger_is_readable(tmp_path):
    ledger = initialize_ledger(tmp_path / "empty.sqlite")
    assert verify_readable(ledger) == ledger.resolve()
    assert read_runs(ledger) == []


def test_completion_plans_are_persisted_with_stable_ids(tmp_path, broken_result):
    ledger = tmp_path / "ledger.sqlite"
    write_run(broken_result, ledger)

    detail = read_run(ledger, broken_result.run_id)
    assert detail is not None
    assert len(detail["completion_plans"]) == len(broken_result.completion_plans)
    stored = detail["completion_plans"][0]
    expected = broken_result.completion_plans[0]
    assert stored["plan_id"] == expected.plan_id
    assert stored["source"] == "orders"
    assert stored["gap_range"] == "2:2"
    assert stored["candidate_count"] == 1
    assert stored["chain_request_count"] == 1
    assert stored["plan"]["plan_id"] == expected.plan_id
    assert stored["plan"]["candidates"][0]["sequence"] == 2

    # Re-writing the same run refreshes the same plan row instead of appending.
    write_run(broken_result, ledger)
    connection = sqlite3.connect(ledger)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM completion_plans"
        ).fetchone()[0]
        distinct = connection.execute(
            "SELECT COUNT(DISTINCT plan_id) FROM completion_plans"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == distinct == len(broken_result.completion_plans)


def test_completion_plan_history_is_queryable_by_source(
    tmp_path, demo_result, broken_result
):
    ledger = tmp_path / "ledger.sqlite"
    write_run(demo_result, ledger)
    write_run(broken_result, ledger)

    rows = read_completion_plan_ids(ledger, "orders")
    # The clean demo run has no plans; only the broken run contributes.
    assert len(rows) == len(broken_result.completion_plans)
    assert rows[0]["plan_id"] == broken_result.completion_plans[0].plan_id
    assert rows[0]["source"] == "orders"
    assert rows[0]["run_id"] == broken_result.run_id
    assert read_completion_plan_ids(ledger, "never-audited-source") == []


def test_clean_run_writes_no_completion_plans(tmp_path, demo_result):
    ledger = tmp_path / "ledger.sqlite"
    write_run(demo_result, ledger)
    detail = read_run(ledger, demo_result.run_id)
    assert detail is not None
    assert detail["completion_plans"] == []
    connection = sqlite3.connect(ledger)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM completion_plans"
        ).fetchone()[0] == 0
    finally:
        connection.close()
