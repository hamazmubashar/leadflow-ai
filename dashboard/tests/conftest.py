from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Run each test in an empty folder with no LEADFLOW_* variables.

    Settings read .env from the working directory, so this keeps the real
    project .env (and its token) out of every test.
    """
    for name in ("API_URL", "OPERATOR_TOKEN", "REQUEST_TIMEOUT_SECONDS", "DATABASE_URL"):
        monkeypatch.delenv(f"LEADFLOW_{name}", raising=False)
    monkeypatch.chdir(tmp_path)
    yield tmp_path
