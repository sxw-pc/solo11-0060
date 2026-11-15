"""Canonical partition identity.

Every place that needs to identify a partition — manifest declarations, CSV
row partition columns, watermark state keys, duplicate-record grouping and the
ledger ``partition_label`` — must go through :class:`PartitionScheme` so that
surface differences such as ``eu`` versus ``EU`` never create two identities
for one partition.

A source may optionally declare ``partition_templates`` describing how raw
partition values normalize:

.. code-block:: yaml

    partition_templates:
      region:
        case: upper
        aliases:
          EMEA: [emea]

``case`` is applied to both the canonical value and to raw values before alias
lookup. ``aliases`` maps the canonical value to additional raw spellings that
must resolve to it. When no template exists for a source, normalization is the
identity function, preserving the historical string-compare behavior.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .errors import ConfigError
from .models import BatchManifest, Finding, SourceConfig, partition_label

_CASE_TRANSFORMS = {
    "none": lambda value: value,
    "upper": str.upper,
    "lower": str.lower,
}


@dataclass(frozen=True, slots=True)
class _KeyTemplate:
    """Normalization rule for one partition key."""

    key: str
    case: str
    aliases: dict[str, tuple[str, ...]]
    # Folded raw spelling -> canonical value. Built once at config load time.
    _lookup: dict[str, str] = field(default_factory=dict)

    def normalize(self, raw: str) -> str:
        folded = _CASE_TRANSFORMS[self.case](raw)
        return self._lookup.get(folded, folded)


@dataclass(frozen=True, slots=True)
class PartitionScheme:
    """The single authority that turns raw partition values into identity.

    Instances are built once per source at configuration load time and shared
    by every later pipeline stage. Without configured templates every key
    normalizes to itself, so existing configurations keep their exact behavior.
    """

    source: str
    keys: tuple[str, ...]
    templates: Mapping[str, _KeyTemplate]
    # Whether any key actually changes values; controls raw-value bookkeeping.
    active: bool = False

    def normalize_value(self, key: str, raw: str) -> str:
        """Return the canonical value of one partition key's raw value."""
        template = self.templates.get(key)
        if template is None:
            return raw
        return template.normalize(raw)

    def normalize_partition(self, partition: Mapping[str, str]) -> dict[str, str]:
        """Canonicalize a complete partition mapping (key order preserved)."""
        return {
            key: self.normalize_value(key, str(partition[key]))
            for key in self.keys
            if key in partition
        }

    def identity_key(self, partition: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        """Hashable canonical identity, ordered like ``partition_keys``."""
        canonical = self.normalize_partition(partition)
        return tuple((key, canonical[key]) for key in self.keys)

    @classmethod
    def identity(cls, source: SourceConfig) -> "PartitionScheme":
        """Scheme without templates: normalization is the identity function."""
        return cls(source.name, tuple(source.partition_keys), {}, active=False)

    @classmethod
    def build(cls, source: SourceConfig, raw_templates: object) -> "PartitionScheme":
        """Parse and statically validate ``partition_templates`` for a source."""
        label = f"sources[{source.name!r}].partition_templates"
        if raw_templates is None:
            return cls.identity(source)
        if not isinstance(raw_templates, dict):
            raise ConfigError(f"{label} must be a mapping")

        unknown = sorted(set(raw_templates) - set(source.partition_keys))
        if unknown:
            raise ConfigError(
                f"{label} contains keys not in partition_keys: {', '.join(unknown)}"
            )
        missing = sorted(set(source.partition_keys) - set(raw_templates))
        if missing:
            raise ConfigError(
                f"{label} must cover every partition key; missing: "
                f"{', '.join(missing)}"
            )

        templates: dict[str, _KeyTemplate] = {}
        active = False
        for key in source.partition_keys:
            entry = raw_templates[key]
            entry_label = f"{label}.{key}"
            if not isinstance(entry, dict):
                raise ConfigError(f"{entry_label} must be a mapping")
            unknown_fields = sorted(set(entry) - {"case", "aliases"})
            if unknown_fields:
                raise ConfigError(
                    f"{entry_label} contains unknown fields: "
                    f"{', '.join(unknown_fields)}"
                )

            case = entry.get("case", "none")
            if not isinstance(case, str) or case not in _CASE_TRANSFORMS:
                raise ConfigError(
                    f"{entry_label}.case must be one of "
                    f"{sorted(_CASE_TRANSFORMS)}"
                )

            raw_aliases = entry.get("aliases", {})
            if not isinstance(raw_aliases, dict):
                raise ConfigError(f"{entry_label}.aliases must be a mapping")

            aliases: dict[str, tuple[str, ...]] = {}
            lookup: dict[str, str] = {}
            for canonical, spellings in raw_aliases.items():
                if not isinstance(canonical, str) or not canonical.strip():
                    raise ConfigError(
                        f"{entry_label}.aliases keys must be non-empty strings"
                    )
                if isinstance(spellings, bool) or not isinstance(
                    spellings, (list, tuple)
                ):
                    raise ConfigError(
                        f"{entry_label}.aliases.{canonical} must be a list"
                    )
                cleaned: list[str] = []
                for spelling in spellings:
                    if not isinstance(spelling, str) or not spelling.strip():
                        raise ConfigError(
                            f"{entry_label}.aliases.{canonical} entries must be "
                            "non-empty strings"
                        )
                    cleaned.append(spelling)
                aliases[canonical] = tuple(cleaned)

            # The canonical spelling itself is always a valid raw spelling.
            for canonical in aliases:
                folded_canonical = _CASE_TRANSFORMS[case](canonical)
                if folded_canonical in lookup and lookup[folded_canonical] != canonical:
                    raise ConfigError(
                        f"{entry_label}.aliases: canonical values {canonical!r} "
                        f"and {lookup[folded_canonical]!r} collide after "
                        f"case={case!r} normalization"
                    )
                lookup[folded_canonical] = canonical
            for canonical, spellings in aliases.items():
                for spelling in spellings:
                    folded = _CASE_TRANSFORMS[case](spelling)
                    owner = lookup.get(folded)
                    if owner is not None and owner != canonical:
                        raise ConfigError(
                            f"{entry_label}.aliases: raw value {spelling!r} "
                            f"cannot be aliased to {canonical!r}; it already "
                            f"resolves to {owner!r}"
                        )
                    lookup[folded] = canonical
            # Any case folding or any declared alias makes the rule non-identity.
            if case != "none" or aliases:
                active = True

            templates[key] = _KeyTemplate(
                key=key, case=case, aliases=aliases, _lookup=dict(lookup)
            )

        return cls(source.name, tuple(source.partition_keys), templates, active)

    def to_dict(self) -> dict[str, object]:
        """Stable configuration representation for the semantic config digest.

        Keys whose rule is the identity (``case: none`` without aliases) are
        omitted, so template-less behavior never perturbs the config digest.
        """
        payload: dict[str, object] = {}
        for key in self.keys:
            template = self.templates.get(key)
            if template is None:
                continue
            if template.case == "none" and not template.aliases:
                continue
            payload[key] = {
                "case": template.case,
                "aliases": {
                    canonical: list(spellings)
                    for canonical, spellings in sorted(template.aliases.items())
                },
            }
        return payload


def scheme_of(source: SourceConfig) -> "PartitionScheme":
    """Return the source's scheme, or an identity scheme when none is set.

    Keeps callers robust against ``SourceConfig`` instances built directly in
    unit tests without going through :func:`load_config`.
    """
    if source.partition_scheme is not None:
        return source.partition_scheme
    return PartitionScheme.identity(source)


def find_partition_alias_collisions(
    manifests: Sequence[BatchManifest], source: SourceConfig
) -> list[Finding]:
    """Detect batch ids that do not map to a single canonical partition.

    Two error conditions are reported, both under ``PARTITION_ALIAS_COLLISION``:

    1. Two different raw partition values normalize to the same canonical
       partition inside one ``batch_id`` (a batch is declared with spellings
       that merge onto one identity);
    2. One ``batch_id`` shows up under more than one canonical partition.

    Ordinary alias merging — different raw values that consistently describe
    one partition across different batch ids — is expected and yields nothing.
    """
    scheme = scheme_of(source)
    # batch_id -> canonical identity -> raw values seen per key + manifests
    by_batch: dict[
        str, dict[tuple[tuple[str, str], ...], _PartitionObservation]
    ] = defaultdict(dict)
    for manifest in manifests:
        identity = scheme.identity_key(manifest.partition)
        observation = by_batch[manifest.batch_id].setdefault(
            identity, _PartitionObservation()
        )
        observation.manifests.append(manifest.manifest_relpath)
        for key in scheme.keys:
            raw_value = str(manifest.raw_partition.get(key, manifest.partition[key]))
            observation.raw_values[key].add(raw_value)

    findings: list[Finding] = []
    for batch_id in sorted(by_batch):
        groups = by_batch[batch_id]

        if len(groups) > 1:
            identities = sorted(groups, key=lambda item: partition_label(dict(item)))
            involved = sorted(
                {
                    path
                    for observation in groups.values()
                    for path in observation.manifests
                }
            )
            mapping = []
            for identity in identities:
                observation = groups[identity]
                mapping.append(
                    {
                        "canonical_partition": dict(identity),
                        "raw_values": {
                            key: sorted(values)
                            for key, values in sorted(
                                observation.raw_values.items()
                            )
                        },
                        "manifests": sorted(observation.manifests),
                    }
                )
            findings.append(
                Finding(
                    "error",
                    "PARTITION_ALIAS_COLLISION",
                    f"batch_id {batch_id!r} is declared under "
                    f"{len(groups)} distinct canonical partitions",
                    source=source.name,
                    batch_id=batch_id,
                    evidence={
                        "reason": "batch_id_spans_multiple_partitions",
                        "raw_value_mapping": mapping,
                        "manifests": involved,
                    },
                )
            )
            continue

        identity, observation = next(iter(groups.items()))
        merged_keys = {
            key: sorted(values)
            for key, values in observation.raw_values.items()
            if len(values) > 1
        }
        if merged_keys:
            findings.append(
                Finding(
                    "error",
                    "PARTITION_ALIAS_COLLISION",
                    f"batch_id {batch_id!r} uses raw partition values that "
                    "normalize to the same canonical partition",
                    source=source.name,
                    partition=dict(identity),
                    batch_id=batch_id,
                    evidence={
                        "reason": "raw_values_merge_to_one_partition",
                        "canonical_partition": dict(identity),
                        "raw_value_mapping": merged_keys,
                        "manifests": sorted(observation.manifests),
                    },
                )
            )
    return findings


class _PartitionObservation:
    __slots__ = ("manifests", "raw_values")

    def __init__(self) -> None:
        self.manifests: list[str] = []
        self.raw_values: dict[str, set[str]] = defaultdict(set)
