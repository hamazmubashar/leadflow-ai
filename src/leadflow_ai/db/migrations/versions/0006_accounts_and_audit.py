"""Add operator accounts (users), login sessions (user_sessions) and who-did-what audit.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20

Hand-written and reviewed; does not import application code. Runs inside one
transaction (see env.py). Additive: two new tables, and nullable audit columns added
to existing tables with ``ALTER TABLE ... ADD COLUMN`` (no table rebuild).

- ``users``: one row per account. Passwords are stored only as salted scrypt hashes;
  usernames are lowercase and can't change; accounts are deactivated, never deleted.
  A reserved, inactive ``system`` account (fixed id) is inserted: actions taken with
  the optional service key are recorded against it. It can never log in.
- ``user_sessions``: login sessions. Only a SHA-256 hash of each session token is
  stored, so a copy of the database can't be used to log in. A session can be revoked
  (logout) but never extended or reassigned.
- Audit columns: ``lead_events.actor_id``, ``lead_drafts.created_by_id`` and
  ``decided_by_id``, ``workflow_runs.started_by_id``. NULL means "recorded before
  0.9.0" (no actor was known). They are not foreign keys, because SQLite can't drop a
  column that has one (downgrade); triggers enforce the same thing instead:
    * an actor must be an existing account;
    * an actor can never be changed once recorded;
    * ``decided_by_id`` can only be set by the update that decides the draft.

Downgrade drops the audit columns and both tables: it permanently deletes all
accounts, sessions and audit information (leads, drafts and runs are kept).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The system account's fixed id. It must equal SYSTEM_USER_ID in the application's
# accounts package (a test checks this); migrations are frozen snapshots and don't import
# application code, so the value is repeated in the INSERT below.
SYSTEM_USER_ID = "00000000-0000-4000-8000-000000000000"
_USER_EXISTS = "EXISTS (SELECT 1 FROM users WHERE id = {})"

TRIGGERS = {
    "trg_users_no_delete": (
        "CREATE TRIGGER trg_users_no_delete BEFORE DELETE ON users"
        " BEGIN SELECT RAISE(ABORT, 'users cannot be deleted; deactivate them'); END"
    ),
    "trg_users_identity_immutable": (
        "CREATE TRIGGER trg_users_identity_immutable"
        " BEFORE UPDATE OF id, username, created_at ON users"
        " BEGIN SELECT RAISE(ABORT, 'users identity is immutable'); END"
    ),
    "trg_user_sessions_identity_immutable": (
        "CREATE TRIGGER trg_user_sessions_identity_immutable"
        " BEFORE UPDATE OF id, user_id, token_hash, created_at, expires_at ON user_sessions"
        " BEGIN SELECT RAISE(ABORT, 'user_sessions identity is immutable'); END"
    ),
    "trg_user_sessions_revocation_final": (
        "CREATE TRIGGER trg_user_sessions_revocation_final"
        " BEFORE UPDATE OF revoked_at ON user_sessions WHEN OLD.revoked_at IS NOT NULL"
        " BEGIN SELECT RAISE(ABORT, 'a revoked session stays revoked'); END"
    ),
    "trg_lead_events_actor_valid": (
        "CREATE TRIGGER trg_lead_events_actor_valid BEFORE INSERT ON lead_events"
        " WHEN NEW.actor_id IS NOT NULL AND NOT " + _USER_EXISTS.format("NEW.actor_id") + ""
        " BEGIN SELECT RAISE(ABORT, 'lead_events actor must be an account'); END"
    ),
    "trg_lead_events_actor_immutable": (
        "CREATE TRIGGER trg_lead_events_actor_immutable"
        " BEFORE UPDATE OF actor_id ON lead_events"
        " BEGIN SELECT RAISE(ABORT, 'lead_events actor is immutable'); END"
    ),
    "trg_lead_drafts_actors_valid": (
        "CREATE TRIGGER trg_lead_drafts_actors_valid BEFORE INSERT ON lead_drafts"
        " WHEN (NEW.created_by_id IS NOT NULL AND NOT "
        + _USER_EXISTS.format("NEW.created_by_id")
        + ") OR (NEW.decided_by_id IS NOT NULL AND (NEW.status = 'pending_review' OR NOT "
        + _USER_EXISTS.format("NEW.decided_by_id")
        + "))"
        " BEGIN SELECT RAISE(ABORT, 'lead_drafts actors are invalid'); END"
    ),
    "trg_lead_drafts_creator_immutable": (
        "CREATE TRIGGER trg_lead_drafts_creator_immutable"
        " BEFORE UPDATE OF created_by_id ON lead_drafts"
        " BEGIN SELECT RAISE(ABORT, 'lead_drafts creator is immutable'); END"
    ),
    "trg_lead_drafts_decider_valid": (
        "CREATE TRIGGER trg_lead_drafts_decider_valid"
        " BEFORE UPDATE OF decided_by_id ON lead_drafts"
        " WHEN NEW.decided_by_id IS NOT NULL AND (NEW.status = 'pending_review' OR NOT "
        + _USER_EXISTS.format("NEW.decided_by_id")
        + ")"
        " BEGIN SELECT RAISE(ABORT, 'lead_drafts decider is invalid'); END"
    ),
    "trg_workflow_runs_starter_valid": (
        "CREATE TRIGGER trg_workflow_runs_starter_valid BEFORE INSERT ON workflow_runs"
        " WHEN NEW.started_by_id IS NOT NULL AND NOT "
        + _USER_EXISTS.format("NEW.started_by_id")
        + " BEGIN SELECT RAISE(ABORT, 'workflow_runs starter must be an account'); END"
    ),
    "trg_workflow_runs_starter_immutable": (
        "CREATE TRIGGER trg_workflow_runs_starter_immutable"
        " BEFORE UPDATE OF started_by_id ON workflow_runs"
        " BEGIN SELECT RAISE(ABORT, 'workflow_runs starter is immutable'); END"
    ),
}

AUDIT_COLUMNS = (
    ("lead_events", "actor_id"),
    ("lead_drafts", "created_by_id"),
    ("lead_drafts", "decided_by_id"),
    ("workflow_runs", "started_by_id"),
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
        sa.CheckConstraint(
            "length(username) BETWEEN 3 AND 64 AND username = lower(username)"
            " AND username NOT GLOB '*[^a-z0-9._-]*'",
            name=op.f("ck_users_username_format"),
        ),
        sa.CheckConstraint("role IN ('admin', 'viewer')", name=op.f("ck_users_role_allowed")),
        sa.CheckConstraint("failed_logins >= 0", name=op.f("ck_users_failed_logins_non_negative")),
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_sessions_user_id_users")
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_sessions_token_hash")),
        sa.CheckConstraint(
            "length(token_hash) = 64", name=op.f("ck_user_sessions_token_hash_length")
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_user_sessions_expires_after_creation")
        ),
    )
    op.create_index(op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"])

    # The reserved, inactive "system" account. Its password hash ("!") matches nothing.
    op.execute(
        "INSERT INTO users (id, username, password_hash, role, is_active, failed_logins,"
        " locked_until, created_at, password_changed_at) VALUES"
        " ('00000000-0000-4000-8000-000000000000', 'system', '!', 'admin', 0, 0, NULL,"
        " '2026-10-20 00:00:00.000000', '2026-10-20 00:00:00.000000')"
    )

    for table, column in AUDIT_COLUMNS:
        op.add_column(table, sa.Column(column, sa.String(length=36), nullable=True))
    for statement in TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
    for table, column in reversed(AUDIT_COLUMNS):
        op.drop_column(table, column)
    op.drop_index(op.f("ix_user_sessions_user_id"), table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("users")
