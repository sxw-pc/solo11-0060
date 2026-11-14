"""Batch sequence continuity and optional manifest-chain checks."""

from __future__ import annotations

from typing import Sequence

from .models import BatchManifest, Finding, SourceConfig

_MAX_MISSING_SAMPLES = 100


def find_sequence_findings(
    manifests: Sequence[BatchManifest], source: SourceConfig
) -> list[Finding]:
    """Check contiguous sequence policy and declared predecessor links."""
    findings: list[Finding] = []
    ordered = sorted(
        manifests, key=lambda item: (item.sequence, item.manifest_relpath)
    )
    sequences = sorted({item.sequence for item in ordered})
    if source.sequence_policy == "contiguous" and sequences:
        lowest, highest = sequences[0], sequences[-1]
        present = set(sequences)
        total_missing = (highest - lowest + 1) - len(sequences)
        if total_missing > 0:
            missing_samples: list[int] = []
            for candidate in range(lowest, highest + 1):
                if candidate not in present:
                    missing_samples.append(candidate)
                    if len(missing_samples) >= _MAX_MISSING_SAMPLES:
                        break
            findings.append(
                Finding(
                    "error",
                    "SEQUENCE_GAP",
                    f"source {source.name!r} is missing {total_missing} sequence "
                    f"number(s) between {lowest} and {highest}",
                    source=source.name,
                    evidence={
                        "policy": source.sequence_policy,
                        "range": [lowest, highest],
                        "missing_count": total_missing,
                        "missing_samples": missing_samples,
                        "samples_truncated": total_missing > len(missing_samples),
                    },
                )
            )

    batch_ids = {item.batch_id for item in ordered}
    by_id: dict[str, list[BatchManifest]] = {}
    for item in ordered:
        by_id.setdefault(item.batch_id, []).append(item)
    for item in ordered:
        if not item.prev_batch_id:
            continue
        if item.prev_batch_id not in batch_ids:
            findings.append(
                Finding(
                    "error",
                    "CHAIN_BROKEN",
                    f"batch {item.batch_id!r} references missing predecessor "
                    f"{item.prev_batch_id!r}",
                    source=source.name,
                    partition=dict(item.partition),
                    batch_id=item.batch_id,
                    evidence={
                        "prev_batch_id": item.prev_batch_id,
                        "manifest_path": item.manifest_relpath,
                    },
                )
            )
            continue
        predecessors = by_id[item.prev_batch_id]
        if len(predecessors) == 1 and predecessors[0].sequence >= item.sequence:
            findings.append(
                Finding(
                    "error",
                    "CHAIN_ORDER_INVALID",
                    f"predecessor {item.prev_batch_id!r} does not precede "
                    f"batch {item.batch_id!r}",
                    source=source.name,
                    partition=dict(item.partition),
                    batch_id=item.batch_id,
                    evidence={
                        "prev_batch_id": item.prev_batch_id,
                        "prev_sequence": predecessors[0].sequence,
                        "sequence": item.sequence,
                    },
                )
            )
    return findings
