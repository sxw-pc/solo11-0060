from __future__ import annotations

from pathlib import Path

from lakeinlet.convergence import apply_convergence, converge_records
from lakeinlet.models import (
    BatchManifest,
    BatchScan,
    FileRef,
    FileScan,
    Record,
)
from lakeinlet.timeutil import parse_utc
from lakeinlet.watermark import summarize_batches


def _record(record_id, event_time, path, line, raw=""):
    return Record(
        record_id=record_id,
        event_time=parse_utc(event_time) if event_time else None,
        file_path=path,
        line_number=line,
        raw_event_time=raw,
    )


def _scan(batch_id, declared_at, records, source="s"):
    manifest = BatchManifest(
        manifest_path=Path(f"/{batch_id}.yaml"),
        manifest_relpath=f"_manifests/{batch_id}.yaml",
        batch_id=batch_id,
        source=source,
        sequence=1,
        partition={"dt": "d"},
        declared_at=parse_utc(declared_at),
        files=(),
    )
    file_scan = FileScan(
        ref=FileRef(path="data/x.csv", sha256="0" * 64),
        relative_path="data/x.csv",
        exists=True,
        actual_sha256="0" * 64,
        actual_bytes=0,
        header=(),
        row_count=len(records),
        records=tuple(records),
        event_time_min=None,
        event_time_max=None,
    )
    parseable = [r.event_time for r in records if r.event_time is not None]
    return BatchScan(
        manifest=manifest,
        files=(file_scan,),
        record_count=len(parseable),
        event_time_min=min(parseable) if parseable else None,
        event_time_max=max(parseable) if parseable else None,
        raw_record_count=len(records),
    )


def _account(result, record_id):
    return next(item for item in result.accounts if item.record_id == record_id)


def test_later_event_time_wins():
    early = _scan("b1", "2025-03-01T01:00:00Z", [_record("r", "2025-03-01T00:01:00Z", "a.csv", 2)])
    late = _scan("b2", "2025-03-01T00:30:00Z", [_record("r", "2025-03-01T00:02:00Z", "z.csv", 2)])
    result = converge_records([early, late])
    account = _account(result, "r")
    assert account.retained.file == "z.csv"
    assert account.basis == "event_time"
    assert account.superseded[0].basis == "event_time"
    assert account.superseded[0].retained_location.file == "z.csv"


def test_declared_at_breaks_event_time_tie():
    earlier_batch = _scan(
        "b1", "2025-03-01T01:00:00Z", [_record("r", "2025-03-01T00:05:00Z", "a.csv", 2)]
    )
    later_batch = _scan(
        "b2", "2025-03-01T01:30:00Z", [_record("r", "2025-03-01T00:05:00Z", "z.csv", 2)]
    )
    result = converge_records([earlier_batch, later_batch])
    account = _account(result, "r")
    assert account.retained.file == "z.csv"
    assert account.basis == "declared_at"
    assert account.superseded[0].basis == "declared_at"


def test_location_order_breaks_remaining_tie():
    one = _scan("b1", "2025-03-01T01:00:00Z", [_record("r", "2025-03-01T00:05:00Z", "a.csv", 2)])
    two = _scan("b2", "2025-03-01T01:00:00Z", [_record("r", "2025-03-01T00:05:00Z", "b.csv", 2)])
    result = converge_records([two, one])
    account = _account(result, "r")
    assert account.retained.file == "a.csv"
    assert account.basis == "location"
    assert account.superseded[0].basis == "location"


def test_unparseable_duplicate_joins_group_but_cannot_win():
    good = _scan(
        "b1", "2025-03-01T01:00:00Z", [_record("r", "2025-03-01T00:01:00Z", "a.csv", 2)]
    )
    bad = _scan(
        "b2",
        "2025-03-01T05:00:00Z",
        [_record("r", None, "b.csv", 2, raw="not-a-time")],
    )
    result = converge_records([good, bad])
    account = _account(result, "r")
    assert account.raw_count == 2
    assert account.retained.file == "a.csv"
    assert account.superseded[0].location.file == "b.csv"
    assert account.superseded[0].location.parseable is False
    assert account.superseded[0].basis == "unparseable_event_time"
    assert account.superseded[0].detail.startswith("event_time cannot be parsed")


def test_all_unparseable_duplicates_retain_nothing():
    one = _scan("b1", "2025-03-01T01:00:00Z", [_record("r", None, "a.csv", 2, raw="bad")])
    two = _scan("b2", "2025-03-01T01:00:00Z", [_record("r", None, "b.csv", 2, raw="bad")])
    result = converge_records([one, two])
    account = _account(result, "r")
    assert account.retained is None
    assert account.effective_count == 0
    assert account.basis == "no_parseable_event_time"
    assert [item.location.file for item in account.superseded] == ["a.csv", "b.csv"]


def test_apply_convergence_updates_counts_ranges_and_watermark_inputs():
    first = _scan(
        "b1",
        "2025-03-01T01:00:00Z",
        [
            _record("dup", "2025-03-01T00:10:00Z", "a.csv", 2),
            _record("only", "2025-03-01T00:20:00Z", "a.csv", 3),
        ],
    )
    second = _scan(
        "b2",
        "2025-03-01T02:00:00Z",
        [_record("dup", "2025-03-01T00:30:00Z", "b.csv", 2)],
    )
    result = converge_records([first, second])
    apply_convergence([first, second], result)

    assert first.effective_records == (first.records[1],)
    assert first.record_count == 1
    assert first.superseded_record_count == 1
    assert first.event_time_min == parse_utc("2025-03-01T00:20:00Z")
    assert first.event_time_max == parse_utc("2025-03-01T00:20:00Z")
    assert first.raw_record_count == 2

    assert second.record_count == 1
    assert second.superseded_record_count == 0
    assert second.event_time_max == parse_utc("2025-03-01T00:30:00Z")

    summaries = summarize_batches([first, second])
    assert summaries[0].record_count == 1
    assert summaries[0].raw_record_count == 2
    assert summaries[0].event_time_max == parse_utc("2025-03-01T00:20:00Z")


def test_unique_records_are_retained_without_account():
    scan = _scan(
        "b1",
        "2025-03-01T01:00:00Z",
        [
            _record("a", "2025-03-01T00:01:00Z", "x.csv", 2),
            _record("b", None, "x.csv", 3, raw="bad"),
        ],
    )
    result = converge_records([scan])
    assert result.accounts == ()
    assert result.retained_by_scan[0] == (scan.records[0],)
