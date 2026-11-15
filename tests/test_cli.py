from __future__ import annotations

import json

from lakeinlet.cli import main
from lakeinlet.config import load_config
from lakeinlet.ledger import read_completion_plan_ids

def test_audit_command_writes_reports_and_ledger(tmp_path, demo_config, capsys):
    out = tmp_path / "audit-out"
    code = main(
        [
            "audit",
            "--config",
            str(demo_config.config_path),
            "--out",
            str(out),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "status: passed" in captured.out
    assert "findings: 0" in captured.out
    assert (out / "report.json").is_file()
    assert (out / "completion_plans.json").is_file()
    assert (out / "report.md").is_file()
    assert (out / "report.html").is_file()
    assert (out / "ledger.sqlite").is_file()


def test_broken_audit_writes_completion_plan_file(tmp_path, broken_config, capsys):
    out = tmp_path / "broken-plans"
    code = main(
        [
            "audit",
            "--config",
            str(broken_config.config_path),
            "--out",
            str(out),
            "--fail-on",
            "never",
        ]
    )
    assert code == 0
    payload = json.loads((out / "completion_plans.json").read_text(encoding="utf-8"))
    assert payload["plan_count"] >= 1
    plan_ids = [plan["plan_id"] for plan in payload["completion_plans"]]

    # Re-running the audit refreshes the same plan ids (no history growth).
    main(
        [
            "audit",
            "--config",
            str(broken_config.config_path),
            "--out",
            str(out),
            "--fail-on",
            "never",
            "--quiet",
        ]
    )
    replayed = json.loads(
        (out / "completion_plans.json").read_text(encoding="utf-8")
    )
    assert [plan["plan_id"] for plan in replayed["completion_plans"]] == plan_ids

    history = read_completion_plan_ids(out / "ledger.sqlite", "orders")
    assert {row["plan_id"] for row in history} == set(plan_ids)

def test_broken_audit_exits_one_by_default(tmp_path, broken_config, capsys):
    code = main(
        [
            "audit",
            "--config",
            str(broken_config.config_path),
            "--out",
            str(tmp_path / "broken"),
        ]
    )
    output = capsys.readouterr().out
    assert code == 1
    assert "status: failed" in output

def test_fail_on_never_and_quiet(tmp_path, broken_config, capsys):
    out = tmp_path / "broken-never"
    code = main(
        [
            "audit",
            "--config",
            str(broken_config.config_path),
            "--out",
            str(out),
            "--fail-on",
            "never",
            "--quiet",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert (out / "ledger.sqlite").is_file()

def test_fail_on_warning_treats_warnings_as_failure(
    tmp_path, config_writer, capsys
):
    lake = tmp_path / "warning-lake"
    (lake / "_manifests" / "orders").mkdir(parents=True)
    (lake / "_manifests" / "clicks").mkdir(parents=True)
    config_path = config_writer(lake_root=lake)
    code = main(
        [
            "audit",
            "--config",
            str(config_path),
            "--out",
            str(tmp_path / "warnings"),
            "--fail-on",
            "warning",
        ]
    )
    assert code == 1
    assert "passed_with_warnings" in capsys.readouterr().out

def test_custom_ledger_path(tmp_path, demo_config):
    ledger = tmp_path / "custom" / "audit.sqlite"
    code = main(
        [
            "audit",
            "--config",
            str(demo_config.config_path),
            "--out",
            str(tmp_path / "reports"),
            "--ledger",
            str(ledger),
            "--quiet",
        ]
    )
    assert code == 0
    assert ledger.is_file()

def test_validate_command(demo_config, capsys):
    code = main(["validate", "--config", str(demo_config.config_path)])
    assert code == 0
    assert "config OK" in capsys.readouterr().out

def test_invalid_config_returns_operational_exit_code(config_writer, capsys):
    path = config_writer(raw="project: [broken\n")
    code = main(["validate", "--config", str(path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "lakeinlet: error:" in captured.err

def test_serve_rejects_non_loopback_host_before_start(tmp_path, capsys):
    code = main(
        [
            "serve",
            "--ledger",
            str(tmp_path / "anything.sqlite"),
            "--host",
            "0.0.0.0",
            "--port",
            "8099",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "refusing to bind" in captured.err

def test_main_rejects_missing_config(tmp_path, capsys):
    code = main(["validate", "--config", str(tmp_path / "missing.yaml")])
    assert code == 2
    assert "config file not found" in capsys.readouterr().err

def test_cli_audit_result_matches_library(demo_config, tmp_path):
    from lakeinlet.rules import run_audit

    expected = run_audit(load_config(demo_config.config_path))
    code = main(
        [
            "audit",
            "--config",
            str(demo_config.config_path),
            "--out",
            str(tmp_path / "check"),
            "--quiet",
        ]
    )
    assert code == 0
    from lakeinlet.ledger import read_runs

    runs = read_runs(tmp_path / "check" / "ledger.sqlite")
    assert runs[0]["run_id"] == expected.run_id
