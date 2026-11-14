"""Command-line interface for lakeinlet."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .errors import LakeInletError
from .ledger import write_run
from .report_html import write_report_html
from .report_json import write_report_json
from .report_md import write_report_markdown
from .rules import run_audit
from .server import serve_viewer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lakeinlet",
        description=(
            "Offline data-lake ingestion watermark, lateness, sequence, and "
            "idempotency auditor."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit", help="run a complete audit")
    audit.add_argument("--config", required=True, help="path to config.yaml")
    audit.add_argument(
        "--out", default="out/audit", help="output directory for reports and ledger"
    )
    audit.add_argument(
        "--ledger",
        default=None,
        help="override ledger path (default: <out>/ledger.sqlite)",
    )
    audit.add_argument(
        "--fail-on",
        choices=("error", "warning", "never"),
        default="error",
        help="exit non-zero when this severity is present (default: error)",
    )
    audit.add_argument("--quiet", action="store_true", help="suppress the summary")

    validate = commands.add_parser("validate", help="validate configuration only")
    validate.add_argument("--config", required=True, help="path to config.yaml")

    serve = commands.add_parser("serve", help="start the read-only local viewer")
    serve.add_argument("--ledger", required=True, help="path to ledger.sqlite")
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback host only (127.0.0.1, localhost, or ::1)",
    )
    serve.add_argument("--port", type=int, default=8088, help="local TCP port")
    return parser


def _cmd_audit(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = Path(args.ledger) if args.ledger else out_dir / "ledger.sqlite"
    report_json = out_dir / "report.json"
    report_md = out_dir / "report.md"
    report_html = out_dir / "report.html"

    result = run_audit(config)
    write_report_json(result, report_json)
    write_report_markdown(result, report_md)
    write_report_html(result, report_html)
    write_run(
        result,
        ledger_path,
        artifacts={
            "json": str(report_json.resolve()),
            "md": str(report_md.resolve()),
            "html": str(report_html.resolve()),
        },
    )

    if not args.quiet:
        print(f"project: {result.project}")
        print(f"run_id: {result.run_id}")
        print(f"status: {result.status}")
        print(
            "summary: "
            f"{result.manifest_count} manifests, {len(result.batches)} batches, "
            f"{result.file_count} files, {result.record_count} records"
        )
        print(
            f"findings: {result.finding_count} "
            f"(errors={result.error_count}, warnings={result.warning_count}, "
            f"info={result.info_count})"
        )
        print(f"json_report: {report_json}")
        print(f"markdown_report: {report_md}")
        print(f"html_report: {report_html}")
        print(f"ledger: {ledger_path}")

    if args.fail_on == "never":
        return 0
    if args.fail_on == "warning":
        return 1 if result.error_count or result.warning_count else 0
    return 1 if result.error_count else 0


def _cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(
        f"config OK: project={config.project!r}, "
        f"as_of={config.as_of.isoformat()}, sources={len(config.sources)}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            return _cmd_audit(args)
        if args.command == "validate":
            return _cmd_validate(args)
        if args.command == "serve":
            serve_viewer(args.ledger, args.host, args.port)
            return 0
    except LakeInletError as exc:
        print(f"lakeinlet: error: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
