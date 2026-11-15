from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from lakeinlet.manifest import load_source_manifests
from lakeinlet.models import (
    BatchManifest,
    SourceConfig,
    build_plan_id,
    partition_label,
)
from lakeinlet.sequence import analyze_sequences, find_sequence_findings


def _source(policy: str = "contiguous") -> SourceConfig:
    return SourceConfig(
        name="orders",
        partition_keys=("dt", "region"),
        allowed_lateness_seconds=600,
        future_tolerance_seconds=60,
        sequence_policy=policy,
    )


def _manifest(
    tmp_path,
    sequence: int,
    partition: dict[str, str],
    *,
    batch_id: str | None = None,
    prev_batch_id: str | None = None,
) -> BatchManifest:
    batch_id = batch_id or f"orders-{sequence}"
    return BatchManifest(
        manifest_path=tmp_path / f"{sequence:04d}.yaml",
        manifest_relpath=f"_manifests/orders/{sequence:04d}.yaml",
        batch_id=batch_id,
        source="orders",
        sequence=sequence,
        partition=dict(partition),
        declared_at=datetime(2025, 3, 1, 1, sequence, tzinfo=UTC),
        files=(),
        prev_batch_id=prev_batch_id,
    )


EU = {"dt": "2025-03-01", "region": "eu"}
US = {"dt": "2025-03-01", "region": "us"}


def test_clean_contiguous_sequence_has_no_findings(demo_config):
    source = demo_config.source_map["orders"]
    manifests, _ = load_source_manifests(demo_config, source)
    assert find_sequence_findings(manifests, source) == []

def test_missing_sequence_number_is_detected(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    findings = find_sequence_findings(manifests, source)
    gap = next(item for item in findings if item.code == "SEQUENCE_GAP")
    assert gap.evidence["missing_count"] == 1
    assert gap.evidence["missing_samples"] == [2]
    assert gap.evidence["range"] == [1, 6]

def test_gapped_policy_allows_missing_sequence_numbers(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    gapped = replace(source, sequence_policy="gapped")
    findings = find_sequence_findings(manifests, gapped)
    assert not any(item.code == "SEQUENCE_GAP" for item in findings)
    assert any(item.code == "CHAIN_BROKEN" for item in findings)

def test_missing_predecessor_is_chain_broken(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    findings = find_sequence_findings(manifests, source)
    chain = next(item for item in findings if item.code == "CHAIN_BROKEN")
    assert chain.batch_id == "orders-future"
    assert chain.evidence["prev_batch_id"] == "orders-does-not-exist"

def test_duplicate_sequence_is_not_reported_here(broken_config):
    """Duplicate sequence numbers are the duplicate auditor's responsibility."""
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    findings = find_sequence_findings(manifests, source)
    assert not any(item.code == "DUPLICATE_SEQUENCE" for item in findings)

def test_chain_order_is_checked(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    target = next(item for item in manifests if item.sequence == 3)
    target.prev_batch_id = "orders-broken-files"
    findings = find_sequence_findings(manifests, source)
    order = next(item for item in findings if item.code == "CHAIN_ORDER_INVALID")
    assert order.batch_id == "orders-late"
    assert order.evidence["prev_sequence"] == 6


# ---------------------------------------------------------------------------
# Completion plans
# ---------------------------------------------------------------------------


def test_broken_fixture_emits_gap_plan_with_partition_and_chain(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    findings, plans = analyze_sequences(manifests, source)

    assert any(item.code == "SEQUENCE_GAP" for item in findings)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.source == "orders"
    assert plan.gap_range == (2, 2)
    assert plan.partition == {"dt": "2025-03-01", "region": "eu"}
    assert plan.needs_manual_decision is False

    candidate = plan.candidates[0]
    assert candidate.sequence == 2
    assert candidate.prev_sequence == 1
    assert candidate.next_sequence == 3
    assert len(candidate.partition_options) == 1
    option = candidate.partition_options[0]
    assert option.basis == "both_neighbors"
    assert option.needs_manual_decision is False
    # The missing batch must chain to the existing same-partition predecessor.
    assert candidate.suggested_prev_batch_id == "orders-early"

    # The CHAIN_BROKEN finding attaches its reusable chain info to this plan.
    assert len(plan.chain_requests) == 1
    request = plan.chain_requests[0]
    assert request.finding_code == "CHAIN_BROKEN"
    assert request.batch_id == "orders-future"
    assert request.declared_prev_batch_id == "orders-does-not-exist"
    assert request.partition == plan.partition
    assert request.manifest_path.endswith("05_future.yaml")


def test_plan_id_is_stable_across_replays(tmp_path):
    source = _source()
    manifests = [
        _manifest(tmp_path, 1, EU),
        _manifest(tmp_path, 3, EU),
    ]
    findings_one, plans_one = analyze_sequences(manifests, source)
    _findings_two, plans_two = analyze_sequences(list(manifests), source)

    assert findings_one and plans_one and len(plans_one) == 1
    assert [plan.plan_id for plan in plans_one] == [
        plan.plan_id for plan in plans_two
    ]
    expected = build_plan_id("orders", (2, 2), partition_label(EU))
    assert plans_one[0].plan_id == expected


def test_plan_id_changes_with_partition_identity(tmp_path):
    source = _source()
    eu_gap = analyze_sequences(
        [_manifest(tmp_path, 1, EU), _manifest(tmp_path, 3, EU)], source
    )[1]
    us_gap = analyze_sequences(
        [_manifest(tmp_path, 1, US), _manifest(tmp_path, 3, US)], source
    )[1]
    assert eu_gap[0].plan_id != us_gap[0].plan_id


def test_cross_partition_gap_becomes_manual_multicandidate(tmp_path):
    source = _source()
    manifests = [
        _manifest(tmp_path, 1, EU),
        _manifest(tmp_path, 3, US),
    ]
    findings, plans = analyze_sequences(manifests, source)

    assert any(item.code == "SEQUENCE_GAP" for item in findings)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.needs_manual_decision is True
    assert plan.partition is None
    assert plan.uncertainty_reason is not None
    assert "不一致" in plan.uncertainty_reason

    candidate = plan.candidates[0]
    labels = {
        partition_label(option.partition)
        for option in candidate.partition_options
    }
    assert labels == {partition_label(EU), partition_label(US)}
    assert all(
        option.needs_manual_decision and option.basis == "ambiguous_neighbors"
        for option in candidate.partition_options
    )
    # No blind chain suggestion while the partition is undecided.
    assert candidate.suggested_prev_batch_id is None
    assert candidate.chain_note is not None

    # The ambiguous plan identity covers both candidate partitions.
    expected = build_plan_id(
        "orders",
        (2, 2),
        "|".join(sorted(labels)),
    )
    assert plan.plan_id == expected


def test_multiple_gap_runs_each_get_their_own_plan(tmp_path):
    source = _source()
    manifests = [
        _manifest(tmp_path, 1, EU),
        _manifest(tmp_path, 3, EU),
        _manifest(tmp_path, 5, EU),
    ]
    _findings, plans = analyze_sequences(manifests, source)
    assert [plan.gap_range for plan in plans] == [(2, 2), (4, 4)]
    assert len({plan.plan_id for plan in plans}) == 2
    # First candidate chains to seq 1; the later gap chains to seq 3.
    assert plans[0].candidates[0].suggested_prev_batch_id == "orders-1"
    assert plans[1].candidates[0].suggested_prev_batch_id == "orders-3"


def test_chain_only_plan_when_broken_link_has_no_gap(tmp_path):
    source = _source()
    manifests = [
        _manifest(tmp_path, 1, EU, batch_id="orders-1"),
        _manifest(
            tmp_path,
            2,
            EU,
            batch_id="orders-2",
            prev_batch_id="orders-ghost",
        ),
    ]
    findings, plans = analyze_sequences(manifests, source)
    assert any(item.code == "CHAIN_BROKEN" for item in findings)
    assert len(plans) == 1
    plan = plans[0]
    assert plan.gap_range is None
    assert plan.partition == EU
    assert plan.candidates == ()
    assert len(plan.chain_requests) == 1
    assert plan.chain_requests[0].suggested_prev_batch_id == "orders-1"
    assert plan.plan_id == build_plan_id("orders", None, partition_label(EU))


def test_chain_order_invalid_requests_same_partition_predecessor(tmp_path):
    source = _source()
    manifests = [
        _manifest(tmp_path, 1, EU, batch_id="orders-1"),
        _manifest(tmp_path, 2, EU, batch_id="orders-2"),
        _manifest(
            tmp_path,
            3,
            EU,
            batch_id="orders-3",
            prev_batch_id="orders-1",
        ),
    ]
    findings, plans = analyze_sequences(manifests, source)
    # orders-1 precedes orders-3, so this is valid: flip it backwards instead.
    assert not any(item.code == "CHAIN_ORDER_INVALID" for item in findings)

    manifests[-1].prev_batch_id = "orders-2"  # still valid; now force inversion
    target = manifests[1]
    target.prev_batch_id = "orders-3"  # predecessor seq 3 >= current seq 2
    findings, plans = analyze_sequences(manifests, source)
    assert any(item.code == "CHAIN_ORDER_INVALID" for item in findings)
    request = next(
        item
        for plan in plans
        for item in plan.chain_requests
        if item.finding_code == "CHAIN_ORDER_INVALID"
    )
    assert request.batch_id == "orders-2"
    assert request.suggested_prev_batch_id == "orders-1"


def test_never_suggests_cross_partition_predecessor(tmp_path):
    source = _source()
    manifests = [
        _manifest(tmp_path, 1, EU, batch_id="orders-eu-1"),
        _manifest(
            tmp_path,
            2,
            US,
            batch_id="orders-us-2",
            prev_batch_id="orders-eu-1",
        ),
    ]
    _findings, plans = analyze_sequences(manifests, source)
    for plan in plans:
        for request in plan.chain_requests:
            # No same-partition predecessor exists for the us batch, so the
            # suggestion must be None rather than the eu batch.
            assert request.partition == US
            assert request.suggested_prev_batch_id is None
        for candidate in plan.candidates:
            assert candidate.suggested_prev_batch_id is None


def test_gapped_policy_produces_no_gap_plan(tmp_path):
    source = _source(policy="gapped")
    manifests = [
        _manifest(tmp_path, 1, EU),
        _manifest(tmp_path, 3, EU),
    ]
    findings, plans = analyze_sequences(manifests, source)
    assert not any(item.code == "SEQUENCE_GAP" for item in findings)
    assert plans == []


def test_find_sequence_findings_backwards_compatible(broken_config):
    source = broken_config.source_map["orders"]
    manifests, _ = load_source_manifests(broken_config, source)
    findings = find_sequence_findings(manifests, source)
    assert {item.code for item in findings} >= {
        "SEQUENCE_GAP",
        "CHAIN_BROKEN",
    }
