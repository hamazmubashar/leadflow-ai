"""AI-assisted drafts in the dashboard (M9): wording, error messages and the
one-request-at-a-time guard. No Streamlit here, so all of it is unit-tested.

The real protection against duplicates is in the API: one pending draft per lead and
one generation at a time (``llm.busy``). The guard below only stops the button from
being clicked again while a request is visibly running.
"""

from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dashboard.api_client import ApiError, ApiErrorKind
from dashboard.leads import describe_api_error
from dashboard.models import AssistStatus, Draft, DraftTemplate

AI_WARNING = (
    "Written by a local AI model from internal notes. "
    "Check for internal remarks, dates or promises before approving."
)
AI_LIMITS_NOTE = (
    "The model is told not to mention internal notes, dates or promises, but instructions "
    "are not a guarantee: automatic checks only block links, email addresses, phone "
    "numbers and markup. You decide what is approved."
)
WAIT_MESSAGE = (
    "Writing the draft with the local model. This can take up to a minute; "
    "please wait and do not click again."
)
PENDING_KEY = "assist.pending"
# st.session_state in the app (its keys are typed ``str | int``), a dict in tests.
State = MutableMapping[Any, Any]
# A request older than this is treated as finished (e.g. the page was reloaded while it
# ran). It is longer than the dashboard's own timeout for the request.
STALE_AFTER = timedelta(minutes=6)

_ERROR_LINES: dict[str, str] = {
    "llm.disabled": (
        "AI drafts are turned off. Set LEADFLOW_LLM_URL in .env to your local Ollama "
        "address (for example http://127.0.0.1:11434) and restart the API."
    ),
    "llm.busy": (
        "Another AI draft is being written right now. Wait for it to finish, then try again."
    ),
    "llm.unavailable": (
        "The local model server (Ollama) is not reachable. Start Ollama, then try again."
    ),
    "llm.model_not_found": (
        "The configured model is not installed in Ollama. Install it with `ollama pull`, "
        "or change LEADFLOW_LLM_MODEL and restart the API."
    ),
    "llm.timeout": (
        "The local model did not finish in time. You can try again, or use a smaller model."
    ),
    "llm.invalid_output": (
        "The model's text failed the automatic safety checks (for example a link, an email "
        "address, a phone number or markup). Each attempt writes different text, so you "
        "can try again."
    ),
}
NOTHING_SAVED = "No draft was created."
DASHBOARD_TIMEOUT = (
    "The dashboard stopped waiting for the API, so the draft may or may not have been "
    "created. Reload this lead and check its drafts before trying again."
)


@dataclass(frozen=True)
class PendingRequest:
    """A click on the AI button. ``sent`` becomes True just before the POST goes out, so a
    request whose page run was interrupted is never sent a second time."""

    lead_id: str
    template: DraftTemplate
    started_at: datetime
    sent: bool = False


def offer_ai(status: AssistStatus | None) -> bool:
    """Show the AI button only when the API confirmed the feature is enabled."""
    return status is not None and status.enabled


def provenance_label(draft: Draft) -> str | None:
    """ "AI-assisted · qwen2.5:3b" for a generated draft; None for a template draft."""
    if not draft.is_ai_assisted:
        return None
    return "AI-assisted · " + (draft.model or "local model")


def describe_ai_error(error: ApiError) -> list[str]:
    """Safe lines for a failed AI-draft request. Only fixed wording and error codes are
    used: the API never returns model text, and none is shown here."""
    if error.kind is ApiErrorKind.TIMEOUT:
        return [DASHBOARD_TIMEOUT]
    if error.code in _ERROR_LINES:
        return [_ERROR_LINES[error.code], NOTHING_SAVED]
    return describe_api_error(error)


def begin(state: State, lead_id: str, template: DraftTemplate, now: datetime) -> bool:
    """Record a click for ``lead_id``; False (nothing recorded) if a request is running."""
    if running(state, now) is not None:
        return False
    state[PENDING_KEY] = PendingRequest(lead_id, template, now)
    return True


def running(state: State, now: datetime) -> PendingRequest | None:
    """The request in progress, if any (stale ones are dropped)."""
    pending = state.get(PENDING_KEY)
    if not isinstance(pending, PendingRequest):
        return None
    if now - pending.started_at > STALE_AFTER:
        state.pop(PENDING_KEY, None)
        return None
    return pending


def take_unsent(state: State, lead_id: str, now: datetime) -> PendingRequest | None:
    """The click to send now for ``lead_id``, marked as sent; None if there is nothing to
    send (no click, another lead's click, or a request that was already sent once)."""
    pending = running(state, now)
    if pending is None or pending.lead_id != lead_id or pending.sent:
        return None
    sent = PendingRequest(pending.lead_id, pending.template, pending.started_at, sent=True)
    state[PENDING_KEY] = sent
    return sent


def finish(state: State) -> None:
    state.pop(PENDING_KEY, None)
