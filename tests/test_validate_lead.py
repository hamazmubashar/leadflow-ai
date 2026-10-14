from collections.abc import Mapping

import pytest

from leadflow_ai.leads import Lead, LeadSource, LeadValidationError, validate_lead

VALID_PHONE = "0300-1234567"


def error_codes(data: Mapping[str, object], **kwargs: str | None) -> list[tuple[str, str]]:
    with pytest.raises(LeadValidationError) as exc_info:
        validate_lead(data, **kwargs)
    return [(error.field, error.code) for error in exc_info.value.errors]


# --- happy paths -------------------------------------------------------------


def test_full_lead_is_normalized() -> None:
    lead = validate_lead(
        {
            "full_name": "  Ayesha   Khan ",
            "email": " Ayesha.Khan@Example.COM ",
            "phone": "0300 123 4567",
            "company": " Acme  Traders ",
            "source": "Web_Form",
            "notes": "Prefers WhatsApp.\r\nCall after 5pm. ",
        }
    )
    assert lead == Lead(
        full_name="Ayesha Khan",
        email="ayesha.khan@example.com",
        phone="+923001234567",
        company="Acme Traders",
        source=LeadSource.WEB_FORM,
        notes="Prefers WhatsApp.\nCall after 5pm.",
    )


def test_minimal_lead_with_email_only() -> None:
    lead = validate_lead({"full_name": "Ayesha Khan", "email": "a@example.com"})
    assert lead.phone is None
    assert lead.company is None
    assert lead.notes is None
    assert lead.source is LeadSource.OTHER


def test_minimal_lead_with_phone_only() -> None:
    lead = validate_lead({"full_name": "Ayesha Khan", "phone": VALID_PHONE})
    assert lead.email is None
    assert lead.phone == "+923001234567"


def test_international_phone_without_default_region() -> None:
    lead = validate_lead({"full_name": "A", "phone": "+44 20 7946 0958"}, default_region=None)
    assert lead.phone == "+442079460958"


# --- required and optional fields --------------------------------------------


@pytest.mark.parametrize("missing", [None, "", "   ", "\t\n"])
def test_full_name_is_required(missing: str | None) -> None:
    data: dict[str, object] = {"email": "a@example.com"}
    if missing is not None:
        data["full_name"] = missing
    assert error_codes(data) == [("full_name", "field.required")]


@pytest.mark.parametrize("field", ["email", "phone", "company", "source", "notes"])
@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_optional_fields_are_treated_as_missing(field: str, blank: str | None) -> None:
    data: dict[str, object] = {
        "full_name": "A",
        "email": "a@example.com",
        "phone": VALID_PHONE,
        "company": "Acme",
        "source": "event",
        "notes": "Hi",
    }
    # Missing optional fields become None; a missing source falls back to its default.
    missing_value = LeadSource.OTHER if field == "source" else None

    # Control: with a real value the field is set, so the blank case cannot pass vacuously.
    assert getattr(validate_lead(data), field) is not missing_value

    data[field] = blank
    assert getattr(validate_lead(data), field) is missing_value


@pytest.mark.parametrize(
    ("field", "length", "accepted"),
    [
        ("full_name", 100, True),
        ("full_name", 101, False),
        ("company", 120, True),
        ("company", 121, False),
        ("notes", 1000, True),
        ("notes", 1001, False),
    ],
)
def test_text_length_limits(field: str, length: int, accepted: bool) -> None:
    data: dict[str, object] = {"full_name": "A", "email": "a@example.com", field: "x" * length}
    if accepted:
        assert getattr(validate_lead(data), field) == "x" * length
    else:
        assert error_codes(data) == [(field, "field.too_long")]


# --- contact requirement -------------------------------------------------------


def test_email_or_phone_is_required() -> None:
    assert error_codes({"full_name": "A"}) == [("contact", "lead.contact_required")]
    assert error_codes({"full_name": "A", "email": " ", "phone": ""}) == [
        ("contact", "lead.contact_required")
    ]


def test_invalid_contact_reports_field_error_not_contact_required() -> None:
    assert error_codes({"full_name": "A", "email": "not-an-email"}) == [
        ("email", "email.invalid_format")
    ]
    assert error_codes({"full_name": "A", "phone": "021-12345678"}) == [
        ("phone", "phone.national_format_unsupported")
    ]


def test_one_valid_contact_does_not_excuse_an_invalid_one() -> None:
    assert error_codes({"full_name": "A", "email": "a@example.com", "phone": "0300-12"}) == [
        ("phone", "phone.invalid_pk_mobile")
    ]


def test_national_phone_with_no_region_needs_country_code() -> None:
    assert error_codes({"full_name": "A", "phone": VALID_PHONE}, default_region=None) == [
        ("phone", "phone.region_required")
    ]


def test_raw_phone_input_is_capped() -> None:
    padded = "0300" + "-" * 40 + "1234567"
    assert error_codes({"full_name": "A", "phone": padded}) == [("phone", "field.too_long")]


# --- source choices ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("web_form", LeadSource.WEB_FORM),
        ("REFERRAL", LeadSource.REFERRAL),
        ("  Event ", LeadSource.EVENT),
        ("import", LeadSource.IMPORT),
        ("other", LeadSource.OTHER),
        (LeadSource.EVENT, LeadSource.EVENT),
    ],
)
def test_source_choices(raw: str, expected: LeadSource) -> None:
    lead = validate_lead({"full_name": "A", "email": "a@example.com", "source": raw})
    assert lead.source is expected


@pytest.mark.parametrize("raw", ["web form", "web-form", "facebook", "webform", "others"])
def test_invalid_source_is_rejected(raw: str) -> None:
    assert error_codes({"full_name": "A", "email": "a@example.com", "source": raw}) == [
        ("source", "source.invalid_choice")
    ]


# --- types, unknown keys, multiple errors ---------------------------------------


@pytest.mark.parametrize("value", [3001234567, 1.5, True, ["a"], {"a": 1}, b"bytes"])
def test_non_string_values_are_wrong_type(value: object) -> None:
    assert error_codes({"full_name": "A", "email": "a@example.com", "phone": value}) == [
        ("phone", "field.wrong_type")
    ]


def test_unknown_keys_are_rejected_once() -> None:
    data: dict[object, object] = {  # mixed key types on purpose
        "full_name": "A",
        "email": "a@example.com",
        "fullname": "typo",
        "city": "Lahore",
        42: "non-string key",
    }
    with pytest.raises(LeadValidationError) as exc_info:
        validate_lead(data)  # type: ignore[arg-type]
    assert [(e.field, e.code) for e in exc_info.value.errors] == [
        ("unknown_fields", "field.unknown")
    ]
    assert "full_name, email, phone, company, source, notes" in exc_info.value.errors[0].message


def test_all_problems_are_reported_together_in_field_order() -> None:
    data: dict[str, object] = {
        "full_name": "   ",
        "email": "bad@",
        "phone": "0300-CALLNOW",
        "company": "x" * 121,
        "source": "tiktok",
        "notes": "bell\x07",
        "extra": "value",
    }
    assert error_codes(data) == [
        ("unknown_fields", "field.unknown"),
        ("email", "email.invalid_format"),
        ("phone", "phone.invalid_characters"),
        ("company", "field.too_long"),
        ("source", "source.invalid_choice"),
        ("notes", "field.invalid_characters"),
        ("full_name", "field.required"),
    ]


# --- privacy -------------------------------------------------------------------


def test_errors_never_contain_submitted_values() -> None:
    data: dict[str, object] = {
        "full_name": "Ayesha Khan\x07",
        "email": "ayesha.khan@private-example",
        "phone": "0300-98765 ext 4",
        "company": "SecretCo" * 20,
        "source": "ayesha-referral",
        "notes": "CNIC 35202-1234567-1\x00",
        "ayesha.khan@example.com": "key that is personal data",
    }
    with pytest.raises(LeadValidationError) as exc_info:
        validate_lead(data)
    rendered = [str(exc_info.value), repr(exc_info.value)]
    rendered += [f"{e.field} {e.code} {e.message}" for e in exc_info.value.errors]
    for text in rendered:
        for secret in ("Ayesha", "ayesha", "Khan", "private", "98765", "SecretCo", "35202"):
            assert secret not in text


# --- programming errors and normalization -------------------------------------------


def test_non_mapping_input_is_a_programming_error() -> None:
    with pytest.raises(TypeError, match="mapping"):
        validate_lead([("full_name", "A")])  # type: ignore[arg-type]


def test_unsupported_region_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="Unsupported default_region") as exc_info:
        validate_lead({"full_name": "A", "email": "a@example.com"}, default_region="US")
    assert not isinstance(exc_info.value, LeadValidationError)


def test_formatting_variants_share_identity_keys() -> None:
    first = validate_lead(
        {"full_name": "Ayesha Khan", "email": "Ayesha@Example.com", "phone": "0300-1234567"}
    )
    second = validate_lead(
        {"full_name": "A. Khan", "email": " ayesha@example.COM.", "phone": "+92 300 1234567"}
    )
    assert first.identity_keys() == second.identity_keys()


def test_validation_is_idempotent() -> None:
    raw: dict[str, object] = {
        "full_name": "  Ayesha  Khan ",
        "email": "Ayesha@Example.com",
        "phone": "0092 300 1234567",
        "company": " Acme ",
        "source": "EVENT",
        "notes": " hi\r\n",
    }
    once = validate_lead(raw)
    again = validate_lead(
        {
            "full_name": once.full_name,
            "email": once.email,
            "phone": once.phone,
            "company": once.company,
            "source": once.source,
            "notes": once.notes,
        }
    )
    assert again == once


# --- invisible-only and symbol-only text ------------------------------------------


@pytest.mark.parametrize("raw", ["\u200b", "\u200c\u200d", "\ufeff", "\u00ad", "...", "\U0001f600"])
def test_name_without_letters_or_digits_is_rejected(raw: str) -> None:
    assert error_codes({"full_name": raw, "email": "a@example.com"}) == [
        ("full_name", "field.no_readable_text")
    ]


@pytest.mark.parametrize("raw", ["\u200b", "---"])
def test_company_without_letters_or_digits_is_rejected(raw: str) -> None:
    data: dict[str, object] = {"full_name": "A", "email": "a@example.com", "company": raw}
    assert error_codes(data) == [("company", "field.no_readable_text")]


def test_urdu_and_persian_names_are_accepted() -> None:
    urdu = "\u0639\u0627\u0626\u0634\u06c1 \u062e\u0627\u0646"
    persian_zwnj = "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645"
    for name in (urdu, persian_zwnj):
        lead = validate_lead({"full_name": name, "email": "a@example.com"})
        assert lead.full_name == name


def test_notes_may_be_symbols_only() -> None:
    lead = validate_lead({"full_name": "A", "email": "a@example.com", "notes": "\U0001f44d"})
    assert lead.notes == "\U0001f44d"
