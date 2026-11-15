"""Audit orchestration: combine all checks into a reproducible result."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

from .audit_result import AuditResult
from .convergence import apply_convergence, converge_records
from .duplicates import find_duplicate_findings
from .manifest import load_source_manifests
from .models import AuditConfig, BatchScan, Finding
from .summaries import SourceSummary
from .scanner import scan_batch
from .sequence import find_sequence_findings
from .watermark import evaluate_watermarks, summarize_batches


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _dedupe_findings(findings: list[Finding]) -> list[Finding]:
    seen: set[str] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = _canonical(finding.to_dict())
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return sorted(unique, key=lambda item: item.sort_key())


def _status(findings: list[Finding]) -> str:
    if any(item.severity == "error" for item in findings):
        return "failed"
    if any(item.severity == "warning" for item in findings):
        return "passed_with_warnings"
    return "passed"


def _fingerprint(
    result: AuditResult, config: AuditConfig, scans: list[BatchScan]
) -> tuple[str, str]:
    config_digest = hashlib.sha256(
        _canonical(config.semantic_dict()).encode("utf-8")
    ).hexdigest()
    payload = {
        "result": result.to_dict(),
        "config": config.semantic_dict(),
        "inputs": [
            {
                "manifest": scan.manifest.to_dict(),
                "files": [
                    {
                        "path": file_scan.relative_path,
                        "exists": file_scan.exists,
                        "actual_sha256": file_scan.actual_sha256,
                        "actual_bytes": file_scan.actual_bytes,
                        "row_count": file_scan.row_count,
                    }
                    for file_scan in scan.files
                ],
            }
            for scan in scans
        ],
    }
    run_id = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]
    return run_id, config_digest


def _source_summaries(
    config: AuditConfig,
    scans: list[BatchScan],
    findings: list[Finding],
) -> tuple[SourceSummary, ...]:
    scans_by_source = Counter(scan.source for scan in scans)
    files_by_source = Counter(scan.source for scan in scans for _ in scan.files)
    records_by_source = Counter()
    raw_records_by_source = Counter()
    for scan in scans:
        records_by_source[scan.source] += scan.record_count
        raw_records_by_source[scan.source] += scan.raw_record_count
    finding_counts = Counter(
        (finding.source, finding.severity) for finding in findings
    )
    summaries = []
    for source in config.sources:
        summaries.append(
            SourceSummary(
                name=source.name,
                partition_keys=source.partition_keys,
                allowed_lateness_seconds=source.allowed_lateness_seconds,
                future_tolerance_seconds=source.future_tolerance_seconds,
                sequence_policy=source.sequence_policy,
                manifest_count=scans_by_source[source.name],
                file_count=files_by_source[source.name],
                record_count=records_by_source[source.name],
                raw_record_count=raw_records_by_source[source.name],
                error_count=finding_counts[(source.name, "error")],
                warning_count=finding_counts[(source.name, "warning")],
            )
        )
    return tuple(summaries)


def run_audit(config: AuditConfig) -> AuditResult:
    """Execute a deterministic, read-only audit of the configured lake."""
    findings: list[Finding] = []
    scans: list[BatchScan] = []
    manifests_by_source = {}

    for source in config.sources:
        manifests, load_findings = load_source_manifests(config, source)
        findings.extend(load_findings)
        manifests_by_source[source.name] = manifests
        for manifest in manifests:
            scan = scan_batch(manifest, source, config.lake_root)
            scans.append(scan)
            findings.extend(scan.issues)

    # Collapse duplicate record ids per source once; duplicate findings,
    # watermark inputs, and summary counts all read the same convergence set.
    convergence = converge_records(scans)
    apply_convergence(scans, convergence)
    findings.extend(find_duplicate_findings(scans, convergence))
    for source in config.sources:
        findings.extend(
            find_sequence_findings(manifests_by_source[source.name], source)
        )

    summaries = summarize_batches(scans)
    watermarks, watermark_findings = evaluate_watermarks(
        summaries, config.source_map, config.as_of
    )
    findings.extend(watermark_findings)
    findings = _dedupe_findings(findings)

    source_summaries = _source_summaries(config, scans, findings)
    result = AuditResult(
        project=config.project,
        as_of=config.as_of,
        sources=source_summaries,
        findings=findings,
        watermarks=watermarks,
        batches=tuple(summaries),
        manifest_count=len(scans),
        file_count=sum(len(scan.files) for scan in scans),
        record_count=sum(scan.record_count for scan in scans),
        raw_record_count=sum(scan.raw_record_count for scan in scans),
        config_digest="",
        status=_status(findings),
        record_convergences=convergence.duplicate_accounts,
    )
    run_id, config_digest = _fingerprint(result, config, scans)
    result.config_digest = config_digest
    result.run_id = run_id
    return result

