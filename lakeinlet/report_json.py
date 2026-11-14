"""Stable JSON report rendering."""

from __future__ import annotations

import json
from pathlib import Path

from .audit_result import AuditResult


def render_json(result: AuditResult) -> str:
    """Render canonical, deterministic JSON (sorted keys, trailing newline)."""
    return (
        json.dumps(
            result.to_dict(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )


def write_report_json(result: AuditResult, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_json(result), encoding="utf-8", newline="\n")
    return destination
