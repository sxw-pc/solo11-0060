"""SQLite schema, initialization, and read-only connection helpers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import LedgerError

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
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
CREATE TABLE IF NOT EXISTS findings (
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
CREATE TABLE IF NOT EXISTS watermarks (
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
CREATE TABLE IF NOT EXISTS batches (
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
    prev_batch_id TEXT,
    inherited_late INTEGER NOT NULL DEFAULT 0,
    inherited_root_kind TEXT,
    inherited_root_batch_id TEXT,
    inherited_path_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_runs_as_of ON runs(as_of, run_id);
"""

# Columns added to the batches table after the initial schema. Fresh
# databases get them from _SCHEMA; existing databases are upgraded in place
# so the schema version stays stable.
_BATCH_TABLE_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("prev_batch_id", "prev_batch_id TEXT"),
    ("inherited_late", "inherited_late INTEGER NOT NULL DEFAULT 0"),
    ("inherited_root_kind", "inherited_root_kind TEXT"),
    ("inherited_root_batch_id", "inherited_root_batch_id TEXT"),
    ("inherited_path_json", "inherited_path_json TEXT NOT NULL DEFAULT '[]'"),
)


def _migrate_batches(connection: sqlite3.Connection) -> None:
    """Idempotently add post-v1 batches columns to an existing ledger."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(batches)")
    }
    for name, definition in _BATCH_TABLE_MIGRATIONS:
        if name not in columns:
            connection.execute(f"ALTER TABLE batches ADD COLUMN {definition}")


def readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise LedgerError(f"ledger not found: {path}")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise LedgerError(f"cannot open ledger for reading: {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    require_schema(connection, path)
    return connection


def require_schema(connection: sqlite3.Connection, path: Path) -> None:
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    except sqlite3.Error as exc:
        connection.close()
        raise LedgerError(f"cannot inspect ledger {path}: {exc}") from exc
    required = {"runs", "findings", "watermarks", "batches"}
    if version != SCHEMA_VERSION or not required.issubset(tables):
        connection.close()
        raise LedgerError(
            f"ledger has an incompatible schema (version={version}); "
            "regenerate it with the audit command"
        )


def initialize_ledger(path: str | Path) -> Path:
    """Create or verify the ledger schema, without writing any run."""
    ledger_path = Path(path).expanduser().resolve()
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(ledger_path)
    except (OSError, sqlite3.Error) as exc:
        raise LedgerError(f"cannot create ledger {ledger_path}: {exc}") from exc
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_SCHEMA)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise LedgerError(f"unsupported ledger schema version: {version}")
        _migrate_batches(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except sqlite3.Error as exc:
        raise LedgerError(f"cannot initialize ledger: {exc}") from exc
    finally:
        connection.close()
    return ledger_path


def verify_readable(path: str | Path) -> Path:
    """Fail early when the viewer is pointed at a missing/incompatible ledger."""
    ledger_path = Path(path).expanduser().resolve()
    connection = readonly_connection(ledger_path)
    connection.close()
    return ledger_path
