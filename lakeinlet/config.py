"""Load and statically validate the declarative YAML configuration."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError
from .models import AuditConfig, SourceConfig
from .partition import PartitionScheme
from .timeutil import parse_utc

_TOP_LEVEL_KEYS = {"project", "lake_root", "as_of", "sources", "schema_version"}
_SOURCE_KEYS = {
    "name",
    "partition_keys",
    "partition_templates",
    "allowed_lateness_seconds",
    "future_tolerance_seconds",
    "sequence_policy",
    "manifest_dir",
    "manifest_glob",
    "event_time_field",
    "record_id_field",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_POLICY_ALIASES = {
    "contiguous": "contiguous",
    "gapped": "gapped",
    "allow_gaps": "gapped",
    "non_contiguous": "gapped",
}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _required_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _non_negative_int(mapping: dict[str, Any], key: str, label: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{label}.{key} must be a non-negative integer")
    if value < 0:
        raise ConfigError(f"{label}.{key} must be >= 0")
    return value


def _relative_path_text(value: str, label: str) -> str:
    path = Path(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ConfigError(f"{label} must be a relative path without '..'")
    return path.as_posix()


def _build_source(raw: Any, index: int) -> SourceConfig:
    label = f"sources[{index}]"
    item = _mapping(raw, label)
    unknown = sorted(set(item) - _SOURCE_KEYS)
    if unknown:
        raise ConfigError(f"{label} contains unknown keys: {', '.join(unknown)}")

    name = _required_string(item, "name", label)
    if not _NAME_RE.fullmatch(name):
        raise ConfigError(
            f"{label}.name must match {_NAME_RE.pattern} (letters, digits, . _ -)"
        )

    keys = item.get("partition_keys")
    if not isinstance(keys, list) or not keys:
        raise ConfigError(f"{label}.partition_keys must be a non-empty list")
    if any(not isinstance(key, str) or not _NAME_RE.fullmatch(key) for key in keys):
        raise ConfigError(f"{label}.partition_keys entries must be identifier strings")
    if len(set(keys)) != len(keys):
        raise ConfigError(f"{label}.partition_keys contains duplicates")

    allowed = _non_negative_int(item, "allowed_lateness_seconds", label)
    tolerance = _non_negative_int(item, "future_tolerance_seconds", label)
    raw_policy = item.get("sequence_policy", "contiguous")
    if not isinstance(raw_policy, str) or raw_policy not in _POLICY_ALIASES:
        raise ConfigError(
            f"{label}.sequence_policy must be 'contiguous' or 'gapped'"
        )
    manifest_dir = item.get("manifest_dir", f"_manifests/{name}")
    if not isinstance(manifest_dir, str) or not manifest_dir.strip():
        raise ConfigError(f"{label}.manifest_dir must be a non-empty string")
    manifest_dir = _relative_path_text(manifest_dir.strip(), f"{label}.manifest_dir")
    manifest_glob = item.get("manifest_glob", "*.yaml")
    if not isinstance(manifest_glob, str) or not manifest_glob.strip():
        raise ConfigError(f"{label}.manifest_glob must be a non-empty string")
    manifest_glob = manifest_glob.strip()
    if Path(manifest_glob).is_absolute() or ".." in Path(manifest_glob).parts:
        raise ConfigError(f"{label}.manifest_glob must stay inside the manifest dir")

    event_field = item.get("event_time_field", "event_time")
    record_field = item.get("record_id_field", "record_id")
    for key, value in (
        ("event_time_field", event_field),
        ("record_id_field", record_field),
    ):
        if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
            raise ConfigError(f"{label}.{key} must be an identifier string")

    source = SourceConfig(
        name=name,
        partition_keys=tuple(keys),
        allowed_lateness_seconds=allowed,
        future_tolerance_seconds=tolerance,
        sequence_policy=_POLICY_ALIASES[raw_policy],
        manifest_dir=manifest_dir,
        manifest_glob=manifest_glob,
        event_time_field=event_field,
        record_id_field=record_field,
    )
    # Static validation: templates must cover every partition key and alias
    # spellings must not collide with each other or with canonical values.
    scheme = PartitionScheme.build(source, item.get("partition_templates"))
    return dataclasses.replace(source, partition_scheme=scheme)


def load_config(path: str | Path) -> AuditConfig:
    """Read a config file and enforce all static constraints.

    Relative paths are resolved from the config file's directory, so the
    repository works regardless of the current working directory.
    """
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read config file {config_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    root = _mapping(raw, "config")
    unknown = sorted(set(root) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(f"config contains unknown keys: {', '.join(unknown)}")
    if "schema_version" in root:
        version = root["schema_version"]
        if isinstance(version, bool) or version not in (1, "1"):
            raise ConfigError("schema_version must be 1")

    project = _required_string(root, "project", "config")
    lake_root_text = _required_string(root, "lake_root", "config")
    lake_root = (config_path.parent / lake_root_text).resolve()
    if not lake_root.is_dir():
        raise ConfigError(f"lake_root is not an existing directory: {lake_root}")

    if "as_of" not in root:
        raise ConfigError("config.as_of is required")
    try:
        as_of = parse_utc(root["as_of"], "config.as_of")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    raw_sources = root.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("config.sources must be a non-empty list")
    sources = tuple(_build_source(item, index) for index, item in enumerate(raw_sources))
    names = [source.name for source in sources]
    if len(set(names)) != len(names):
        raise ConfigError("config.sources contains duplicate source names")

    return AuditConfig(
        project=project,
        lake_root=lake_root,
        as_of=as_of,
        sources=sources,
        config_path=config_path,
    )
