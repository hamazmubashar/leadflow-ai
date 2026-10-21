"""Backups of the main SQLite database (M12).

Uses SQLite's online backup API, so a copy is consistent even while the API is writing.
Demo sandboxes are disposable and never backed up.

    python -m leadflow_ai.db.backup --to /backups                 # one backup now
    python -m leadflow_ai.db.backup --to /backups --every-hours 24  # keep running (Docker)

Each file is ``leadflow-YYYYMMDD-HHMMSS.db`` (UTC); only the newest ``--keep`` (default 7)
are kept. To restore: stop the API, copy a backup over the database file (and delete any
``-wal``/``-shm`` files next to it), then start the API.
"""

import argparse
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from leadflow_ai.config import DatabaseSettings

PREFIX = "leadflow-"
DEFAULT_KEEP = 7


def database_path(settings: DatabaseSettings) -> Path:
    """The SQLite file behind ``settings.database_url`` (validated as a local file URL)."""
    return Path(str(make_url(settings.database_url).database))


def backup(source: Path, target_dir: Path, *, now: datetime, keep: int = DEFAULT_KEEP) -> Path:
    """Copy ``source`` into ``target_dir``, then delete all but the newest ``keep`` copies."""
    if not source.is_file():
        raise FileNotFoundError("The database file does not exist yet")
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = target_dir / f"{PREFIX}{now.astimezone(UTC):%Y%m%d-%H%M%S}.db"
    partial = target.with_suffix(".db.partial")
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(partial)) as dst:
        src.backup(dst)
    partial.replace(target)  # never leave a half-written file under a backup name
    for old in sorted(target_dir.glob(f"{PREFIX}*.db"))[:-keep]:
        old.unlink()
    return target


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: DatabaseSettings | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_runs: int | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m leadflow_ai.db.backup", description="Back up the LeadFlow database."
    )
    parser.add_argument("--to", type=Path, required=True, help="folder for the backups")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="how many to keep")
    parser.add_argument("--every-hours", type=float, help="repeat forever, this often")
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    source = database_path(settings if settings is not None else DatabaseSettings())
    runs = 0
    while True:
        try:
            print(f"Backed up to {backup(source, args.to, now=clock(), keep=args.keep).name}")
        except (OSError, sqlite3.Error) as error:
            print(f"Backup failed: {error}", file=sys.stderr)
            if args.every_hours is None:
                return 1
        runs += 1
        if args.every_hours is None or (max_runs is not None and runs >= max_runs):
            return 0
        sleep(args.every_hours * 3600)


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
