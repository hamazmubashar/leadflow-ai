"""AI-assisted draft endpoints (M9). Every route requires sign-in; creating needs admin.

``POST /api/v1/leads/{lead_id}/drafts/ai`` asks the local model for a draft body and
stores an ordinary ``pending_review`` draft: a person still approves or rejects it on
the M6 endpoints, and approval still only records a *simulated* outbox item.

AI drafts are off unless ``LEADFLOW_LLM_URL`` points to a loopback model server.
At most one generation runs at a time in this process; a second request gets
``llm.busy`` at once instead of queueing minute-long calls.
"""

import threading
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from leadflow_ai.api.deps import JsonObjectBody, SessionDep
from leadflow_ai.api.drafts import DRAFTS_PREFIX, DraftOut
from leadflow_ai.assist import GenerationError, GenerationErrorCode, TextGenerator
from leadflow_ai.auth import AuthorizedUser, authorize
from leadflow_ai.drafts import DraftTemplate
from leadflow_ai.services.ai_drafts import create_ai_draft
from leadflow_ai.services.drafts import parse_draft_request
from leadflow_ai.services.leads import get_lead

AI_DRAFT_PATH = "/api/v1/leads/{lead_id}/drafts/ai"
STATUS_PATH = "/api/v1/assist/status"

router = APIRouter(tags=["assist"], dependencies=[Depends(authorize)])


class AssistStatusOut(BaseModel):
    """Whether AI-assisted drafts are configured. The model server is not contacted."""

    enabled: bool
    model: str | None


def get_text_generator(request: Request) -> TextGenerator | None:
    """The generator built at startup (see ``create_app``); None when disabled."""
    generator: TextGenerator | None = request.app.state.text_generator
    return generator


def get_generation_lock(request: Request) -> threading.Lock:
    lock: threading.Lock = request.app.state.generation_lock
    return lock


GeneratorDep = Annotated[TextGenerator | None, Depends(get_text_generator)]
LockDep = Annotated[threading.Lock, Depends(get_generation_lock)]


@router.get(STATUS_PATH, response_model=AssistStatusOut, summary="Are AI-assisted drafts enabled?")
def status(generator: GeneratorDep) -> AssistStatusOut:
    return AssistStatusOut(
        enabled=generator is not None, model=None if generator is None else generator.model
    )


@router.post(
    AI_DRAFT_PATH,
    status_code=201,
    response_model=DraftOut,
    summary="Create a draft whose body a local model wrote (awaits human review)",
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
def create_ai(
    lead_id: str,
    payload: JsonObjectBody,
    session: SessionDep,
    response: Response,
    generator: GeneratorDep,
    lock: LockDep,
    principal: AuthorizedUser,
) -> DraftOut:
    template = parse_draft_request(payload)
    lead = get_lead(session, lead_id)
    if lead is None:
        raise HTTPException(
            status_code=404, detail={"code": "lead.not_found", "message": "Lead not found"}
        )
    if generator is None:
        raise GenerationError(GenerationErrorCode.DISABLED)
    if not lock.acquire(blocking=False):
        raise GenerationError(GenerationErrorCode.BUSY)
    try:
        draft = create_ai_draft(session, lead, template, generator, actor_id=principal.user_id)
    finally:
        lock.release()
    response.headers["Location"] = f"{DRAFTS_PREFIX}/{draft.id}"
    return DraftOut.model_validate(draft)
