"""The default scoring rules must ship inside the built wheel."""

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from leadflow_ai.scoring.rules import parse_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOURCE = "leadflow_ai/scoring/default_rules.toml"


def _uv() -> str:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.fail("uv is required to build the wheel (run the tests with `uv run pytest`)")
    return uv


def _build_wheel(out_dir: Path) -> Path:
    base = [_uv(), "build", "--wheel", "--out-dir", str(out_dir), str(REPO_ROOT)]
    # Offline first (the network-blocked test run); fall back to a normal build on a
    # machine whose uv cache does not yet hold the build backend (e.g. fresh CI).
    for command in ([*base[:2], "--offline", *base[2:]], base):
        result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
        if result.returncode == 0:
            break
    else:
        pytest.fail(f"wheel build failed:\n{result.stderr[-2000:]}")
    [wheel] = out_dir.glob("*.whl")
    return wheel


def test_default_rules_are_in_the_built_wheel_and_valid(tmp_path: Path) -> None:
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        assert RESOURCE in archive.namelist()
        packaged = archive.read(RESOURCE)
    assert packaged == (REPO_ROOT / "src" / RESOURCE).read_bytes()
    assert parse_rules(packaged).rules_version == "2026-10-15.1"
