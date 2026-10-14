"""Error types for lead validation.

Messages are fixed strings that describe the problem, never the submitted value,
so errors can be logged or returned without leaking personal data.
"""

from collections.abc import Sequence
from dataclasses import dataclass


class NormalizationError(ValueError):
    """Raised by a single-value normalizer (text, email, phone) when input is invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class FieldError:
    """One problem with one input field."""

    field: str
    code: str
    message: str


class LeadValidationError(ValueError):
    """Raised when a lead has one or more invalid fields. Holds every problem found."""

    def __init__(self, errors: Sequence[FieldError]) -> None:
        if not errors:
            raise ValueError("LeadValidationError needs at least one FieldError")
        self.errors: tuple[FieldError, ...] = tuple(errors)
        super().__init__(self._summary())

    def _summary(self) -> str:
        details = "; ".join(f"{error.field}: {error.code}" for error in self.errors)
        noun = "error" if len(self.errors) == 1 else "errors"
        return f"{len(self.errors)} validation {noun} ({details})"
