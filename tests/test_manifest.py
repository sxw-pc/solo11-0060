from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lakeinlet.config import load_config
from lakeinlet.errors import ManifestError
from lakeinlet.manifest import (
    ManifestParseError,
    load_manifest_file,
    load_source_manifests,
    parse_manifest_document,
    resolve_lake_path,
)
from lakeinlet.timeutil import parse_utc

def test_demo_manifests_are_loaded_in_stable_order(demo_config):
    source = demo_config.source_map["orders"]
    manifests, findings = load_source_manifests(demo_config, source)
    assert findings == []
    assert [item.sequence for item in manifests] == [1, 2, 3, 4]
    assert [item.batch_id for item in manifests] == [
        "orders-eu-0001",
        "orders-eu-0002",
        "orders-eu-0003",
        "orders-us-0004",
    ]
    assert all(item.source == "orders" for item in manifests)

def test_manifest_fields_and_file_metadata(demo_config):
    source = demo_config.source_map["orders"]
    manifests, _ = load_source_manifests(demo_config, source)
    first = manifests[0]
    assert first.sequence == 1
    assert first.partition == {"dt": "2025-03-01", "region": "eu"}
    assert first.declared_at == parse_utc("2025-03-01T00:30:00Z")
    assert first.prev_batch_id is None
    assert first.manifest_relpath == "_manifests/orders/orders_0001.yaml"
    assert len(first.files) == 1
    ref = first.files[0]
    assert ref.path == "data/orders/orders_eu_0001.csv"
    assert len(ref.sha256) == 64
    assert ref.bytes and ref.bytes > 0
    assert ref.rows == 3
    assert ref.event_time_min == parse_utc("2025-03-01T00:05:00Z")
    assert ref.event_time_max == parse_utc("2025-03-01T00:10:00Z")

def test_chain_pointer_is_preserved(demo_config):
    manifests, _ = load_source_manifests(
        demo_config, demo_config.source_map["orders"]
    )
    assert manifests[1].prev_batch_id == "orders-eu-0001"
    assert manifests[3].prev_batch_id == "orders-eu-0003"

def test_missing_required_field_becomes_finding(broken_config):
    manifests, findings = load_source_manifests(
        broken_config, broken_config.source_map["orders"]
    )
    assert len(manifests) == 9
    codes = [finding.code for finding in findings]
    assert "MANIFEST_INVALID_FIELD" in codes
    invalid = next(item for item in findings if item.code == "MANIFEST_INVALID_FIELD")
    assert "sequence" in invalid.message
    assert invalid.evidence["manifest_path"].endswith("99_invalid.yaml")

def test_valid_yaml_with_missing_sequence_raises(demo_config):
    source = demo_config.source_map["orders"]
    document = {
        "batch_id": "missing-sequence",
        "source": "orders",
        "partition": {"dt": "2025-03-01", "region": "eu"},
        "declared_at": "2025-03-01T00:00:00Z",
        "files": [],
    }
    with pytest.raises(ManifestParseError, match="sequence"):
        parse_manifest_document(
            document,
            demo_config.lake_root / "manifest.yaml",
            "_manifests/orders/manifest.yaml",
            source,
            demo_config.lake_root,
        )

def test_source_mismatch_is_rejected(demo_config):
    source = demo_config.source_map["orders"]
    document = {
        "manifest_version": 1,
        "batch_id": "wrong-source",
        "source": "clicks",
        "sequence": 1,
        "partition": {"dt": "2025-03-01", "region": "eu"},
        "declared_at": "2025-03-01T00:00:00Z",
        "files": [],
    }
    with pytest.raises(ManifestParseError, match="does not belong"):
        parse_manifest_document(
            document,
            demo_config.lake_root / "x.yaml",
            "_manifests/orders/x.yaml",
            source,
            demo_config.lake_root,
        )

def test_partition_keys_must_match_configuration(demo_config):
    source = demo_config.source_map["orders"]
    document = {
        "batch_id": "bad-partition",
        "source": "orders",
        "sequence": 1,
        "partition": {"dt": "2025-03-01"},
        "declared_at": "2025-03-01T00:00:00Z",
        "files": [],
    }
    with pytest.raises(ManifestParseError) as excinfo:
        parse_manifest_document(
            document,
            demo_config.lake_root / "x.yaml",
            "_manifests/orders/x.yaml",
            source,
            demo_config.lake_root,
        )
    assert excinfo.value.code == "PARTITION_KEYS_INVALID"

def test_invalid_checksum_field_is_rejected(demo_config, tmp_path):
    source = demo_config.source_map["orders"]
    path = tmp_path / "bad.yaml"
    document = {
        "batch_id": "bad-hash",
        "source": "orders",
        "sequence": 1,
        "partition": {"dt": "2025-03-01", "region": "eu"},
        "declared_at": "2025-03-01T00:00:00Z",
        "files": [
            {
                "path": "data/orders/orders_eu_0001.csv",
                "sha256": "xyz",
                "rows": 1,
            }
        ],
    }
    with pytest.raises(ManifestParseError, match="64 hexadecimal"):
        parse_manifest_document(
            document,
            path,
            "bad.yaml",
            source,
            demo_config.lake_root,
        )

def test_manifest_outside_lake_root_is_rejected(demo_config, tmp_path):
    source = demo_config.source_map["orders"]
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        yaml.safe_dump(
            {
                "batch_id": "outside",
                "source": "orders",
                "sequence": 1,
                "partition": {"dt": "2025-03-01", "region": "eu"},
                "declared_at": "2025-03-01T00:00:00Z",
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestParseError, match="outside the configured lake"):
        load_manifest_file(outside, source, demo_config.lake_root)

def test_path_traversal_is_rejected(tmp_path):
    with pytest.raises(ManifestError, match="must not contain"):
        resolve_lake_path(tmp_path, "../outside.csv")
    with pytest.raises(ManifestError, match="relative"):
        resolve_lake_path(tmp_path, str((tmp_path / "absolute.csv").resolve()))

def test_no_matching_manifests_is_a_warning(config_writer, tmp_path):
    lake = tmp_path / "empty-lake"
    (lake / "_manifests" / "orders").mkdir(parents=True)
    (lake / "_manifests" / "clicks").mkdir(parents=True)
    config = load_config(config_writer(lake_root=lake))
    manifests, findings = load_source_manifests(
        config, config.source_map["orders"]
    )
    assert manifests == []
    assert findings[0].code == "NO_MANIFESTS"
    assert findings[0].severity == "warning"

def test_missing_manifest_directory_is_an_error(config_writer, tmp_path):
    lake = tmp_path / "lake"
    lake.mkdir()
    config = load_config(config_writer(lake_root=lake))
    manifests, findings = load_source_manifests(
        config, config.source_map["orders"]
    )
    assert manifests == []
    assert findings[0].code == "MANIFEST_DIR_MISSING"
    assert findings[0].severity == "error"
