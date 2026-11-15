"""Reproducible convergence of duplicate record ids within a source.

Repeated ``record_id`` values are collapsed to one retained occurrence with a
deterministic, fully reported rule:

1. a later ``event_time`` wins;
2. a tie is broken by the later owning batch ``declared_at``;
3. a remaining tie is broken by ascending file path and line number, keeping
   the first occurrence in that stable order.

Occurrences whose ``event_time`` cannot be parsed still join the group and are
reported as superseded, but are excluded from the comparison so they can never
take the retained slot from a legitimate record.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from .models import BatchScan, Record
from .timeutil import format_utc

BASIS_EVENT_TIME = "event_time"
BASIS_DECLARED_AT = "declared_at"
BASIS_LOCATION = "location"
BASIS_SOLE_PARSEABLE = "sole_parseable_event_time"
BASIS_UNPARSEABLE = "unparseable_event_time"
BASIS_NONE_PARSEABLE = "no_parseable_event_time"

_BASIS_DETAIL = {
    BASIS_EVENT_TIME: "event_time is earlier than the retained occurrence",
    BASIS_DECLARED_AT: (
        "event_time ties; owning batch declared_at is earlier than the "
        "retained occurrence"
    ),
    BASIS_LOCATION: (
        "event_time and declared_at tie; later in the stable "
        "file path / line number order than the retained occurrence"
    ),
    BASIS_UNPARSEABLE: (
        "event_time cannot be parsed; excluded from comparison and covered "
        "by the retained occurrence"
    ),
    BASIS_SOLE_PARSEABLE: "only parseable occurrence in the group",
    BASIS_NONE_PARSEABLE: (
        "no occurrence in the group has a parseable event_time; nothing is "
        "retained"
    ),
}


@dataclass(slots=True, frozen=True)
class RecordEvidence:
    """A record occurrence as seen on disk, with its batch context."""

    manifest_path: str
    batch_id: str
    declared_at: datetime
    file: str
    line: int
    event_time: datetime | None
    raw_event_time: str = ""

    @property
    def parseable(self) -> bool:
        return self.event_time is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_path": self.manifest_path,
            "batch_id": self.batch_id,
            "declared_at": format_utc(self.declared_at),
            "file": self.file,
            "line": self.line,
            "event_time": (
                format_utc(self.event_time) if self.event_time else None
            ),
            "raw_event_time": self.raw_event_time,
            "event_time_parseable": self.parseable,
        }


@dataclass(slots=True, frozen=True)
class SupersededRecord:
    """An occurrence that lost the retained slot, with the comparison basis."""

    location: RecordEvidence
    basis: str
    detail: str
    retained_location: RecordEvidence | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.location.to_dict(),
            "basis": self.basis,
            "detail": self.detail,
            "retained": (
                self.retained_location.to_dict()
                if self.retained_location is not None
                else None
            ),
        }


@dataclass(slots=True, frozen=True)
class RecordConvergence:
    """Convergence account for one (source, record_id) duplicate group."""

    source: str
    record_id: str
    raw_count: int
    retained: RecordEvidence | None
    basis: str
    superseded: tuple[SupersededRecord, ...] = ()

    @property
    def effective_count(self) -> int:
        return 1 if self.retained is not None else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "record_id": self.record_id,
            "raw_count": self.raw_count,
            "effective_count": self.effective_count,
            "retained": self.retained.to_dict() if self.retained else None,
            "basis": self.basis,
            "superseded": [item.to_dict() for item in self.superseded],
        }


@dataclass(slots=True, frozen=True)
class ConvergenceResult:
    """Accounts plus the retained set aligned with the input scan order."""

    accounts: tuple[RecordConvergence, ...]
    retained_by_scan: tuple[tuple[Record, ...], ...]
    superseded_by_scan: tuple[int, ...]

    @property
    def duplicate_accounts(self) -> tuple[RecordConvergence, ...]:
        return tuple(item for item in self.accounts if item.raw_count > 1)


def _evidence(scan: BatchScan, record: Record) -> RecordEvidence:
    return RecordEvidence(
        manifest_path=scan.manifest.manifest_relpath,
        batch_id=scan.batch_id,
        declared_at=scan.manifest.declared_at,
        file=record.file_path,
        line=record.line_number,
        event_time=record.event_time,
        raw_event_time=record.raw_event_time,
    )


def _location_order(item: tuple[int, Record]) -> tuple[str, int, int]:
    """Stable physical order: file path, then line, then scan position."""
    scan_index, record = item
    return (record.file_path, record.line_number, scan_index)


def _rank(
    candidates: Sequence[tuple[int, Record]],
    scans: Sequence[BatchScan],
) -> list[tuple[int, Record]]:
    """Rank candidates best-first, preserving location order inside a tie."""
    by_location = sorted(candidates, key=_location_order)
    return sorted(
        by_location,
        key=lambda item: (
            item[1].event_time,
            scans[item[0]].manifest.declared_at,
        ),
        reverse=True,
    )


def _basis_for(
    record: Record,
    winner: Record,
    scan: BatchScan,
    winner_scan: BatchScan,
) -> str:
    """Name the comparison key that separates a losing row from the winner."""
    if record.event_time is None:
        return BASIS_UNPARSEABLE
    if record.event_time != winner.event_time:
        return BASIS_EVENT_TIME
    if scan.manifest.declared_at != winner_scan.manifest.declared_at:
        return BASIS_DECLARED_AT
    return BASIS_LOCATION


def _group_basis(
    ranked: Sequence[tuple[int, Record]],
    scans: Sequence[BatchScan],
) -> str:
    """Name the comparison step that decided the group's retained occurrence."""
    if len(ranked) < 2:
        return BASIS_SOLE_PARSEABLE
    return _basis_for(
        ranked[1][1], ranked[0][1], scans[ranked[1][0]], scans[ranked[0][0]]
    )


def _loss_basis(
    item: tuple[int, Record],
    retained: tuple[int, Record],
    scans: Sequence[BatchScan],
) -> str:
    return _basis_for(item[1], retained[1], scans[item[0]], scans[retained[0]])


def converge_records(batches: Sequence[BatchScan]) -> ConvergenceResult:
    """Collapse duplicate record ids per source and build the account."""
    scans = list(batches)
    groups: dict[tuple[str, str], list[tuple[int, Record]]] = defaultdict(list)
    for index, scan in enumerate(scans):
        for record in scan.records:
            groups[(scan.source, record.record_id)].append((index, record))

    retained_by_scan: list[list[Record]] = [[] for _ in scans]
    superseded_by_scan = [0 for _ in scans]
    accounts: list[RecordConvergence] = []

    for source, record_id in sorted(groups):
        occurrences = sorted(groups[(source, record_id)], key=_location_order)
        if len(occurrences) < 2:
            # A unique row needs no account. An unparseable unique row is
            # already reported as CSV_INVALID_EVENT_TIME and never effective.
            if occurrences and occurrences[0][1].event_time is not None:
                retained_by_scan[occurrences[0][0]].append(occurrences[0][1])
            continue

        candidates = [item for item in occurrences if item[1].event_time is not None]
        superseded: list[SupersededRecord] = []
        retained: tuple[int, Record] | None = None
        retained_evidence: RecordEvidence | None = None
        if candidates:
            ranked = _rank(candidates, scans)
            retained = ranked[0]
            basis = _group_basis(ranked, scans)
            retained_evidence = _evidence(scans[retained[0]], retained[1])
            retained_by_scan[retained[0]].append(retained[1])
            lost = [item for item in occurrences if item != retained]
        else:
            basis = BASIS_NONE_PARSEABLE
            lost = list(occurrences)

        for item in lost:
            if retained is not None:
                loss = _loss_basis(item, retained, scans)
            else:
                loss = BASIS_UNPARSEABLE
            superseded.append(
                SupersededRecord(
                    location=_evidence(scans[item[0]], item[1]),
                    basis=loss,
                    detail=_BASIS_DETAIL[loss],
                    retained_location=retained_evidence,
                )
            )
            superseded_by_scan[item[0]] += 1

        accounts.append(
            RecordConvergence(
                source=source,
                record_id=record_id,
                raw_count=len(occurrences),
                retained=retained_evidence,
                basis=basis,
                superseded=tuple(superseded),
            )
        )

    return ConvergenceResult(
        accounts=tuple(accounts),
        retained_by_scan=tuple(tuple(items) for items in retained_by_scan),
        superseded_by_scan=tuple(superseded_by_scan),
    )


def apply_convergence(
    batches: Sequence[BatchScan], result: ConvergenceResult
) -> None:
    """Replace each scan's effective records, count, and event range in place."""
    for index, scan in enumerate(batches):
        retained = result.retained_by_scan[index]
        times = [
            record.event_time for record in retained if record.event_time is not None
        ]
        scan.effective_records = tuple(retained)
        scan.record_count = len(retained)
        scan.event_time_min = min(times) if times else None
        scan.event_time_max = max(times) if times else None
        scan.superseded_record_count = result.superseded_by_scan[index]
