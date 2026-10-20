"""Manage operator accounts from the command line (M11). There is no sign-up page.

    uv run python -m leadflow_ai.manage_users create alice --role admin
    uv run python -m leadflow_ai.manage_users create demo --role viewer
    uv run python -m leadflow_ai.manage_users set-password alice
    uv run python -m leadflow_ai.manage_users deactivate demo
    uv run python -m leadflow_ai.manage_users activate demo
    uv run python -m leadflow_ai.manage_users list

Passwords are typed at a hidden prompt (twice), never passed as arguments, so they
don't end up in shell history or the process list. Only ``LEADFLOW_DATABASE_URL`` is
read; the operator token is not needed.
"""

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from leadflow_ai.accounts import AccountRuleError, Role
from leadflow_ai.config import DatabaseSettings
from leadflow_ai.db.engine import create_db_engine
from leadflow_ai.db.session import create_session_factory
from leadflow_ai.services.accounts import (
    DuplicateUsernameError,
    UnknownUserError,
    create_user,
    list_users,
    set_active,
    set_password,
)

Prompt = Callable[[str], str]
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt: Prompt = getpass.getpass,
    settings: DatabaseSettings | None = None,
) -> int:
    args = _parser().parse_args(argv)
    engine = create_db_engine(settings if settings is not None else DatabaseSettings())
    try:
        with create_session_factory(engine)() as session:
            return _run(session, args, prompt)
    except OperationalError:
        _err("The database is missing or not up to date. Run: uv run alembic upgrade head")
        return EXIT_ERROR
    finally:
        engine.dispose()


def _run(session: Session, args: argparse.Namespace, prompt: Prompt) -> int:
    try:
        if args.command == "create":
            password = _new_password(prompt)
            if password is None:
                return EXIT_USAGE
            user = create_user(session, args.username, password, Role(args.role))
            print(f"Created {user.role} account '{user.username}'.")
        elif args.command == "set-password":
            password = _new_password(prompt)
            if password is None:
                return EXIT_USAGE
            set_password(session, args.username, password)
            print("Password changed. Existing sessions of this account were ended.")
        elif args.command in ("activate", "deactivate"):
            set_active(session, args.username, args.command == "activate")
            print(f"Account {args.command}d.")
        else:
            for user in list_users(session):
                state = "active" if user.is_active else "inactive"
                print(f"{user.username}\t{user.role}\t{state}")
    except AccountRuleError as error:
        _err(error.message)
        return EXIT_USAGE
    except DuplicateUsernameError:
        _err("An account with this username already exists.")
        return EXIT_ERROR
    except UnknownUserError:
        _err("There is no such account.")
        return EXIT_ERROR
    return EXIT_OK


def _new_password(prompt: Prompt) -> str | None:
    first = prompt("New password (at least 12 characters): ")
    second = prompt("Repeat the password: ")
    if first != second:
        _err("The passwords don't match; nothing was changed.")
        return None
    return first


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m leadflow_ai.manage_users", description="Manage LeadFlow AI accounts."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create an account")
    create.add_argument("username")
    create.add_argument("--role", choices=[role.value for role in Role], required=True)
    for name, text in (
        ("set-password", "set a new password (ends the account's sessions)"),
        ("activate", "allow the account to log in"),
        ("deactivate", "stop the account logging in (ends its sessions)"),
    ):
        commands.add_parser(name, help=text).add_argument("username")
    commands.add_parser("list", help="list accounts")
    return parser


def _err(message: str) -> None:
    print(message, file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
