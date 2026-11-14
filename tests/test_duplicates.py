from __future__ import annotations

from lakeinlet.manifest import load_source_manifests
from lakeinlet.scanner import scan_batch

def _broken_scans(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    return [scan_batch(item, source, broken_config.lake_root) for item in manifests]

def test_clean_fixture_has_no_duplicate_findings(demo_result):
    assert not any(
        finding.code.startswith("DUPLICATE") for finding in demo_result.findings
    )

def test_broken_fixture_detects_all_duplicate_classes(broken_result):
    codes = {finding.code for finding in broken_result.findings}
    assert {
        "DUPLICATE_BATCH_ID",
        "DUPLICATE_SEQUENCE",
        "DUPLICATE_FILE_DIGEST",
        "DUPLICATE_RECORD_ID",
    }.issubset(codes)

def test_duplicate_batch_evidence_lists_manifests(broken_result):
    finding = next(
        item for item in broken_result.findings if item.code == "DUPLICATE_BATCH_ID"
    )
    assert finding.batch_id == "orders-early"
    assert finding.evidence["count"] == 2
    assert len(finding.evidence["manifests"]) == 2

def test_duplicate_sequence_is_per_source(broken_result):
    finding = next(
        item for item in broken_result.findings if item.code == "DUPLICATE_SEQUENCE"
    )
    assert finding.evidence["sequence"] == 4
    assert sorted(finding.evidence["batch_ids"]) == [
        "orders-early",
        "orders-seqdup",
    ]

def test_duplicate_file_digest_identifies_both_paths(broken_result):
    finding = next(
        item
        for item in broken_result.findings
        if item.code == "DUPLICATE_FILE_DIGEST"
    )
    assert finding.evidence["count"] == 2
    paths = {item["file"] for item in finding.evidence["locations"]}
    assert paths == {"data/orders/copy.csv", "data/orders/duplicate.csv"}

def test_duplicate_record_identifiers_include_locations(broken_result):
    findings = [
        item
        for item in broken_result.findings
        if item.code == "DUPLICATE_RECORD_ID"
    ]
    by_id = {item.evidence["record_id"]: item for item in findings}
    assert "r-1001" in by_id
    assert by_id["r-1001"].evidence["count"] >= 2
    location = by_id["r-1001"].evidence["locations"][0]
    assert {"manifest_path", "batch_id", "file", "line"}.issubset(location)

def test_duplicate_detection_uses_actual_digest(broken_config):
    scans = _broken_scans(broken_config)
    copy_scan = next(scan for scan in scans if scan.batch_id == "orders-seqdup")
    duplicate_scan = next(
        scan for scan in scans if scan.batch_id == "orders-early" and scan.manifest.sequence == 4
    )
    assert copy_scan.files[0].actual_sha256 == duplicate_scan.files[0].actual_sha256
