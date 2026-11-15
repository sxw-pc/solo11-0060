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


def test_duplicate_record_evidence_answers_which_row_is_effective(broken_result):
    finding = next(
        item
        for item in broken_result.findings
        if item.code == "DUPLICATE_RECORD_ID"
        and item.evidence["record_id"] == "r-1001"
    )
    evidence = finding.evidence
    assert evidence["raw_count"] == 3
    assert evidence["effective_count"] == 1
    assert evidence["superseded_count"] == 2
    assert evidence["retained"]["file"] == "data/orders/copy.csv"
    assert evidence["retained"]["batch_id"] == "orders-seqdup"
    assert evidence["retained"]["event_time"] == "2025-03-01T01:05:00Z"
    assert evidence["basis"] == "declared_at"
    bases = {item["basis"] for item in evidence["superseded"]}
    assert bases == {"declared_at", "event_time"}
    for item in evidence["superseded"]:
        assert item["retained"]["file"] == evidence["retained"]["file"]
        assert item["detail"]
        assert {"manifest_path", "batch_id", "file", "line"}.issubset(item)


def test_unparseable_duplicate_is_superseded_never_retained(broken_result):
    finding = next(
        item
        for item in broken_result.findings
        if item.code == "DUPLICATE_RECORD_ID"
        and item.evidence["record_id"] == "r-3002"
    )
    evidence = finding.evidence
    assert evidence["raw_count"] == 2
    assert evidence["effective_count"] == 1
    assert evidence["retained"]["file"] == "data/orders/late.csv"
    assert evidence["retained"]["event_time"] == "2025-03-01T00:20:00Z"
    assert evidence["basis"] == "sole_parseable_event_time"
    superseded = evidence["superseded"][0]
    assert superseded["file"] == "data/orders/tampered.csv"
    assert superseded["event_time_parseable"] is False
    assert superseded["event_time"] is None
    assert superseded["raw_event_time"] == "not-a-time"
    assert superseded["basis"] == "unparseable_event_time"


def test_report_and_ledger_carry_full_convergence_account(broken_result, tmp_path):
    import json

    from lakeinlet.ledger import read_run, write_run
    from lakeinlet.report_json import render_json

    payload = json.loads(render_json(broken_result))
    accounts = {
        item["record_id"]: item for item in payload["record_convergences"]
    }
    assert set(accounts) == {"r-1001", "r-2002", "r-3001", "r-3002"}
    assert accounts["r-1001"]["raw_count"] == 3
    assert accounts["r-1001"]["effective_count"] == 1
    assert len(accounts["r-1001"]["superseded"]) == 2

    ledger = tmp_path / "ledger.sqlite"
    write_run(broken_result, ledger)
    detail = read_run(ledger, broken_result.run_id)
    assert detail["record_count"] == broken_result.record_count
    assert detail["raw_record_count"] == broken_result.raw_record_count
    assert detail["raw_record_count"] > detail["record_count"]
    finding = next(
        item
        for item in detail["findings"]
        if item["code"] == "DUPLICATE_RECORD_ID"
        and item["evidence"]["record_id"] == "r-1001"
    )
    assert finding["evidence"]["retained"]["file"] == "data/orders/copy.csv"
    assert len(finding["evidence"]["superseded"]) == 2


def test_duplicate_findings_use_effective_records_for_watermarks(broken_result):
    by_batch = {
        (batch.batch_id, batch.manifest_relpath): batch
        for batch in broken_result.batches
    }
    duplicate_batch = next(
        batch
        for batch in broken_result.batches
        if batch.batch_id == "orders-early"
        and batch.manifest_relpath.endswith("03_duplicate_batch.yaml")
    )
    assert duplicate_batch.record_count == 0
    assert duplicate_batch.raw_record_count == 2
    assert duplicate_batch.superseded_record_count == 2
    assert duplicate_batch.event_time_min is None
    first_batch = next(
        batch
        for batch in broken_result.batches
        if batch.batch_id == "orders-early"
        and batch.manifest_relpath.endswith("01_early.yaml")
    )
    assert first_batch.record_count == 1
    assert first_batch.raw_record_count == 2
    assert first_batch.superseded_record_count == 1
    assert first_batch.event_time_max.isoformat() == "2025-03-01T01:00:00+00:00"
    watermark = next(
        item
        for item in broken_result.watermarks
        if item.source == "orders" and item.partition.get("region") == "eu"
    )
    assert watermark.max_event_time.isoformat() == "2025-03-01T01:10:00+00:00"
    assert by_batch  # keep the lookup exercised

def test_duplicate_detection_uses_actual_digest(broken_config):
    scans = _broken_scans(broken_config)
    copy_scan = next(scan for scan in scans if scan.batch_id == "orders-seqdup")
    duplicate_scan = next(
        scan for scan in scans if scan.batch_id == "orders-early" and scan.manifest.sequence == 4
    )
    assert copy_scan.files[0].actual_sha256 == duplicate_scan.files[0].actual_sha256
