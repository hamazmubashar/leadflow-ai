"""Labels and wording for the Workflows page (no Streamlit here).

The workflow itself runs in the API: the dashboard starts a run, then shows the run
and its per-lead results exactly as the API recorded them.
"""

from dashboard.api_client import ApiError, ApiErrorKind
from dashboard.models import WorkflowRun

STEPS = (
    "Selects leads whose status is **New** that no earlier live run has processed "
    "(oldest first, up to 500 per run).",
    "Scores each lead with the current scoring rules.",
    "**Hot** leads with an email address get an *Introduction* draft that waits for your "
    "review on the Drafts page. Everything else is skipped, with the reason recorded.",
    "Nothing is approved, rejected or sent by a run. Approval is always your decision.",
)
MODE_LABELS = {"preview": "Preview", "live": "Live"}
STATUS_LABELS = {
    "running": "Running",
    "completed": "Completed",
    "failed": "Failed",
    "interrupted": "Interrupted",
}
OUTCOME_LABELS = {
    "draft_created": "Draft created",
    "would_create_draft": "Would create a draft",
    "skipped": "Skipped",
    "failed": "Failed",
}
REASON_LABELS = {
    "not_hot": "Not a hot lead",
    "no_email": "No email address",
    "not_new": "Status changed since the run started",
    "pending_draft_exists": "Already has a draft awaiting review",
    "not_eligible": "Not eligible for a draft",
    "scoring_error": "Could not be scored",
    "database_busy": "Database busy; retried by the next run",
    "concurrent_update": "Processed elsewhere at the same time",
    "unexpected_error": "Unexpected error; retried by the next run",
}
RUN_ERROR_LABELS = {
    "stale_heartbeat": "The run stopped (server restarted or crashed); a later run took over",
    "recording_failed": "A result could not be saved, so the run stopped",
}


def reason_label(code: str | None) -> str:
    """A readable reason; unknown codes from a newer API are shown as-is."""
    return "" if code is None else REASON_LABELS.get(code, code)


def summarize(run: WorkflowRun) -> str:
    """One plain sentence about a finished run, for the confirmation message."""
    counts = run.counts
    if run.mode == "preview":
        head = (
            f"Preview of {run.planned_count} leads: {counts.would_create_draft} would get a "
            f"draft, {counts.skipped} skipped, {counts.failed} failed. Nothing was created."
        )
    else:
        head = (
            f"Live run on {run.planned_count} leads: {counts.draft_created} drafts created, "
            f"{counts.skipped} skipped, {counts.failed} failed."
        )
        if counts.draft_created:
            head += " The drafts are waiting for your review on the Drafts page."
    if run.status != "completed":
        detail = RUN_ERROR_LABELS.get(run.error_code or "", run.error_code or "")
        head += f" The run ended as {STATUS_LABELS[run.status].lower()}: {detail}."
    return head


def describe_start_error(error: ApiError) -> str:
    """Why a run didn't start (or might not have finished), from the API's fixed facts."""
    if error.kind is ApiErrorKind.TIMEOUT:
        return (
            "The API did not answer in time. The run may still be in progress or may have "
            "finished: check the run history before starting another one."
        )
    if error.code == "workflow.run_in_progress":
        return "Another workflow run is in progress. Wait for it to finish, then try again."
    return error.message
