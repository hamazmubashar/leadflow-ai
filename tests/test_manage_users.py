"""M11 account command line: create, set-password, activate/deactivate, list."""

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from leadflow_ai.accounts import verify_password
from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.models import UserRecord
from leadflow_ai.manage_users import EXIT_ERROR, EXIT_OK, EXIT_USAGE, main

PASSWORD = "correct horse battery"


def answers(*values: str) -> Callable[[str], str]:
    queue: Iterator[str] = iter(values)
    return lambda _prompt: next(queue)


@pytest.fixture
def settings(migrated_engine: Engine, database_url: str) -> DatabaseSettings:
    return DatabaseSettings(_env_file=None, database_url=database_url)


def run(settings: DatabaseSettings, *argv: str, prompt: Callable[[str], str] | None = None) -> int:
    return main(list(argv), prompt=prompt or answers(PASSWORD, PASSWORD), settings=settings)


def user(migrated_engine: Engine, username: str) -> UserRecord:
    with Session(migrated_engine) as session:
        return session.scalars(select(UserRecord).where(UserRecord.username == username)).one()


def test_create_list_and_manage(
    settings: DatabaseSettings, migrated_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(settings, "create", "Alice", "--role", "admin") == EXIT_OK
    assert run(settings, "create", "demo", "--role", "viewer") == EXIT_OK
    assert verify_password(PASSWORD, user(migrated_engine, "alice").password_hash)
    assert run(settings, "deactivate", "demo") == EXIT_OK
    assert run(settings, "list") == EXIT_OK
    out = capsys.readouterr().out
    assert "Created admin account 'alice'." in out
    assert "Account deactivated." in out
    assert "alice\tadmin\tactive\ndemo\tviewer\tinactive\n" in out
    assert "system" not in out
    assert PASSWORD not in out
    assert run(settings, "activate", "demo") == EXIT_OK
    assert user(migrated_engine, "demo").is_active


def test_set_password(settings: DatabaseSettings, migrated_engine: Engine) -> None:
    run(settings, "create", "alice", "--role", "admin")
    new = "another long passphrase"
    assert run(settings, "set-password", "alice", prompt=answers(new, new)) == EXIT_OK
    assert verify_password(new, user(migrated_engine, "alice").password_hash)


@pytest.mark.parametrize("command", [["create", "bob", "--role", "admin"], ["set-password", "bob"]])
def test_mismatched_passwords_change_nothing(
    settings: DatabaseSettings, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    if command[0] == "set-password":
        run(settings, "create", "bob", "--role", "admin")
    code = run(settings, *command, prompt=answers(PASSWORD, PASSWORD + "x"))
    assert code == EXIT_USAGE
    assert "don't match" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "prompt", "code", "message"),
    [
        (["create", "system", "--role", "admin"], None, EXIT_USAGE, "reserved"),
        (["create", "bob", "--role", "admin"], ("short", "short"), EXIT_USAGE, "characters"),
        (["deactivate", "nobody"], None, EXIT_ERROR, "no such account"),
        (["set-password", "system"], None, EXIT_ERROR, "no such account"),
    ],
)
def test_refusals(
    settings: DatabaseSettings,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    prompt: tuple[str, str] | None,
    code: int,
    message: str,
) -> None:
    assert run(settings, *argv, prompt=answers(*prompt) if prompt else None) == code
    assert message in capsys.readouterr().err


def test_duplicate(settings: DatabaseSettings, capsys: pytest.CaptureFixture[str]) -> None:
    run(settings, "create", "alice", "--role", "admin")
    assert run(settings, "create", "ALICE", "--role", "viewer") == EXIT_ERROR
    assert "already exists" in capsys.readouterr().err


def _no_prompt(_prompt: str) -> str:
    raise AssertionError("asked for a password")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["create", "ALICE", "--role", "viewer"], "already exists"),
        (["create", "system", "--role", "admin"], "reserved"),
        (["create", "x", "--role", "admin"], "sername"),
        (["set-password", "nobody"], "no such account"),
        (["set-password", "system"], "no such account"),
    ],
)
def test_bad_usernames_are_refused_before_any_password_prompt(
    settings: DatabaseSettings, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    run(settings, "create", "alice", "--role", "admin")
    assert run(settings, *argv, prompt=_no_prompt) in (EXIT_ERROR, EXIT_USAGE)
    assert message in capsys.readouterr().err


def test_an_unmigrated_database_is_explained(
    database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = DatabaseSettings(_env_file=None, database_url=database_url)  # no migrations
    assert run(settings, "list") == EXIT_ERROR
    assert "alembic upgrade head" in capsys.readouterr().err


def test_default_settings_are_read_from_the_environment(
    migrated_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """``database_url`` comes from LEADFLOW_DATABASE_URL (set by the test fixture)."""
    assert main(["list"], prompt=answers()) == EXIT_OK


def test_bad_usage_exits_with_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["create", "alice"])  # --role is required
    assert caught.value.code == EXIT_USAGE
