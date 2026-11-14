from __future__ import annotations

from lakeinlet.csvcheck import inspect_csv
from lakeinlet.manifest import load_source_manifests

def _context(demo_config):
    source = demo_config.source_map["orders"]
    manifests, _ = load_source_manifests(demo_config, source)
    return manifests[0], source

def _inspect(tmp_path, demo_config, text: str):
    manifest, source = _context(demo_config)
    path = tmp_path / "sample.csv"
    path.write_bytes(text.encode("utf-8"))
    return inspect_csv(path, "data/orders/sample.csv", manifest, source)

def test_valid_csv_is_fully_parsed(tmp_path, demo_config):
    result = _inspect(
        tmp_path,
        demo_config,
        "record_id,event_time,dt,region,amount\n"
        "r1,2025-03-01T00:01:00Z,2025-03-01,eu,10\n"
        "r2,2025-03-01T00:02:00Z,2025-03-01,eu,20\n",
    )
    assert result.issues == ()
    assert result.row_count == 2
    assert result.header[:2] == ("record_id", "event_time")
    assert [record.record_id for record in result.records] == ["r1", "r2"]
    assert result.event_time_min.isoformat() == "2025-03-01T00:01:00+00:00"
    assert result.event_time_max.isoformat() == "2025-03-01T00:02:00+00:00"

def test_missing_required_columns_are_reported(tmp_path, demo_config):
    result = _inspect(
        tmp_path,
        demo_config,
        "id,ts,dt,region,amount\nr1,2025-03-01T00:01:00Z,2025-03-01,eu,10\n",
    )
    assert result.row_count == 1
    assert result.records == ()
    issue = result.issues[0]
    assert issue.code == "CSV_MISSING_COLUMNS"
    assert "event_time" in issue.evidence["missing"]
    assert "record_id" in issue.evidence["missing"]

def test_malformed_row_is_counted_but_not_parsed(tmp_path, demo_config):
    result = _inspect(
        tmp_path,
        demo_config,
        "record_id,event_time,dt,region,amount\n"
        "r1,2025-03-01T00:01:00Z,2025-03-01,eu,10\n"
        "r2,2025-03-01T00:02:00Z,2025-03-01,eu,20,extra\n",
    )
    assert result.row_count == 2
    assert [record.record_id for record in result.records] == ["r1"]
    issue = next(item for item in result.issues if item.code == "CSV_MALFORMED_ROWS")
    assert issue.evidence["count"] == 1
    assert issue.evidence["samples"][0]["line"] == 3

def test_invalid_time_missing_id_and_partition_mismatch(tmp_path, demo_config):
    result = _inspect(
        tmp_path,
        demo_config,
        "record_id,event_time,dt,region,amount\n"
        "r1,not-a-time,2025-03-01,eu,10\n"
        ",2025-03-01T00:02:00Z,2025-03-01,us,20\n"
        "r3,2025-03-01T00:03:00Z,2025-03-01,eu,30\n",
    )
    codes = {issue.code for issue in result.issues}
    assert codes == {
        "CSV_INVALID_EVENT_TIME",
        "CSV_MISSING_RECORD_ID",
        "PARTITION_MISMATCH",
    }
    assert result.row_count == 3
    assert [record.record_id for record in result.records] == ["r3"]
    mismatch = next(
        issue for issue in result.issues if issue.code == "PARTITION_MISMATCH"
    )
    assert mismatch.evidence["samples"][0]["values"]["region"] == {
        "expected": "eu",
        "actual": "us",
    }

def test_empty_file_is_reported(tmp_path, demo_config):
    result = _inspect(tmp_path, demo_config, "")
    assert result.row_count == 0
    assert result.header == ()
    assert result.issues[0].code == "CSV_EMPTY"

def test_duplicate_header_is_reported(tmp_path, demo_config):
    result = _inspect(
        tmp_path,
        demo_config,
        "record_id,event_time,dt,region,record_id\n"
        "r1,2025-03-01T00:01:00Z,2025-03-01,eu,r1\n",
    )
    assert any(issue.code == "CSV_DUPLICATE_HEADER" for issue in result.issues)

def test_unreadable_path_is_reported(tmp_path, demo_config):
    manifest, source = _context(demo_config)
    result = inspect_csv(
        tmp_path / "missing.csv",
        "data/orders/missing.csv",
        manifest,
        source,
    )
    assert result.issues[0].code == "CSV_READ_ERROR"
    assert result.records == ()

def test_bom_and_crlf_are_supported(tmp_path, demo_config):
    path = tmp_path / "bom.csv"
    path.write_bytes(
        b"\xef\xbb\xbfrecord_id,event_time,dt,region,amount\r\n"
        b"r1,2025-03-01T00:01:00Z,2025-03-01,eu,10\r\n"
    )
    manifest, source = _context(demo_config)
    result = inspect_csv(path, "data/orders/bom.csv", manifest, source)
    assert result.row_count == 1
    assert result.records[0].record_id == "r1"
    assert result.issues == ()
