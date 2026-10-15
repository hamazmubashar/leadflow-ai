"""Add lead lifecycle status and the lead_events activity history.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15

Hand-written and reviewed; does not import application code. Runs inside one
transaction (see env.py), so every step below is applied or none is.

Backfill: every existing lead gets one ``lead_created`` event. Its occurred_at is
reconstructed from the lead's stored ``created_at`` (copied verbatim), because no
history was recorded before this migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "'new', 'contacted', 'qualified', 'won', 'lost'"


def upgrade() -> None:
    # 1. Current status on leads; every existing lead starts as "new".
    op.add_column(
        "leads",
        sa.Column(
            "status",
            sa.String(length=20),
            sa.CheckConstraint(f"status IN ({_STATUSES})", name=op.f("ck_leads_status_allowed")),
            nullable=False,
            server_default="new",
        ),
    )

    # 2. Append-only activity history.
    op.create_table(
        "lead_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lead_events")),
        sa.ForeignKeyConstraint(
            ["lead_id"], ["leads.id"], name=op.f("fk_lead_events_lead_id_leads")
        ),
        sa.UniqueConstraint("lead_id", "sequence", name=op.f("uq_lead_events_lead_id_sequence")),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_lead_events_sequence_positive")),
        sa.CheckConstraint(
            "event_type IN ('lead_created', 'status_changed')",
            name=op.f("ck_lead_events_event_type_allowed"),
        ),
        sa.CheckConstraint(
            f"to_status IN ({_STATUSES})", name=op.f("ck_lead_events_to_status_allowed")
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_STATUSES})",
            name=op.f("ck_lead_events_from_status_allowed"),
        ),
        sa.CheckConstraint(
            "(event_type = 'lead_created' AND from_status IS NULL AND to_status = 'new')"
            " OR (event_type = 'status_changed' AND from_status IS NOT NULL"
            " AND from_status <> to_status)",
            name=op.f("ck_lead_events_shape"),
        ),
    )

    # 3. Backfill one lead_created event per existing lead, occurred_at = created_at
    #    (copied verbatim). A single INSERT ... SELECT, so it also works with
    #    `alembic upgrade head --sql`. Ids are random RFC 4122 version-4 UUIDs built in
    #    SQLite: xxxxxxxx-xxxx-4xxx-[89ab]xxx-xxxxxxxxxxxx.
    op.execute(
        "INSERT INTO lead_events"
        " (id, lead_id, sequence, event_type, from_status, to_status, note, occurred_at)"
        " SELECT"
        " lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4'"
        " || substr(lower(hex(randomblob(2))), 2) || '-'"
        " || substr('89ab', 1 + (abs(random()) % 4), 1)"
        " || substr(lower(hex(randomblob(2))), 2) || '-' || lower(hex(randomblob(6))),"
        " id, 1, 'lead_created', NULL, 'new', NULL, created_at"
        " FROM leads"
    )


def downgrade() -> None:
    op.drop_table("lead_events")
    op.drop_column("leads", "status")
