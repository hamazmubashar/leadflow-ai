"""Request dependencies: database session and authenticated JSON body."""

import json
from collections.abc import Generator
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from leadflow_ai.api.errors import InvalidRequestBodyError, UnsupportedMediaTypeError
from leadflow_ai.auth import OperatorAuth
from leadflow_ai.db.session import Database, session_scope

JSON_MEDIA_TYPE = "application/json"


def get_session(request: Request) -> Generator[Session]:
    """One session per request; the engine is created on first use."""
    database: Database = request.app.state.database
    yield from session_scope(database.session_factory())


async def json_object_body(request: Request, _: OperatorAuth) -> dict[str, object]:
    """Parse the body as one JSON object, only after authentication succeeds.

    FastAPI parses declared body parameters *before* running dependencies, so
    an unauthenticated client would see 422s. Reading the body here instead
    guarantees the order 413 (middleware) -> 401 -> 415 -> 422 and keeps parse
    errors privacy-safe.
    """
    if _media_type(request.headers.get("content-type")) != JSON_MEDIA_TYPE:
        raise UnsupportedMediaTypeError
    raw = await request.body()
    try:
        data: Any = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError):  # bad JSON, bad UTF-8, duplicate keys, deep nesting
        raise InvalidRequestBodyError from None
    if not isinstance(data, dict):
        raise InvalidRequestBodyError
    return data


def _media_type(content_type: str | None) -> str | None:
    """``"Application/JSON; charset=utf-8"`` -> ``"application/json"``."""
    if content_type is None:
        return None
    return content_type.split(";", 1)[0].strip().lower()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


SessionDep = Annotated[Session, Depends(get_session)]
JsonObjectBody = Annotated[dict[str, object], Depends(json_object_body)]
