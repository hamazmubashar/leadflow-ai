"""AI-assisted drafts (M9): a local model writes the body; a person still decides.

Order of operations, so a slow model never holds a database lock:

1. Cheap pre-checks (reads only): the lead has an email, is eligible and has no
   pending draft. A request that would fail anyway fails before spending up to a
   minute generating.
2. The prompt fields are copied out of the ORM object and the read transaction is
   ended (``session.rollback()``). **No transaction is open while the model runs.**
3. The model's text is checked (``check_generated``) and wrapped in the fixed
   greeting and sign-off. A rejected text writes nothing.
4. ``stage_pending_draft`` takes SQLite's write lock and **re-checks** eligibility and
   the one-pending-draft rule (the lead may have changed during generation), then the
   draft is committed once, as ``pending_review``. The 0005 trigger refuses any other
   status for a ``local_llm`` draft.

Nothing is retried automatically, and nothing is approved, rejected or sent here.
No log line or exception carries the lead's notes or the model's text.
"""

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leadflow_ai.assist import (
    PROMPT_VERSION,
    GenerationError,
    GenerationErrorCode,
    InvalidOutputError,
    LeadContext,
    OllamaGenerator,
    TextGenerator,
    build_messages,
    check_generated,
    compose_body,
)
from leadflow_ai.config import Settings
from leadflow_ai.db.models import LeadDraftRecord, LeadRecord
from leadflow_ai.drafts import DraftStatus, DraftTemplate, is_eligible
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.services.drafts import (
    GeneratedBody,
    LeadHasNoEmailError,
    LeadNotEligibleError,
    PendingDraftExistsError,
    is_pending_conflict,
    stage_pending_draft,
)


def generator_from_settings(settings: Settings) -> TextGenerator | None:
    """The configured local model, or None when AI drafts are disabled (the default)."""
    if settings.llm_url is None:
        return None
    return OllamaGenerator(settings.llm_url, settings.llm_model, settings.llm_timeout_seconds)


def create_ai_draft(
    session: Session,
    lead: LeadRecord,
    template: DraftTemplate,
    generator: TextGenerator | None,
) -> LeadDraftRecord:
    """Generate a body for ``lead`` and store it as a pending draft, in one commit.

    Raises GenerationError (fixed codes ``llm.*``), LeadHasNoEmailError,
    LeadNotEligibleError or PendingDraftExistsError; on any failure nothing is written.
    """
    if generator is None:
        raise GenerationError(GenerationErrorCode.DISABLED)
    _precheck(session, lead)
    context = LeadContext(full_name=lead.full_name, company=lead.company, notes=lead.notes)
    messages = build_messages(template, context)
    session.rollback()  # end the read transaction: nothing is open during generation

    raw = generator.generate(messages)
    try:
        generated = check_generated(raw)
    except InvalidOutputError:
        raise GenerationError(GenerationErrorCode.INVALID_OUTPUT) from None
    body = GeneratedBody(
        body=compose_body(context.full_name, generated),
        model=generator.model,
        prompt_version=PROMPT_VERSION,
    )

    draft = stage_pending_draft(session, lead, template, generated=body)  # re-checks
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if is_pending_conflict(exc):
            raise PendingDraftExistsError from None
        raise
    return draft


def _precheck(session: Session, lead: LeadRecord) -> None:
    """Fail early (reads only); ``stage_pending_draft`` repeats these under the write lock."""
    if lead.email is None:
        raise LeadHasNoEmailError
    status = LeadStatus(
        session.scalars(select(LeadRecord.status).where(LeadRecord.id == lead.id)).one()
    )
    if not is_eligible(status):
        session.rollback()
        raise LeadNotEligibleError(status)
    pending = session.scalar(
        select(
            exists().where(
                LeadDraftRecord.lead_id == lead.id,
                LeadDraftRecord.status == DraftStatus.PENDING_REVIEW.value,
            )
        )
    )
    if pending:
        session.rollback()
        raise PendingDraftExistsError
