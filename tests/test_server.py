from __future__ import annotations

import pytest

from lakeinlet.errors import LedgerError, ServeError
from lakeinlet.ledger import write_run
from lakeinlet.server import (
    LOOPBACK_HOSTS,
    create_app,
    validate_host,
    validate_port,
)

@pytest.fixture
def ledger(tmp_path, demo_result):
    path = tmp_path / "ledger.sqlite"
    write_run(demo_result, path)
    return path

@pytest.fixture
def client(ledger):
    app = create_app(ledger)
    app.config.update(TESTING=True)
    return app.test_client()

def test_only_explicit_loopback_hosts_are_allowed():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert host in LOOPBACK_HOSTS
        assert validate_host(host) == host
    for host in ("0.0.0.0", "127.0.0.2", "::", "example.test", "", "[::1]"):
        with pytest.raises(ServeError, match="refusing to bind"):
            validate_host(host)

def test_port_validation():
    assert validate_port(8088) == 8088
    for port in (0, 65536, -1, True):
        with pytest.raises(ServeError):
            validate_port(port)

def test_create_app_requires_existing_ledger(tmp_path):
    with pytest.raises(LedgerError):
        create_app(tmp_path / "missing.sqlite")

def test_index_lists_run(client, demo_result):
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "lakeinlet-demo" in body
    assert demo_result.run_id in body
    assert "passed" in body
    assert 'href="/static/style.css"' in body

def test_run_detail_shows_watermarks_and_batches(client, demo_result):
    response = client.get(f"/run/{demo_result.run_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "orders-eu-0001" in body
    assert "当前安全水位" in body
    assert "2025-03-01T01:50:00Z" in body
    assert "无发现项。" in body

def test_unknown_run_returns_404(client):
    assert client.get("/run/not-a-run").status_code == 404

def test_viewer_has_no_write_routes(client):
    assert client.post("/").status_code == 405
    assert client.post("/run/anything").status_code == 405
    rules = {rule.rule for rule in client.application.url_map.iter_rules()}
    assert "/" in rules
    assert "/run/<run_id>" in rules

def test_health_endpoint(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "read_only": True}

def test_security_headers_and_local_stylesheet(client):
    response = client.get("/")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "script-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    css = client.get("/static/style.css")
    assert css.status_code == 200
    assert b"--accent" in css.data

def test_broken_run_detail_shows_convergence_account(tmp_path, broken_result):
    path = tmp_path / "ledger.sqlite"
    write_run(broken_result, path)
    app = create_app(path)
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.get(f"/run/{broken_result.run_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "记录收敛账" in body
    assert "data/orders/copy.csv:2" in body
    assert "unparseable_event_time" in body
    assert "未修改入湖数据" not in body


def test_server_does_not_open_database_for_writes(ledger, client):
    import sqlite3

    read_only = sqlite3.connect(f"{ledger.resolve().as_uri()}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            read_only.execute("DELETE FROM runs")
    finally:
        read_only.close()
