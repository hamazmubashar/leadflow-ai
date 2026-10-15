from datetime import UTC, datetime
from typing import Any

import pytest

from dashboard.api_client import ApiError, ApiErrorKind
from dashboard.models import WorkflowRun
from dashboard.workflows import OUTCOME_LABELS, describe_start_error, reason_label, summarize

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def run(**overrides: Any) -> WorkflowRun:
    data: dict[str, Any] = {
        "id": "r1",
        "workflow": "new_lead_outreach",
        "workflow_version": "2026-10-15.1",
        "rules_version": "2026-10-15.1",
        "mode": "live",
        "status": "completed",
        "error_code": None,
        "planned_count": 5,
        "counts": {
            "draft_created": 2,
            "would_create_draft": 0,
            "skipped": 3,
            "failed": 0,
            "processed": 5,
        },
        "drafts_need_human_review": True,
        "started_at": NOW,
        "finished_at": NOW,
    }
    data.update(overrides)
    return WorkflowRun.model_validate(data)


def test_live_summary_points_to_human_review() -> None:
    text = summarize(run())
    assert text.startswith("Live run on 5 leads: 2 drafts created, 3 skipped, 0 failed.")
    assert "waiting for your review" in text


def test_preview_summary_says_nothing_was_created() -> None:
    preview = run(
        mode="preview",
        counts={
            "draft_created": 0,
            "would_create_draft": 2,
            "skipped": 3,
            "failed": 0,
            "processed": 5,
        },
        drafts_need_human_review=False,
    )
    assert summarize(preview) == (
        "Preview of 5 leads: 2 would get a draft, 3 skipped, 0 failed. Nothing was created."
    )


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        ("interrupted", "stale_heartbeat", "ended as interrupted: The run stopped"),
        ("failed", "recording_failed", "ended as failed: A result could not be saved"),
        ("failed", "future_code", "ended as failed: future_code"),
    ],
)
def test_unfinished_runs_say_so(status: str, code: str, expected: str) -> None:
    assert expected in summarize(run(status=status, error_code=code))


def test_reason_labels() -> None:
    assert reason_label(None) == ""
    assert reason_label("not_hot") == "Not a hot lead"
    assert reason_label("from_a_newer_api") == "from_a_newer_api"
    assert set(OUTCOME_LABELS) == {"draft_created", "would_create_draft", "skipped", "failed"}


def test_start_errors() -> None:
    timeout = describe_start_error(ApiError(ApiErrorKind.TIMEOUT, "slow"))
    assert "check the run history" in timeout
    busy = ApiError(
        ApiErrorKind.HTTP_ERROR, "API text", status_code=409, code="workflow.run_in_progress"
    )
    assert describe_start_error(busy).startswith("Another workflow run is in progress")
    other = ApiError(ApiErrorKind.UNAUTHORIZED, "Authentication failed", status_code=401)
    assert describe_start_error(other) == "Authentication failed"
