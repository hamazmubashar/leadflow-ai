"""Lead lifecycle (M5): statuses and allowed transitions. Pure Python, no I/O."""

from leadflow_ai.lifecycle.statuses import (
    INITIAL_STATUS,
    TRANSITIONS,
    LeadStatus,
    allowed_next,
    is_allowed,
)

__all__ = ["INITIAL_STATUS", "TRANSITIONS", "LeadStatus", "allowed_next", "is_allowed"]
