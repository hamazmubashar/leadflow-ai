"""M9 dashboard logic: when the AI button is offered, the warning and provenance, safe
error messages, and the guard against sending a request twice."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from dashboard import assist
from dashboard.api_client import ApiError, ApiErrorKind
from dashboard.models import AssistStatus, Draft, DraftTemplate

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
INTRO = DraftTemplate.INTRODUCTION
LLM_CODES = (
    "llm.disabled",
    "llm.busy",
    "llm.unavailable",
    "llm.model_not_found",
    "llm.timeout",
    "llm.invalid_output",
)


def draft(**overrides: Any) -> Draft:
    data: dict[str, Any] = {
        "id": "d1",
        "lead_id": "l1",
        "template": "introduction",
        "template_version": "2026-10-15.1",
        "channel": "email",
        "recipient": "a@example.com",
        "subject": "Introduction for Northwind",
        "body": "Hello",
        "status": "pending_review",
        "rejection_reason": None,
        "created_at": NOW,
        "decided_at": None,
    }
    data.update(overrides)
    return Draft.model_validate(data)


def http_error(code: str | None, message: str = "API message", status: int = 503) -> ApiError:
    return ApiError(ApiErrorKind.HTTP_ERROR, message, status_code=status, code=code)


# --- offering the feature ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "offered"),
    [
        (None, False),  # status could not be read
        (AssistStatus(enabled=False, model=None), False),
        (AssistStatus(enabled=True, model="qwen2.5:3b"), True),
    ],
)
def test_the_button_is_offered_only_when_the_api_confirms(
    status: AssistStatus | None, offered: bool
) -> None:
    assert assist.offer_ai(status) is offered


# --- warning and provenance ----------------------------------------------------------------


def test_the_warning_text_is_exact() -> None:
    assert assist.AI_WARNING == (
        "Written by a local AI model from internal notes. "
        "Check for internal remarks, dates or promises before approving."
    )


def test_the_limits_note_says_instructions_are_not_a_guarantee() -> None:
    assert "not a guarantee" in assist.AI_LIMITS_NOTE
    assert "You decide what is approved" in assist.AI_LIMITS_NOTE


@pytest.mark.parametrize("status", ["pending_review", "approved", "rejected"])
def test_every_ai_draft_has_a_provenance_label_whatever_its_status(status: str) -> None:
    decided = None if status == "pending_review" else NOW
    ai = draft(generator="local_llm", model="qwen2.5:3b", status=status, decided_at=decided)
    assert assist.provenance_label(ai) == "AI-assisted · qwen2.5:3b"


def test_template_drafts_have_no_ai_label() -> None:
    assert assist.provenance_label(draft()) is None
    assert assist.provenance_label(draft(generator="template")) is None


def test_an_ai_draft_without_a_model_name_is_still_labelled() -> None:
    assert assist.provenance_label(draft(generator="local_llm")) == "AI-assisted · local model"


# --- error messages ------------------------------------------------------------------------


@pytest.mark.parametrize("code", LLM_CODES)
def test_every_llm_code_has_its_own_message_and_says_nothing_was_saved(code: str) -> None:
    lines = assist.describe_ai_error(http_error(code, message="RAW SERVER TEXT"))
    assert lines[-1] == "No draft was created."
    assert "RAW SERVER TEXT" not in " ".join(lines)  # fixed wording only
    assert len({tuple(assist.describe_ai_error(http_error(c))) for c in LLM_CODES}) == len(
        LLM_CODES
    )


def test_a_dashboard_timeout_says_to_check_before_trying_again() -> None:
    lines = assist.describe_ai_error(ApiError(ApiErrorKind.TIMEOUT, "timed out"))
    assert lines == [assist.DASHBOARD_TIMEOUT]
    assert "may or may not have been created" in lines[0]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("draft.pending_exists", "API message"),
        ("lead.not_found", "API message"),
    ],
)
def test_other_errors_use_the_existing_safe_descriptions(code: str, expected: str) -> None:
    assert assist.describe_ai_error(http_error(code, status=409)) == [expected]


def test_lead_not_eligible_keeps_its_existing_wording() -> None:
    error = ApiError(
        ApiErrorKind.HTTP_ERROR,
        "Drafts can only be created while eligible",
        status_code=409,
        code="draft.lead_not_eligible",
        detail={"lead_status": "lost"},
    )
    assert assist.describe_ai_error(error) == [
        "Drafts can only be created while eligible (this lead is Lost)."
    ]


# --- one request at a time -------------------------------------------------------------------


def test_a_click_is_sent_exactly_once() -> None:
    state: dict[str, Any] = {}
    assert assist.begin(state, "l1", INTRO, NOW)
    request = assist.take_unsent(state, "l1", NOW)
    assert request is not None and (request.lead_id, request.template) == ("l1", INTRO)
    assert request.sent
    # An interrupted run reruns the page: the request is not handed out again.
    assert assist.take_unsent(state, "l1", NOW + timedelta(seconds=5)) is None
    running = assist.running(state, NOW + timedelta(seconds=5))
    assert running is not None and running.sent


def test_a_second_click_while_running_is_ignored() -> None:
    state: dict[str, Any] = {}
    assert assist.begin(state, "l1", INTRO, NOW)
    assert not assist.begin(state, "l2", DraftTemplate.FOLLOW_UP, NOW + timedelta(seconds=1))
    pending = assist.running(state, NOW)
    assert pending is not None and pending.lead_id == "l1"  # the first click is kept


def test_another_leads_page_does_not_send_the_request() -> None:
    state: dict[str, Any] = {}
    assist.begin(state, "l1", INTRO, NOW)
    assert assist.take_unsent(state, "l2", NOW) is None
    assert assist.take_unsent(state, "l1", NOW) is not None


def test_finish_allows_the_next_request() -> None:
    state: dict[str, Any] = {}
    assist.begin(state, "l1", INTRO, NOW)
    assist.take_unsent(state, "l1", NOW)
    assist.finish(state)
    assert assist.running(state, NOW) is None
    assert assist.begin(state, "l1", INTRO, NOW)
    assist.finish(state)
    assist.finish(state)  # finishing twice is harmless


def test_a_stale_request_is_dropped() -> None:
    state: dict[str, Any] = {}
    assist.begin(state, "l1", INTRO, NOW)
    later = NOW + assist.STALE_AFTER + timedelta(seconds=1)
    assert assist.running(state, later) is None
    assert assist.PENDING_KEY not in state
    assert assist.begin(state, "l1", INTRO, later)


def test_unrelated_state_values_are_ignored() -> None:
    state: dict[str, Any] = {assist.PENDING_KEY: "not a request"}
    assert assist.running(state, NOW) is None
    assert assist.take_unsent(state, "l1", NOW) is None


def test_the_stale_limit_outlasts_the_dashboard_timeout() -> None:
    from dashboard.api_client import AI_DRAFT_READ_TIMEOUT_SECONDS

    assert assist.STALE_AFTER.total_seconds() > AI_DRAFT_READ_TIMEOUT_SECONDS
