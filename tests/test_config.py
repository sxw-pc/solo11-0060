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


def _with_templates(config_writer, templates: dict):
    def mutate(data):
        data["sources"][0]["partition_templates"] = templates

    return load_config(config_writer(mutate))


def test_partition_templates_are_parsed_and_normalize_values(config_writer):
    config = _with_templates(
        config_writer,
        {
            "dt": {"case": "none"},
            "region": {"case": "upper", "aliases": {"EU": ["emea"]}},
        },
    )
    scheme = config.sources[0].partition_scheme
    assert scheme.active is True
    assert scheme.normalize_value("region", "eu") == "EU"
    assert scheme.normalize_value("region", "EU") == "EU"
    assert scheme.normalize_value("region", "emea") == "EU"
    assert scheme.normalize_value("region", "EMEA") == "EU"
    assert scheme.normalize_value("dt", "2025-03-01") == "2025-03-01"
    assert scheme.identity_key({"dt": "2025-03-01", "region": "eu"}) == (
        ("dt", "2025-03-01"),
        ("region", "EU"),
    )


def test_lower_case_template_normalizes_to_lower(config_writer):
    config = _with_templates(
        config_writer, {"dt": {"case": "none"}, "region": {"case": "lower"}}
    )
    scheme = config.sources[0].partition_scheme
    assert scheme.normalize_value("region", "EU") == "eu"


def test_missing_template_coverage_is_rejected(config_writer):
    with pytest.raises(ConfigError, match="must cover every partition key"):
        _with_templates(config_writer, {"region": {"case": "upper"}})


def test_template_for_unknown_partition_key_is_rejected(config_writer):
    with pytest.raises(ConfigError, match="not in partition_keys"):
        _with_templates(
            config_writer,
            {
                "dt": {"case": "none"},
                "region": {"case": "upper"},
                "country": {"case": "upper"},
            },
        )


def test_unknown_template_field_and_case_are_rejected(config_writer):
    with pytest.raises(ConfigError, match="unknown fields"):
        _with_templates(
            config_writer,
            {
                "dt": {"case": "none"},
                "region": {"case": "upper", "trim": True},
            },
        )
    with pytest.raises(ConfigError, match="case"):
        _with_templates(
            config_writer,
            {"dt": {"case": "none"}, "region": {"case": "title"}},
        )


def test_alias_collision_with_another_canonical_key_is_rejected(config_writer):
    # eu is aliased to both EU and EMEA canonical values.
    with pytest.raises(ConfigError, match="already resolves"):
        _with_templates(
            config_writer,
            {
                "dt": {"case": "none"},
                "region": {
                    "case": "upper",
                    "aliases": {"EU": ["emea"], "EMEA": ["eu"]},
                },
            },
        )


def test_duplicate_alias_spelling_within_one_key_is_rejected(config_writer):
    with pytest.raises(ConfigError, match="already resolves"):
        _with_templates(
            config_writer,
            {
                "dt": {"case": "none"},
                "region": {
                    "case": "upper",
                    "aliases": {"EU": ["emea"], "US": ["EMEA"]},
                },
            },
        )


def test_canonical_values_colliding_after_case_fold_are_rejected(config_writer):
    with pytest.raises(ConfigError, match="collide after"):
        _with_templates(
            config_writer,
            {
                "dt": {"case": "none"},
                "region": {
                    "case": "lower",
                    "aliases": {"eu": [], "EU": []},
                },
            },
        )


def test_alias_list_must_be_a_list(config_writer):
    with pytest.raises(ConfigError, match="must be a list"):
        _with_templates(
            config_writer,
            {
                "dt": {"case": "none"},
                "region": {"case": "upper", "aliases": {"EU": "emea"}},
            },
        )


def test_alias_folding_to_its_own_canonical_is_allowed(config_writer):
    # With case: upper, "eu" folds to the canonical EU: an explicit, redundant
    # alias to the same target is valid rather than a self-collision.
    config = _with_templates(
        config_writer,
        {
            "dt": {"case": "none"},
            "region": {"case": "upper", "aliases": {"EU": ["eu", "EMEA"]}},
        },
    )
    scheme = config.sources[0].partition_scheme
    assert scheme.normalize_value("region", "eu") == "EU"
    assert scheme.normalize_value("region", "emea") == "EU"


def test_identity_only_template_is_inactive_and_absent_from_digest(config_writer):
    config = _with_templates(
        config_writer, {"dt": {"case": "none"}, "region": {"case": "none"}}
    )
    assert config.sources[0].partition_scheme.active is False
    semantic = config.semantic_dict()["sources"][0]
    assert "partition_templates" not in semantic


def test_templates_participate_in_semantic_config_digest(config_writer):
    without = load_config(config_writer())
    with_templates = _with_templates(
        config_writer,
        {"dt": {"case": "none"}, "region": {"case": "upper"}},
    )
    # Template-less sources keep their historical semantic shape exactly.
    assert "partition_templates" not in without.semantic_dict()["sources"][0]
    assert (
        with_templates.semantic_dict()["sources"][0]["partition_templates"]
        == {"region": {"case": "upper", "aliases": {}}}
    )
