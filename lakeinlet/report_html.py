"""Self-contained, deterministic HTML report rendering."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .audit_result import AuditResult
from .models import Finding, partition_label
from .timeutil import format_utc

_STATUS_CLASSES = {"passed", "failed", "passed_with_warnings"}
_SEVERITY_CLASSES = {"error", "warning", "info"}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _cell(value: Any) -> str:
    if value is None or value == "":
        return '<span class="muted">—</span>'
    return _e(value)


def _badge(value: str, allowed: set[str], prefix: str) -> str:
    safe = value if value in allowed else "failed"
    return f'<span class="{prefix} {prefix}-{safe}">{_e(value)}</span>'


def _inline_css() -> str:
    return (Path(__file__).parent / "static" / "style.css").read_text(encoding="utf-8")


def _summary_cards(result: AuditResult) -> str:
    summary = result.summary_dict()
    cards = [
        ("batches", summary["batch_count"]),
        ("files", summary["file_count"]),
        ("records", summary["record_count"]),
        ("findings", summary["finding_count"]),
        ("errors", summary["error_count"]),
        ("warnings", summary["warning_count"]),
    ]
    return '<div class="summary-grid">' + "".join(
        f'<div class="metric"><span>{_e(label)}</span><strong>{value}</strong></div>'
        for label, value in cards
    ) + "</div>"


def _source_rows(result: AuditResult) -> str:
    rows = []
    for source in result.sources:
        rows.append(
            "<tr>"
            f"<td>{_e(source.name)}</td>"
            f"<td>{_e(', '.join(source.partition_keys))}</td>"
            f'<td class="num">{source.allowed_lateness_seconds}</td>'
            f'<td class="num">{source.future_tolerance_seconds}</td>'
            f"<td>{_e(source.sequence_policy)}</td>"
            f'<td class="num">{source.manifest_count}</td>'
            f'<td class="num">{source.file_count}</td>'
            f'<td class="num">{source.record_count}</td>'
            f'<td class="num">{source.error_count}</td>'
            f'<td class="num">{source.warning_count}</td>'
            "</tr>"
        )
    return "".join(rows)


def _watermark_rows(result: AuditResult) -> str:
    if not result.watermarks:
        return '<tr><td colspan="6" class="muted">无可计算水位。</td></tr>'
    rows = []
    for watermark in result.watermarks:
        rows.append(
            "<tr>"
            f"<td>{_e(watermark.source)}</td>"
            f"<td>{_e(partition_label(watermark.partition))}</td>"
            f"<td>{_cell(format_utc(watermark.max_event_time) if watermark.max_event_time else None)}</td>"
            f"<td>{_cell(format_utc(watermark.safe_watermark) if watermark.safe_watermark else None)}</td>"
            f'<td class="num">{watermark.batch_count}</td>'
            f'<td class="num">{watermark.late_batch_count}</td>'
            "</tr>"
        )
    return "".join(rows)


def _batch_rows(result: AuditResult) -> str:
    if not result.batches:
        return '<tr><td colspan="8" class="muted">无有效批次。</td></tr>'
    rows = []
    for batch in result.batches:
        event_range = "—"
        if batch.event_time_min and batch.event_time_max:
            event_range = (
                f"{format_utc(batch.event_time_min)} ~ "
                f"{format_utc(batch.event_time_max)}"
            )
        rows.append(
            "<tr>"
            f"<td>{_e(batch.source)}</td>"
            f"<td class=\"mono\">{_e(batch.batch_id)}</td>"
            f'<td class="num">{batch.sequence}</td>'
            f"<td>{_e(partition_label(batch.partition))}</td>"
            f"<td>{_cell(format_utc(batch.declared_at))}</td>"
            f"<td>{_e(event_range)}</td>"
            f'<td class="num">{batch.record_count}</td>'
            f"<td>{'yes' if batch.late else 'no'}</td>"
            "</tr>"
        )
    return "".join(rows)


def _finding_html(index: int, finding: Finding) -> str:
    evidence = json.dumps(
        finding.to_dict()["evidence"],
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return (
        '<details class="finding">'
        "<summary>"
        f"{_badge(finding.severity, _SEVERITY_CLASSES, 'severity')} "
        f"<strong>F{index:04d}</strong> "
        f"<code>{_e(finding.code)}</code> — {_e(finding.message)}"
        "</summary>"
        '<div class="meta">'
        f"<span>source: {_cell(finding.source)}</span>"
        f"<span>batch_id: {_cell(finding.batch_id)}</span>"
        f"<span>partition: {_cell(finding.partition_label)}</span>"
        "</div>"
        f'<pre class="mono">{_e(evidence)}</pre>'
        "</details>"
    )


def _findings_section(result: AuditResult) -> str:
    if not result.findings:
        return '<div class="empty">无发现项。批次水位、幂等与序列检查均通过。</div>'
    return "".join(
        _finding_html(index, finding)
        for index, finding in enumerate(result.findings, start=1)
    )


def render_html(result: AuditResult) -> str:
    """Return a fully self-contained HTML document (no scripts or remote URLs)."""
    status = _badge(result.status, _STATUS_CLASSES, "status")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="lakeinlet">
<title>{_e(result.project)} · 入湖审计报告</title>
<style>
{_inline_css()}
</style>
</head>
<body>
<header class="site">
  <div class="wrap site-inner">
    <div>
      <div class="brand">湖入水位台 <small>lakeinlet</small></div>
    </div>
    <div>{status}</div>
  </div>
</header>
<main class="wrap">
  <div class="eyebrow">offline ingestion audit</div>
  <h1>{_e(result.project)}</h1>
  <div class="meta">
    <span>as_of <code>{format_utc(result.as_of)}</code></span>
    <span>run_id <code>{_e(result.run_id)}</code></span>
    <span>config <code>{_e(result.config_digest[:16])}</code></span>
  </div>
  {_summary_cards(result)}

  <h2>数据源</h2>
  <div class="panel table-wrap"><table>
    <thead><tr><th>数据源</th><th>分区键</th><th class="num">迟到窗口(s)</th><th class="num">未来容忍(s)</th><th>序列策略</th><th class="num">清单</th><th class="num">文件</th><th class="num">记录</th><th class="num">错误</th><th class="num">警告</th></tr></thead>
    <tbody>{_source_rows(result)}</tbody>
  </table></div>

  <h2>当前安全水位</h2>
  <p class="muted">安全水位 = 已观测最大事件时间 − 允许迟到窗口。</p>
  <div class="panel table-wrap"><table>
    <thead><tr><th>数据源</th><th>分区</th><th>最大事件时间</th><th>安全水位</th><th class="num">批次数</th><th class="num">迟到批次</th></tr></thead>
    <tbody>{_watermark_rows(result)}</tbody>
  </table></div>

  <h2>批次</h2>
  <div class="panel table-wrap"><table>
    <thead><tr><th>数据源</th><th>batch_id</th><th class="num">序列</th><th>分区</th><th>声明时间</th><th>事件时间范围</th><th class="num">记录</th><th>迟到</th></tr></thead>
    <tbody>{_batch_rows(result)}</tbody>
  </table></div>

  <h2>发现项</h2>
  <p class="muted">共 {result.finding_count} 项：{result.error_count} error，{result.warning_count} warning，{result.info_count} info。</p>
  {_findings_section(result)}
</main>
<footer class="foot"><div class="wrap">由 lakeinlet 在完全离线模式下生成。审计为只读操作，未修改入湖数据。</div></footer>
</body>
</html>
"""


def write_report_html(result: AuditResult, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_html(result), encoding="utf-8", newline="\n")
    return destination
