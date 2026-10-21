"""Lead endpoints. Every route requires sign-in; changes require the admin role."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from leadflow_ai.api.deps import CsvBody, JsonObjectBody, ScoringRulesDep, SessionDep
from leadflow_ai.auth import AuthorizedUser, authorize
from leadflow_ai.db.models import LeadEventRecord
from leadflow_ai.demo.sandboxes import MAX_DEMO_IMPORT_ROWS, DemoLimitError, check_demo_lead_quota
from leadflow_ai.imports.csv_reader import read_leads_csv
from leadflow_ai.leads import LeadSource
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.scoring.engine import score_lead
from leadflow_ai.services.imports import import_leads
from leadflow_ai.services.leads import (
    DEFAULT_PAGE_LIMIT,
    MAX_OFFSET,
    MAX_PAGE_LIMIT,
    create_lead,
    get_lead,
    list_leads,
)
from leadflow_ai.services.lifecycle import change_status, parse_status_change, read_activity

PREFIX = "/api/v1/leads"
IMPORT_PATH = f"{PREFIX}/import"

router = APIRouter(prefix=PREFIX, tags=["leads"], dependencies=[Depends(authorize)])


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


class ScoreFactorOut(BaseModel):
    """One rule's contribution. ``rule`` is a fixed rule name, never a lead value."""

    rule: str
    matched: bool
    points: int


class ScoreOut(BaseModel):
    """A prioritization heuristic from configurable rules, not a conversion prediction."""

    lead_id: str
    score: int
    max_score: int
    tier: Literal["hot", "warm", "cold"]
    rules_version: str
    factors: list[ScoreFactorOut]


class LeadEventOut(BaseModel):
    """One activity event. ``sequence`` orders events; ``occurred_at`` is UTC."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    sequence: int
    type: Literal["lead_created", "status_changed"]
    from_status: LeadStatus | None
    to_status: LeadStatus
    note: str | None
    occurred_at: datetime


class StatusChangeOut(BaseModel):
    lead_id: str
    status: LeadStatus
    event: LeadEventOut


class ActivityOut(BaseModel):
    """A lead's current status and its history, oldest first."""

    lead_id: str
    status: LeadStatus
    items: list[LeadEventOut]
    limit: int
    offset: int


class ImportOut(BaseModel):
    """Result of an all-or-nothing CSV import. ``lead_ids`` are in file order."""

    imported: int
    lead_ids: list[str]


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
def create(
    payload: JsonObjectBody, session: SessionDep, response: Response, principal: AuthorizedUser
) -> LeadOut:
    if principal.is_demo:
        check_demo_lead_quota(session, 1)
    record = create_lead(session, payload, actor_id=principal.user_id)
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


@router.post(
    "/import",
    status_code=201,
    response_model=ImportOut,
    summary="Import leads from a CSV file (all or nothing)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
def import_csv(body: CsvBody, session: SessionDep, principal: AuthorizedUser) -> ImportOut:
    parsed = read_leads_csv(body)
    if principal.is_demo:
        if len(parsed.rows) > MAX_DEMO_IMPORT_ROWS:
            raise DemoLimitError
        check_demo_lead_quota(session, len(parsed.rows))
    result = import_leads(session, parsed, actor_id=principal.user_id)
    return ImportOut(imported=result.imported, lead_ids=list(result.lead_ids))


@router.get(
    "/{lead_id}/score",
    response_model=ScoreOut,
    summary="Score a lead with the configured rules (calculated on request)",
)
def score(lead_id: str, session: SessionDep, rules: ScoringRulesDep) -> ScoreOut:
    record = get_lead(session, lead_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail={"code": "lead.not_found", "message": "Lead not found"}
        )
    result = score_lead(record, rules)
    return ScoreOut(
        lead_id=record.id,
        score=result.score,
        max_score=result.max_score,
        tier=result.tier,
        rules_version=result.rules_version,
        factors=[
            ScoreFactorOut(rule=f.rule, matched=f.matched, points=f.points) for f in result.factors
        ],
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404, detail={"code": "lead.not_found", "message": "Lead not found"}
    )


def _event_out(event: LeadEventRecord) -> LeadEventOut:
    # model_validate checks event_type against the Literal at runtime (no type: ignore).
    return LeadEventOut.model_validate(
        {
            "id": event.id,
            "sequence": event.sequence,
            "type": event.event_type,
            "from_status": event.from_status,
            "to_status": event.to_status,
            "note": event.note,
            "occurred_at": event.occurred_at,
        }
    )


@router.post(
    "/{lead_id}/status",
    response_model=StatusChangeOut,
    summary="Change a lead's status (requires the expected current status)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["status", "expected_status"],
                        "properties": {
                            "status": {"type": "string", "enum": [s.value for s in LeadStatus]},
                            "expected_status": {
                                "type": "string",
                                "enum": [s.value for s in LeadStatus],
                            },
                            "note": {"type": "string", "maxLength": 500},
                        },
                    }
                }
            },
        }
    },
)
def change_lead_status(
    lead_id: str, payload: JsonObjectBody, session: SessionDep, principal: AuthorizedUser
) -> StatusChangeOut:
    request = parse_status_change(payload)
    record = get_lead(session, lead_id)
    if record is None:
        raise _not_found()
    event = change_status(session, record, request, actor_id=principal.user_id)
    return StatusChangeOut(
        lead_id=record.id, status=LeadStatus(record.status), event=_event_out(event)
    )


@router.get(
    "/{lead_id}/activity",
    response_model=ActivityOut,
    summary="A lead's current status and activity history, oldest first",
)
def lead_activity(
    lead_id: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> ActivityOut:
    record = get_lead(session, lead_id)
    if record is None:
        raise _not_found()
    # Status and items come from one consistent read (see read_activity).
    page = read_activity(session, record.id, limit=limit, offset=offset)
    return ActivityOut(
        lead_id=record.id,
        status=page.status,
        items=[_event_out(event) for event in page.events],
        limit=limit,
        offset=offset,
    )
