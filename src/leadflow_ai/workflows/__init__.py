"""Fixed, versioned workflows (M8). Pure Python, no I/O."""

from leadflow_ai.workflows.outreach import (
    MAX_LEADS_PER_RUN,
    STALE_RUN_AFTER,
    WORKFLOW_NAME,
    WORKFLOW_VERSION,
    Decision,
    FailureReason,
    ItemOutcome,
    RunError,
    RunMode,
    RunStatus,
    SkipReason,
    decide,
)

__all__ = [
    "MAX_LEADS_PER_RUN",
    "STALE_RUN_AFTER",
    "WORKFLOW_NAME",
    "WORKFLOW_VERSION",
    "Decision",
    "FailureReason",
    "ItemOutcome",
    "RunError",
    "RunMode",
    "RunStatus",
    "SkipReason",
    "decide",
]
