from __future__ import annotations

import hashlib

from lakeinlet.manifest import load_source_manifests
from lakeinlet.scanner import scan_batch

def _batch(demo_config, source_name: str, batch_id: str):
    source = demo_config.source_map[source_name]
    manifests, findings = load_source_manifests(demo_config, source)
    assert findings == []
    manifest = next(item for item in manifests if item.batch_id == batch_id)
    return scan_batch(manifest, source, demo_config.lake_root)

def test_clean_batch_scan_digest_rows_and_events(demo_config):
    scan = _batch(demo_config, "orders", "orders-eu-0001")
    assert scan.issues == ()
    assert scan.record_count == 3
    assert len(scan.files) == 1
    file_scan = scan.files[0]
    assert file_scan.exists is True
    assert file_scan.actual_sha256 == file_scan.ref.sha256
    assert file_scan.actual_bytes == file_scan.ref.bytes
    assert file_scan.row_count == 3
    assert file_scan.header == (
        "record_id",
        "event_time",
        "dt",
        "region",
        "amount",
    )
    assert [record.record_id for record in file_scan.records] == [
        "ord-eu-0001",
        "ord-eu-0002",
        "ord-eu-0003",
    ]
    assert scan.event_time_min.isoformat() == "2025-03-01T00:05:00+00:00"
    assert scan.event_time_max.isoformat() == "2025-03-01T00:10:00+00:00"

def test_scan_does_not_modify_data(demo_config):
    source = demo_config.source_map["orders"]
    manifests, _ = load_source_manifests(demo_config, source)
    path = demo_config.lake_root / manifests[0].files[0].path
    before = (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    scan_batch(manifests[0], source, demo_config.lake_root)
    after = (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    assert after == before

def test_broken_batch_reports_file_level_failures(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    manifest = next(
        item for item in manifests if item.batch_id == "orders-broken-files"
    )
    scan = scan_batch(manifest, source, broken_config.lake_root)
    codes = {issue.code for issue in scan.issues}
    assert {
        "MISSING_DATA_FILE",
        "FILE_CHECKSUM_MISMATCH",
        "FILE_BYTES_MISMATCH",
        "FILE_ROW_COUNT_MISMATCH",
        "MANIFEST_EVENT_RANGE_MISMATCH",
        "CSV_INVALID_EVENT_TIME",
        "CSV_MISSING_RECORD_ID",
        "CSV_MISSING_COLUMNS",
        "CSV_MALFORMED_ROWS",
        "PARTITION_MISMATCH",
    }.issubset(codes)
    assert len(scan.files) == 4
    assert scan.files[0].exists is False
    assert scan.files[0].actual_sha256 is None
    header_file = next(
        item for item in scan.files if item.relative_path.endswith("header_broken.csv")
    )
    assert header_file.exists is True
    assert header_file.records == ()
    assert header_file.row_count == 1

def test_checksum_and_byte_evidence_is_recorded(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    manifest = next(
        item for item in manifests if item.batch_id == "orders-broken-files"
    )
    scan = scan_batch(manifest, source, broken_config.lake_root)
    tampered = next(
        item for item in scan.files if item.relative_path.endswith("tampered.csv")
    )
    assert tampered.actual_sha256 != tampered.ref.sha256
    assert tampered.actual_bytes != tampered.ref.bytes
    checksum = next(
        issue for issue in scan.issues if issue.code == "FILE_CHECKSUM_MISMATCH"
    )
    assert checksum.evidence["expected"] == "0" * 64
    assert checksum.evidence["actual"] == tampered.actual_sha256
    assert checksum.batch_id == "orders-broken-files"

def test_missing_file_is_not_parsed(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    manifest = next(
        item for item in manifests if item.batch_id == "orders-broken-files"
    )
    scan = scan_batch(manifest, source, broken_config.lake_root)
    missing = next(
        item for item in scan.files if item.relative_path.endswith("missing.csv")
    )
    assert missing.exists is False
    assert missing.row_count == 0
    assert missing.header == ()
