"""Validate and normalize raw lead input (e.g. a parsed form or CSV row)."""

from collections.abc import Callable, Mapping

from leadflow_ai.leads.emails import normalize_email
from leadflow_ai.leads.errors import FieldError, LeadValidationError, NormalizationError
from leadflow_ai.leads.models import Lead, LeadSource
from leadflow_ai.leads.phones import DEFAULT_REGION, SUPPORTED_REGIONS, normalize_phone
from leadflow_ai.leads.text import clean_text

MAX_NAME_LENGTH = 100
MAX_COMPANY_LENGTH = 120
MAX_NOTES_LENGTH = 1000

ALLOWED_FIELDS = ("full_name", "email", "phone", "company", "source", "notes")
UNKNOWN_FIELDS = "unknown_fields"


def validate_lead(
    data: Mapping[str, object], *, default_region: str | None = DEFAULT_REGION
) -> Lead:
    """Return a normalized ``Lead`` or raise ``LeadValidationError`` listing every problem.

    Values must be strings. ``None``, ``""`` and whitespace-only strings count as
    missing. ``full_name`` is required, and at least one of ``email`` or
    ``phone`` must be present. Error messages never contain submitted values.
    """
    if not isinstance(data, Mapping):
        raise TypeError("data must be a mapping of field names to values")
    if default_region is not None and default_region not in SUPPORTED_REGIONS:
        raise ValueError(f"Unsupported default_region; expected one of {sorted(SUPPORTED_REGIONS)}")

    errors: list[FieldError] = []

    def read(field: str, normalize: Callable[[str], str]) -> str | None:
        raw = data.get(field)
        if raw is None:
            return None
        if not isinstance(raw, str):
            errors.append(FieldError(field, "field.wrong_type", "Must be text"))
            return None
        if not raw.strip():
            return None
        try:
            return normalize(raw)
        except NormalizationError as problem:
            errors.append(FieldError(field, problem.code, problem.message))
            return None

    if any(key not in ALLOWED_FIELDS for key in data):
        # Key names are not echoed: they come from the input and may hold personal data.
        errors.append(
            FieldError(
                UNKNOWN_FIELDS,
                "field.unknown",
                f"Contains unexpected fields; allowed fields are: {', '.join(ALLOWED_FIELDS)}",
            )
        )

    full_name = read(
        "full_name",
        lambda v: clean_text(v, max_length=MAX_NAME_LENGTH, require_letter_or_digit=True),
    )
    email = read("email", normalize_email)
    phone = read("phone", lambda v: normalize_phone(v, default_region=default_region))
    company = read(
        "company",
        lambda v: clean_text(v, max_length=MAX_COMPANY_LENGTH, require_letter_or_digit=True),
    )
    source = read("source", _parse_source)
    notes = read("notes", lambda v: clean_text(v, max_length=MAX_NOTES_LENGTH, allow_newlines=True))

    fields_with_errors = {error.field for error in errors}
    if full_name is None and "full_name" not in fields_with_errors:
        errors.append(FieldError("full_name", "field.required", "Required"))
    if email is None and phone is None and not {"email", "phone"} & fields_with_errors:
        errors.append(
            FieldError(
                "contact", "lead.contact_required", "Provide an email address or phone number"
            )
        )

    if errors or full_name is None:
        raise LeadValidationError(errors)
    return Lead(
        full_name=full_name,
        email=email,
        phone=phone,
        company=company,
        source=LeadSource(source) if source is not None else LeadSource.OTHER,
        notes=notes,
    )


def _parse_source(value: str) -> str:
    choice = value.strip().lower()
    if choice not in {source.value for source in LeadSource}:
        allowed = ", ".join(source.value for source in LeadSource)
        raise NormalizationError("source.invalid_choice", f"Must be one of: {allowed}")
    return choice
