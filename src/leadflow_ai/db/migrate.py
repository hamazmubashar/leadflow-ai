"""Apply database migrations without ``alembic.ini`` (M12).

``alembic.ini`` points at the source tree, which a container doesn't have. This uses the
migration scripts that ship inside the package, so the Docker image can run:

    python -m leadflow_ai.db.migrate     # upgrade LEADFLOW_DATABASE_URL to the latest schema
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

# The Alembic scripts ship inside the package (src/leadflow_ai/db/migrations).
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def upgrade(database_url: str | None = None) -> None:
    """Upgrade ``database_url`` (default: ``LEADFLOW_DATABASE_URL``) to the newest revision."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


if __name__ == "__main__":  # pragma: no cover - exercised through upgrade() in tests
    upgrade()
