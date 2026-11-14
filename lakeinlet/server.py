"""Read-only loopback viewer for the SQLite audit ledger."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, jsonify, render_template

from .errors import ServeError
from .ledger import read_run, read_runs, verify_readable

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def validate_host(host: str) -> str:
    """Accept only explicit loopback hosts; never bind a wildcard interface."""
    if not isinstance(host, str) or host not in LOOPBACK_HOSTS:
        raise ServeError(
            f"refusing to bind viewer to {host!r}; allowed hosts are "
            "127.0.0.1, localhost, and ::1"
        )
    return host


def validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ServeError("port must be an integer between 1 and 65535")
    return port


def create_app(ledger_path: str | Path) -> Flask:
    """Create the read-only Flask app after verifying the ledger schema."""
    path = verify_readable(ledger_path)
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["LAKEINLET_LEDGER"] = str(path)
    app.config["LAKEINLET_READ_ONLY"] = True

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self'; "
            "script-src 'none'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        runs = read_runs(path)
        return render_template("index.html", runs=runs)

    @app.get("/run/<run_id>")
    def run_detail(run_id: str):
        run = read_run(path, run_id)
        if run is None:
            abort(404)
        return render_template("run_detail.html", run=run)

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "read_only": True})

    return app


def serve_viewer(
    ledger_path: str | Path, host: str = "127.0.0.1", port: int = 8088
) -> None:
    """Run the local viewer. Non-loopback hosts are rejected before startup."""
    validate_host(host)
    validate_port(port)
    app = create_app(ledger_path)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
