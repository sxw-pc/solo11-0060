from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lakeinlet.config import load_config  # noqa: E402
from lakeinlet.rules import run_audit  # noqa: E402

DEMO_CONFIG = ROOT / "fixtures" / "demo" / "config.yaml"
BROKEN_CONFIG = ROOT / "fixtures" / "broken" / "config.yaml"
DEMO_LAKE = ROOT / "fixtures" / "demo" / "lake"
BROKEN_LAKE = ROOT / "fixtures" / "broken" / "lake"

@pytest.fixture(scope="session")
def demo_config():
    return load_config(DEMO_CONFIG)

@pytest.fixture(scope="session")
def broken_config():
    return load_config(BROKEN_CONFIG)

@pytest.fixture(scope="session")
def demo_result(demo_config):
    return run_audit(demo_config)

@pytest.fixture(scope="session")
def broken_result(broken_config):
    return run_audit(broken_config)

@pytest.fixture
def config_writer(tmp_path: Path) -> Callable:
    """Write a modified copy of the demo config into a temporary directory."""

    def write(
        mutator: Callable[[dict], None] | None = None,
        *,
        raw: str | None = None,
        lake_root: Path | None = None,
        name: str = "config.yaml",
    ) -> Path:
        path = tmp_path / name
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return path
        data = yaml.safe_load(DEMO_CONFIG.read_text(encoding="utf-8"))
        data["lake_root"] = str((lake_root or DEMO_LAKE).as_posix())
        if mutator is not None:
            mutator(data)
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
            newline="\n",
        )
        return path

    return write
