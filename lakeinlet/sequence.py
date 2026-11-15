"""Batch sequence continuity and optional manifest-chain checks."""

from __future__ import annotations

from typing import Sequence

from .models import (
    PARTITION_BASIS_AMBIGUOUS,
    PARTITION_BASIS_BOTH_NEIGHBORS,
    PARTITION_BASIS_SINGLE_NEIGHBOR,
    BatchManifest,
    ChainRequest,
    CompletionPlan,
    Finding,
    PartitionOption,
    SequenceCandidate,
    SourceConfig,
    build_plan_id,
    partition_label,
)

_MAX_MISSING_SAMPLES = 100


def find_sequence_findings(
    manifests: Sequence[BatchManifest], source: SourceConfig
) -> list[Finding]:
    """Check contiguous sequence policy and declared predecessor links."""
    findings, _plans = analyze_sequences(manifests, source)
    return findings


def analyze_sequences(
    manifests: Sequence[BatchManifest], source: SourceConfig
) -> tuple[list[Finding], list[CompletionPlan]]:
    """Return sequence/chain findings plus actionable completion plans.

    Findings stay byte-compatible with the historical report; the plans add a
    second, operational layer describing which sequence numbers to backfill,
    which partition each candidate should land in, and how to repair the
    ``prev_batch_id`` chain afterwards.
    """
    findings: list[Finding] = []
    ordered = sorted(
        manifests, key=lambda item: (item.sequence, item.manifest_relpath)
    )
    sequences = sorted({item.sequence for item in ordered})
    gap_runs: list[list[int]] = []
    if source.sequence_policy == "contiguous" and sequences:
        lowest, highest = sequences[0], sequences[-1]
        present = set(sequences)
        total_missing = (highest - lowest + 1) - len(sequences)
        if total_missing > 0:
            missing_samples: list[int] = []
            current_run: list[int] = []
            for candidate in range(lowest, highest + 1):
                if candidate in present:
                    if current_run:
                        gap_runs.append(current_run)
                        current_run = []
                    continue
                # Gap-run discovery always covers the full range; only the
                # sample list embedded in the finding evidence is capped.
                if len(missing_samples) < _MAX_MISSING_SAMPLES:
                    missing_samples.append(candidate)
                current_run.append(candidate)
            if current_run:
                gap_runs.append(current_run)
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

    chain_requests: list[ChainRequest] = []
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
            chain_requests.append(
                _chain_request(
                    "CHAIN_BROKEN",
                    item,
                    ordered,
                    note=(
                        f"声明的前驱批次 {item.prev_batch_id!r} 不存在；"
                        "prev_batch_id 仅在同一分区内连续有效，不能跨分区接链，"
                        "请在同分区内按序列补齐后重接前驱。"
                    ),
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
            chain_requests.append(
                _chain_request(
                    "CHAIN_ORDER_INVALID",
                    item,
                    ordered,
                    note=(
                        f"声明的前驱 {item.prev_batch_id!r} 序列 "
                        f"{predecessors[0].sequence} 不早于当前批次序列 "
                        f"{item.sequence}，链序颠倒；请改指向同分区内紧邻的"
                        "低序列批次，跨分区不可接链。"
                    ),
                )
            )

    plans = _build_plans(source.name, ordered, gap_runs, chain_requests)
    return findings, plans


def _neighbor_manifests(
    ordered: Sequence[BatchManifest], sequence_value: int
) -> tuple[BatchManifest | None, BatchManifest | None]:
    """Nearest present manifests strictly below / above a sequence number."""
    lower = [item for item in ordered if item.sequence < sequence_value]
    upper = [item for item in ordered if item.sequence > sequence_value]
    prev_item = max(lower, key=lambda item: item.sequence) if lower else None
    next_item = min(upper, key=lambda item: item.sequence) if upper else None
    return prev_item, next_item


def _partition_options_for_gap(
    ordered: Sequence[BatchManifest], sequence_value: int
) -> tuple[PartitionOption, ...]:
    """Suggest partition(s) for one missing sequence from adjacent manifests.

    A partition may only be copied when the manifests immediately surrounding
    the gap agree on it. When neighboring manifests carry different partition
    key values, neither side can be copied blindly: every distinct partition
    becomes a manual-decision candidate with the uncertainty spelled out.
    """
    prev_item, next_item = _neighbor_manifests(ordered, sequence_value)
    neighbor_partitions: dict[str, dict[str, str]] = {}
    for neighbor in (prev_item, next_item):
        if neighbor is not None:
            neighbor_partitions.setdefault(
                neighbor.partition_label, dict(neighbor.partition)
            )
    if not neighbor_partitions:
        return (
            PartitionOption(
                partition={},
                basis=PARTITION_BASIS_SINGLE_NEIGHBOR,
                needs_manual_decision=True,
                uncertainty_reason=(
                    "缺口两侧均无已存在清单，无法从相邻 manifest 推断分区，"
                    "必须人工指定。"
                ),
            ),
        )
    if len(neighbor_partitions) == 1:
        partition = next(iter(neighbor_partitions.values()))
        if prev_item is not None and next_item is not None:
            basis = PARTITION_BASIS_BOTH_NEIGHBORS
            reason = None
        else:
            basis = PARTITION_BASIS_SINGLE_NEIGHBOR
            side = "前序" if prev_item is not None else "后序"
            reason = f"缺口仅一侧存在 {side} manifest，分区按该侧建议，需人工确认。"
        return (
            PartitionOption(
                partition=partition,
                basis=basis,
                needs_manual_decision=False,
                uncertainty_reason=reason,
            ),
        )
    reason = (
        "缺口前后相邻 manifest 的分区键值不一致"
        f"（候选：{'; '.join(sorted(neighbor_partitions))}），不能照抄前一个分区，"
        "必须人工判定每个缺失 sequence 的归属分区。"
    )
    return tuple(
        PartitionOption(
            partition=partition,
            basis=PARTITION_BASIS_AMBIGUOUS,
            needs_manual_decision=True,
            uncertainty_reason=reason,
        )
        for _label, partition in sorted(neighbor_partitions.items())
    )


def _same_partition_predecessor(
    ordered: Sequence[BatchManifest], item: BatchManifest
) -> BatchManifest | None:
    """Nearest present batch below ``item`` inside the same partition."""
    candidates = [
        other
        for other in ordered
        if other.sequence < item.sequence
        and other.partition_label == item.partition_label
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda other: other.sequence)


def _chain_request(
    code: str,
    item: BatchManifest,
    ordered: Sequence[BatchManifest],
    note: str,
) -> ChainRequest:
    predecessor = _same_partition_predecessor(ordered, item)
    if predecessor is None:
        suggested = None
        full_note = (
            note + " 同分区内没有更低序列的已存在批次，需先补齐该分区的缺口。"
        )
    else:
        suggested = predecessor.batch_id
        full_note = note
    return ChainRequest(
        finding_code=code,
        batch_id=item.batch_id,
        declared_prev_batch_id=item.prev_batch_id,
        partition=dict(item.partition),
        manifest_path=item.manifest_relpath,
        sequence=item.sequence,
        suggested_prev_batch_id=suggested,
        note=full_note,
    )


def _gap_candidate(
    ordered: Sequence[BatchManifest],
    sequence_value: int,
    options: tuple[PartitionOption, ...],
) -> SequenceCandidate:
    prev_item, next_item = _neighbor_manifests(ordered, sequence_value)
    ambiguous = any(option.needs_manual_decision for option in options)
    if ambiguous:
        return SequenceCandidate(
            sequence=sequence_value,
            prev_sequence=prev_item.sequence if prev_item else None,
            next_sequence=next_item.sequence if next_item else None,
            partition_options=options,
            suggested_prev_batch_id=None,
            chain_note=(
                "分区归属尚需人工决策；prev_batch_id 仅在同一分区内连续有效，"
                "确定分区并补齐批次后再在同分区内接链。"
            ),
        )
    partition = options[0].partition
    same_partition_lower = [
        other
        for other in ordered
        if other.sequence < sequence_value
        and other.partition_label == partition_label(partition)
    ]
    if same_partition_lower:
        predecessor = max(same_partition_lower, key=lambda other: other.sequence)
        suggested = predecessor.batch_id
        note: str | None = None
    else:
        suggested = None
        note = (
            "同分区内没有更低序列的已存在批次；该候选是本分区的链头，"
            "补齐后由后续批次依次回接，不得跨分区接链。"
        )
    return SequenceCandidate(
        sequence=sequence_value,
        prev_sequence=prev_item.sequence if prev_item else None,
        next_sequence=next_item.sequence if next_item else None,
        partition_options=options,
        suggested_prev_batch_id=suggested,
        chain_note=note,
    )


def _build_plans(
    source_name: str,
    ordered: Sequence[BatchManifest],
    gap_runs: Sequence[Sequence[int]],
    chain_requests: Sequence[ChainRequest],
) -> list[CompletionPlan]:
    # Gap plans are kept in their own list (a partition can host several
    # distinct gap intervals). Chain-only plans are keyed by partition label.
    # The frozen CompletionPlan objects are built once everything is attached.
    gap_records: list[dict[str, object]] = []
    gap_record_by_partition: dict[str, dict[str, object]] = {}
    chain_only: dict[str, dict[str, object]] = {}

    for run in gap_runs:
        run_range = (run[0], run[-1])
        first_options = _partition_options_for_gap(ordered, run[0])
        ambiguous = any(
            option.needs_manual_decision for option in first_options
        )
        candidates = tuple(
            _gap_candidate(
                ordered,
                sequence_value,
                _partition_options_for_gap(ordered, sequence_value),
            )
            for sequence_value in run
        )
        if ambiguous:
            identity = "|".join(
                sorted(
                    partition_label(option.partition) for option in first_options
                )
            )
            gap_records.append(
                {
                    "plan_id": build_plan_id(source_name, run_range, identity),
                    "gap_range": run_range,
                    "partition": None,
                    "label": identity,
                    "needs_manual_decision": True,
                    "uncertainty_reason": first_options[0].uncertainty_reason,
                    "candidates": candidates,
                    "requests": [],
                }
            )
            continue
        partition = dict(first_options[0].partition)
        label = partition_label(partition)
        record = {
            "plan_id": build_plan_id(source_name, run_range, label),
            "gap_range": run_range,
            "partition": partition,
            "label": label,
            "needs_manual_decision": False,
            "uncertainty_reason": first_options[0].uncertainty_reason,
            "candidates": candidates,
            "requests": [],
        }
        gap_records.append(record)
        # Chain requests attach to the earliest gap plan for the partition.
        gap_record_by_partition.setdefault(label, record)

    # Attach chain repairs to a concrete plan for the same partition;
    # otherwise emit a chain-only plan (gap_range=None) for that partition.
    for request in chain_requests:
        label = partition_label(request.partition)
        record = gap_record_by_partition.get(label) or chain_only.get(label)
        if record is None:
            record = {
                "plan_id": build_plan_id(source_name, None, label),
                "gap_range": None,
                "partition": dict(request.partition),
                "label": label,
                "needs_manual_decision": False,
                "uncertainty_reason": None,
                "candidates": (),
                "requests": [],
            }
            chain_only[label] = record
        if request not in record["requests"]:
            record["requests"].append(request)  # type: ignore[union-attr]

    all_records = gap_records + list(chain_only.values())
    plans: list[CompletionPlan] = []
    for record in all_records:
        requests = tuple(
            sorted(
                record["requests"],  # type: ignore[arg-type]
                key=lambda item: (
                    item.sequence,
                    item.batch_id,
                    item.finding_code,
                ),
            )
        )
        plans.append(
            CompletionPlan(
                plan_id=record["plan_id"],  # type: ignore[arg-type]
                source=source_name,
                gap_range=record["gap_range"],  # type: ignore[arg-type]
                partition=record["partition"],  # type: ignore[arg-type]
                needs_manual_decision=record["needs_manual_decision"],  # type: ignore[arg-type]
                uncertainty_reason=record["uncertainty_reason"],  # type: ignore[arg-type]
                candidates=record["candidates"],  # type: ignore[arg-type]
                chain_requests=requests,
            )
        )
    plans.sort(
        key=lambda plan: (
            plan.source,
            plan.gap_range[0] if plan.gap_range is not None else 10**12,
            plan.gap_range[1] if plan.gap_range is not None else 10**12,
            plan.partition_label,
            plan.plan_id,
        )
    )
    return plans
