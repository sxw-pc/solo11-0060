from __future__ import annotations

import sqlite3

import pytest

from lakeinlet.errors import LedgerError
from lakeinlet.ledger import (
    initialize_ledger,
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
    assert runs[0]["raw_record_count"] == 17

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
        assert {"runs", "findings", "watermarks", "batches"}.issubset(tables)
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
    accounts = {
        item["record_id"]: item for item in detail["record_convergences"]
    }
    assert accounts["r-1001"]["raw_count"] == 3
    assert accounts["r-1001"]["retained"]["file"] == "data/orders/copy.csv"
    assert len(accounts["r-1001"]["superseded"]) == 2
    assert accounts["r-3002"]["superseded"][0]["event_time_parseable"] is False
    assert detail["raw_record_count"] > detail["record_count"]
    batches = [item for item in detail["batches"] if item["batch_id"] == "orders-early"]
    assert batches[0]["raw_record_count"] == 2
    assert batches[0]["superseded_record_count"] == 1

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

def test_empty_initialized_ledger_is_readable(tmp_path):
    ledger = initialize_ledger(tmp_path / "empty.sqlite")
    assert verify_readable(ledger) == ledger.resolve()
    assert read_runs(ledger) == []


def test_v1_ledger_is_migrated_in_place(tmp_path):
    ledger = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(ledger)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, project TEXT NOT NULL,
            status TEXT NOT NULL, as_of TEXT NOT NULL,
            recorded_at TEXT NOT NULL, config_digest TEXT NOT NULL,
            manifest_count INTEGER NOT NULL, batch_count INTEGER NOT NULL,
            file_count INTEGER NOT NULL, record_count INTEGER NOT NULL,
            finding_count INTEGER NOT NULL, error_count INTEGER NOT NULL,
            warning_count INTEGER NOT NULL, info_count INTEGER NOT NULL,
            report_json TEXT, report_md TEXT, report_html TEXT
        );
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            severity TEXT NOT NULL, code TEXT NOT NULL, source TEXT,
            partition_label TEXT, batch_id TEXT, message TEXT NOT NULL,
            evidence_json TEXT NOT NULL, UNIQUE(run_id, ordinal)
        );
        CREATE TABLE watermarks (
            run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            source TEXT NOT NULL, partition_label TEXT NOT NULL,
            max_event_time TEXT, safe_watermark TEXT,
            batch_count INTEGER NOT NULL, late_batch_count INTEGER NOT NULL,
            UNIQUE(run_id, ordinal)
        );
        CREATE TABLE batches (
            run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            manifest_path TEXT NOT NULL, source TEXT NOT NULL,
            batch_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            partition_label TEXT NOT NULL, declared_at TEXT NOT NULL,
            file_count INTEGER NOT NULL, record_count INTEGER NOT NULL,
            event_time_min TEXT, event_time_max TEXT,
            watermark_before TEXT, watermark_after TEXT,
            late INTEGER NOT NULL, UNIQUE(run_id, ordinal)
        );
        PRAGMA user_version = 1;
        """
    )
    connection.commit()
    connection.close()

    assert initialize_ledger(ledger) == ledger.resolve()
    connection = sqlite3.connect(ledger)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        run_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
        batch_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(batches)")
        }
        assert "raw_record_count" in run_columns
        assert {
            "raw_record_count",
            "superseded_record_count",
        }.issubset(batch_columns)
    finally:
        connection.close()
