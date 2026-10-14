"""The validated ``Lead`` record and its ``LeadSource`` choices."""

from dataclasses import dataclass
from enum import StrEnum


class LeadSource(StrEnum):
    """Where a lead came from."""

    WEB_FORM = "web_form"
    REFERRAL = "referral"
    EVENT = "event"
    IMPORT = "import"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Lead:
    """A validated, normalized lead. Build it with ``validate_lead()``.

    ``email`` is lowercased; ``phone`` is E.164 (e.g. ``+923001234567``).
    At least one of them is set.
    """

    full_name: str
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    source: LeadSource = LeadSource.OTHER
    notes: str | None = None

    def identity_keys(self) -> frozenset[str]:
        """Return stable keys for spotting possible duplicates.

        Two leads sharing any key likely refer to the same contact. Matching
        and merging are out of scope for M1.
        """
        keys: set[str] = set()
        if self.email is not None:
            keys.add(f"email:{self.email}")
        if self.phone is not None:
            keys.add(f"phone:{self.phone}")
        return frozenset(keys)

    def __repr__(self) -> str:
        # Keep personal data out of logs and tracebacks.
        return (
            f"Lead(source={self.source.value!r}, has_email={self.email is not None}, "
            f"has_phone={self.phone is not None})"
        )
