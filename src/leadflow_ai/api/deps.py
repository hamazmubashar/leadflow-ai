"""Request dependencies: database session and authenticated JSON or CSV bodies."""

import json
from typing import Annotated, Any

from fastapi import Depends, Request

from leadflow_ai.api.db_session import SessionDep, get_session
from leadflow_ai.api.errors import InvalidRequestBodyError, UnsupportedMediaTypeError
from leadflow_ai.auth import AuthorizedUser
from leadflow_ai.scoring.rules import ScoringRules

__all__ = [
    "CsvBody",
    "JsonObjectBody",
    "PublicJsonObjectBody",
    "ScoringRulesDep",
    "SessionDep",
    "get_session",
]

JSON_MEDIA_TYPE = "application/json"
CSV_MEDIA_TYPE = "text/csv"
CSV_EXPECTED = "text/csv (UTF-8)"


def get_scoring_rules(request: Request) -> ScoringRules:
    """The rules validated at startup (see ``create_app``)."""
    rules: ScoringRules = request.app.state.scoring_rules
    return rules


async def json_object_body(request: Request, _: AuthorizedUser) -> dict[str, object]:
    """Parse the body as one JSON object, only after authentication and the role check.

    FastAPI parses declared body parameters *before* running dependencies, so
    an unauthenticated client would see 422s. Reading the body here instead
    guarantees the order 413 (middleware) -> 401 -> 403 -> 415 -> 422 and keeps
    parse errors privacy-safe.
    """
    return await _read_json_object(request)


async def public_json_object_body(request: Request) -> dict[str, object]:
    """The same parsing for the one route that has no caller yet: login."""
    return await _read_json_object(request)


async def _read_json_object(request: Request) -> dict[str, object]:
    if _media_type(request.headers.get("content-type")) != JSON_MEDIA_TYPE:
        raise UnsupportedMediaTypeError(JSON_MEDIA_TYPE)
    raw = await request.body()
    try:
        data: Any = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (ValueError, RecursionError):  # bad JSON, bad UTF-8, duplicate keys, deep nesting
        raise InvalidRequestBodyError from None
    if not isinstance(data, dict):
        raise InvalidRequestBodyError
    return data


async def csv_body(request: Request, _: AuthorizedUser) -> bytes:
    """Return the raw CSV bytes, only after authentication and the role check.

    Order: 413 (middleware) -> 401 -> 403 -> 415 here -> 422 when the file is read.
    The media type must be ``text/csv``; a ``charset`` parameter, if given, must
    be UTF-8. Nothing is written to disk.
    """
    content_type = request.headers.get("content-type")
    if _media_type(content_type) != CSV_MEDIA_TYPE or _charset(content_type) not in (None, "utf-8"):
        raise UnsupportedMediaTypeError(CSV_EXPECTED)
    return await request.body()


def _charset(content_type: str | None) -> str | None:
    """``"text/csv; Charset=\"UTF-8\""`` -> ``"utf-8"``; ``None`` if absent."""
    for parameter in (content_type or "").split(";")[1:]:
        name, _, value = parameter.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip('"').lower()
    return None


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


JsonObjectBody = Annotated[dict[str, object], Depends(json_object_body)]
PublicJsonObjectBody = Annotated[dict[str, object], Depends(public_json_object_body)]
CsvBody = Annotated[bytes, Depends(csv_body)]
ScoringRulesDep = Annotated[ScoringRules, Depends(get_scoring_rules)]
