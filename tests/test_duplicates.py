from __future__ import annotations

from dataclasses import replace

from lakeinlet.duplicates import find_duplicate_findings
from lakeinlet.manifest import load_source_manifests
from lakeinlet.models import SourceConfig
from lakeinlet.partition import PartitionScheme, find_partition_alias_collisions
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
    findings = [
        item for item in broken_result.findings if item.code == "DUPLICATE_BATCH_ID"
    ]
    by_id = {item.batch_id: item for item in findings}
    finding = by_id["orders-early"]
    assert finding.evidence["count"] == 2
    assert len(finding.evidence["manifests"]) == 2
    # The cross-partition reused batch id is still flagged as a plain duplicate.
    assert "orders-cross-region" in by_id
    assert by_id["orders-cross-region"].evidence["count"] == 2

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


def test_case_variant_records_are_recognized_as_duplicates(broken_result):
    """r-1001/r-2002 in the EU-spelling batch duplicate the eu-spelling batches."""
    findings = [
        item
        for item in broken_result.findings
        if item.code == "DUPLICATE_RECORD_ID"
    ]
    by_id = {item.evidence["record_id"]: item for item in findings}
    r1001 = by_id["r-1001"]
    # The upper-region manifest is among the duplicate locations.
    manifests = {
        location["manifest_path"]
        for location in r1001.evidence["locations"]
    }
    assert "_manifests/orders/07_upper_region.yaml" in manifests
    # Canonical partition is reported; both raw spellings stay in evidence.
    assert r1001.partition == {"dt": "2025-03-01", "region": "EU"}
    assert r1001.evidence["canonical_partition"] == {
        "dt": "2025-03-01",
        "region": "EU",
    }
    assert r1001.evidence["raw_partition_values"]["region"] == ["EU", "eu"]


def test_same_record_id_in_different_canonical_partitions_is_not_duplicate(
    broken_config,
):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    scans = [
        scan_batch(item, source, broken_config.lake_root) for item in manifests
    ]
    # Build a synthetic same-id/different-canonical-partition pair by
    # relabeling the US record to reuse the EU record's id.
    eu_scan = next(
        scan
        for scan in scans
        if scan.batch_id == "orders-cross-region"
        and scan.manifest.partition["region"] == "EU"
    )
    us_scan = next(
        scan
        for scan in scans
        if scan.batch_id == "orders-cross-region"
        and scan.manifest.partition["region"] == "US"
    )
    shared_id = eu_scan.files[0].records[0].record_id
    record = us_scan.files[0].records[0]
    us_scan.files[0].records = (replace(record, record_id=shared_id),)
    findings = find_duplicate_findings(
        [eu_scan, us_scan], broken_config.source_map
    )
    assert not any(
        item.code == "DUPLICATE_RECORD_ID"
        and item.evidence["record_id"] == shared_id
        for item in findings
    )


def test_partition_alias_collision_flags_batch_id_across_partitions(
    broken_result,
):
    collisions = [
        item
        for item in broken_result.findings
        if item.code == "PARTITION_ALIAS_COLLISION"
    ]
    assert len(collisions) == 1
    finding = collisions[0]
    assert finding.severity == "error"
    assert finding.batch_id == "orders-cross-region"
    assert finding.evidence["reason"] == "batch_id_spans_multiple_partitions"
    canonical = {
        tuple(sorted(group["canonical_partition"].items()))
        for group in finding.evidence["raw_value_mapping"]
    }
    assert canonical == {
        (("dt", "2025-03-01"), ("region", "EU")),
        (("dt", "2025-03-01"), ("region", "US")),
    }
    assert finding.evidence["manifests"] == [
        "_manifests/orders/08_collision_eu.yaml",
        "_manifests/orders/09_collision_us.yaml",
    ]


def test_plain_alias_merging_does_not_create_collision_finding(broken_result):
    """The eu/EU variant batch is ordinary merging — no collision finding."""
    collisions = [
        item
        for item in broken_result.findings
        if item.code == "PARTITION_ALIAS_COLLISION"
    ]
    assert all(
        item.batch_id != "orders-upper-region" for item in collisions
    )


def _scheme() -> PartitionScheme:
    base = SourceConfig(
        name="s",
        partition_keys=("region",),
        allowed_lateness_seconds=0,
        future_tolerance_seconds=0,
    )
    return PartitionScheme.build(
        base, {"region": {"case": "upper", "aliases": {"EU": ["emea"]}}}
    )


def _manifest(relpath: str, batch_id: str, raw_region: str):
    from pathlib import Path

    from lakeinlet.models import BatchManifest
    from lakeinlet.timeutil import parse_utc

    scheme = _scheme()
    raw = {"region": raw_region}
    return BatchManifest(
        manifest_path=Path(relpath),
        manifest_relpath=relpath,
        batch_id=batch_id,
        source="s",
        sequence=1,
        partition=scheme.normalize_partition(raw),
        declared_at=parse_utc("2025-03-01T01:00:00Z"),
        files=(),
        raw_partition=raw,
    )


def test_collision_when_raw_values_merge_inside_one_batch_id():
    source = replace(
        SourceConfig(
            name="s",
            partition_keys=("region",),
            allowed_lateness_seconds=0,
            future_tolerance_seconds=0,
        ),
        partition_scheme=_scheme(),
    )
    manifests = [
        _manifest("_manifests/s/a.yaml", "shared-batch", "eu"),
        _manifest("_manifests/s/b.yaml", "shared-batch", "EU"),
    ]
    findings = find_partition_alias_collisions(manifests, source)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "PARTITION_ALIAS_COLLISION"
    assert finding.batch_id == "shared-batch"
    assert finding.evidence["reason"] == "raw_values_merge_to_one_partition"
    assert finding.partition == {"region": "EU"}
    assert finding.evidence["raw_value_mapping"] == {"region": ["EU", "eu"]}
    assert finding.evidence["manifests"] == [
        "_manifests/s/a.yaml",
        "_manifests/s/b.yaml",
    ]


def test_consistent_alias_merging_yields_no_collision():
    source = replace(
        SourceConfig(
            name="s",
            partition_keys=("region",),
            allowed_lateness_seconds=0,
            future_tolerance_seconds=0,
        ),
        partition_scheme=_scheme(),
    )
    manifests = [
        _manifest("_manifests/s/a.yaml", "batch-a", "eu"),
        _manifest("_manifests/s/b.yaml", "batch-b", "EU"),
        _manifest("_manifests/s/c.yaml", "batch-c", "emea"),
    ]
    assert find_partition_alias_collisions(manifests, source) == []
