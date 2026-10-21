"""Workflow run endpoints (M8). Every route requires the operator token.

A run only ever *creates* drafts that wait for human review; approving, rejecting and
the simulated outbox stay on the M6 draft endpoints. ``mode`` must always be given, so
a live run never starts by accident.
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from leadflow_ai.api.deps import JsonObjectBody, ScoringRulesDep, SessionDep
from leadflow_ai.auth import AuthorizedUser, authorize
from leadflow_ai.db.models import WorkflowRunRecord
from leadflow_ai.services.leads import DEFAULT_PAGE_LIMIT, MAX_OFFSET, MAX_PAGE_LIMIT
from leadflow_ai.services.workflows import (
    RunCounts,
    get_run,
    list_items,
    list_runs,
    parse_run_request,
    run_counts,
    run_workflow,
)
from leadflow_ai.workflows import ItemOutcome, RunMode, RunStatus

RUNS_PREFIX = "/api/v1/workflow-runs"
START_PATH = "/api/v1/workflows/new-lead-outreach/runs"

router = APIRouter(tags=["workflows"], dependencies=[Depends(authorize)])


class CountsOut(BaseModel):
    """Results per outcome, counted from the stored per-lead results."""

    draft_created: int
    would_create_draft: int
    skipped: int
    failed: int
    processed: int


class RunOut(BaseModel):
    """One run. ``completed`` means every planned lead has a result."""

    id: str
    workflow: str
    workflow_version: str
    rules_version: str
    mode: RunMode
    status: RunStatus
    error_code: str | None
    planned_count: int
    counts: CountsOut
    drafts_need_human_review: bool
    started_at: datetime
    finished_at: datetime | None


class RunPage(BaseModel):
    items: list[RunOut]
    limit: int
    offset: int


class RunItemOut(BaseModel):
    """What the run did with one lead. No contact data: fetch the lead for that."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    lead_id: str
    outcome: ItemOutcome
    reason: str | None
    score: int | None
    tier: Literal["hot", "warm", "cold"] | None
    draft_id: str | None
    created_at: datetime


class RunItemPage(BaseModel):
    items: list[RunItemOut]
    limit: int
    offset: int


def _run_out(run: WorkflowRunRecord, counts: RunCounts) -> RunOut:
    return RunOut(
        id=run.id,
        workflow=run.workflow,
        workflow_version=run.workflow_version,
        rules_version=run.rules_version,
        mode=RunMode(run.mode),
        status=RunStatus(run.status),
        error_code=run.error_code,
        planned_count=run.planned_count,
        counts=CountsOut(
            draft_created=counts.draft_created,
            would_create_draft=counts.would_create_draft,
            skipped=counts.skipped,
            failed=counts.failed,
            processed=counts.processed,
        ),
        drafts_need_human_review=counts.draft_created > 0,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _run_or_404(session: SessionDep, run_id: str) -> WorkflowRunRecord:
    run = get_run(session, run_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "workflow_run.not_found", "message": "Workflow run not found"},
        )
    return run


@router.post(
    START_PATH,
    status_code=201,
    response_model=RunOut,
    summary="Run the new-lead outreach workflow now (preview or live; drafts await review)",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["mode"],
                        "properties": {
                            "mode": {"type": "string", "enum": [m.value for m in RunMode]}
                        },
                    }
                }
            },
        }
    },
)
def start(
    payload: JsonObjectBody,
    session: SessionDep,
    rules: ScoringRulesDep,
    response: Response,
    principal: AuthorizedUser,
) -> RunOut:
    mode = parse_run_request(payload)
    run = run_workflow(session, rules, mode, started_by_id=principal.user_id)
    response.headers["Location"] = f"{RUNS_PREFIX}/{run.id}"
    return _run_out(run, run_counts(session, [run.id])[run.id])


@router.get(RUNS_PREFIX, response_model=RunPage, summary="Workflow runs, newest first")
def runs(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> RunPage:
    found = list_runs(session, limit=limit, offset=offset)
    counts = run_counts(session, [run.id for run in found])
    return RunPage(
        items=[_run_out(run, counts[run.id]) for run in found], limit=limit, offset=offset
    )


@router.get(RUNS_PREFIX + "/{run_id}", response_model=RunOut, summary="Get a workflow run")
def retrieve(run_id: str, session: SessionDep) -> RunOut:
    run = _run_or_404(session, run_id)
    return _run_out(run, run_counts(session, [run.id])[run.id])


@router.get(
    RUNS_PREFIX + "/{run_id}/items",
    response_model=RunItemPage,
    summary="A run's per-lead results, in the order they were recorded",
)
def items(
    run_id: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> RunItemPage:
    run = _run_or_404(session, run_id)
    found = list_items(session, run.id, limit=limit, offset=offset)
    return RunItemPage(
        items=[RunItemOut.model_validate(item) for item in found], limit=limit, offset=offset
    )
