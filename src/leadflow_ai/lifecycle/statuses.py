"""Lead statuses and the fixed transition table (not configurable)."""

from enum import StrEnum
from types import MappingProxyType


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    WON = "won"
    LOST = "lost"


INITIAL_STATUS = LeadStatus.NEW

# Allowed next statuses. A change to the same status is never allowed; WON is final.
TRANSITIONS: MappingProxyType[LeadStatus, frozenset[LeadStatus]] = MappingProxyType(
    {
        LeadStatus.NEW: frozenset({LeadStatus.CONTACTED, LeadStatus.QUALIFIED, LeadStatus.LOST}),
        LeadStatus.CONTACTED: frozenset({LeadStatus.QUALIFIED, LeadStatus.LOST}),
        LeadStatus.QUALIFIED: frozenset({LeadStatus.WON, LeadStatus.LOST}),
        LeadStatus.LOST: frozenset({LeadStatus.CONTACTED}),
        LeadStatus.WON: frozenset(),
    }
)


def allowed_next(status: LeadStatus) -> tuple[LeadStatus, ...]:
    """The statuses ``status`` may change to, in a stable (declaration) order."""
    return tuple(s for s in LeadStatus if s in TRANSITIONS[status])


def is_allowed(current: LeadStatus, target: LeadStatus) -> bool:
    return target in TRANSITIONS[current]
