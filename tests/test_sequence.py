from __future__ import annotations

from dataclasses import replace

from lakeinlet.manifest import load_source_manifests
from lakeinlet.sequence import find_sequence_findings

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
    assert gap.evidence["range"] == [1, 9]

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
