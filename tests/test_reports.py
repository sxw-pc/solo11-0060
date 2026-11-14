from __future__ import annotations

import copy
import json

from lakeinlet.models import Finding
from lakeinlet.report_html import render_html, write_report_html
from lakeinlet.report_json import render_json, write_report_json
from lakeinlet.report_md import render_markdown, write_report_markdown
from lakeinlet.rules import run_audit

def test_json_report_is_valid_and_stable(demo_result):
    first = render_json(demo_result)
    second = render_json(demo_result)
    assert first == second
    payload = json.loads(first)
    assert payload["schema_version"] == "1.0"
    assert payload["status"] == "passed"
    assert payload["project"] == "lakeinlet-demo"
    assert payload["as_of"] == "2025-03-01T04:00:00Z"
    assert payload["run_id"] == demo_result.run_id
    assert payload["summary"]["batch_count"] == 6
    assert payload["watermarks"]
    assert first.endswith("\n")

def test_markdown_report_contains_core_sections(demo_result, broken_result):
    clean = render_markdown(demo_result)
    assert clean.startswith("# 湖入水位台审计报告")
    assert "## 当前安全水位" in clean
    assert "## 发现项" in clean
    assert "无发现项。" in clean
    assert "2025-03-01T04:00:00Z" in clean

    broken = render_markdown(broken_result)
    assert "**failed**" in broken
    assert "`SEQUENCE_GAP`" in broken
    assert "`DUPLICATE_RECORD_ID`" in broken
    assert "### 发现项证据" in broken

def test_html_report_is_self_contained(demo_result):
    rendered = render_html(demo_result)
    assert rendered.startswith("<!doctype html>")
    assert "<style>" in rendered
    assert "<script" not in rendered
    assert "<link" not in rendered
    assert "http://" not in rendered
    assert "https://" not in rendered
    assert "lakeinlet-demo" in rendered
    assert demo_result.run_id in rendered
    assert "当前安全水位" in rendered

def test_html_report_escapes_finding_text(demo_result):
    injected = copy.copy(demo_result)
    injected.findings = [
        Finding(
            "error",
            "XSS_TEST",
            '<script>alert("x")</script>',
            source="<source>",
            evidence={"html": "<img src=x onerror=alert(1)>"},
        )
    ]
    rendered = render_html(injected)
    assert '<script>alert("x")</script>' not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&lt;source&gt;" in rendered
    assert "&lt;img" in rendered

def test_report_writers_create_files_and_are_deterministic(tmp_path, demo_result):
    json_one = write_report_json(demo_result, tmp_path / "one" / "report.json")
    json_two = write_report_json(demo_result, tmp_path / "two" / "report.json")
    md_one = write_report_markdown(demo_result, tmp_path / "one" / "report.md")
    md_two = write_report_markdown(demo_result, tmp_path / "two" / "report.md")
    html_one = write_report_html(demo_result, tmp_path / "one" / "report.html")
    html_two = write_report_html(demo_result, tmp_path / "two" / "report.html")

    assert json_one.read_bytes() == json_two.read_bytes()
    assert md_one.read_bytes() == md_two.read_bytes()
    assert html_one.read_bytes() == html_two.read_bytes()

def test_independent_audits_render_identical_reports(demo_config):
    first = run_audit(demo_config)
    second = run_audit(demo_config)
    assert first.run_id == second.run_id
    assert render_json(first) == render_json(second)
    assert render_markdown(first) == render_markdown(second)
    assert render_html(first) == render_html(second)

def test_broken_json_report_lists_expected_findings(broken_result):
    payload = json.loads(render_json(broken_result))
    codes = {item["code"] for item in payload["findings"]}
    assert payload["status"] == "failed"
    assert {
        "LATE_BATCH",
        "FUTURE_EVENT_TIME",
        "SEQUENCE_GAP",
        "DUPLICATE_BATCH_ID",
        "FILE_CHECKSUM_MISMATCH",
    }.issubset(codes)
    assert all(item["evidence"] for item in payload["findings"])
