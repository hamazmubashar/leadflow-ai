"""Lead endpoints. Every route requires the operator bearer token."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from leadflow_ai.api.deps import JsonObjectBody, SessionDep
from leadflow_ai.auth import require_operator
from leadflow_ai.leads import LeadSource
from leadflow_ai.services.leads import (
    DEFAULT_PAGE_LIMIT,
    MAX_OFFSET,
    MAX_PAGE_LIMIT,
    create_lead,
    get_lead,
    list_leads,
)

PREFIX = "/api/v1/leads"

router = APIRouter(prefix=PREFIX, tags=["leads"], dependencies=[Depends(require_operator)])


class LeadOut(BaseModel):
    """A stored lead. ``email`` is lowercased and ``phone`` is E.164."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    full_name: str
    email: str | None
    phone: str | None
    company: str | None
    source: LeadSource
    notes: str | None
    created_at: datetime


class LeadPage(BaseModel):
    items: list[LeadOut]
    limit: int
    offset: int


# The body is parsed by a dependency (see api.deps), so describe it for OpenAPI here.
_LEAD_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["full_name"],
    "properties": {
        "full_name": {"type": "string", "maxLength": 100},
        "email": {"type": "string", "description": "Required if phone is absent"},
        "phone": {"type": "string", "description": "Required if email is absent"},
        "company": {"type": "string", "maxLength": 120},
        "source": {"type": "string", "enum": [source.value for source in LeadSource]},
        "notes": {"type": "string", "maxLength": 1000},
    },
}


@router.post(
    "",
    status_code=201,
    response_model=LeadOut,
    summary="Create a lead",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _LEAD_INPUT_SCHEMA}},
        }
    },
)
def create(payload: JsonObjectBody, session: SessionDep, response: Response) -> LeadOut:
    record = create_lead(session, payload)
    response.headers["Location"] = f"{PREFIX}/{record.id}"
    return LeadOut.model_validate(record)


@router.get("", response_model=LeadPage, summary="List leads, newest first")
def list_(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> LeadPage:
    records = list_leads(session, limit=limit, offset=offset)
    return LeadPage(
        items=[LeadOut.model_validate(record) for record in records], limit=limit, offset=offset
    )


@router.get("/{lead_id}", response_model=LeadOut, summary="Get a lead")
def retrieve(lead_id: str, session: SessionDep) -> LeadOut:
    record = get_lead(session, lead_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail={"code": "lead.not_found", "message": "Lead not found"}
        )
    return LeadOut.model_validate(record)
