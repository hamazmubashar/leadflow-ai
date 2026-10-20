"""Login session tokens (M11). Pure, no I/O.

A token is 32 random bytes (URL-safe text), given to the client once at login. Only
its SHA-256 hash is stored, so the database alone can't be used to log in. Session
lifetime is fixed at login and is never extended.
"""

import hashlib
import secrets
from datetime import timedelta

SESSION_LIFETIME = timedelta(hours=8)
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
# Expired sessions are deleted at the next login once they're this old.
EXPIRED_SESSION_RETENTION = timedelta(days=7)
_TOKEN_BYTES = 32
_MAX_TOKEN_LENGTH = 128


def new_session_token() -> tuple[str, str]:
    """A new random token and its stored form (SHA-256, hex)."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    return token, _sha256(token)


def token_hash(token: str) -> str | None:
    """The stored form of ``token``, or None for text that can't be a session token."""
    if not 1 <= len(token) <= _MAX_TOKEN_LENGTH or not token.isascii():
        return None
    return _sha256(token)


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()
