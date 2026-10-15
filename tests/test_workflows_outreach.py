"""The fixed new-lead outreach workflow definition (pure)."""

import ast
from pathlib import Path

import pytest

import leadflow_ai.workflows
from leadflow_ai.drafts import DraftTemplate
from leadflow_ai.lifecycle import LeadStatus
from leadflow_ai.workflows import Decision, SkipReason, decide
from leadflow_ai.workflows.outreach import DRAFT_TEMPLATE, QUALIFYING_TIER

WORKFLOWS_DIR = Path(leadflow_ai.workflows.__file__).parent


def test_only_hot_new_leads_with_an_email_qualify() -> None:
    assert decide(status=LeadStatus.NEW, tier="hot", has_email=True) == Decision(True, None)


@pytest.mark.parametrize(
    ("status", "tier", "has_email", "reason"),
    [
        (LeadStatus.CONTACTED, "hot", True, SkipReason.NOT_NEW),
        (LeadStatus.QUALIFIED, "hot", True, SkipReason.NOT_NEW),
        (LeadStatus.WON, "hot", True, SkipReason.NOT_NEW),
        (LeadStatus.LOST, "hot", True, SkipReason.NOT_NEW),
        (LeadStatus.NEW, "warm", True, SkipReason.NOT_HOT),
        (LeadStatus.NEW, "cold", True, SkipReason.NOT_HOT),
        (LeadStatus.NEW, "hot", False, SkipReason.NO_EMAIL),
        (LeadStatus.CONTACTED, "cold", False, SkipReason.NOT_NEW),  # status is checked first
        (LeadStatus.NEW, "cold", False, SkipReason.NOT_HOT),  # then the tier
    ],
)
def test_everything_else_is_skipped_with_a_reason(
    status: LeadStatus, tier: str, has_email: bool, reason: SkipReason
) -> None:
    assert decide(status=status, tier=tier, has_email=has_email) == Decision(False, reason)


def test_the_workflow_only_ever_drafts_introductions_for_hot_leads() -> None:
    assert DRAFT_TEMPLATE is DraftTemplate.INTRODUCTION
    assert QUALIFYING_TIER == "hot"


@pytest.mark.parametrize("path", sorted(WORKFLOWS_DIR.glob("*.py")), ids=lambda p: p.name)
def test_definition_is_pure(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
            )
            for name in names:
                assert name.split(".")[0] in {"dataclasses", "datetime", "enum", "leadflow_ai"}
                assert not name.startswith(("leadflow_ai.db", "leadflow_ai.services")), name
