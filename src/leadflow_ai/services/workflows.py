"""Workflow runs (M8): the fixed "new-lead outreach" workflow, in preview or live mode.

Safety rules, each also enforced by the database (migration 0004):

- **Human approval stays mandatory.** A run can only *create* pending-review drafts,
  through the M6 draft service. Nothing here approves, rejects or sends anything.
- **Preview writes no drafts.** Its results say ``would_create_draft`` and can never
  point to a draft (CHECK constraint).
- **One running run at a time** (partial unique index). A run whose process stopped
  keeps the slot only until its heartbeat is ``STALE_RUN_AFTER`` old; the next run
  marks it ``interrupted``. Every recorded lead refreshes the heartbeat, and results
  can only be recorded while the run is still ``running`` (trigger), so an
  interrupted run can't add results afterwards.
- **No duplicate drafts.** A live draft and its result row commit in one transaction;
  a live run processes a lead at most once per workflow version (partial unique
  index; failed leads are retried by a later run). The M6 one-pending-draft index
  still applies.
- **No false completion.** Counts are derived from the stored results, never kept
  separately, and a run can only be marked ``completed`` when every planned lead has
  a result (trigger).

Each lead is processed in its own transaction, which starts by taking SQLite's
write lock (the heartbeat compare-and-set), so every check runs on current data.
Errors for one lead are recorded as that lead's ``failed`` result with a fixed code
(never an exception message or lead value) and the run continues.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, exists, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from leadflow_ai.db.models import (
    LeadDraftRecord,
    LeadRecord,
    WorkflowRunItemRecord,
    WorkflowRunRecord,
)
from leadflow_ai.drafts import DraftStatus
from leadflow_ai.leads import FieldError, LeadValidationError
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.scoring.engine import ScoreResult, score_lead
from leadflow_ai.scoring.rules import ScoringRules
from leadflow_ai.services.drafts import (
    LeadNotEligibleError,
    PendingDraftExistsError,
    is_pending_conflict,
    stage_pending_draft,
)
from leadflow_ai.services.leads import DEFAULT_PAGE_LIMIT, MAX_OFFSET, MAX_PAGE_LIMIT
from leadflow_ai.workflows import (
    MAX_LEADS_PER_RUN,
    STALE_RUN_AFTER,
    WORKFLOW_NAME,
    WORKFLOW_VERSION,
    FailureReason,
    ItemOutcome,
    RunError,
    RunMode,
    RunStatus,
    SkipReason,
    decide,
)
from leadflow_ai.workflows.outreach import DRAFT_TEMPLATE

_RUNNING = RunStatus.RUNNING.value
_LIVE = RunMode.LIVE.value
# SQLite's messages for the 0004 safeguards (matched to classify an IntegrityError).
_ONE_RUNNING = "workflow_runs.workflow"
_LIVE_ONCE = "workflow_run_items.workflow_version, workflow_run_items.lead_id"
_RUN_NOT_RUNNING = "workflow_run_items require a running run"


RUN_FIELDS = ("mode",)
_MODE_CHOICES = ", ".join(mode.value for mode in RunMode)


def parse_run_request(data: Mapping[str, object]) -> RunMode:
    """Validate a start-run body. ``mode`` is required: a live run never starts by default.

    Raises LeadValidationError (the shared field-error format); no values are echoed.
    """
    errors: list[FieldError] = []
    if any(key not in RUN_FIELDS for key in data):
        errors.append(
            FieldError(
                "unknown_fields",
                "field.unknown",
                "Contains unexpected fields; allowed fields are: " + ", ".join(RUN_FIELDS),
            )
        )
    value = data.get("mode")
    mode = None
    if value is None or (isinstance(value, str) and not value.strip()):
        errors.append(FieldError("mode", "field.required", "Required"))
    elif not isinstance(value, str):
        errors.append(FieldError("mode", "field.wrong_type", "Must be text"))
    else:
        try:
            mode = RunMode(value.strip().lower())
        except ValueError:
            errors.append(
                FieldError("mode", "mode.invalid_choice", f"Must be one of: {_MODE_CHOICES}")
            )
    if errors or mode is None:
        raise LeadValidationError(errors)
    return mode


class RunInProgressError(Exception):
    """Another run of the workflow is still running (and not stale)."""

    def __init__(self, run_id: str | None) -> None:
        super().__init__("a workflow run is already in progress")
        self.run_id = run_id


class _RecordingFailedError(Exception):
    """A lead's result could not be stored; the run cannot honestly continue."""


@dataclass(frozen=True, slots=True)
class RunCounts:
    """Results per outcome, counted from the stored rows."""

    draft_created: int = 0
    would_create_draft: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def processed(self) -> int:
        return self.draft_created + self.would_create_draft + self.skipped + self.failed


def _utc_now() -> datetime:
    return datetime.now(UTC)


# --- run -------------------------------------------------------------------------------------


def run_workflow(
    session: Session,
    rules: ScoringRules,
    mode: RunMode,
    *,
    max_leads: int = MAX_LEADS_PER_RUN,
) -> WorkflowRunRecord:
    """Run the new-lead outreach workflow now and return the finished run.

    Raises RunInProgressError if another run is running. The returned run is
    ``completed`` only if every planned lead has a result; otherwise it is
    ``failed`` (a result could not be stored) or ``interrupted`` (another run found
    this one stale and took over).
    """
    if isinstance(max_leads, bool) or not 1 <= max_leads <= MAX_LEADS_PER_RUN:
        raise ValueError(f"max_leads must be between 1 and {MAX_LEADS_PER_RUN}")
    run, lead_ids = _begin(session, rules, mode, max_leads)
    for lead_id in lead_ids:
        try:
            keep_going = _process_lead(session, run, lead_id, rules, mode)
        except _RecordingFailedError:
            _finish(session, run.id, RunStatus.FAILED, RunError.RECORDING_FAILED)
            break
        if not keep_going:
            break  # the run is no longer running: another run marked it interrupted
    else:
        _finish(session, run.id, RunStatus.COMPLETED, None)
    return _reload_run(session, run.id)


def _begin(
    session: Session, rules: ScoringRules, mode: RunMode, max_leads: int
) -> tuple[WorkflowRunRecord, list[str]]:
    """Mark stale runs interrupted, select the leads and store the new run, in one commit."""
    now = _utc_now()
    # The first statement is an UPDATE, so the write lock is held from here to the
    # commit: the lead selection below can't change before the run is stored.
    session.execute(
        update(WorkflowRunRecord)
        .where(
            WorkflowRunRecord.status == _RUNNING,
            WorkflowRunRecord.heartbeat_at < now - STALE_RUN_AFTER,
        )
        .values(
            status=RunStatus.INTERRUPTED.value,
            finished_at=now,
            error_code=RunError.STALE_HEARTBEAT.value,
        )
        .execution_options(synchronize_session=False)
    )
    running = session.scalar(
        select(WorkflowRunRecord.id).where(
            WorkflowRunRecord.workflow == WORKFLOW_NAME, WorkflowRunRecord.status == _RUNNING
        )
    )
    if running is not None:
        session.rollback()
        raise RunInProgressError(running)

    lead_ids = list(session.scalars(_candidates().limit(max_leads)))
    run = WorkflowRunRecord(
        id=str(uuid.uuid4()),
        workflow=WORKFLOW_NAME,
        workflow_version=WORKFLOW_VERSION,
        rules_version=rules.rules_version,
        mode=mode.value,
        status=_RUNNING,
        planned_count=len(lead_ids),
        error_code=None,
        started_at=now,
        heartbeat_at=now,
        finished_at=None,
    )
    session.add(run)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _ONE_RUNNING in str(exc.orig):  # the index: the final safeguard
            raise RunInProgressError(None) from None
        raise  # pragma: no cover  (no other constraint can fail on this insert)
    return run, lead_ids


def _candidates() -> Any:
    """``new`` leads that no live run of this workflow version has processed, oldest first."""
    processed = exists().where(
        WorkflowRunItemRecord.lead_id == LeadRecord.id,
        WorkflowRunItemRecord.workflow_version == WORKFLOW_VERSION,
        WorkflowRunItemRecord.mode == _LIVE,
        WorkflowRunItemRecord.outcome != ItemOutcome.FAILED.value,
    )
    return (
        select(LeadRecord.id)
        .where(LeadRecord.status == LeadStatus.NEW.value, ~processed)
        .order_by(LeadRecord.created_at, LeadRecord.id)
    )


def _process_lead(
    session: Session, run: WorkflowRunRecord, lead_id: str, rules: ScoringRules, mode: RunMode
) -> bool:
    """Process one lead in one transaction. False means: stop, the run is no longer running."""
    try:
        if not _touch(session, run.id):
            return False
        lead = session.execute(
            select(LeadRecord)
            .where(LeadRecord.id == lead_id)
            .execution_options(populate_existing=True)
        ).scalar_one()
        try:
            score = score_lead(lead, rules)
        except ValueError:
            session.rollback()
            return _record(session, run, lead_id, ItemOutcome.FAILED, FailureReason.SCORING_ERROR)
        decision = decide(
            status=LeadStatus(lead.status), tier=score.tier, has_email=lead.email is not None
        )
        if not decision.draft:
            session.add(_item(run, lead_id, ItemOutcome.SKIPPED, decision.reason, score))
        elif mode is RunMode.PREVIEW:
            pending = _has_pending_draft(session, lead_id)
            outcome = ItemOutcome.SKIPPED if pending else ItemOutcome.WOULD_CREATE_DRAFT
            reason = SkipReason.PENDING_DRAFT_EXISTS if pending else None
            session.add(_item(run, lead_id, outcome, reason, score))
        else:
            try:
                draft = stage_pending_draft(session, lead, DRAFT_TEMPLATE)
            except PendingDraftExistsError:  # already rolled back
                return _record(
                    session,
                    run,
                    lead_id,
                    ItemOutcome.SKIPPED,
                    SkipReason.PENDING_DRAFT_EXISTS,
                    score,
                )
            except LeadNotEligibleError:
                return _record(
                    session, run, lead_id, ItemOutcome.SKIPPED, SkipReason.NOT_ELIGIBLE, score
                )
            # The draft and its result commit together: never a draft without a result.
            session.add(
                _item(run, lead_id, ItemOutcome.DRAFT_CREATED, None, score, draft_id=draft.id)
            )
        session.commit()
        return True
    except IntegrityError as exc:
        session.rollback()  # also undoes a staged draft
        message = str(exc.orig)
        if _RUN_NOT_RUNNING in message:
            return False
        if is_pending_conflict(exc):
            return _record(
                session, run, lead_id, ItemOutcome.SKIPPED, SkipReason.PENDING_DRAFT_EXISTS
            )
        if _LIVE_ONCE in message:
            return _record(
                session, run, lead_id, ItemOutcome.FAILED, FailureReason.CONCURRENT_UPDATE
            )
        return _record(session, run, lead_id, ItemOutcome.FAILED, FailureReason.UNEXPECTED_ERROR)
    except OperationalError as exc:
        session.rollback()
        busy = getattr(exc.orig, "sqlite_errorname", None) == "SQLITE_BUSY"
        failure = FailureReason.DATABASE_BUSY if busy else FailureReason.UNEXPECTED_ERROR
        return _record(session, run, lead_id, ItemOutcome.FAILED, failure)
    except _RecordingFailedError:
        raise
    except Exception:  # one lead's unexpected error must not stop the run
        session.rollback()
        return _record(session, run, lead_id, ItemOutcome.FAILED, FailureReason.UNEXPECTED_ERROR)


def _record(
    session: Session,
    run: WorkflowRunRecord,
    lead_id: str,
    outcome: ItemOutcome,
    reason: SkipReason | FailureReason,
    score: ScoreResult | None = None,
) -> bool:
    """Store one result in a fresh transaction. False: the run is no longer running."""
    try:
        if not _touch(session, run.id):
            return False
        session.add(_item(run, lead_id, outcome, reason, score))
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _RUN_NOT_RUNNING in str(exc.orig):
            return False
        raise _RecordingFailedError from None
    except Exception:
        session.rollback()
        raise _RecordingFailedError from None
    return True


def _touch(session: Session, run_id: str) -> bool:
    """Refresh the heartbeat (taking the write lock); False if the run is no longer running."""
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(WorkflowRunRecord)
            .where(WorkflowRunRecord.id == run_id, WorkflowRunRecord.status == _RUNNING)
            .values(heartbeat_at=_utc_now())
            .execution_options(synchronize_session=False)
        ),
    )
    if result.rowcount == 1:
        return True
    session.rollback()
    return False


def _finish(session: Session, run_id: str, status: RunStatus, error: RunError | None) -> None:
    """Compare-and-set the run from ``running``. Nothing happens if it was interrupted."""
    try:
        session.execute(
            update(WorkflowRunRecord)
            .where(WorkflowRunRecord.id == run_id, WorkflowRunRecord.status == _RUNNING)
            .values(
                status=status.value,
                finished_at=_utc_now(),
                error_code=error.value if error else None,
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except IntegrityError:
        # "completed" refused because a result is missing: record the truth instead.
        session.rollback()
        if status is not RunStatus.COMPLETED:  # pragma: no cover  (only completion can fail)
            raise
        _finish(session, run_id, RunStatus.FAILED, RunError.RECORDING_FAILED)


def _item(
    run: WorkflowRunRecord,
    lead_id: str,
    outcome: ItemOutcome,
    reason: SkipReason | FailureReason | None,
    score: ScoreResult | None,
    *,
    draft_id: str | None = None,
) -> WorkflowRunItemRecord:
    return WorkflowRunItemRecord(
        id=str(uuid.uuid4()),
        run_id=run.id,
        lead_id=lead_id,
        workflow_version=run.workflow_version,
        mode=run.mode,
        outcome=outcome.value,
        reason=reason.value if reason else None,
        score=score.score if score else None,
        tier=score.tier if score else None,
        draft_id=draft_id,
        created_at=_utc_now(),
    )


def _has_pending_draft(session: Session, lead_id: str) -> bool:
    return (
        session.scalar(
            select(LeadDraftRecord.id).where(
                LeadDraftRecord.lead_id == lead_id,
                LeadDraftRecord.status == DraftStatus.PENDING_REVIEW.value,
            )
        )
        is not None
    )


def _reload_run(session: Session, run_id: str) -> WorkflowRunRecord:
    return session.execute(
        select(WorkflowRunRecord)
        .where(WorkflowRunRecord.id == run_id)
        .execution_options(populate_existing=True)
    ).scalar_one()


# --- read ------------------------------------------------------------------------------------


def get_run(session: Session, run_id: str) -> WorkflowRunRecord | None:
    if not _is_canonical_uuid(run_id):
        return None
    return session.execute(
        select(WorkflowRunRecord)
        .where(WorkflowRunRecord.id == run_id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def list_runs(
    session: Session, *, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
) -> list[WorkflowRunRecord]:
    """Runs, newest first (ties ordered by id so pages are stable)."""
    _check_page(limit, offset)
    statement = (
        select(WorkflowRunRecord)
        .order_by(WorkflowRunRecord.started_at.desc(), WorkflowRunRecord.id.desc())
        .limit(limit)
        .offset(offset)
        .execution_options(populate_existing=True)
    )
    return list(session.scalars(statement))


def list_items(
    session: Session, run_id: str, *, limit: int = DEFAULT_PAGE_LIMIT, offset: int = 0
) -> list[WorkflowRunItemRecord]:
    """A run's results in the order they were recorded."""
    _check_page(limit, offset)
    statement = (
        select(WorkflowRunItemRecord)
        .where(WorkflowRunItemRecord.run_id == run_id)
        .order_by(WorkflowRunItemRecord.created_at, WorkflowRunItemRecord.id)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(statement))


def run_counts(session: Session, run_ids: list[str]) -> dict[str, RunCounts]:
    """Results per outcome for each run, counted from the stored rows."""
    counts: dict[str, dict[str, int]] = {run_id: {} for run_id in run_ids}
    if run_ids:
        rows = session.execute(
            select(
                WorkflowRunItemRecord.run_id,
                WorkflowRunItemRecord.outcome,
                func.count(),
            )
            .where(WorkflowRunItemRecord.run_id.in_(run_ids))
            .group_by(WorkflowRunItemRecord.run_id, WorkflowRunItemRecord.outcome)
        ).all()
        for run_id, outcome, count in rows:
            counts[run_id][outcome] = count
    return {run_id: RunCounts(**by_outcome) for run_id, by_outcome in counts.items()}


def _check_page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
    if isinstance(offset, bool) or not 0 <= offset <= MAX_OFFSET:
        raise ValueError(f"offset must be between 0 and {MAX_OFFSET}")


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False
