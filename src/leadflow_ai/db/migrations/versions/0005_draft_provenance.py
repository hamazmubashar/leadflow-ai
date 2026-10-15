"""Record how each draft was produced (M9): template or a local language model.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15

Hand-written and reviewed; does not import application code. Runs inside one
transaction (see env.py). Additive only: three columns are added to ``lead_drafts``
with ``ALTER TABLE ... ADD COLUMN`` (no table rebuild), and every existing draft
becomes ``generator = 'template'``, which is true: before M9 every draft was rendered
from a template.

Integrity is enforced in the database, as for 0003 and 0004:
- ``trg_lead_drafts_provenance_valid`` refuses an insert whose provenance is not
  exactly one of these shapes:
    * ``template``: ``model`` and ``prompt_version`` are NULL;
    * ``local_llm``: ``model`` and ``prompt_version`` are set, and the draft starts
      as ``pending_review`` (a generated draft can never be stored already decided);
- ``trg_lead_drafts_content_immutable`` is recreated with the three new columns, so
  provenance can never be changed after insert. The other 0003/0004 triggers, the
  one-pending-draft index and the outbox rules are untouched.

Downgrade refuses while any ``local_llm`` draft exists: dropping the columns would
make generated drafts indistinguishable from template drafts. The refusal is plain
SQL (a CHECK on a temporary table), so it also works in offline (``--sql``) mode.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONTENT_IMMUTABLE_0003 = (
    "CREATE TRIGGER trg_lead_drafts_content_immutable"
    " BEFORE UPDATE OF id, lead_id, template, template_version, channel, recipient,"
    " subject, body, created_at ON lead_drafts"
    " BEGIN SELECT RAISE(ABORT, 'lead_drafts content is immutable'); END"
)
CONTENT_IMMUTABLE_0005 = (
    "CREATE TRIGGER trg_lead_drafts_content_immutable"
    " BEFORE UPDATE OF id, lead_id, template, template_version, channel, recipient,"
    " subject, body, created_at, generator, model, prompt_version ON lead_drafts"
    " BEGIN SELECT RAISE(ABORT, 'lead_drafts content is immutable'); END"
)
PROVENANCE_VALID = (
    "CREATE TRIGGER trg_lead_drafts_provenance_valid"
    " BEFORE INSERT ON lead_drafts"
    " WHEN NOT ("
    "(NEW.generator = 'template' AND NEW.model IS NULL AND NEW.prompt_version IS NULL)"
    " OR (NEW.generator = 'local_llm' AND NEW.model IS NOT NULL AND NEW.model <> ''"
    " AND NEW.prompt_version IS NOT NULL AND NEW.prompt_version <> ''"
    " AND NEW.status = 'pending_review'))"
    " BEGIN SELECT RAISE(ABORT, 'lead_drafts provenance is invalid'); END"
)


def upgrade() -> None:
    op.add_column(
        "lead_drafts",
        sa.Column("generator", sa.String(length=20), nullable=False, server_default="template"),
    )
    op.add_column("lead_drafts", sa.Column("model", sa.String(length=100), nullable=True))
    op.add_column("lead_drafts", sa.Column("prompt_version", sa.String(length=32), nullable=True))
    op.execute("DROP TRIGGER trg_lead_drafts_content_immutable")
    op.execute(CONTENT_IMMUTABLE_0005)
    op.execute(PROVENANCE_VALID)


def downgrade() -> None:
    # Refuse (and roll back) while generated drafts exist: CHECK fails on a non-zero count.
    op.execute(
        "CREATE TEMP TABLE m0005_downgrade_guard (local_llm_drafts INTEGER NOT NULL"
        " CONSTRAINT local_llm_drafts_exist_downgrade_refused CHECK (local_llm_drafts = 0))"
    )
    op.execute(
        "INSERT INTO m0005_downgrade_guard (local_llm_drafts)"
        " SELECT count(*) FROM lead_drafts WHERE generator = 'local_llm'"
    )
    op.execute("DROP TABLE m0005_downgrade_guard")

    op.execute("DROP TRIGGER trg_lead_drafts_provenance_valid")
    op.execute("DROP TRIGGER trg_lead_drafts_content_immutable")
    op.execute(CONTENT_IMMUTABLE_0003)
    op.drop_column("lead_drafts", "prompt_version")
    op.drop_column("lead_drafts", "model")
    op.drop_column("lead_drafts", "generator")
