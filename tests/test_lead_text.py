import pytest

from leadflow_ai.leads import NormalizationError, clean_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ayesha Khan", "Ayesha Khan"),
        ("  Ayesha   Khan  ", "Ayesha Khan"),
        ("Ayesha\tKhan", "Ayesha Khan"),
        ("Ayesha\r\nKhan", "Ayesha Khan"),
        ("Ayesha\u00a0Khan", "Ayesha Khan"),  # non-breaking space
        ("Jose\u0301", "Jos\u00e9"),  # decomposed accent -> composed (NFC)
        ("عائشہ خان", "عائشہ خان"),  # Urdu script
        (
            "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
            "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
        ),  # Persian word with a zero-width non-joiner (kept)
        ("", ""),
        ("   \t ", ""),
    ],
)
def test_single_line_cleaning(raw: str, expected: str) -> None:
    assert clean_text(raw, max_length=100) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("line one\nline two", "line one\nline two"),
        ("line one\r\nline two\rline three", "line one\nline two\nline three"),
        ("  \n padded \n  ", "padded"),
        ("keeps\ttabs", "keeps\ttabs"),
    ],
)
def test_multi_line_cleaning(raw: str, expected: str) -> None:
    assert clean_text(raw, max_length=100, allow_newlines=True) == expected


@pytest.mark.parametrize(
    ("raw", "allow_newlines"),
    [
        ("bell\x07char", False),
        ("null\x00byte", False),
        ("escape\x1b[31m", True),
        ("vertical\vtab", True),  # allowed only when collapsing to one line
        ("form\x0cfeed", True),
        ("lone \ud800 surrogate", False),
    ],
)
def test_control_characters_are_rejected(raw: str, allow_newlines: bool) -> None:
    with pytest.raises(NormalizationError) as exc_info:
        clean_text(raw, max_length=100, allow_newlines=allow_newlines)
    assert exc_info.value.code == "field.invalid_characters"


def test_length_is_checked_after_cleaning() -> None:
    assert clean_text("  " + "a" * 5 + "  ", max_length=5) == "aaaaa"
    with pytest.raises(NormalizationError) as exc_info:
        clean_text("a" * 6, max_length=5)
    assert exc_info.value.code == "field.too_long"
    assert exc_info.value.message == "Must be at most 5 characters"


def test_length_counts_composed_characters() -> None:
    # "e" + combining accent is 2 code points before NFC, 1 after.
    assert clean_text("e\u0301" * 3, max_length=3) == "\u00e9" * 3


def test_error_messages_do_not_echo_input() -> None:
    secret = "Ayesha Khan\x07 03001234567"
    with pytest.raises(NormalizationError) as exc_info:
        clean_text(secret, max_length=100)
    assert "Ayesha" not in str(exc_info.value)
    assert "0300" not in str(exc_info.value)


def test_cleaning_is_idempotent() -> None:
    once = clean_text("  Jose\u0301 \t Ali ", max_length=100)
    assert clean_text(once, max_length=100) == once


def test_invalid_max_length_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="max_length"):
        clean_text("x", max_length=0)


@pytest.mark.parametrize(
    "raw",
    [
        "\u200b",  # zero-width space
        "\u200b\u200c\u200d",  # zero-width space, non-joiner, joiner
        "\u2060",  # word joiner
        "\ufeff",  # byte-order mark / zero-width no-break space
        "\u00ad",  # soft hyphen
        "\u200e\u200f",  # left-to-right / right-to-left marks
        "\u0301",  # lone combining accent
        "...",
        "- / -",
        "\U0001f600",  # emoji only
        "\u200b \u200b",  # invisible characters around a space
    ],
)
def test_text_without_letters_or_digits_is_rejected_when_required(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc_info:
        clean_text(raw, max_length=100, require_letter_or_digit=True)
    assert exc_info.value.code == "field.no_readable_text"
    assert exc_info.value.message == "Must contain at least one letter or digit"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Urdu: "Ayesha Khan".
        (
            "\u0639\u0627\u0626\u0634\u06c1 \u062e\u0627\u0646",
            "\u0639\u0627\u0626\u0634\u06c1 \u062e\u0627\u0646",
        ),
        # Persian word with a zero-width non-joiner between letters (kept as-is).
        (
            "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
            "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645",
        ),
        # Urdu text containing a zero-width joiner.
        ("\u0644\u200d\u0627", "\u0644\u200d\u0627"),
        # Devanagari with combining vowel signs (categories Mc/Mn beside letters).
        ("\u0905\u0928\u093f\u0924\u093e", "\u0905\u0928\u093f\u0924\u093e"),
        ("\u738b\u82b3", "\u738b\u82b3"),  # Chinese
        ("Jos\u00e9 O'Brien-Smith", "Jos\u00e9 O'Brien-Smith"),
        ("Ali \U0001f600", "Ali \U0001f600"),  # letters plus emoji
        ("\u0663\u0661", "\u0663\u0661"),  # Arabic-Indic digits count as digits
        ("3M", "3M"),
        ("", ""),  # blank stays blank; the caller decides whether it is required
    ],
)
def test_legitimate_text_passes_letter_or_digit_check(raw: str, expected: str) -> None:
    assert clean_text(raw, max_length=100, require_letter_or_digit=True) == expected


def test_letter_or_digit_check_is_off_by_default() -> None:
    assert clean_text("\u200b", max_length=10) == "\u200b"
    assert clean_text("...", max_length=10, allow_newlines=True) == "..."
