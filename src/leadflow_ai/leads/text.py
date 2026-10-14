"""Cleaning for free-text fields such as names, company names and notes."""

import unicodedata

from leadflow_ai.leads.errors import NormalizationError

# Control characters that are really whitespace and are safe to normalize.
_SINGLE_LINE_WHITESPACE = frozenset("\t\n\r\v\f")
_MULTI_LINE_WHITESPACE = frozenset("\t\n\r")


def clean_text(
    value: str,
    *,
    max_length: int,
    allow_newlines: bool = False,
    require_letter_or_digit: bool = False,
) -> str:
    """Return ``value`` normalized for storage. May return ``""`` for blank input.

    - Applies Unicode NFC so visually identical text compares equal.
    - Rejects control characters other than ordinary whitespace.
    - Single-line: collapses all whitespace runs (including newlines) to one space.
    - Multi-line: converts CRLF/CR to LF and keeps line breaks.
    - Trims leading and trailing whitespace, then enforces ``max_length``.
    - With ``require_letter_or_digit``, non-blank text must contain at least one
      Unicode letter or digit (any script). This rejects text made only of
      invisible formatting characters, punctuation or symbols, while keeping
      joiners such as ZWNJ that appear *between* letters in Urdu or Persian.
    """
    if max_length < 1:
        raise ValueError("max_length must be at least 1")

    text = unicodedata.normalize("NFC", value)
    allowed = _MULTI_LINE_WHITESPACE if allow_newlines else _SINGLE_LINE_WHITESPACE
    for char in text:
        category = unicodedata.category(char)
        if (category == "Cc" and char not in allowed) or category == "Cs":
            raise NormalizationError("field.invalid_characters", "Contains control characters")

    if allow_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    else:
        text = " ".join(text.split())

    if len(text) > max_length:
        raise NormalizationError("field.too_long", f"Must be at most {max_length} characters")
    if require_letter_or_digit and text and not any(_is_letter_or_digit(c) for c in text):
        raise NormalizationError(
            "field.no_readable_text", "Must contain at least one letter or digit"
        )
    return text


def _is_letter_or_digit(char: str) -> bool:
    # Unicode general categories L* (letters, incl. Arabic script "Lo") and N* (numbers).
    return unicodedata.category(char)[0] in ("L", "N")
