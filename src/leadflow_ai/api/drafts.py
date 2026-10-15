"""Draft review and simulated outbox endpoints (M6). Every route requires the operator token.

Nothing here sends anything: approval records a ``simulated`` outbox row only.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from leadflow_ai.api.deps import JsonObjectBody, SessionDep
from leadflow_ai.auth import require_operator
from leadflow_ai.db.models import LeadDraftRecord
from leadflow_ai.drafts import DraftStatus, DraftTemplate
from leadflow_ai.services.drafts import (
    MAX_REASON_LENGTH,
    OutboxItem,
    approve_draft,
    create_draft,
    get_draft,
    get_outbox_item,
    list_drafts,
    list_outbox,
    parse_draft_request,
    parse_rejection,
    reject_draft,
)
from leadflow_ai.services.leads import DEFAULT_PAGE_LIMIT, MAX_OFFSET, MAX_PAGE_LIMIT, get_lead

DRAFTS_PREFIX = "/api/v1/drafts"
OUTBOX_PREFIX = "/api/v1/outbox"

router = APIRouter(prefix="/api/v1", tags=["drafts"], dependencies=[Depends(require_operator)])
outbox_router = APIRouter(
    prefix=OUTBOX_PREFIX, tags=["outbox"], dependencies=[Depends(require_operator)]
)


class DraftOut(BaseModel):
    """A rendered draft. Its text never changes; ``decided_at`` is set on approve/reject."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_id: str
    template: DraftTemplate
    template_version: str
    channel: Literal["email"]
    recipient: str
    subject: str
    body: str
    status: DraftStatus
    rejection_reason: str | None
    created_at: datetime
    decided_at: datetime | None


class DraftPage(BaseModel):
    items: list[DraftOut]
    limit: int
    offset: int


class OutboxMessageOut(BaseModel):
    """A simulated outbox item: nothing was sent. Content comes from the approved draft."""

    id: str
    draft_id: str
    lead_id: str
    status: Literal["simulated"]
    recipient: str
    subject: str
    body: str
    created_at: datetime


class OutboxPage(BaseModel):
    items: list[OutboxMessageOut]
    limit: int
    offset: int


class ApprovalOut(BaseModel):
    draft: DraftOut
    outbox_message: OutboxMessageOut


def _outbox_out(item: OutboxItem) -> OutboxMessageOut:
    # model_validate checks status against the Literal at runtime (no type: ignore).
    return OutboxMessageOut.model_validate(
        {
            "id": item.message.id,
            "draft_id": item.message.draft_id,
            "lead_id": item.message.lead_id,
            "status": item.message.status,
            "recipient": item.draft.recipient,
            "subject": item.draft.subject,
            "body": item.draft.body,
            "created_at": item.message.created_at,
        }
    )


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def _draft_or_404(session: SessionDep, draft_id: str) -> LeadDraftRecord:
    draft = get_draft(session, draft_id)
    if draft is None:
        raise _not_found("draft.not_found", "Draft not found")
    return draft


@router.post(
    "/leads/{lead_id}/drafts",
    status_code=201,
    response_model=DraftOut,
    summary="Create a draft from a fixed template (awaits human review)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["template"],
                        "properties": {
                            "template": {"type": "string", "enum": [t.value for t in DraftTemplate]}
                        },
                    }
                }
            },
        }
    },
)
def create(
    lead_id: str, payload: JsonObjectBody, session: SessionDep, response: Response
) -> DraftOut:
    template = parse_draft_request(payload)
    lead = get_lead(session, lead_id)
    if lead is None:
        raise _not_found("lead.not_found", "Lead not found")
    draft = create_draft(session, lead, template)
    response.headers["Location"] = f"{DRAFTS_PREFIX}/{draft.id}"
    return DraftOut.model_validate(draft)


@router.get(
    "/leads/{lead_id}/drafts", response_model=DraftPage, summary="A lead's drafts, newest first"
)
def list_for_lead(
    lead_id: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> DraftPage:
    lead = get_lead(session, lead_id)
    if lead is None:
        raise _not_found("lead.not_found", "Lead not found")
    drafts = list_drafts(session, lead.id, limit=limit, offset=offset)
    return DraftPage(
        items=[DraftOut.model_validate(draft) for draft in drafts], limit=limit, offset=offset
    )


@router.get("/drafts/{draft_id}", response_model=DraftOut, summary="Get a draft")
def retrieve(draft_id: str, session: SessionDep) -> DraftOut:
    return DraftOut.model_validate(_draft_or_404(session, draft_id))


@router.post(
    "/drafts/{draft_id}/approve",
    response_model=ApprovalOut,
    summary="Approve a draft and record a simulated outbox item (nothing is sent)",
)
def approve(draft_id: str, session: SessionDep) -> ApprovalOut:
    draft, message = approve_draft(session, _draft_or_404(session, draft_id))
    return ApprovalOut(
        draft=DraftOut.model_validate(draft), outbox_message=_outbox_out(OutboxItem(message, draft))
    )


@router.post(
    "/drafts/{draft_id}/reject",
    response_model=DraftOut,
    summary="Reject a draft (optional reason)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "reason": {"type": "string", "maxLength": MAX_REASON_LENGTH}
                        },
                    }
                }
            },
        }
    },
)
def reject(draft_id: str, payload: JsonObjectBody, session: SessionDep) -> DraftOut:
    reason = parse_rejection(payload)
    return DraftOut.model_validate(reject_draft(session, _draft_or_404(session, draft_id), reason))


@outbox_router.get(
    "", response_model=OutboxPage, summary="Simulated outbox items, newest first (never sent)"
)
def outbox_list(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> OutboxPage:
    items = list_outbox(session, limit=limit, offset=offset)
    return OutboxPage(items=[_outbox_out(item) for item in items], limit=limit, offset=offset)


@outbox_router.get("/{message_id}", response_model=OutboxMessageOut, summary="Get an outbox item")
def outbox_retrieve(message_id: str, session: SessionDep) -> OutboxMessageOut:
    item = get_outbox_item(session, message_id)
    if item is None:
        raise _not_found("outbox.not_found", "Outbox item not found")
    return _outbox_out(item)
