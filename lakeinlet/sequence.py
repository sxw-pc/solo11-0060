"""Batch sequence continuity and optional manifest-chain checks."""

from __future__ import annotations

from typing import Sequence

from .models import BatchManifest, BatchSummary, Finding, SourceConfig

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


def _resolve_predecessor(
    summary: BatchSummary, by_id: dict[str, list[BatchSummary]]
) -> BatchSummary | None:
    """Return the unique declared predecessor, or None when unresolvable."""
    if summary.prev_batch_id is None:
        return None
    candidates = by_id.get(summary.prev_batch_id)
    if not candidates or len(candidates) > 1:
        # Missing predecessors are handled by the caller; a duplicated
        # batch_id makes the link ambiguous (already reported as
        # DUPLICATE_BATCH_ID), so it must not propagate anything.
        return None
    return candidates[0]


def _detect_dependency_cycles(
    summaries: Sequence[BatchSummary],
) -> list[Finding]:
    """Report every directed cycle in the prev_batch_id graph, once each.

    Detection runs independently of lateness propagation so a cycle can
    never hide behind a late root or trap the propagation walk. Each node
    declares at most one predecessor, so any repeated node along a walk
    closes exactly one cycle.
    """
    findings: list[Finding] = []
    by_source: dict[str, list[BatchSummary]] = {}
    for summary in summaries:
        by_source.setdefault(summary.source, []).append(summary)

    for source_name in sorted(by_source):
        nodes = by_source[source_name]
        by_id: dict[str, list[BatchSummary]] = {}
        for node in nodes:
            by_id.setdefault(node.batch_id, []).append(node)
        visited: set[int] = set()
        cycles: dict[tuple[str, ...], list[str]] = {}
        for node in nodes:
            if id(node) in visited:
                continue
            walk: list[BatchSummary] = [node]
            positions: dict[int, int] = {id(node): 0}
            cursor = node
            while True:
                predecessor = _resolve_predecessor(cursor, by_id)
                if predecessor is None or id(predecessor) in visited:
                    break
                if id(predecessor) in positions:
                    cycle_ids = [
                        item.batch_id for item in walk[positions[id(predecessor)] :]
                    ]
                    start = cycle_ids.index(min(cycle_ids))
                    rotated = cycle_ids[start:] + cycle_ids[:start]
                    cycles[tuple(sorted(set(cycle_ids)))] = rotated
                    break
                positions[id(predecessor)] = len(walk)
                walk.append(predecessor)
                cursor = predecessor
            visited.update(id(item) for item in walk)
        for members in sorted(cycles):
            rotated = cycles[members]
            findings.append(
                Finding(
                    "error",
                    "DEPENDENCY_CYCLE",
                    f"dependency cycle detected among {len(rotated)} batch(es): "
                    + ", ".join(repr(member) for member in members),
                    source=source_name,
                    batch_id=members[0],
                    evidence={
                        "members": list(members),
                        "cycle": rotated + [rotated[0]],
                    },
                )
            )
    return findings


def apply_inherited_lateness(
    summaries: Sequence[BatchSummary],
) -> list[Finding]:
    """Propagate lateness outcomes downstream along prev_batch_id links.

    The per-partition lateness verdict (``BatchSummary.late``) is final and
    is never changed here; this pass only marks downstream batches whose
    declared dependency chain dragged in a compromised predecessor:

    - a nearest upstream batch that is itself late is a ``late`` root cause;
    - a declared predecessor that no valid manifest provides is a
      ``missing`` (broken-chain) root cause, never a late one;
    - future-dated batches are never late, so they can transmit but never
      originate inherited lateness;
    - root-cause batches keep their own records: a late batch is never
      re-labelled as inherited;
    - dependency cycles are reported as ``DEPENDENCY_CYCLE`` and are never
      followed indefinitely.

    Every affected non-late batch is marked on its summary and reported as
    ``INHERITED_LATE_BATCH`` with the full root-to-batch path as evidence.
    """
    findings = _detect_dependency_cycles(summaries)

    by_source: dict[str, list[BatchSummary]] = {}
    for summary in summaries:
        by_source.setdefault(summary.source, []).append(summary)

    for source_name in sorted(by_source):
        nodes = by_source[source_name]
        by_id: dict[str, list[BatchSummary]] = {}
        for node in nodes:
            by_id.setdefault(node.batch_id, []).append(node)
        # Resolved root cause per node: (kind, root_batch_id, path root->node).
        memo: dict[int, tuple[str, str, tuple[str, ...]] | None] = {}

        for node in nodes:
            if node.late:
                # Root causes keep their own late record; they are never
                # re-labelled as inherited.
                continue
            if id(node) in memo:
                continue
            walk: list[BatchSummary] = [node]
            seen: set[int] = {id(node)}
            resolution: tuple[str, str, tuple[str, ...]] | None = None
            cursor = node
            while True:
                prev_id = cursor.prev_batch_id
                if prev_id is None:
                    break
                candidates = by_id.get(prev_id)
                if not candidates:
                    resolution = ("missing", prev_id, (prev_id,))
                    break
                if len(candidates) > 1:
                    break
                predecessor = candidates[0]
                if id(predecessor) in seen:
                    # Already reported by _detect_dependency_cycles.
                    break
                if predecessor.late:
                    resolution = ("late", predecessor.batch_id, (predecessor.batch_id,))
                    break
                if id(predecessor) in memo:
                    resolution = memo[id(predecessor)]
                    break
                walk.append(predecessor)
                seen.add(id(predecessor))
                cursor = predecessor

            if resolution is None:
                for item in walk:
                    memo[id(item)] = None
                continue
            kind, root_id, prefix = resolution
            chain = tuple(item.batch_id for item in reversed(walk))
            full_path = prefix + chain
            for index, item in enumerate(walk):
                memo[id(item)] = (kind, root_id, full_path[: len(full_path) - index])

        for node in nodes:
            if node.late:
                continue
            entry = memo.get(id(node))
            if entry is None:
                continue
            kind, root_id, path = entry
            node.inherited_late = True
            node.inherited_root_kind = kind
            node.inherited_root_batch_id = root_id
            node.inherited_path = path
            if kind == "late":
                message = (
                    f"batch inherits lateness from late batch {root_id!r} "
                    "through the prev_batch_id chain"
                )
            else:
                message = (
                    f"batch inherits lateness from missing predecessor "
                    f"{root_id!r} through the prev_batch_id chain"
                )
            findings.append(
                Finding(
                    "error",
                    "INHERITED_LATE_BATCH",
                    message,
                    source=node.source,
                    partition=dict(node.partition),
                    batch_id=node.batch_id,
                    evidence={
                        "root_kind": kind,
                        "root_batch_id": root_id,
                        "path": list(path),
                        "path_length": len(path) - 1,
                        "prev_batch_id": node.prev_batch_id,
                        "manifest_path": node.manifest_relpath,
                    },
                )
            )
    return findings
