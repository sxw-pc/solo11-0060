"""Deterministic Markdown report rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit_result import AuditResult
from .convergence import RecordConvergence
from .models import Finding, partition_label
from .timeutil import format_utc


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    text = str(value).replace("\\", "\\\\").replace("|", "\\|")
    return " ".join(text.split())


def _finding_markdown(index: int, finding: Finding) -> str:
    partition = finding.partition_label or "—"
    lines = [
        f"### F{index:04d} · `{finding.code}` · {finding.severity}",
        "",
        finding.message,
        "",
        "| field | value |",
        "| --- | --- |",
        f"| source | {_cell(finding.source)} |",
        f"| batch_id | {_cell(finding.batch_id)} |",
        f"| partition | {_cell(partition)} |",
        "",
        "~~~json",
        json.dumps(
            finding.to_dict()["evidence"],
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ),
        "~~~",
        "",
    ]
    return "\n".join(lines)


def _convergence_markdown(account: RecordConvergence) -> str:
    retained = account.retained
    retained_cell = (
        f"`{_cell(retained.file)}:{retained.line}` "
        f"(batch `{_cell(retained.batch_id)}`, "
        f"event_time `{_cell(format_utc(retained.event_time) if retained.event_time else None)}`)"
        if retained is not None
        else "—（无可解析事件时间，保留集为空）"
    )
    lines = [
        f"### record_id `{_cell(account.record_id)}` · source `{_cell(account.source)}`",
        "",
        f"- 原始出现：{account.raw_count} 条；有效保留：{account.effective_count} 条；"
        f"被覆盖：{len(account.superseded)} 条",
        f"- 保留位置：{retained_cell}",
        f"- 收敛依据：`{account.basis}`",
        "",
        "| 被覆盖位置 | 事件时间 | 可解析 | 批次 | 比较依据 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in account.superseded:
        location = item.location
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(f"{location.file}:{location.line}"),
                    _cell(
                        format_utc(location.event_time)
                        if location.event_time
                        else (location.raw_event_time or None)
                    ),
                    "yes" if location.parseable else "no",
                    _cell(f"{location.batch_id} @ {format_utc(location.declared_at)}"),
                    _cell(f"{item.basis}: {item.detail}"),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def render_markdown(result: AuditResult) -> str:
    """Render the complete report without clocks, URLs, or external assets."""
    summary = result.summary_dict()
    lines = [
        "# 湖入水位台审计报告",
        "",
        f"- 项目：`{result.project}`",
        f"- 审计基准时间（as_of）：`{format_utc(result.as_of)}`",
        f"- 运行标识：`{result.run_id}`",
        f"- 状态：**{result.status}**",
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 清单数 | {summary['manifest_count']} |",
        f"| 批次数 | {summary['batch_count']} |",
        f"| 文件数 | {summary['file_count']} |",
        f"| 有效记录数 | {summary['record_count']} |",
        f"| 原始记录数 | {summary['raw_record_count']} |",
        f"| 发现项 | {summary['finding_count']} |",
        f"| 错误 | {summary['error_count']} |",
        f"| 警告 | {summary['warning_count']} |",
        f"| 提示 | {summary['info_count']} |",
        "",
        "## 数据源",
        "",
        "| 数据源 | 分区键 | 允许迟到(s) | 未来容忍(s) | 序列策略 | 清单 | 文件 | 有效记录 | 原始记录 | 错误 | 警告 |",
        "| --- | --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for source in result.sources:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(source.name),
                    _cell(",".join(source.partition_keys)),
                    str(source.allowed_lateness_seconds),
                    str(source.future_tolerance_seconds),
                    _cell(source.sequence_policy),
                    str(source.manifest_count),
                    str(source.file_count),
                    str(source.record_count),
                    str(source.raw_record_count),
                    str(source.error_count),
                    str(source.warning_count),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## 当前安全水位",
            "",
            "> 安全水位 = 已观测最大事件时间 − 允许迟到窗口。",
            "",
            "| 数据源 | 分区 | 最大事件时间 | 安全水位 | 批次数 | 迟到批次 |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    for watermark in result.watermarks:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(watermark.source),
                    _cell(partition_label(watermark.partition)),
                    _cell(
                        format_utc(watermark.max_event_time)
                        if watermark.max_event_time
                        else None
                    ),
                    _cell(
                        format_utc(watermark.safe_watermark)
                        if watermark.safe_watermark
                        else None
                    ),
                    str(watermark.batch_count),
                    str(watermark.late_batch_count),
                ]
            )
            + " |"
        )
    if not result.watermarks:
        lines.append("| — | 无可计算水位 | — | — | 0 | 0 |")

    lines.extend(
        [
            "",
            "## 批次",
            "",
            "| 数据源 | batch_id | 序列 | 分区 | 声明时间 | 事件时间范围 | 有效记录 | 原始记录 | 迟到 |",
            "| --- | --- | ---: | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    for batch in result.batches:
        event_range = " — "
        if batch.event_time_min and batch.event_time_max:
            event_range = (
                f"{format_utc(batch.event_time_min)} ~ "
                f"{format_utc(batch.event_time_max)}"
            )
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(batch.source),
                    _cell(batch.batch_id),
                    str(batch.sequence),
                    _cell(partition_label(batch.partition)),
                    _cell(format_utc(batch.declared_at)),
                    _cell(event_range),
                    str(batch.record_count),
                    str(batch.raw_record_count),
                    "yes" if batch.late else "no",
                ]
            )
            + " |"
        )
    if not result.batches:
        lines.append("| — | 无有效批次 | 0 | — | — | — | 0 | 0 | no |")

    lines.extend(
        [
            "",
            "## 记录收敛账",
            "",
            "> 同一 source 内同一 `record_id` 只保留一条有效记录：`event_time` 更晚者优先；"
            "并列时所属批次 `declared_at` 更晚者优先；仍并列时按文件路径与行号的稳定次序取先者。"
            "`event_time` 无法解析的重复条目参与分组并作为被覆盖方留证，但不参与比较。",
            "",
        ]
    )
    if not result.record_convergences:
        lines.append("无重复 `record_id`，无需收敛。")
    else:
        for account in result.record_convergences:
            lines.append(_convergence_markdown(account))

    lines.extend(["", "## 发现项", ""])
    if not result.findings:
        lines.append("无发现项。")
    else:
        lines.extend(
            [
                "| # | 严重级别 | 代码 | 数据源 | batch_id | 分区 | 说明 |",
                "| ---: | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for index, finding in enumerate(result.findings, start=1):
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"F{index:04d}",
                        _cell(finding.severity),
                        f"`{finding.code}`",
                        _cell(finding.source),
                        _cell(finding.batch_id),
                        _cell(finding.partition_label),
                        _cell(finding.message),
                    ]
                )
                + " |"
            )
        lines.extend(["", "### 发现项证据", ""])
        for index, finding in enumerate(result.findings, start=1):
            lines.append(_finding_markdown(index, finding))

    lines.extend(
        [
            "",
            "---",
            "",
            "本报告由 lakeinlet 在完全离线模式下生成；未修改任何入湖数据。",
            "",
        ]
    )
    return "\n".join(lines)


def write_report_markdown(result: AuditResult, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_markdown(result), encoding="utf-8", newline="\n")
    return destination
