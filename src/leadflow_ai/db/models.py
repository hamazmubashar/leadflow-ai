"""ORM models. The schema itself is created only by Alembic migrations."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from leadflow_ai.db.base import Base
from leadflow_ai.db.types import UTCDateTime
from leadflow_ai.drafts import (
    CHANNEL_EMAIL,
    MAX_BODY_LENGTH,
    MAX_SUBJECT_LENGTH,
    OUTBOX_STATUS_SIMULATED,
    DraftGenerator,
    DraftStatus,
    DraftTemplate,
)
from leadflow_ai.leads import LeadSource
from leadflow_ai.lifecycle import INITIAL_STATUS, LeadStatus
from leadflow_ai.workflows import WORKFLOW_NAME, ItemOutcome, RunMode, RunStatus

_SOURCE_VALUES = ", ".join(f"'{source.value}'" for source in LeadSource)
_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in LeadStatus)
_TEMPLATE_VALUES = ", ".join(f"'{template.value}'" for template in DraftTemplate)
_DRAFT_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in DraftStatus)
_PENDING = DraftStatus.PENDING_REVIEW.value
_RUN_MODE_VALUES = ", ".join(f"'{mode.value}'" for mode in RunMode)
_RUN_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in RunStatus)
_OUTCOME_VALUES = ", ".join(f"'{outcome.value}'" for outcome in ItemOutcome)
_RUNNING = RunStatus.RUNNING.value
_LIVE = RunMode.LIVE.value
_PREVIEW = RunMode.PREVIEW.value
_FAILED = ItemOutcome.FAILED.value

EVENT_LEAD_CREATED = "lead_created"
EVENT_STATUS_CHANGED = "status_changed"


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LeadRecord(Base):
    """A stored lead. Values are already normalized by M1's ``validate_lead()``.

    ``email`` and ``phone`` are each UNIQUE (NULLs allowed), which enforces the
    same identity rule as ``Lead.identity_keys()``.
    """

    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="contact_required"),
        CheckConstraint(f"source IN ({_SOURCE_VALUES})", name="source_allowed"),
        CheckConstraint("email IS NULL OR email = lower(email)", name="email_lowercase"),
        CheckConstraint(f"status IN ({_STATUS_VALUES})", name="status_allowed"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    full_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    phone: Mapped[str | None] = mapped_column(String(16), unique=True)
    company: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)
    # Current lifecycle status (M5). Its history is in lead_events.
    status: Mapped[str] = mapped_column(
        String(20), default=INITIAL_STATUS.value, server_default=INITIAL_STATUS.value
    )

    def __repr__(self) -> str:
        # Keep personal data out of logs and tracebacks.
        return f"LeadRecord(id={self.id!r})"


class LeadEventRecord(Base):
    """One append-only activity event for a lead.

    ``sequence`` (1, 2, 3, ... per lead) defines the order; ``occurred_at`` is the UTC
    time the event was recorded. ``note`` is operator-written text and may contain
    personal data, so it is never logged or included in errors.
    """

    __tablename__ = "lead_events"
    __table_args__ = (
        UniqueConstraint("lead_id", "sequence", name=conv("uq_lead_events_lead_id_sequence")),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            f"event_type IN ('{EVENT_LEAD_CREATED}', '{EVENT_STATUS_CHANGED}')",
            name="event_type_allowed",
        ),
        CheckConstraint(f"to_status IN ({_STATUS_VALUES})", name="to_status_allowed"),
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_STATUS_VALUES})", name="from_status_allowed"
        ),
        CheckConstraint(
            f"(event_type = '{EVENT_LEAD_CREATED}' AND from_status IS NULL"
            f" AND to_status = '{INITIAL_STATUS.value}')"
            f" OR (event_type = '{EVENT_STATUS_CHANGED}' AND from_status IS NOT NULL"
            " AND from_status <> to_status)",
            name="shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(30))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)

    def __repr__(self) -> str:
        # Never include the note, which may contain personal data.
        return f"LeadEventRecord(id={self.id!r}, sequence={self.sequence!r})"


def lead_created_event(lead: LeadRecord) -> LeadEventRecord:
    """The ``lead_created`` event (sequence 1) for a lead that has just been flushed."""
    return LeadEventRecord(
        lead_id=lead.id,
        sequence=1,
        event_type=EVENT_LEAD_CREATED,
        from_status=None,
        to_status=INITIAL_STATUS.value,
        note=None,
        occurred_at=lead.created_at,
    )


class LeadDraftRecord(Base):
    """A rendered response draft awaiting (or past) human review (M6).

    The text is stored exactly as rendered, so approval covers exactly what the
    operator reviewed. Content columns can never change and a decided draft can
    never change at all: migration 0003 enforces both with SQLite triggers. At most
    one draft per lead is ``pending_review`` (partial unique index). ``recipient``,
    ``subject``, ``body`` and ``rejection_reason`` may contain personal data, so they
    are never logged or included in errors.
    """

    __tablename__ = "lead_drafts"
    __table_args__ = (
        CheckConstraint(f"template IN ({_TEMPLATE_VALUES})", name="template_allowed"),
        CheckConstraint(f"channel = '{CHANNEL_EMAIL}'", name="channel_allowed"),
        CheckConstraint("recipient = lower(recipient)", name="recipient_lowercase"),
        CheckConstraint(f"status IN ({_DRAFT_STATUS_VALUES})", name="status_allowed"),
        CheckConstraint(
            f"(status = '{_PENDING}' AND decided_at IS NULL AND rejection_reason IS NULL)"
            f" OR (status = '{DraftStatus.APPROVED.value}' AND decided_at IS NOT NULL"
            " AND rejection_reason IS NULL)"
            f" OR (status = '{DraftStatus.REJECTED.value}' AND decided_at IS NOT NULL)",
            name="shape",
        ),
        Index(
            conv("uq_lead_drafts_one_pending_per_lead"),
            "lead_id",
            unique=True,
            sqlite_where=text(f"status = '{_PENDING}'"),
        ),
        Index(conv("ix_lead_drafts_lead_id_created_at"), "lead_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id"))
    template: Mapped[str] = mapped_column(String(20))
    template_version: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(10))
    recipient: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(MAX_SUBJECT_LENGTH))
    body: Mapped[str] = mapped_column(String(MAX_BODY_LENGTH))
    status: Mapped[str] = mapped_column(String(20))
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # M9 provenance (migration 0005). The insert trigger allows exactly two shapes:
    # template (model and prompt_version NULL) or local_llm (both set, pending_review).
    generator: Mapped[str] = mapped_column(
        String(20),
        default=DraftGenerator.TEMPLATE.value,
        server_default=DraftGenerator.TEMPLATE.value,
    )
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(32))

    def __repr__(self) -> str:
        # Never include the recipient, text or rejection reason.
        return f"LeadDraftRecord(id={self.id!r}, status={self.status!r})"


class OutboxMessageRecord(Base):
    """A simulated outbox item: created only by a successful approval, never delivered.

    It holds no message content: recipient, subject and body are read from the
    immutable approved draft. ``status`` can only be ``simulated``. Rows cannot be
    updated or deleted, and a draft with an outbox row cannot be deleted (0003 triggers).
    """

    __tablename__ = "outbox_messages"
    __table_args__ = (
        CheckConstraint(f"status = '{OUTBOX_STATUS_SIMULATED}'", name="status_allowed"),
        Index(conv("ix_outbox_messages_created_at"), "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    draft_id: Mapped[str] = mapped_column(String(36), ForeignKey("lead_drafts.id"), unique=True)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id"))
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)

    def __repr__(self) -> str:
        return f"OutboxMessageRecord(id={self.id!r})"


class WorkflowRunRecord(Base):
    """One execution of a fixed workflow (M8), in ``preview`` or ``live`` mode.

    Its per-lead results are ``WorkflowRunItemRecord`` rows; counts are always derived
    from them, never stored, so they can't drift. At most one run per workflow is
    ``running`` (partial unique index). ``heartbeat_at`` is refreshed with every
    recorded lead, so a run whose process stopped can be recognised and marked
    ``interrupted``. Migration 0004 makes finished runs immutable and only lets a run
    complete once every planned lead has a result.
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(f"workflow IN ('{WORKFLOW_NAME}')", name="workflow_allowed"),
        CheckConstraint(f"mode IN ({_RUN_MODE_VALUES})", name="mode_allowed"),
        CheckConstraint(f"status IN ({_RUN_STATUS_VALUES})", name="status_allowed"),
        CheckConstraint("planned_count >= 0", name="planned_count_non_negative"),
        CheckConstraint(
            f"(status = '{_RUNNING}' AND finished_at IS NULL AND error_code IS NULL)"
            f" OR (status = '{RunStatus.COMPLETED.value}' AND finished_at IS NOT NULL"
            " AND error_code IS NULL)"
            f" OR (status IN ('{RunStatus.FAILED.value}', '{RunStatus.INTERRUPTED.value}')"
            " AND finished_at IS NOT NULL AND error_code IS NOT NULL)",
            name="shape",
        ),
        Index(
            conv("uq_workflow_runs_one_running"),
            "workflow",
            unique=True,
            sqlite_where=text(f"status = '{_RUNNING}'"),
        ),
        Index(conv("ix_workflow_runs_started_at"), "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    workflow: Mapped[str] = mapped_column(String(40))
    workflow_version: Mapped[str] = mapped_column(String(32))
    rules_version: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20))
    planned_count: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    def __repr__(self) -> str:
        return f"WorkflowRunRecord(id={self.id!r}, mode={self.mode!r}, status={self.status!r})"


class WorkflowRunItemRecord(Base):
    """What a run did with one lead: its score and tier, and the outcome with a reason.

    Holds no lead contact data. ``reason`` is a fixed code (``SkipReason`` or
    ``FailureReason``), never an exception message. Rows are immutable (0004).
    """

    __tablename__ = "workflow_run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "lead_id", name=conv("uq_workflow_run_items_run_id_lead_id")),
        CheckConstraint(f"mode IN ({_RUN_MODE_VALUES})", name="mode_allowed"),
        CheckConstraint(f"outcome IN ({_OUTCOME_VALUES})", name="outcome_allowed"),
        CheckConstraint("tier IS NULL OR tier IN ('hot', 'warm', 'cold')", name="tier_allowed"),
        CheckConstraint(
            f"(outcome = '{ItemOutcome.DRAFT_CREATED.value}' AND mode = '{_LIVE}'"
            " AND draft_id IS NOT NULL AND reason IS NULL)"
            f" OR (outcome = '{ItemOutcome.WOULD_CREATE_DRAFT.value}' AND mode = '{_PREVIEW}'"
            " AND draft_id IS NULL AND reason IS NULL)"
            f" OR (outcome IN ('{ItemOutcome.SKIPPED.value}', '{_FAILED}')"
            " AND draft_id IS NULL AND reason IS NOT NULL)",
            name="shape",
        ),
        Index(
            conv("uq_workflow_run_items_live_once"),
            "workflow_version",
            "lead_id",
            unique=True,
            sqlite_where=text(f"mode = '{_LIVE}' AND outcome <> '{_FAILED}'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("workflow_runs.id"))
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id"))
    workflow_version: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(10))
    outcome: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(String(40))
    score: Mapped[int | None] = mapped_column(Integer)
    tier: Mapped[str | None] = mapped_column(String(10))
    draft_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("lead_drafts.id"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)

    def __repr__(self) -> str:
        return f"WorkflowRunItemRecord(id={self.id!r}, outcome={self.outcome!r})"
