"""Email address normalization using structural checks only.

This checks the *shape* of an address. It does not look up DNS or contact a mail
server, so a well-formed address may still not exist or accept mail.
"""

import re

from leadflow_ai.leads.errors import NormalizationError

MAX_EMAIL_LENGTH = 254
MAX_LOCAL_PART_LENGTH = 64
MAX_DOMAIN_LABEL_LENGTH = 63

# One dot-separated piece of the local part (RFC 5322 "atext"), ASCII only.
_LOCAL_ATOM = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+")
# One domain label: letters, digits, hyphens; no hyphen at either end.
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
# Top-level domain: letters only, or an IDNA-encoded ("xn--") label.
_TOP_LEVEL_DOMAIN = re.compile(r"[a-z]{2,}|xn--[a-z0-9-]+")


def normalize_email(value: str) -> str:
    """Return the email trimmed and lowercased, or raise ``NormalizationError``.

    Rules: ASCII only; exactly one ``@``; a dot-atom local part of 1-64
    characters; a domain of at least two labels with a letter-only (or ``xn--``)
    top-level domain; one trailing dot on the domain is dropped; 254 characters
    at most. Quoted local parts and IP-address domains are not accepted.
    """
    text = value.strip()
    # Reject oversized input before doing any further work. One extra character
    # allows the trailing domain dot that normalization removes.
    if len(text) > MAX_EMAIL_LENGTH + 1:
        raise NormalizationError("field.too_long", f"Must be at most {MAX_EMAIL_LENGTH} characters")
    if not text.isascii():
        raise NormalizationError(
            "email.unsupported_characters", "Email addresses must use ASCII characters only"
        )
    if any(char.isspace() or not char.isprintable() for char in text):
        raise _invalid_format()
    if text.count("@") != 1:
        raise _invalid_format()

    local_part, domain = text.split("@")
    _check_local_part(local_part)
    domain = _normalize_domain(domain)

    email = f"{local_part.lower()}@{domain}"
    if len(email) > MAX_EMAIL_LENGTH:
        raise NormalizationError("field.too_long", f"Must be at most {MAX_EMAIL_LENGTH} characters")
    return email


def _check_local_part(local_part: str) -> None:
    if not 1 <= len(local_part) <= MAX_LOCAL_PART_LENGTH:
        raise _invalid_format()
    # Splitting on "." yields an empty atom for leading, trailing or doubled dots.
    if not all(_LOCAL_ATOM.fullmatch(atom) for atom in local_part.split(".")):
        raise _invalid_format()


def _normalize_domain(domain: str) -> str:
    domain = domain.lower()
    if domain.endswith("."):
        domain = domain[:-1]
    labels = domain.split(".")
    if len(labels) < 2:
        raise _invalid_format()
    for label in labels:
        if len(label) > MAX_DOMAIN_LABEL_LENGTH or not _DOMAIN_LABEL.fullmatch(label):
            raise _invalid_format()
    if not _TOP_LEVEL_DOMAIN.fullmatch(labels[-1]):
        raise _invalid_format()
    return domain


def _invalid_format() -> NormalizationError:
    return NormalizationError("email.invalid_format", "Not a valid email address")
