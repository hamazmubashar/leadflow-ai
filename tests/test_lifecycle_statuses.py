"""The lifecycle transition table: exhaustive check against the approved design."""

import ast
from itertools import product
from pathlib import Path

import pytest

from leadflow_ai import lifecycle
from leadflow_ai.lifecycle import INITIAL_STATUS, TRANSITIONS, LeadStatus, allowed_next, is_allowed

APPROVED = {
    "new": {"contacted", "qualified", "lost"},
    "contacted": {"qualified", "lost"},
    "qualified": {"won", "lost"},
    "lost": {"contacted"},
    "won": set(),
}


def test_statuses_and_initial_status() -> None:
    assert [s.value for s in LeadStatus] == ["new", "contacted", "qualified", "won", "lost"]
    assert INITIAL_STATUS is LeadStatus.NEW


@pytest.mark.parametrize(("current", "target"), list(product(LeadStatus, LeadStatus)))
def test_every_pair_matches_the_approved_table(current: LeadStatus, target: LeadStatus) -> None:
    assert is_allowed(current, target) is (target.value in APPROVED[current.value])


@pytest.mark.parametrize("status", list(LeadStatus))
def test_same_status_is_never_allowed(status: LeadStatus) -> None:
    assert not is_allowed(status, status)


def test_won_is_final_and_lost_can_reopen() -> None:
    assert allowed_next(LeadStatus.WON) == ()
    assert allowed_next(LeadStatus.LOST) == (LeadStatus.CONTACTED,)


def test_allowed_next_is_in_declaration_order() -> None:
    assert allowed_next(LeadStatus.NEW) == (
        LeadStatus.CONTACTED,
        LeadStatus.QUALIFIED,
        LeadStatus.LOST,
    )


def test_transition_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        TRANSITIONS[LeadStatus.WON] = frozenset({LeadStatus.NEW})  # type: ignore[index]


def test_lifecycle_package_is_pure() -> None:
    for path in Path(lifecycle.__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] in {"enum", "types", "leadflow_ai"}, (
                    path.name
                )
            elif isinstance(node, ast.Import):
                pytest.fail(f"{path.name} uses a plain import")
