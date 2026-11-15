"""Stable JSON report rendering."""

from __future__ import annotations

import json
from pathlib import Path

from .audit_result import AuditResult
from .timeutil import format_utc

COMPLETION_PLANS_FILENAME = "completion_plans.json"


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


def render_completion_plans(result: AuditResult) -> str:
    """Render only the completion-plan layer as deterministic JSON."""
    payload = {
        "schema_version": "1.0",
        "project": result.project,
        "as_of": format_utc(result.as_of),
        "run_id": result.run_id,
        "plan_count": len(result.completion_plans),
        "completion_plans": [plan.to_dict() for plan in result.completion_plans],
    }
    return (
        json.dumps(
            payload,
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


def write_completion_plans(result: AuditResult, path: str | Path) -> Path:
    """Write the standalone, replay-stable completion-plan document."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_completion_plans(result), encoding="utf-8", newline="\n"
    )
    return destination
