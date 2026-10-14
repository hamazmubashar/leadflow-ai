import pytest

from leadflow_ai.leads import DEFAULT_REGION, NormalizationError, normalize_phone
from leadflow_ai.leads.phones import MAX_PHONE_INPUT_LENGTH


def test_default_region_is_pakistan() -> None:
    assert DEFAULT_REGION == "PK"


@pytest.mark.parametrize(
    "raw",
    [
        "03001234567",
        "0300-1234567",
        "0300 1234567",
        "0300 123 4567",
        "(0300) 1234567",
        "0300.123.4567",
        "0300/1234567",
        "  0300-1234567  ",
        "+923001234567",
        "+92 300 1234567",
        "+92-300-1234567",
        "+92 (300) 1234567",
        "00923001234567",
        "0092 300 1234567",
    ],
)
def test_pakistani_mobile_formats_normalize_to_e164(raw: str) -> None:
    assert normalize_phone(raw) == "+923001234567"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("03451234567", "+923451234567"),
        ("0355-1234567", "+923551234567"),  # structural rule, not an operator list
        ("0399 9999999", "+923999999999"),  # structurally valid; may not be assigned
    ],
)
def test_pakistani_mobile_rule_is_structural(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+92 21 12345678", "+922112345678"),  # PK landline (Karachi), structural only
        ("+92 42 1234567", "+92421234567"),  # PK landline (Lahore), structural only
        ("+1 415 555 0123", "+14155550123"),
        ("+44 20 7946 0958", "+442079460958"),
        ("+971 50 123 4567", "+971501234567"),
        ("0044 20 7946 0958", "+442079460958"),
    ],
)
def test_international_numbers_get_structural_check_only(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("digits", "accepted"),
    [
        ("1234567", False),  # 7 digits
        ("12345678", True),  # 8 digits (minimum)
        ("123456789012345", True),  # 15 digits (E.164 maximum)
        ("1234567890123456", False),  # 16 digits
    ],
)
def test_international_length_boundaries(digits: str, accepted: bool) -> None:
    if accepted:
        assert normalize_phone(f"+{digits}") == f"+{digits}"
    else:
        with pytest.raises(NormalizationError) as exc_info:
            normalize_phone(f"+{digits}")
        assert exc_info.value.code == "phone.invalid_length"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        # Pakistani landlines and other national formats need a country code.
        ("021-12345678", "phone.national_format_unsupported"),
        ("042 1234567", "phone.national_format_unsupported"),
        ("051-1234567", "phone.national_format_unsupported"),
        ("3001234567", "phone.national_format_unsupported"),  # mobile without trunk 0
        ("923001234567", "phone.national_format_unsupported"),  # ambiguous: no + or 00
        ("4155550123", "phone.national_format_unsupported"),
        # Malformed Pakistani mobiles.
        ("0300-123456", "phone.invalid_pk_mobile"),  # 10 digits
        ("0300-12345678", "phone.invalid_pk_mobile"),  # 12 digits
        ("+92 300 123456", "phone.invalid_pk_mobile"),
        ("+92 300 12345678", "phone.invalid_pk_mobile"),
        # Structural format problems.
        ("+92 0300 1234567", "phone.invalid_format"),  # trunk 0 kept after +92
        ("0092 0300 1234567", "phone.invalid_format"),
        ("+0092 300 1234567", "phone.invalid_format"),  # country code starts with 0
        ("000 92 300 1234567", "phone.invalid_format"),
        # Letters, extensions and stray symbols.
        ("0300-CALLNOW", "phone.invalid_characters"),
        ("0300-1234567 x12", "phone.invalid_characters"),
        ("0300-1234567 ext. 12", "phone.invalid_characters"),
        ("0300-1234567#12", "phone.invalid_characters"),
        ("0300-1234567;12", "phone.invalid_characters"),
        ("++923001234567", "phone.invalid_characters"),
        ("92+3001234567", "phone.invalid_characters"),
        ("(+92) 300 1234567", "phone.invalid_characters"),
        ("tel:+923001234567", "phone.invalid_characters"),
        (
            "\u06f0\u06f3\u06f0\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7",
            "phone.invalid_characters",
        ),  # Urdu digits
        (
            "\uff10\uff13\uff10\uff10\uff11\uff12\uff13\uff14\uff15\uff16\uff17",
            "phone.invalid_characters",
        ),  # fullwidth digits
        ("0300\u20131234567", "phone.invalid_characters"),  # en dash
        # Empty or digit-free input.
        ("", "phone.invalid_length"),
        ("   ", "phone.invalid_length"),
        ("+", "phone.invalid_length"),
        ("( ) - .", "phone.invalid_length"),
        ("00", "phone.invalid_length"),
        ("+92", "phone.invalid_length"),
    ],
)
def test_invalid_numbers_are_rejected(raw: str, code: str) -> None:
    with pytest.raises(NormalizationError) as exc_info:
        normalize_phone(raw)
    assert exc_info.value.code == code


def test_region_none_requires_international_format() -> None:
    assert normalize_phone("+923001234567", default_region=None) == "+923001234567"
    assert normalize_phone("00923001234567", default_region=None) == "+923001234567"
    with pytest.raises(NormalizationError) as exc_info:
        normalize_phone("0300-1234567", default_region=None)
    assert exc_info.value.code == "phone.region_required"


@pytest.mark.parametrize("region", ["US", "pk", "IN", ""])
def test_unsupported_region_is_a_programming_error(region: str) -> None:
    with pytest.raises(ValueError, match="Unsupported default_region") as exc_info:
        normalize_phone("+923001234567", default_region=region)
    assert not isinstance(exc_info.value, NormalizationError)


@pytest.mark.parametrize(
    "raw",
    ["0300-1234567", "0092 300 1234567", "+92 21 12345678", "+1 (415) 555-0123"],
)
def test_normalization_is_idempotent(raw: str) -> None:
    once = normalize_phone(raw)
    assert normalize_phone(once) == once
    assert normalize_phone(once, default_region=None) == once


@pytest.mark.parametrize(
    "raw",
    [
        "0300-1234567 x12",
        "021-34567890",
        "0300-123456",
        "+92 0300 1234567",
        "+92 300 98765432",
        "+9876543210987654",
    ],
)
def test_errors_do_not_echo_input(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc_info:
        normalize_phone(raw)
    message = str(exc_info.value)
    digits = "".join(char for char in raw if char.isdigit())
    # No run of 4+ submitted digits may appear in the message.
    for start in range(len(digits) - 3):
        assert digits[start : start + 4] not in message


def test_raw_input_length_is_capped_after_trimming() -> None:
    at_cap = "0300" + "-" * 21 + "1234567"  # 32 characters
    assert len(at_cap) == MAX_PHONE_INPUT_LENGTH
    assert normalize_phone(f"   {at_cap}   ") == "+923001234567"
    with pytest.raises(NormalizationError) as exc_info:
        normalize_phone(at_cap + "-")
    assert exc_info.value.code == "field.too_long"
