"""Tests for inherited-lateness propagation along prev_batch_id links."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import yaml

from lakeinlet.config import load_config
from lakeinlet.ledger import initialize_ledger, read_run, verify_readable, write_run
from lakeinlet.models import BatchSummary
from lakeinlet.report_html import render_html
from lakeinlet.report_json import render_json
from lakeinlet.report_md import render_markdown
from lakeinlet.rules import run_audit
from lakeinlet.sequence import apply_inherited_lateness
from lakeinlet.timeutil import parse_utc


def _summary(
    batch_id: str,
    sequence: int,
    *,
    source: str = "orders",
    region: str = "eu",
    prev: str | None = None,
    late: bool = False,
) -> BatchSummary:
    return BatchSummary(
        source=source,
        batch_id=batch_id,
        sequence=sequence,
        partition={"dt": "2025-03-01", "region": region},
        declared_at=parse_utc(f"2025-03-01T01:{sequence % 60:02d}:00Z"),
        manifest_relpath=f"_manifests/{source}/{sequence:02d}_{batch_id}.yaml",
        file_count=1,
        record_count=1,
        event_time_min=None,
        event_time_max=None,
        prev_batch_id=prev,
        late=late,
    )


def _by_id(summaries):
    return {item.batch_id: item for item in summaries}


# ---------------------------------------------------------------------------
# Unit tests for apply_inherited_lateness
# ---------------------------------------------------------------------------


def test_direct_and_transitive_inheritance_with_path_evidence():
    summaries = [
        _summary("root", 1, late=True),
        _summary("child", 2, prev="root"),
        _summary("grandchild", 3, prev="child"),
        _summary("clean", 4),
    ]
    findings = apply_inherited_lateness(summaries)
    by_batch = {finding.batch_id: finding for finding in findings}
    assert set(by_batch) == {"child", "grandchild"}
    child = by_batch["child"]
    assert child.code == "INHERITED_LATE_BATCH"
    assert child.severity == "error"
    assert child.evidence["root_kind"] == "late"
    assert child.evidence["root_batch_id"] == "root"
    assert child.evidence["path"] == ["root", "child"]
    assert child.evidence["path_length"] == 1
    grandchild = by_batch["grandchild"]
    assert grandchild.evidence["root_batch_id"] == "root"
    assert grandchild.evidence["path"] == ["root", "child", "grandchild"]
    assert grandchild.evidence["path_length"] == 2

    batches = _by_id(summaries)
    assert batches["root"].inherited_late is False
    assert batches["child"].inherited_late is True
    assert batches["child"].inherited_root_kind == "late"
    assert batches["child"].inherited_root_batch_id == "root"
    assert batches["child"].inherited_path == ("root", "child")
    assert batches["grandchild"].inherited_path == ("root", "child", "grandchild")
    assert batches["clean"].inherited_late is False


def test_missing_predecessor_uses_missing_branch_not_late():
    summaries = [
        _summary("child", 1, prev="ghost"),
        _summary("grandchild", 2, prev="child"),
    ]
    findings = apply_inherited_lateness(summaries)
    by_batch = {finding.batch_id: finding for finding in findings}
    assert set(by_batch) == {"child", "grandchild"}
    assert by_batch["child"].evidence["root_kind"] == "missing"
    assert by_batch["child"].evidence["root_batch_id"] == "ghost"
    assert by_batch["child"].evidence["path"] == ["ghost", "child"]
    assert by_batch["grandchild"].evidence["root_kind"] == "missing"
    assert by_batch["grandchild"].evidence["path"] == ["ghost", "child", "grandchild"]
    batches = _by_id(summaries)
    assert batches["child"].inherited_root_kind == "missing"
    assert batches["grandchild"].inherited_root_batch_id == "ghost"


def test_late_root_keeps_own_record_and_is_not_relabelled():
    summaries = [
        _summary("late-root", 1, prev="ghost", late=True),
        _summary("dependent", 2, prev="late-root"),
    ]
    findings = apply_inherited_lateness(summaries)
    inherited = [f for f in findings if f.code == "INHERITED_LATE_BATCH"]
    assert [finding.batch_id for finding in inherited] == ["dependent"]
    assert inherited[0].evidence["root_kind"] == "late"
    assert inherited[0].evidence["root_batch_id"] == "late-root"
    late_root = _by_id(summaries)["late-root"]
    assert late_root.late is True
    assert late_root.inherited_late is False
    assert late_root.inherited_path == ()


def test_on_time_chain_has_no_inherited_marks():
    summaries = [
        _summary("a", 1),
        _summary("b", 2, prev="a"),
        _summary("c", 3, prev="b"),
    ]
    assert apply_inherited_lateness(summaries) == []
    assert all(not item.inherited_late for item in summaries)


def test_inheritance_crosses_partitions_within_a_source():
    summaries = [
        _summary("eu-root", 1, region="eu", late=True),
        _summary("us-child", 2, region="us", prev="eu-root"),
    ]
    findings = apply_inherited_lateness(summaries)
    inherited = [f for f in findings if f.code == "INHERITED_LATE_BATCH"]
    assert len(inherited) == 1
    assert inherited[0].batch_id == "us-child"
    assert inherited[0].partition == {"dt": "2025-03-01", "region": "us"}
    assert inherited[0].evidence["path"] == ["eu-root", "us-child"]


def test_cross_source_predecessor_is_treated_as_missing():
    summaries = [
        _summary("other-batch", 1, source="clicks", late=True),
        _summary("mine", 2, source="orders", prev="other-batch"),
    ]
    findings = apply_inherited_lateness(summaries)
    inherited = [f for f in findings if f.code == "INHERITED_LATE_BATCH"]
    assert len(inherited) == 1
    assert inherited[0].batch_id == "mine"
    assert inherited[0].evidence["root_kind"] == "missing"
    assert inherited[0].evidence["path"] == ["other-batch", "mine"]


def test_ambiguous_duplicate_predecessor_does_not_propagate():
    summaries = [
        _summary("dup", 1, late=True),
        _summary("dup", 2),
        _summary("child", 3, prev="dup"),
    ]
    findings = apply_inherited_lateness(summaries)
    assert not any(f.code == "INHERITED_LATE_BATCH" for f in findings)
    assert not any(f.code == "DEPENDENCY_CYCLE" for f in findings)
    assert _by_id(summaries)["child"].inherited_late is False


def test_cycle_is_reported_once_and_does_not_hang():
    summaries = [
        _summary("cyc-a", 1, prev="cyc-b"),
        _summary("cyc-b", 2, prev="cyc-a"),
        _summary("downstream", 3, prev="cyc-a"),
    ]
    findings = apply_inherited_lateness(summaries)
    cycles = [f for f in findings if f.code == "DEPENDENCY_CYCLE"]
    assert len(cycles) == 1
    assert cycles[0].severity == "error"
    assert cycles[0].evidence["members"] == ["cyc-a", "cyc-b"]
    assert cycles[0].evidence["cycle"] == ["cyc-a", "cyc-b", "cyc-a"]
    # No late/missing root is reachable, so nothing is marked inherited.
    assert not any(f.code == "INHERITED_LATE_BATCH" for f in findings)
    assert all(not item.inherited_late for item in summaries)


def test_self_loop_is_reported_as_cycle():
    summaries = [_summary("selfish", 1, prev="selfish")]
    findings = apply_inherited_lateness(summaries)
    cycles = [f for f in findings if f.code == "DEPENDENCY_CYCLE"]
    assert len(cycles) == 1
    assert cycles[0].evidence["members"] == ["selfish"]
    assert cycles[0].evidence["cycle"] == ["selfish", "selfish"]


def test_cycle_with_late_member_still_reports_and_marks_dependents():
    summaries = [
        _summary("late-one", 1, prev="peer", late=True),
        _summary("peer", 2, prev="late-one"),
    ]
    findings = apply_inherited_lateness(summaries)
    cycles = [f for f in findings if f.code == "DEPENDENCY_CYCLE"]
    assert len(cycles) == 1
    assert cycles[0].evidence["members"] == ["late-one", "peer"]
    inherited = [f for f in findings if f.code == "INHERITED_LATE_BATCH"]
    assert [finding.batch_id for finding in inherited] == ["peer"]
    assert inherited[0].evidence["root_batch_id"] == "late-one"
    assert _by_id(summaries)["late-one"].inherited_late is False


def test_apply_is_deterministic_for_same_input():
    def build():
        return [
            _summary("root", 1, late=True),
            _summary("mid", 2, prev="root"),
            _summary("leaf", 3, prev="mid"),
            _summary("orphan", 4, prev="ghost"),
        ]

    first = apply_inherited_lateness(build())
    second = apply_inherited_lateness(build())
    assert [finding.to_dict() for finding in first] == [
        finding.to_dict() for finding in second
    ]


# ---------------------------------------------------------------------------
# End-to-end tests with a synthetic lake on disk
# ---------------------------------------------------------------------------

_EU = {"dt": "2025-03-01", "region": "eu"}
_US = {"dt": "2025-03-01", "region": "us"}
_AP = {"dt": "2025-03-01", "region": "ap"}


def _write_batch(
    lake: Path,
    batch_id: str,
    *,
    sequence: int,
    partition: dict[str, str],
    declared_at: str,
    events: list[str],
    prev_batch_id: str | None = None,
) -> None:
    source = "orders"
    manifest_dir = lake / "_manifests" / source
    manifest_dir.mkdir(parents=True, exist_ok=True)
    data_dir = lake / "data" / source
    data_dir.mkdir(parents=True, exist_ok=True)
    header = ["record_id", "event_time", *partition]
    rows = [
        ",".join([f"{batch_id}-{index}", event, *partition.values()])
        for index, event in enumerate(events, start=1)
    ]
    content = "\n".join([",".join(header), *rows]) + "\n"
    (data_dir / f"{batch_id}.csv").write_text(content, encoding="utf-8")
    manifest = {
        "manifest_version": 1,
        "batch_id": batch_id,
        "source": source,
        "sequence": sequence,
        "partition": dict(partition),
        "declared_at": declared_at,
        "files": [
            {
                "path": f"data/{source}/{batch_id}.csv",
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "bytes": len(content.encode("utf-8")),
                "rows": len(rows),
                "event_time_min": min(events) if events else None,
                "event_time_max": max(events) if events else None,
            }
        ],
    }
    if prev_batch_id is not None:
        manifest["prev_batch_id"] = prev_batch_id
    (manifest_dir / f"{batch_id}.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )


def _write_config(tmp_path: Path, lake: Path, name: str = "config.yaml") -> Path:
    config = {
        "project": "inheritance-fixture",
        "lake_root": lake.as_posix(),
        "as_of": "2025-03-01T06:00:00Z",
        "sources": [
            {
                "name": "orders",
                "partition_keys": ["dt", "region"],
                "allowed_lateness_seconds": 600,
                "future_tolerance_seconds": 60,
                "sequence_policy": "gapped",
            }
        ],
    }
    path = tmp_path / name
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _build_inheritance_lake(lake: Path) -> None:
    # batch-b is the per-partition late root cause in region=eu.
    _write_batch(
        lake, "batch-a", sequence=1, partition=_EU,
        declared_at="2025-03-01T01:00:00Z",
        events=["2025-03-01T00:50:00Z", "2025-03-01T01:00:00Z"],
    )
    _write_batch(
        lake, "batch-b", sequence=2, partition=_EU,
        declared_at="2025-03-01T01:10:00Z",
        events=["2025-03-01T00:10:00Z", "2025-03-01T00:20:00Z"],
    )
    # Cross-partition dependents: on time in region=us, but dragged by batch-b.
    _write_batch(
        lake, "batch-c", sequence=3, partition=_US,
        declared_at="2025-03-01T01:20:00Z",
        events=["2025-03-01T01:05:00Z", "2025-03-01T01:10:00Z"],
        prev_batch_id="batch-b",
    )
    _write_batch(
        lake, "batch-d", sequence=4, partition=_US,
        declared_at="2025-03-01T01:30:00Z",
        events=["2025-03-01T01:10:00Z", "2025-03-01T01:15:00Z"],
        prev_batch_id="batch-c",
    )
    # Future batch: excluded from advancement, must not become a root cause.
    _write_batch(
        lake, "batch-f", sequence=5, partition=_AP,
        declared_at="2025-03-01T01:40:00Z",
        events=["2025-03-01T06:30:00Z"],
    )
    _write_batch(
        lake, "batch-g", sequence=6, partition=_AP,
        declared_at="2025-03-01T01:50:00Z",
        events=["2025-03-01T05:00:00Z"],
        prev_batch_id="batch-f",
    )
    # Broken chain: missing predecessor propagates through the missing branch.
    _write_batch(
        lake, "batch-m", sequence=7, partition=_EU,
        declared_at="2025-03-01T02:00:00Z",
        events=["2025-03-01T01:55:00Z"],
        prev_batch_id="ghost-batch",
    )
    _write_batch(
        lake, "batch-n", sequence=8, partition=_EU,
        declared_at="2025-03-01T02:10:00Z",
        events=["2025-03-01T02:05:00Z"],
        prev_batch_id="batch-m",
    )


def _build_cycle_lake(lake: Path) -> None:
    _write_batch(
        lake, "cyc-x", sequence=1, partition=_EU,
        declared_at="2025-03-01T01:00:00Z",
        events=["2025-03-01T00:50:00Z"],
        prev_batch_id="cyc-y",
    )
    _write_batch(
        lake, "cyc-y", sequence=2, partition=_EU,
        declared_at="2025-03-01T01:10:00Z",
        events=["2025-03-01T01:00:00Z"],
        prev_batch_id="cyc-x",
    )
    _write_batch(
        lake, "cyc-z", sequence=3, partition=_EU,
        declared_at="2025-03-01T01:20:00Z",
        events=["2025-03-01T01:10:00Z"],
        prev_batch_id="cyc-z",
    )


def _run_inheritance_audit(tmp_path: Path):
    lake = tmp_path / "lake"
    _build_inheritance_lake(lake)
    config = load_config(_write_config(tmp_path, lake))
    return run_audit(config)


def test_end_to_end_cross_partition_inheritance(tmp_path):
    result = _run_inheritance_audit(tmp_path)
    batches = {item.batch_id: item for item in result.batches}

    # The per-partition verdict is untouched: only batch-b is late.
    assert [item.batch_id for item in result.batches if item.late] == ["batch-b"]
    eu = next(w for w in result.watermarks if w.partition.get("region") == "eu")
    assert eu.late_batch_count == 1

    assert batches["batch-b"].inherited_late is False
    assert batches["batch-c"].late is False
    assert batches["batch-c"].inherited_late is True
    assert batches["batch-c"].inherited_root_kind == "late"
    assert batches["batch-c"].inherited_root_batch_id == "batch-b"
    assert batches["batch-c"].inherited_path == ("batch-b", "batch-c")
    assert batches["batch-c"].partition["region"] == "us"
    assert batches["batch-d"].inherited_late is True
    assert batches["batch-d"].inherited_path == ("batch-b", "batch-c", "batch-d")

    inherited = [f for f in result.findings if f.code == "INHERITED_LATE_BATCH"]
    assert {f.batch_id for f in inherited} == {
        "batch-c",
        "batch-d",
        "batch-m",
        "batch-n",
    }
    evidence = {f.batch_id: f.evidence for f in inherited}
    assert evidence["batch-c"]["path"] == ["batch-b", "batch-c"]
    assert evidence["batch-d"]["path"] == ["batch-b", "batch-c", "batch-d"]


def test_future_batch_is_not_an_inheritance_root(tmp_path):
    result = _run_inheritance_audit(tmp_path)
    batches = {item.batch_id: item for item in result.batches}
    future = next(f for f in result.findings if f.code == "FUTURE_EVENT_TIME")
    assert future.batch_id == "batch-f"
    assert batches["batch-f"].late is False
    assert batches["batch-f"].inherited_late is False
    # The dependent of the future batch inherits nothing.
    assert batches["batch-g"].inherited_late is False
    assert not any(
        f.code == "INHERITED_LATE_BATCH" and f.batch_id == "batch-g"
        for f in result.findings
    )
    # The future batch never advanced the watermark either.
    ap = next(w for w in result.watermarks if w.partition.get("region") == "ap")
    assert ap.max_event_time == parse_utc("2025-03-01T05:00:00Z")


def test_missing_predecessor_chain_end_to_end(tmp_path):
    result = _run_inheritance_audit(tmp_path)
    batches = {item.batch_id: item for item in result.batches}
    assert batches["batch-m"].inherited_root_kind == "missing"
    assert batches["batch-m"].inherited_root_batch_id == "ghost-batch"
    assert batches["batch-m"].inherited_path == ("ghost-batch", "batch-m")
    assert batches["batch-n"].inherited_root_kind == "missing"
    assert batches["batch-n"].inherited_path == ("ghost-batch", "batch-m", "batch-n")
    # The broken-chain finding for the direct child is preserved as well.
    chain = next(f for f in result.findings if f.code == "CHAIN_BROKEN")
    assert chain.batch_id == "batch-m"
    assert chain.evidence["prev_batch_id"] == "ghost-batch"


def test_cycle_lake_audit_completes_with_cycle_findings(tmp_path):
    lake = tmp_path / "cycle-lake"
    _build_cycle_lake(lake)
    config = load_config(_write_config(tmp_path, lake))
    result = run_audit(config)
    cycles = [f for f in result.findings if f.code == "DEPENDENCY_CYCLE"]
    assert len(cycles) == 2
    members = sorted(tuple(f.evidence["members"]) for f in cycles)
    assert members == [("cyc-x", "cyc-y"), ("cyc-z",)]
    assert not any(f.code == "INHERITED_LATE_BATCH" for f in result.findings)
    assert all(not item.inherited_late for item in result.batches)


def test_repeated_audits_give_identical_run_id_and_reports(tmp_path):
    lake = tmp_path / "lake"
    _build_inheritance_lake(lake)
    config = load_config(_write_config(tmp_path, lake))
    first = run_audit(config)
    second = run_audit(config)
    assert first.run_id == second.run_id
    assert render_json(first) == render_json(second)
    assert render_markdown(first) == render_markdown(second)
    assert render_html(first) == render_html(second)


def test_ledger_persists_inherited_fields(tmp_path):
    result = _run_inheritance_audit(tmp_path)
    ledger = tmp_path / "ledger.sqlite"
    write_run(result, ledger)
    detail = read_run(ledger, result.run_id)
    assert detail is not None
    batches = {item["batch_id"]: item for item in detail["batches"]}
    assert batches["batch-b"]["late"] is True
    assert batches["batch-b"]["inherited_late"] is False
    assert batches["batch-c"]["inherited_late"] is True
    assert batches["batch-c"]["inherited_root_kind"] == "late"
    assert batches["batch-c"]["inherited_root_batch_id"] == "batch-b"
    assert batches["batch-c"]["inherited_path"] == ["batch-b", "batch-c"]
    assert batches["batch-c"]["prev_batch_id"] == "batch-b"
    assert batches["batch-d"]["inherited_path"] == ["batch-b", "batch-c", "batch-d"]
    assert batches["batch-m"]["inherited_root_kind"] == "missing"
    assert batches["batch-m"]["inherited_path"] == ["ghost-batch", "batch-m"]
    codes = {item["code"] for item in detail["findings"]}
    assert "INHERITED_LATE_BATCH" in codes


def test_existing_v1_ledger_is_migrated_in_place(tmp_path, demo_result):
    ledger = tmp_path / "old.sqlite"
    connection = sqlite3.connect(ledger)
    try:
        connection.executescript(
            """
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                status TEXT NOT NULL,
                as_of TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                config_digest TEXT NOT NULL,
                manifest_count INTEGER NOT NULL,
                batch_count INTEGER NOT NULL,
                file_count INTEGER NOT NULL,
                record_count INTEGER NOT NULL,
                finding_count INTEGER NOT NULL,
                error_count INTEGER NOT NULL,
                warning_count INTEGER NOT NULL,
                info_count INTEGER NOT NULL,
                report_json TEXT,
                report_md TEXT,
                report_html TEXT
            );
            CREATE TABLE findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                severity TEXT NOT NULL,
                code TEXT NOT NULL,
                source TEXT,
                partition_label TEXT,
                batch_id TEXT,
                message TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                UNIQUE(run_id, ordinal)
            );
            CREATE TABLE watermarks (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                source TEXT NOT NULL,
                partition_label TEXT NOT NULL,
                max_event_time TEXT,
                safe_watermark TEXT,
                batch_count INTEGER NOT NULL,
                late_batch_count INTEGER NOT NULL,
                UNIQUE(run_id, ordinal)
            );
            CREATE TABLE batches (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                manifest_path TEXT NOT NULL,
                source TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                partition_label TEXT NOT NULL,
                declared_at TEXT NOT NULL,
                file_count INTEGER NOT NULL,
                record_count INTEGER NOT NULL,
                event_time_min TEXT,
                event_time_max TEXT,
                watermark_before TEXT,
                watermark_after TEXT,
                late INTEGER NOT NULL,
                UNIQUE(run_id, ordinal)
            );
            """
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    migrated = initialize_ledger(ledger)
    assert migrated == ledger.resolve()
    assert verify_readable(ledger) == ledger.resolve()
    connection = sqlite3.connect(ledger)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(batches)")
        }
        assert {
            "prev_batch_id",
            "inherited_late",
            "inherited_root_kind",
            "inherited_root_batch_id",
            "inherited_path_json",
        }.issubset(columns)
    finally:
        connection.close()
    write_run(demo_result, ledger)
    detail = read_run(ledger, demo_result.run_id)
    assert detail is not None
    assert all(item["inherited_late"] is False for item in detail["batches"])


def test_reports_render_inherited_columns(tmp_path):
    result = _run_inheritance_audit(tmp_path)

    payload = json.loads(render_json(result))
    batches = {item["batch_id"]: item for item in payload["batches"]}
    assert batches["batch-c"]["inherited_late"] is True
    assert batches["batch-c"]["inherited_root_kind"] == "late"
    assert batches["batch-c"]["inherited_root_batch_id"] == "batch-b"
    assert batches["batch-c"]["inherited_path"] == ["batch-b", "batch-c"]
    assert batches["batch-c"]["prev_batch_id"] == "batch-b"
    assert batches["batch-b"]["inherited_late"] is False

    markdown = render_markdown(result)
    assert "继承延迟" in markdown
    assert "yes (late: batch-b)" in markdown
    assert "yes (missing: ghost-batch)" in markdown
    assert "`INHERITED_LATE_BATCH`" in markdown

    html = render_html(result)
    assert "继承延迟" in html
    assert "yes (late: batch-b)" in html
    assert "yes (missing: ghost-batch)" in html


def test_viewer_shows_inherited_column_from_ledger(tmp_path):
    from lakeinlet.server import create_app

    result = _run_inheritance_audit(tmp_path)
    ledger = tmp_path / "ledger.sqlite"
    write_run(result, ledger)
    app = create_app(ledger)
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.get(f"/run/{result.run_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "继承延迟" in body
    assert "yes (late: batch-b)" in body
    assert "yes (missing: ghost-batch)" in body


# ---------------------------------------------------------------------------
# Broken fixture: new results appear without drifting existing verdicts
# ---------------------------------------------------------------------------


def test_broken_fixture_marks_missing_predecessor_inheritance(broken_result):
    inherited = [
        finding
        for finding in broken_result.findings
        if finding.code == "INHERITED_LATE_BATCH"
    ]
    assert len(inherited) == 1
    finding = inherited[0]
    assert finding.batch_id == "orders-future"
    assert finding.evidence["root_kind"] == "missing"
    assert finding.evidence["root_batch_id"] == "orders-does-not-exist"
    assert finding.evidence["path"] == ["orders-does-not-exist", "orders-future"]

    batches = {item.batch_id: item for item in broken_result.batches}
    future = batches["orders-future"]
    assert future.late is False
    assert future.inherited_late is True
    assert future.inherited_root_kind == "missing"
    assert future.inherited_path == ("orders-does-not-exist", "orders-future")


def test_broken_fixture_existing_verdicts_do_not_drift(broken_result):
    batches = {item.batch_id: item for item in broken_result.batches}
    late = batches["orders-late"]
    assert late.late is True
    assert late.inherited_late is False
    watermark = next(
        item
        for item in broken_result.watermarks
        if item.source == "orders" and item.partition.get("region") == "eu"
    )
    assert watermark.late_batch_count == 1
    assert watermark.batch_count == 6
    codes = {finding.code for finding in broken_result.findings}
    assert {
        "LATE_BATCH",
        "FUTURE_EVENT_TIME",
        "SEQUENCE_GAP",
        "CHAIN_BROKEN",
        "INHERITED_LATE_BATCH",
    }.issubset(codes)


def test_broken_fixture_run_id_is_stable(broken_config):
    first = run_audit(broken_config)
    second = run_audit(broken_config)
    assert first.run_id == second.run_id
    assert [f.to_dict() for f in first.findings] == [
        f.to_dict() for f in second.findings
    ]
