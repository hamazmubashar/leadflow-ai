"""Usernames, roles, password rules and password hashing (M11). Pure, no I/O.

Passwords are hashed with scrypt (Python's ``hashlib``; no extra dependency), with a
random 16-byte salt per password. The stored form records the parameters, so they
can be raised later without breaking existing hashes:

    scrypt$<n>$<r>$<p>$<salt, base64>$<hash, base64>

Verification never raises on bad input and always takes about the same time, so a
caller can't tell a malformed or unusable hash from a wrong password.
"""

import base64
import hashlib
import hmac
import re
import secrets
from enum import StrEnum

SYSTEM_USER_ID = "00000000-0000-4000-8000-000000000000"
SYSTEM_USERNAME = "system"
RESERVED_USERNAMES = frozenset({SYSTEM_USERNAME, "admin", "root"})
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_SALT_BYTES = 16
_HASH_BYTES = 32
_USERNAME = re.compile(r"[a-z0-9._-]{3,64}")
# A few of the most common passwords of the minimum length or longer; the length rule
# does most of the work.
_COMMON = frozenset(
    {
        "password1234",
        "123456789012",
        "qwertyuiop12",
        "administrator",
        "letmein12345",
        "passwordpassword",
        "iloveyou1234",
    }
)


class Role(StrEnum):
    """``admin`` can do everything; ``viewer`` can only read."""

    ADMIN = "admin"
    VIEWER = "viewer"


class AccountRuleError(ValueError):
    """A username or password breaks a rule. ``message`` never contains the value."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def normalize_username(value: str) -> str:
    """Lowercase, trimmed username, or AccountRuleError."""
    username = value.strip().lower()
    if not _USERNAME.fullmatch(username):
        raise AccountRuleError(
            "Usernames are 3 to 64 characters: letters, digits, dots, dashes or underscores"
        )
    return username


def check_new_password(password: str, username: str) -> None:
    """Raise AccountRuleError if ``password`` is too weak to set."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise AccountRuleError(
            f"Passwords are {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters long"
        )
    if not password.strip() or len(set(password)) < 4:
        raise AccountRuleError("The password is too simple")
    lowered = password.lower()
    if lowered in _COMMON or username.lower() in lowered:
        raise AccountRuleError("The password is too easy to guess")


def hash_password(password: str) -> str:
    """A new salted scrypt hash of ``password`` in the stored form."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, stored: str | None) -> bool:
    """True only if ``password`` matches ``stored``. Never raises; constant work.

    A missing or unusable hash (for example the system account's ``!``) is checked
    against a dummy hash, so a failed login costs the same work either way.
    """
    parsed = _parse(stored) if stored is not None else None
    matched = _check(password, parsed if parsed is not None else _DUMMY)
    return matched and parsed is not None


def needs_rehash(stored: str) -> bool:
    """True when ``stored`` uses weaker parameters than the current ones."""
    parsed = _parse(stored)
    return parsed is None or parsed[:3] != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


_Parsed = tuple[int, int, int, bytes, bytes]


def _parse(stored: str) -> _Parsed | None:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        digest = base64.b64decode(parts[5], validate=True)
    except ValueError:
        return None
    # Bounds keep a tampered hash from requesting unbounded work or memory.
    if not (2**10 <= n <= 2**17 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
        return None
    if len(salt) < _SALT_BYTES or len(digest) != _HASH_BYTES:
        return None
    return n, r, p, salt, digest


def _check(password: str, parsed: _Parsed) -> bool:
    n, r, p, salt, digest = parsed
    return hmac.compare_digest(_scrypt(password, salt, n, r, p), digest)


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8", "surrogatepass"),  # never fails on odd JSON strings
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=256 * 1024 * 1024,
        dklen=_HASH_BYTES,
    )


# Checked when the account doesn't exist or can't log in (see verify_password).
_DUMMY: _Parsed = (SCRYPT_N, SCRYPT_R, SCRYPT_P, bytes(_SALT_BYTES), bytes(_HASH_BYTES))
