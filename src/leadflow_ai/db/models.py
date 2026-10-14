"""ORM models. The schema itself is created only by Alembic migrations."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from leadflow_ai.db.base import Base
from leadflow_ai.db.types import UTCDateTime
from leadflow_ai.leads import LeadSource

_SOURCE_VALUES = ", ".join(f"'{source.value}'" for source in LeadSource)


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LeadRecord(Base):
    """A stored lead. Values are already normalized by M1's ``validate_lead()``.

    ``email`` and ``phone`` are each UNIQUE (NULLs allowed), which enforces the
    same identity rule as ``Lead.identity_keys()``.
    """

    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="contact_required"),
        CheckConstraint(f"source IN ({_SOURCE_VALUES})", name="source_allowed"),
        CheckConstraint("email IS NULL OR email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    full_name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    phone: Mapped[str | None] = mapped_column(String(16), unique=True)
    company: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)

    def __repr__(self) -> str:
        # Keep personal data out of logs and tracebacks.
        return f"LeadRecord(id={self.id!r})"
