"""The per-request database session, shared by authentication and the route itself.

FastAPI caches a dependency per request, so the session that resolves a login token is
the same one the route then uses: one connection per request.
"""

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from leadflow_ai.db.session import Database, session_scope


def get_session(request: Request) -> Generator[Session]:
    """One session per request; the engine is created on first use."""
    database: Database = request.app.state.database
    yield from session_scope(database.session_factory())


SessionDep = Annotated[Session, Depends(get_session)]
