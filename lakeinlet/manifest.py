"""Manifest discovery and parsing.

A manifest is the unit of an ingestion batch. It declares its source,
sequence, partition, declared arrival time, and each data file with its
expected digest, size, row count, and event-time bounds.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .errors import ManifestError
from .models import AuditConfig, BatchManifest, FileRef, Finding, SourceConfig
from .timeutil import parse_utc

_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")

class ManifestParseError(ManifestError):
    """A single malformed manifest; other manifests remain auditable."""

    def __init__(self, code: str, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = evidence


def _fail(code: str, message: str, **evidence: Any):
    raise ManifestParseError(code, message, evidence)

def resolve_lake_path(lake_root: Path, relative: str, label: str = "path") -> Path:
    """Resolve a manifest-declared relative path beneath ``lake_root``.

    Absolute paths and any path that escapes the lake root are rejected. This
    keeps the auditor inside the explicitly configured local data boundary.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise ManifestError(f"{label} must be a non-empty relative path")
    raw = Path(relative.strip())
    if raw.is_absolute() or any(part == ".." for part in raw.parts):
        raise ManifestError(f"{label} must be relative and must not contain '..'")
    root = lake_root.resolve()
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"{label} escapes the lake root") from exc
    return candidate

def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("MANIFEST_INVALID_FIELD",
              f"{field} must be a non-empty string", field=field)
    return value.strip()

def _optional_non_negative_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("MANIFEST_INVALID_FIELD",
              f"{field} must be a non-negative integer when present", field=field)
    return value

def _parse_optional_time(value: Any, field: str):
    if value is None:
        return None
    try:
        return parse_utc(value, field)
    except ValueError as exc:
        raise ManifestParseError("MANIFEST_INVALID_FIELD", str(exc), {"field": field}) from exc

def _parse_partition(value: Any, source: SourceConfig) -> dict[str, str]:
    if not isinstance(value, dict):
        _fail("PARTITION_KEYS_INVALID", "partition must be a mapping")
    expected = set(source.partition_keys)
    supplied = set(value)
    if supplied != expected:
        _fail("PARTITION_KEYS_INVALID",
              f"partition keys must be exactly {sorted(expected)}",
              expected=sorted(expected), actual=sorted(supplied))
    result: dict[str, str] = {}
    for key in source.partition_keys:
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            _fail("PARTITION_KEYS_INVALID",
                  f"partition value {key!r} must be a scalar", key=key)
        text = str(raw)
        if not text.strip():
            _fail("PARTITION_KEYS_INVALID",
                  f"partition value {key!r} must not be empty", key=key)
        result[key] = text
    return result

def _parse_file_ref(
    raw: Any, index: int, lake_root: Path, source: SourceConfig
) -> FileRef:
    label = f"files[{index}]"
    if not isinstance(raw, dict):
        _fail("MANIFEST_INVALID_FIELD", f"{label} must be a mapping", field=label)
    path_text = _non_empty_string(raw.get("path"), f"{label}.path")
    try:
        resolve_lake_path(lake_root, path_text, f"{label}.path")
    except ManifestError as exc:
        raise ManifestParseError("INVALID_FILE_PATH", str(exc), {"path": path_text}) from exc
    sha = _non_empty_string(raw.get("sha256"), f"{label}.sha256")
    if not _SHA256_RE.fullmatch(sha):
        _fail("MANIFEST_INVALID_FIELD",
              f"{label}.sha256 must be 64 hexadecimal characters", path=path_text)
    byte_count = _optional_non_negative_int(raw.get("bytes"), f"{label}.bytes")
    rows = _optional_non_negative_int(raw.get("rows"), f"{label}.rows")
    event_min = _parse_optional_time(raw.get("event_time_min"), f"{label}.event_time_min")
    event_max = _parse_optional_time(raw.get("event_time_max"), f"{label}.event_time_max")
    if event_min is not None and event_max is not None and event_min > event_max:
        _fail("MANIFEST_INVALID_FIELD",
              f"{label} has event_time_min after event_time_max", path=path_text)
    return FileRef(
        path=Path(path_text.strip()).as_posix(),
        sha256=sha.lower(),
        bytes=byte_count,
        rows=rows,
        event_time_min=event_min,
        event_time_max=event_max,
    )

def parse_manifest_document(
    document: Any,
    manifest_path: Path,
    manifest_relpath: str,
    source: SourceConfig,
    lake_root: Path,
) -> BatchManifest:
    """Validate one already-loaded YAML document."""
    if not isinstance(document, dict):
        _fail("MANIFEST_INVALID", "manifest root must be a mapping")
    version = document.get("manifest_version", 1)
    if isinstance(version, bool) or version != 1:
        _fail("MANIFEST_INVALID_FIELD", "manifest_version must be 1",
              manifest_version=version)
    batch_id = _non_empty_string(document.get("batch_id"), "batch_id")
    source_name = _non_empty_string(document.get("source"), "source")
    if source_name != source.name:
        _fail("MANIFEST_SOURCE_MISMATCH",
              f"manifest source {source_name!r} does not belong to audited source "
              f"{source.name!r}", declared_source=source_name,
              expected_source=source.name)
    sequence = document.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        _fail("MANIFEST_INVALID_FIELD",
              "sequence must be a non-negative integer", field="sequence")
    partition = _parse_partition(document.get("partition"), source)
    try:
        declared_at = parse_utc(document.get("declared_at"), "declared_at")
    except ValueError as exc:
        raise ManifestParseError(
            "MANIFEST_INVALID_FIELD", str(exc), {"field": "declared_at"}) from exc
    raw_files = document.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        _fail("MANIFEST_INVALID_FIELD",
              "files must be a non-empty list", field="files")
    files = tuple(
        _parse_file_ref(item, index, lake_root, source)
        for index, item in enumerate(raw_files)
    )
    paths = [item.path for item in files]
    if len(set(paths)) != len(paths):
        _fail("DUPLICATE_FILE_REF",
              "the same data file is referenced more than once in this manifest",
              paths=sorted(paths))
    prev_batch_id = document.get("prev_batch_id")
    if prev_batch_id is not None:
        prev_batch_id = _non_empty_string(prev_batch_id, "prev_batch_id")
    return BatchManifest(
        manifest_path=manifest_path,
        manifest_relpath=manifest_relpath,
        batch_id=batch_id,
        source=source.name,
        sequence=sequence,
        partition=partition,
        declared_at=declared_at,
        files=files,
        prev_batch_id=prev_batch_id,
    )

def load_manifest_file(
    path: Path, source: SourceConfig, lake_root: Path
) -> BatchManifest:
    """Read and parse a single manifest, raising ManifestParseError on failure."""
    try:
        relative = path.resolve().relative_to(lake_root.resolve()).as_posix()
    except ValueError as exc:
        raise ManifestParseError(
            "INVALID_FILE_PATH", "manifest is outside the configured lake root",
            {"manifest_path": path.name}) from exc
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManifestParseError(
            "MANIFEST_PARSE_ERROR", f"cannot read manifest: {exc}",
            {"manifest_path": relative}) from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestParseError(
            "MANIFEST_PARSE_ERROR", f"invalid YAML: {exc}",
            {"manifest_path": relative}) from exc
    try:
        return parse_manifest_document(
            document, path.resolve(), relative, source, lake_root
        )
    except ManifestParseError as exc:
        exc.evidence.setdefault("manifest_path", relative)
        raise

def load_source_manifests(
    config: AuditConfig, source: SourceConfig
) -> tuple[list[BatchManifest], list[Finding]]:
    """Discover and parse all manifests for one source.

    A malformed manifest becomes a finding and does not prevent the rest of the
    source from being audited.
    """
    findings: list[Finding] = []
    try:
        manifest_dir = resolve_lake_path(
            config.lake_root, source.manifest_dir, "manifest_dir"
        )
    except ManifestError as exc:
        return [], [
            Finding(
                "error",
                "MANIFEST_DIR_INVALID",
                str(exc),
                source=source.name,
                evidence={"manifest_dir": source.manifest_dir},
            )
        ]
    if not manifest_dir.is_dir():
        return [], [
            Finding(
                "error",
                "MANIFEST_DIR_MISSING",
                f"manifest directory does not exist: {source.manifest_dir}",
                source=source.name,
                evidence={"manifest_dir": source.manifest_dir},
            )
        ]
    paths = sorted(
        (path for path in manifest_dir.glob(source.manifest_glob) if path.is_file()),
        key=lambda item: item.as_posix(),
    )
    if not paths:
        findings.append(
            Finding(
                "warning",
                "NO_MANIFESTS",
                f"no manifests matched {source.manifest_glob!r}",
                source=source.name,
                evidence={"manifest_dir": source.manifest_dir},
            )
        )
        return [], findings
    manifests: list[BatchManifest] = []
    for path in paths:
        try:
            manifests.append(load_manifest_file(path, source, config.lake_root))
        except ManifestParseError as exc:
            findings.append(
                Finding(
                    "error",
                    exc.code,
                    str(exc),
                    source=source.name,
                    evidence=exc.evidence,
                )
            )
    return manifests, findings
