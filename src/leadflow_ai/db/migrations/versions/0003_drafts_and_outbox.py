"""Add response drafts (lead_drafts) and the simulated outbox (outbox_messages).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15

Hand-written and reviewed; does not import application code. Runs inside one
transaction (see env.py), so every step below is applied or none is. Only creates
new tables, indexes and triggers: existing tables are not rebuilt or changed.

The triggers make drafts and outbox items immutable in the database itself, not
only in application code:
- a draft's content columns can never be updated;
- a decided (approved or rejected) draft can never be updated at all;
- an outbox row can only be inserted for an approved draft of the same lead;
- an outbox row can never be updated or deleted;
- a draft that has an outbox row can never be deleted.

Downgrade drops both tables, which permanently deletes all drafts and outbox items.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGERS = {
    "trg_lead_drafts_content_immutable": (
        "CREATE TRIGGER trg_lead_drafts_content_immutable"
        " BEFORE UPDATE OF id, lead_id, template, template_version, channel, recipient,"
        " subject, body, created_at ON lead_drafts"
        " BEGIN SELECT RAISE(ABORT, 'lead_drafts content is immutable'); END"
    ),
    "trg_lead_drafts_decided_immutable": (
        "CREATE TRIGGER trg_lead_drafts_decided_immutable"
        " BEFORE UPDATE ON lead_drafts WHEN OLD.status <> 'pending_review'"
        " BEGIN SELECT RAISE(ABORT, 'decided lead_drafts are immutable'); END"
    ),
    "trg_lead_drafts_no_delete_with_outbox": (
        "CREATE TRIGGER trg_lead_drafts_no_delete_with_outbox"
        " BEFORE DELETE ON lead_drafts"
        " WHEN EXISTS (SELECT 1 FROM outbox_messages WHERE draft_id = OLD.id)"
        " BEGIN SELECT RAISE(ABORT, 'lead_drafts with an outbox message cannot be deleted'); END"
    ),
    "trg_outbox_messages_require_approved_draft": (
        "CREATE TRIGGER trg_outbox_messages_require_approved_draft"
        " BEFORE INSERT ON outbox_messages"
        " WHEN NOT EXISTS (SELECT 1 FROM lead_drafts WHERE id = NEW.draft_id"
        " AND lead_id = NEW.lead_id AND status = 'approved')"
        " BEGIN SELECT RAISE(ABORT, 'outbox_messages require an approved draft'); END"
    ),
    "trg_outbox_messages_immutable": (
        "CREATE TRIGGER trg_outbox_messages_immutable"
        " BEFORE UPDATE ON outbox_messages"
        " BEGIN SELECT RAISE(ABORT, 'outbox_messages are immutable'); END"
    ),
    "trg_outbox_messages_no_delete": (
        "CREATE TRIGGER trg_outbox_messages_no_delete"
        " BEFORE DELETE ON outbox_messages"
        " BEGIN SELECT RAISE(ABORT, 'outbox_messages cannot be deleted'); END"
    ),
}


def upgrade() -> None:
    op.create_table(
        "lead_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=False),
        sa.Column("template", sa.String(length=20), nullable=False),
        sa.Column("template_version", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("recipient", sa.String(length=254), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("body", sa.String(length=2000), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("rejection_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lead_drafts")),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_lead_drafts_lead_id_leads")
        ),
        sa.CheckConstraint(
            "template IN ('introduction', 'follow_up')",
            name=op.f("ck_lead_drafts_template_allowed"),
        ),
        sa.CheckConstraint("channel = 'email'", name=op.f("ck_lead_drafts_channel_allowed")),
        sa.CheckConstraint(
            "recipient = lower(recipient)", name=op.f("ck_lead_drafts_recipient_lowercase")
        ),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected')",
            name=op.f("ck_lead_drafts_status_allowed"),
        ),
        sa.CheckConstraint(
            "(status = 'pending_review' AND decided_at IS NULL AND rejection_reason IS NULL)"
            " OR (status = 'approved' AND decided_at IS NOT NULL AND rejection_reason IS NULL)"
            " OR (status = 'rejected' AND decided_at IS NOT NULL)",
            name=op.f("ck_lead_drafts_shape"),
        ),
    )
    # At most one draft awaiting review per lead: the final safeguard behind the
    # service's check (which runs under SQLite's write lock).
    op.create_index(
        op.f("uq_lead_drafts_one_pending_per_lead"),
        "lead_drafts",
        ["lead_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending_review'"),
    )
    op.create_index(
        op.f("ix_lead_drafts_lead_id_created_at"), "lead_drafts", ["lead_id", "created_at"]
    )

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_messages")),
        sa.ForeignKeyConstraint(
            ["draft_id"], ["lead_drafts.id"], name=op.f("fk_outbox_messages_draft_id_lead_drafts")
        ),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_outbox_messages_lead_id_leads")
        ),
        sa.UniqueConstraint("draft_id", name=op.f("uq_outbox_messages_draft_id")),
        sa.CheckConstraint("status = 'simulated'", name=op.f("ck_outbox_messages_status_allowed")),
    )
    op.create_index(op.f("ix_outbox_messages_created_at"), "outbox_messages", ["created_at"])

    for statement in TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
    op.drop_index(op.f("ix_outbox_messages_created_at"), table_name="outbox_messages")
    op.drop_table("outbox_messages")
    op.drop_index(op.f("ix_lead_drafts_lead_id_created_at"), table_name="lead_drafts")
    op.drop_index(op.f("uq_lead_drafts_one_pending_per_lead"), table_name="lead_drafts")
    op.drop_table("lead_drafts")
