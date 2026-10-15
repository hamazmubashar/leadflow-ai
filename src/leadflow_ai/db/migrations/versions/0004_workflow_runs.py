"""Add workflow runs (workflow_runs) and their per-lead results (workflow_run_items).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15

Hand-written and reviewed; does not import application code. Runs inside one
transaction (see env.py), so every step below is applied or none is. Only creates
new tables, indexes and triggers: existing tables are not rebuilt or changed.

Database-level safeguards (the service checks the same things first):
- at most one ``running`` run per workflow (partial unique index);
- a lead is recorded at most once per run, and at most once per workflow version by
  live runs, failures excluded so they can be retried (partial unique index);
- results can only be added to a ``running`` run of the same mode and version;
- a live result that created a draft must point to a pending draft of that lead;
  a preview result can never point to a draft;
- a run can only be marked ``completed`` once every planned lead has a result;
- a finished run, and every result, can never change or be deleted.

Downgrade drops both tables, which permanently deletes the run history. Drafts that
runs created are ordinary M6 drafts and are kept.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGERS = {
    "trg_workflow_runs_identity_immutable": (
        "CREATE TRIGGER trg_workflow_runs_identity_immutable"
        " BEFORE UPDATE OF id, workflow, workflow_version, rules_version, mode, planned_count,"
        " started_at ON workflow_runs"
        " BEGIN SELECT RAISE(ABORT, 'workflow_runs identity is immutable'); END"
    ),
    "trg_workflow_runs_finished_immutable": (
        "CREATE TRIGGER trg_workflow_runs_finished_immutable"
        " BEFORE UPDATE ON workflow_runs WHEN OLD.status <> 'running'"
        " BEGIN SELECT RAISE(ABORT, 'finished workflow_runs are immutable'); END"
    ),
    "trg_workflow_runs_complete_requires_all_items": (
        "CREATE TRIGGER trg_workflow_runs_complete_requires_all_items"
        " BEFORE UPDATE OF status ON workflow_runs WHEN NEW.status = 'completed'"
        " AND (SELECT count(*) FROM workflow_run_items WHERE run_id = NEW.id)"
        " <> NEW.planned_count"
        " BEGIN SELECT RAISE(ABORT, 'workflow_runs can only complete with every item'); END"
    ),
    "trg_workflow_runs_no_delete": (
        "CREATE TRIGGER trg_workflow_runs_no_delete"
        " BEFORE DELETE ON workflow_runs"
        " BEGIN SELECT RAISE(ABORT, 'workflow_runs cannot be deleted'); END"
    ),
    "trg_workflow_run_items_require_running_run": (
        "CREATE TRIGGER trg_workflow_run_items_require_running_run"
        " BEFORE INSERT ON workflow_run_items"
        " WHEN NOT EXISTS (SELECT 1 FROM workflow_runs WHERE id = NEW.run_id"
        " AND status = 'running' AND mode = NEW.mode"
        " AND workflow_version = NEW.workflow_version)"
        " BEGIN SELECT RAISE(ABORT, 'workflow_run_items require a running run'); END"
    ),
    "trg_workflow_run_items_draft_matches_lead": (
        "CREATE TRIGGER trg_workflow_run_items_draft_matches_lead"
        " BEFORE INSERT ON workflow_run_items"
        " WHEN NEW.draft_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM lead_drafts"
        " WHERE id = NEW.draft_id AND lead_id = NEW.lead_id AND status = 'pending_review')"
        " BEGIN SELECT RAISE(ABORT, 'workflow_run_items require a pending draft of the lead');"
        " END"
    ),
    "trg_workflow_run_items_immutable": (
        "CREATE TRIGGER trg_workflow_run_items_immutable"
        " BEFORE UPDATE ON workflow_run_items"
        " BEGIN SELECT RAISE(ABORT, 'workflow_run_items are immutable'); END"
    ),
    "trg_workflow_run_items_no_delete": (
        "CREATE TRIGGER trg_workflow_run_items_no_delete"
        " BEFORE DELETE ON workflow_run_items"
        " BEGIN SELECT RAISE(ABORT, 'workflow_run_items cannot be deleted'); END"
    ),
}


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow", sa.String(length=40), nullable=False),
        sa.Column("workflow_version", sa.String(length=32), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("planned_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
        sa.CheckConstraint(
            "workflow IN ('new_lead_outreach')", name=op.f("ck_workflow_runs_workflow_allowed")
        ),
        sa.CheckConstraint(
            "mode IN ('preview', 'live')", name=op.f("ck_workflow_runs_mode_allowed")
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'interrupted')",
            name=op.f("ck_workflow_runs_status_allowed"),
        ),
        sa.CheckConstraint(
            "planned_count >= 0", name=op.f("ck_workflow_runs_planned_count_non_negative")
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND error_code IS NULL)"
            " OR (status = 'completed' AND finished_at IS NOT NULL AND error_code IS NULL)"
            " OR (status IN ('failed', 'interrupted') AND finished_at IS NOT NULL"
            " AND error_code IS NOT NULL)",
            name=op.f("ck_workflow_runs_shape"),
        ),
    )
    # One running run per workflow: the final safeguard behind the service's check.
    op.create_index(
        op.f("uq_workflow_runs_one_running"),
        "workflow_runs",
        ["workflow"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )
    op.create_index(op.f("ix_workflow_runs_started_at"), "workflow_runs", ["started_at"])

    op.create_table(
        "workflow_run_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_version", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("tier", sa.String(length=10), nullable=True),
        sa.Column("draft_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_run_items")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_workflow_run_items_run_id_workflow_runs"),
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_workflow_run_items_lead_id_leads")
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["lead_drafts.id"],
            name=op.f("fk_workflow_run_items_draft_id_lead_drafts"),
        ),
        sa.UniqueConstraint("run_id", "lead_id", name=op.f("uq_workflow_run_items_run_id_lead_id")),
        sa.UniqueConstraint("draft_id", name=op.f("uq_workflow_run_items_draft_id")),
        sa.CheckConstraint(
            "mode IN ('preview', 'live')", name=op.f("ck_workflow_run_items_mode_allowed")
        ),
        sa.CheckConstraint(
            "outcome IN ('draft_created', 'would_create_draft', 'skipped', 'failed')",
            name=op.f("ck_workflow_run_items_outcome_allowed"),
        ),
        sa.CheckConstraint(
            "tier IS NULL OR tier IN ('hot', 'warm', 'cold')",
            name=op.f("ck_workflow_run_items_tier_allowed"),
        ),
        sa.CheckConstraint(
            "(outcome = 'draft_created' AND mode = 'live' AND draft_id IS NOT NULL"
            " AND reason IS NULL)"
            " OR (outcome = 'would_create_draft' AND mode = 'preview' AND draft_id IS NULL"
            " AND reason IS NULL)"
            " OR (outcome IN ('skipped', 'failed') AND draft_id IS NULL AND reason IS NOT NULL)",
            name=op.f("ck_workflow_run_items_shape"),
        ),
    )
    # A live run processes a lead at most once per workflow version; failures don't
    # count, so a later run retries them. Previews never count.
    op.create_index(
        op.f("uq_workflow_run_items_live_once"),
        "workflow_run_items",
        ["workflow_version", "lead_id"],
        unique=True,
        sqlite_where=sa.text("mode = 'live' AND outcome <> 'failed'"),
    )

    for statement in TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
    op.drop_index(op.f("uq_workflow_run_items_live_once"), table_name="workflow_run_items")
    op.drop_table("workflow_run_items")
    op.drop_index(op.f("ix_workflow_runs_started_at"), table_name="workflow_runs")
    op.drop_index(op.f("uq_workflow_runs_one_running"), table_name="workflow_runs")
    op.drop_table("workflow_runs")
