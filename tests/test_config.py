from __future__ import annotations

from pathlib import Path

import pytest

from lakeinlet.config import load_config
from lakeinlet.errors import ConfigError
from lakeinlet.timeutil import parse_utc

def test_loads_demo_config_and_resolves_relative_paths(demo_config):
    assert demo_config.project == "lakeinlet-demo"
    assert demo_config.as_of == parse_utc("2025-03-01T04:00:00Z")
    assert demo_config.lake_root == (
        Path(__file__).resolve().parents[1] / "fixtures" / "demo" / "lake"
    )
    assert [source.name for source in demo_config.sources] == ["orders", "clicks"]

    orders = demo_config.source_map["orders"]
    assert orders.partition_keys == ("dt", "region")
    assert orders.allowed_lateness_seconds == 3600
    assert orders.future_tolerance_seconds == 300
    assert orders.sequence_policy == "contiguous"
    assert orders.manifest_dir == "_manifests/orders"
    assert orders.event_time_field == "event_time"
    assert orders.record_id_field == "record_id"

def test_semantic_config_has_no_local_paths(demo_config):
    semantic = demo_config.semantic_dict()
    assert semantic["project"] == "lakeinlet-demo"
    assert semantic["as_of"] == "2025-03-01T04:00:00Z"
    assert "lake_root" not in semantic
    assert semantic["sources"][0]["name"] == "orders"

def test_unknown_top_level_key_is_rejected(config_writer):
    path = config_writer(lambda data: data.update(unexpected=True))
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(path)

def test_unknown_source_key_is_rejected(config_writer):
    path = config_writer(lambda data: data["sources"][0].update(typo=1))
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(path)

def test_duplicate_source_names_are_rejected(config_writer):
    def mutate(data):
        data["sources"][1]["name"] = data["sources"][0]["name"]

    with pytest.raises(ConfigError, match="duplicate source names"):
        load_config(config_writer(mutate))

def test_non_utc_as_of_is_rejected(config_writer):
    path = config_writer(lambda data: data.update(as_of="2025-03-01T12:00:00+08:00"))
    with pytest.raises(ConfigError, match="must use UTC"):
        load_config(path)

def test_naive_as_of_is_rejected(config_writer):
    path = config_writer(lambda data: data.update(as_of="2025-03-01T12:00:00"))
    with pytest.raises(ConfigError, match="timezone"):
        load_config(path)

def test_missing_lake_root_is_rejected(config_writer, tmp_path):
    with pytest.raises(ConfigError, match="lake_root"):
        load_config(config_writer(lake_root=tmp_path / "does-not-exist"))

def test_negative_lateness_is_rejected(config_writer):
    def mutate(data):
        data["sources"][0]["allowed_lateness_seconds"] = -1

    with pytest.raises(ConfigError, match=">= 0"):
        load_config(config_writer(mutate))

def test_unknown_sequence_policy_is_rejected(config_writer):
    def mutate(data):
        data["sources"][0]["sequence_policy"] = "strict-ish"

    with pytest.raises(ConfigError, match="sequence_policy"):
        load_config(config_writer(mutate))

def test_sequence_policy_alias_is_normalized(config_writer):
    def mutate(data):
        data["sources"][0]["sequence_policy"] = "allow_gaps"

    config = load_config(config_writer(mutate))
    assert config.sources[0].sequence_policy == "gapped"

def test_manifest_dir_traversal_is_rejected(config_writer):
    def mutate(data):
        data["sources"][0]["manifest_dir"] = "../outside"

    with pytest.raises(ConfigError, match="relative path"):
        load_config(config_writer(mutate))

def test_partition_keys_must_be_unique(config_writer):
    def mutate(data):
        data["sources"][0]["partition_keys"] = ["dt", "dt"]

    with pytest.raises(ConfigError, match="duplicates"):
        load_config(config_writer(mutate))

def test_invalid_yaml_is_rejected(config_writer):
    path = config_writer(raw="project: [unterminated\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)

def test_missing_config_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")
