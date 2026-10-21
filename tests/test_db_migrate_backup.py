"""M12: migrations without alembic.ini, and backups of the main database."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.backup import backup, database_path, main
from leadflow_ai.db.migrate import upgrade

T0 = datetime(2026, 10, 21, 2, 0, tzinfo=UTC)


def revision(path: Path) -> str:
    with closing(sqlite3.connect(path)) as db:
        return str(db.execute("SELECT version_num FROM alembic_version").fetchone()[0])


def test_upgrade_a_given_url(tmp_path: Path) -> None:
    target = tmp_path / "fresh.db"
    upgrade(f"sqlite:///{target}")
    assert revision(target) == "0006"


def test_upgrade_defaults_to_the_configured_database(database_url: str) -> None:
    upgrade()
    assert revision(Path(database_url.removeprefix("sqlite:///"))) == "0006"


def test_database_path(database_url: str) -> None:
    settings = DatabaseSettings(_env_file=None, database_url=database_url)
    assert database_path(settings) == Path(database_url.removeprefix("sqlite:///"))


@pytest.fixture
def source(migrated_engine: Engine, database_url: str) -> Path:
    path = Path(database_url.removeprefix("sqlite:///"))
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO leads (id, full_name, email, source, created_at, status) VALUES"
            " ('00000000-0000-4000-8000-000000000001', 'A', 'a@example.com', 'web_form',"
            " '2026-10-21 00:00:00.000000', 'new')"
        )
        db.commit()
    return path


def test_a_backup_is_a_complete_consistent_copy(source: Path, tmp_path: Path) -> None:
    made = backup(source, tmp_path / "backups", now=T0)
    assert made.name == "leadflow-20261021-020000.db"
    with closing(sqlite3.connect(made)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT count(*) FROM leads").fetchone() == (1,)
    assert list((tmp_path / "backups").glob("*.partial")) == []


def test_only_the_newest_are_kept(source: Path, tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    for hours in range(4):
        backup(source, folder, now=T0 + timedelta(hours=hours), keep=2)
    assert sorted(p.name for p in folder.iterdir()) == [
        "leadflow-20261021-040000.db",
        "leadflow-20261021-050000.db",
    ]


def test_a_missing_database_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backup(tmp_path / "nope.db", tmp_path / "backups", now=T0)


def test_main_once(
    source: Path, tmp_path: Path, database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = DatabaseSettings(_env_file=None, database_url=database_url)
    code = main(["--to", str(tmp_path / "b")], settings=settings, clock=lambda: T0)
    assert code == 0
    assert "leadflow-20261021-020000.db" in capsys.readouterr().out


def test_main_reports_a_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = DatabaseSettings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'x.db'}")
    assert main(["--to", str(tmp_path / "b")], settings=settings) == 1
    assert "Backup failed" in capsys.readouterr().err


def test_main_repeats_and_survives_a_failed_run(tmp_path: Path) -> None:
    settings = DatabaseSettings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'x.db'}")
    sleeps: list[float] = []
    code = main(
        ["--to", str(tmp_path / "b"), "--every-hours", "24"],
        settings=settings,
        sleep=sleeps.append,
        max_runs=2,
    )
    assert code == 0
    assert sleeps == [86400.0]


def test_keep_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["--to", str(tmp_path), "--keep", "0"])
    assert stopped.value.code == 2


def test_default_settings(database_url: str, source: Path, tmp_path: Path) -> None:
    assert main(["--to", str(tmp_path / "b")], clock=lambda: T0) == 0
