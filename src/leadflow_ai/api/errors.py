"""Error responses. None of them echo submitted values or raw database errors."""

import sqlite3
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from leadflow_ai.leads import LeadValidationError
from leadflow_ai.services.leads import DuplicateLeadError

RETRY_AFTER_SECONDS = "1"


class InvalidRequestBodyError(Exception):
    """The request body is not a single JSON object."""


class UnsupportedMediaTypeError(Exception):
    """The request body is not declared as ``application/json``."""


def _error(status_code: int, detail: dict[str, object], **headers: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail}, headers=headers)


async def _request_validation(request: Request, exc: Exception) -> JSONResponse:
    # Replaces FastAPI's default 422, which includes the submitted "input".
    validation_error = cast(RequestValidationError, exc)
    errors = [
        {
            "location": [str(part) for part in error["loc"]],
            "code": error["type"],
            "message": error["msg"],
        }
        for error in validation_error.errors()
    ]
    return _error(422, {"code": "request.invalid", "errors": errors})


async def _invalid_body(request: Request, exc: Exception) -> JSONResponse:
    return _error(
        422,
        {"code": "request.invalid_body", "message": "Request body must be a single JSON object"},
    )


async def _unsupported_media_type(request: Request, exc: Exception) -> JSONResponse:
    return _error(
        415,
        {
            "code": "request.unsupported_media_type",
            "message": "Content-Type must be application/json",
        },
    )


async def _lead_invalid(request: Request, exc: Exception) -> JSONResponse:
    lead_error = cast(LeadValidationError, exc)
    errors = [
        {"field": error.field, "code": error.code, "message": error.message}
        for error in lead_error.errors
    ]
    return _error(422, {"code": "lead.invalid", "errors": errors})


async def _lead_duplicate(request: Request, exc: Exception) -> JSONResponse:
    duplicate = cast(DuplicateLeadError, exc)
    conflicts = [
        {"field": conflict.field, "lead_id": conflict.lead_id} for conflict in duplicate.conflicts
    ]
    return _error(
        409,
        {
            "code": "lead.duplicate",
            "message": "A lead with this email or phone already exists",
            "conflicts": conflicts,
        },
    )


async def _database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    original = cast(OperationalError, exc).orig
    if isinstance(original, sqlite3.OperationalError):
        error_name = getattr(original, "sqlite_errorname", None)
        if error_name == "SQLITE_BUSY":
            return _error(
                503,
                {"code": "database.busy", "message": "The database is busy; retry shortly"},
                **{"Retry-After": RETRY_AFTER_SECONDS},
            )
        if error_name == "SQLITE_CANTOPEN":
            # Missing or unwritable directory. Never include the configured path.
            return _error(
                503,
                {
                    "code": "database.unavailable",
                    "message": "The database file cannot be opened; check LEADFLOW_DATABASE_URL",
                },
            )
        if str(original).startswith("no such table:"):
            return _error(
                503,
                {
                    "code": "database.not_migrated",
                    "message": "The database schema is missing; run: alembic upgrade head",
                },
            )
    # Anything else is unexpected: let it surface as a plain 500. Bound
    # parameters are already hidden by the engine (hide_parameters=True).
    raise exc


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _request_validation)
    app.add_exception_handler(InvalidRequestBodyError, _invalid_body)
    app.add_exception_handler(UnsupportedMediaTypeError, _unsupported_media_type)
    app.add_exception_handler(LeadValidationError, _lead_invalid)
    app.add_exception_handler(DuplicateLeadError, _lead_duplicate)
    app.add_exception_handler(OperationalError, _database_unavailable)
