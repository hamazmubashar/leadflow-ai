import pytest

from leadflow_ai.leads import FieldError, LeadValidationError, NormalizationError


def test_normalization_error_exposes_code_and_message() -> None:
    error = NormalizationError("field.too_long", "Must be at most 5 characters")
    assert error.code == "field.too_long"
    assert error.message == "Must be at most 5 characters"
    assert str(error) == "Must be at most 5 characters"
    assert isinstance(error, ValueError)


def test_field_error_is_immutable() -> None:
    error = FieldError(field="email", code="email.invalid_format", message="Bad format")
    with pytest.raises(AttributeError):
        error.code = "other"  # type: ignore[misc]


def test_lead_validation_error_keeps_all_errors_in_order() -> None:
    errors = [
        FieldError("full_name", "field.required", "Required"),
        FieldError("email", "email.invalid_format", "Bad format"),
    ]
    exc = LeadValidationError(errors)
    assert exc.errors == tuple(errors)
    assert str(exc) == (
        "2 validation errors (full_name: field.required; email: email.invalid_format)"
    )


def test_lead_validation_error_singular_summary() -> None:
    exc = LeadValidationError([FieldError("phone", "phone.invalid_length", "Bad length")])
    assert str(exc) == "1 validation error (phone: phone.invalid_length)"


def test_lead_validation_error_requires_errors() -> None:
    with pytest.raises(ValueError, match="at least one"):
        LeadValidationError([])
