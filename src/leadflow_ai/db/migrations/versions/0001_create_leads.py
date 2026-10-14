"""Create the leads table.

Revision ID: 0001
Revises:
Create Date: 2026-10-14

Hand-written and reviewed. It does not import application models, so it stays
a fixed snapshot of the schema at this revision.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("full_name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("phone", sa.String(length=16), nullable=True),
        sa.Column("company", sa.String(length=120), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
        sa.UniqueConstraint("email", name=op.f("uq_leads_email")),
        sa.UniqueConstraint("phone", name=op.f("uq_leads_phone")),
        sa.CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL", name=op.f("ck_leads_contact_required")
        ),
        sa.CheckConstraint(
            "source IN ('web_form', 'referral', 'event', 'import', 'other')",
            name=op.f("ck_leads_source_allowed"),
        ),
        sa.CheckConstraint(
            "email IS NULL OR email = lower(email)", name=op.f("ck_leads_email_lowercase")
        ),
    )


def downgrade() -> None:
    op.drop_table("leads")
