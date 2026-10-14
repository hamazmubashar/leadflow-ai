"""Phone number normalization to E.164 (e.g. ``+923001234567``).

Checks are structural only. A number that passes may still be unassigned,
disconnected or unreachable; nothing here contacts a network or carrier.

Supported input:
- International: ``+`` or ``00`` followed by 8-15 digits, first digit 1-9.
  Numbers starting ``+92 3`` must also match the Pakistani mobile pattern.
- National (no country code), default region ``PK`` only: Pakistani mobile
  numbers ``03XX-XXXXXXX``. Pakistani landlines and all other national
  formats must be entered with a country code, because their lengths vary by
  area and cannot be checked reliably without per-area data.
"""

import re

from leadflow_ai.leads.errors import NormalizationError

DEFAULT_REGION = "PK"
SUPPORTED_REGIONS = frozenset({"PK"})

E164_MIN_DIGITS = 8
E164_MAX_DIGITS = 15
# Raw input cap (after trimming). Generous for separators; rejects pasted blobs early.
MAX_PHONE_INPUT_LENGTH = 32

PK_COUNTRY_CODE = "92"
# Pakistani mobile: national significant number is "3" followed by 9 digits.
_PK_MOBILE_NATIONAL = re.compile(r"03[0-9]{9}")
_PK_MOBILE_SIGNIFICANT = re.compile(r"3[0-9]{9}")

_SEPARATORS = frozenset(" -.()/")
_DIGITS = frozenset("0123456789")


def normalize_phone(value: str, *, default_region: str | None = DEFAULT_REGION) -> str:
    """Return ``value`` as an E.164 string, or raise ``NormalizationError``.

    ``default_region`` decides how numbers without a country code are read.
    ``None`` means only international (``+``/``00``) input is accepted.
    """
    if default_region is not None and default_region not in SUPPORTED_REGIONS:
        raise ValueError(f"Unsupported default_region; expected one of {sorted(SUPPORTED_REGIONS)}")

    text = value.strip()
    if len(text) > MAX_PHONE_INPUT_LENGTH:
        raise NormalizationError(
            "field.too_long", f"Must be at most {MAX_PHONE_INPUT_LENGTH} characters"
        )
    international = text.startswith("+")
    body = text[1:] if international else text
    if any(char not in _DIGITS and char not in _SEPARATORS for char in body):
        raise NormalizationError(
            "phone.invalid_characters",
            "Use only digits, spaces and + ( ) - . / ; letters and extensions are not supported",
        )

    digits = "".join(char for char in body if char in _DIGITS)
    if not digits:
        raise NormalizationError("phone.invalid_length", "Phone number contains no digits")
    if not international and digits.startswith("00"):
        international = True
        digits = digits[2:]

    if international:
        return _normalize_international(digits)
    return _normalize_national(digits, default_region)


def _normalize_international(digits: str) -> str:
    if not E164_MIN_DIGITS <= len(digits) <= E164_MAX_DIGITS:
        raise NormalizationError(
            "phone.invalid_length",
            f"International numbers must have {E164_MIN_DIGITS}-{E164_MAX_DIGITS} digits",
        )
    if digits.startswith("0"):
        raise NormalizationError("phone.invalid_format", "Country code cannot start with 0")

    if digits.startswith(PK_COUNTRY_CODE):
        significant = digits[len(PK_COUNTRY_CODE) :]
        if significant.startswith("0"):
            raise NormalizationError(
                "phone.invalid_format", "Omit the leading 0 after the +92 country code"
            )
        if significant.startswith("3") and not _PK_MOBILE_SIGNIFICANT.fullmatch(significant):
            raise NormalizationError(
                "phone.invalid_pk_mobile", "Pakistani mobile numbers have 10 digits after +92"
            )
    return f"+{digits}"


def _normalize_national(digits: str, default_region: str | None) -> str:
    if default_region is None:
        raise NormalizationError(
            "phone.region_required", "Enter the number with a country code, starting with +"
        )
    # default_region is "PK": the only supported region.
    if _PK_MOBILE_NATIONAL.fullmatch(digits):
        return f"+{PK_COUNTRY_CODE}{digits[1:]}"
    if digits.startswith("03"):
        raise NormalizationError(
            "phone.invalid_pk_mobile", "Pakistani mobile numbers have 11 digits: 03XX-XXXXXXX"
        )
    raise NormalizationError(
        "phone.national_format_unsupported",
        "Only Pakistani mobile numbers (03XX-XXXXXXX) may omit the country code; "
        "enter other numbers starting with +",
    )
