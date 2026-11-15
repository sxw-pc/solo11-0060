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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
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


def test_canonical_partition_labels_are_stable_across_runs(tmp_path, broken_config):
    """Re-auditing identical inputs yields the same run id and partition labels."""
    from lakeinlet.rules import run_audit

    first = run_audit(broken_config)
    second = run_audit(broken_config)
    assert first.run_id == second.run_id

    ledger = tmp_path / "ledger.sqlite"
    write_run(first, ledger)
    write_run(second, ledger)

    runs = read_runs(ledger)
    assert len(runs) == 1
    detail = read_run(ledger, first.run_id)
    assert detail is not None

    # The ledger stores canonical partition spellings only (EU, not eu/EU mix).
    batch_labels = {item["partition_label"] for item in detail["batches"]}
    assert "dt=2025-03-01,region=EU" in batch_labels
    assert "dt=2025-03-01,region=US" in batch_labels
    assert not any("region=eu" in label for label in batch_labels)

    watermark_labels = {
        item["partition_label"] for item in detail["watermarks"]
    }
    assert watermark_labels == batch_labels

    # The alias collision finding persists with its raw-value evidence.
    collision = next(
        item
        for item in detail["findings"]
        if item["code"] == "PARTITION_ALIAS_COLLISION"
    )
    assert collision["evidence"]["manifests"] == [
        "_manifests/orders/08_collision_eu.yaml",
        "_manifests/orders/09_collision_us.yaml",
    ]
