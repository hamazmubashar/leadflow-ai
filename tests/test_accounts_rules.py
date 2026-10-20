"""M11 account rules, password hashing and session tokens. Pure functions, no database."""

import ast
import hashlib
from pathlib import Path

import pytest

import leadflow_ai.accounts
from leadflow_ai.accounts import (
    MIN_PASSWORD_LENGTH,
    RESERVED_USERNAMES,
    SYSTEM_USER_ID,
    AccountRuleError,
    Role,
    check_new_password,
    hash_password,
    needs_rehash,
    new_session_token,
    normalize_username,
    token_hash,
    verify_password,
)
from leadflow_ai.accounts import passwords as passwords_module

PACKAGE_DIR = Path(leadflow_ai.accounts.__file__).parent
GOOD = "correct horse battery"


# --- usernames ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" Alice ", "alice"), ("ops.team-1", "ops.team-1"), ("a_b", "a_b"), ("x" * 64, "x" * 64)],
)
def test_usernames_are_normalized(raw: str, expected: str) -> None:
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "ab", "x" * 65, "has space", "émile", "a/b", "a@b"])
def test_invalid_usernames_are_rejected_without_echo(raw: str) -> None:
    with pytest.raises(AccountRuleError) as caught:
        normalize_username(raw)
    assert raw.strip() == "" or raw not in caught.value.message


def test_reserved_usernames() -> None:
    assert {"system", "admin", "root"} <= RESERVED_USERNAMES
    assert SYSTEM_USER_ID == "00000000-0000-4000-8000-000000000000"


# --- password rules -------------------------------------------------------------------------


def test_a_good_password_passes() -> None:
    check_new_password(GOOD, "alice")


@pytest.mark.parametrize(
    ("password", "fragment"),
    [
        ("short", f"{MIN_PASSWORD_LENGTH} to"),
        ("x" * 129, "to 128"),
        ("aaaaaaaaaaaaaa", "too simple"),
        ("ababababababab", "too simple"),
        ("            ", "too simple"),
        ("Password1234", "too easy"),
        ("my-alice-password", "too easy"),
    ],
)
def test_weak_passwords_are_rejected(password: str, fragment: str) -> None:
    with pytest.raises(AccountRuleError) as caught:
        check_new_password(password, "alice")
    assert fragment in caught.value.message
    assert password not in caught.value.message


# --- hashing --------------------------------------------------------------------------------


def test_hash_format_and_round_trip() -> None:
    stored = hash_password(GOOD)
    parts = stored.split("$")
    assert parts[:4] == ["scrypt", "16384", "8", "1"] and len(parts) == 6
    assert GOOD not in stored
    assert verify_password(GOOD, stored)
    assert not verify_password(GOOD + "!", stored)
    assert not needs_rehash(stored)


def test_every_hash_has_its_own_salt() -> None:
    assert hash_password(GOOD) != hash_password(GOOD)


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "!",
        "",
        "plain-text",
        "scrypt$16384$8$1$!!!$!!!",
        "scrypt$x$8$1$AAAA$AAAA",
        "bcrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 44,
        "scrypt$1000$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 43 + "=",  # n not a power of 2
        "scrypt$1048576$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 43 + "=",  # too much work
        "scrypt$16384$8$1$AAAA$" + "A" * 43 + "=",  # salt too short
        "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAA",  # digest too short
    ],
)
def test_unusable_hashes_never_match_and_never_raise(stored: str | None) -> None:
    assert verify_password(GOOD, stored) is False
    if stored is not None:
        assert needs_rehash(stored)


def test_unusable_hashes_still_cost_a_full_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timing: an unknown account is checked against a dummy hash with the real cost."""
    calls: list[int] = []
    original = passwords_module._scrypt

    def counting(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
        calls.append(n)
        return original(password, salt, n, r, p)

    monkeypatch.setattr(passwords_module, "_scrypt", counting)
    verify_password(GOOD, None)
    verify_password(GOOD, "!")
    verify_password(GOOD, hash_password(GOOD))
    assert calls == [16384, 16384, 16384, 16384]  # one hash for setting, three checks


def test_weaker_parameters_need_a_rehash() -> None:
    salt = "A" * 22 + "=="
    digest = "A" * 43 + "="
    assert needs_rehash(f"scrypt$8192$8$1${salt}${digest}")
    assert verify_password(GOOD, f"scrypt$8192$8$1${salt}${digest}") is False


def test_odd_unicode_passwords_never_raise() -> None:
    stored = hash_password("pässwörd-with-\ud800-surrogate")
    assert verify_password("pässwörd-with-\ud800-surrogate", stored)


def test_roles() -> None:
    assert [role.value for role in Role] == ["admin", "viewer"]


# --- session tokens -------------------------------------------------------------------------


def test_new_tokens_are_random_and_stored_only_as_a_hash() -> None:
    token, stored = new_session_token()
    other, _ = new_session_token()
    assert token != other and len(token) >= 43
    assert stored == hashlib.sha256(token.encode()).hexdigest() == token_hash(token)
    assert len(stored) == 64 and token not in stored


@pytest.mark.parametrize("token", ["", "x" * 129, "naïve-token"])
def test_impossible_tokens_have_no_hash(token: str) -> None:
    assert token_hash(token) is None


# --- purity ---------------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(PACKAGE_DIR.glob("*.py")), ids=lambda p: p.name)
def test_the_accounts_package_is_pure(path: Path) -> None:
    allowed = {"base64", "hashlib", "hmac", "re", "secrets", "enum", "datetime", "leadflow_ai"}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert name.split(".")[0] in allowed, (path.name, name)
            if name.startswith("leadflow_ai"):
                assert name.startswith("leadflow_ai.accounts"), (path.name, name)
