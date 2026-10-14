import pytest

from leadflow_ai.leads import NormalizationError, normalize_email


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ayesha@example.com", "ayesha@example.com"),
        ("  Ayesha.Khan@Example.COM  ", "ayesha.khan@example.com"),
        ("ayesha+leads@example.com", "ayesha+leads@example.com"),  # +tag kept
        ("a.b.c@example.com", "a.b.c@example.com"),  # dots kept
        ("o'neil@example.ie", "o'neil@example.ie"),
        ("x!#$%&'*+/=?^_`{|}~-y@example.com", "x!#$%&'*+/=?^_`{|}~-y@example.com"),
        ("user@mail.example.com.pk", "user@mail.example.com.pk"),
        ("user@my-company.pk", "user@my-company.pk"),
        ("user@123.example.com", "user@123.example.com"),  # numeric non-final label
        ("user@example.com.", "user@example.com"),  # one trailing dot dropped
        ("user@xn--bcher-kva.example", "user@xn--bcher-kva.example"),  # IDNA domain
        ("user@example.xn--mgbai9azgqp6j", "user@example.xn--mgbai9azgqp6j"),  # IDNA TLD
        ("a@bc.de", "a@bc.de"),  # shortest parts
        ("a" * 64 + "@example.com", "a" * 64 + "@example.com"),  # max local part
    ],
)
def test_valid_emails_are_normalized(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "plainaddress",
        "@example.com",
        "user@",
        "user@@example.com",
        "user@ex@ample.com",
        ".user@example.com",
        "user.@example.com",
        "us..er@example.com",
        "us er@example.com",
        "user\t@example.com",
        "user\x00@example.com",
        '"quoted"@example.com',
        "user(comment)@example.com",
        "user@localhost",
        "user@example",
        "user@example.c",  # single-letter TLD
        "user@example.123",  # numeric TLD
        "user@1.2.3.4",
        "user@[192.0.2.1]",  # IP-address literal
        "user@-example.com",
        "user@example-.com",
        "user@exa_mple.com",
        "user@example..com",
        "user@.example.com",
        "user@example.com..",
        "user@example.com/path",
        "a" * 65 + "@example.com",  # local part too long
        "user@" + "a" * 64 + ".com",  # domain label too long
    ],
)
def test_invalid_emails_are_rejected(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc_info:
        normalize_email(raw)
    assert exc_info.value.code == "email.invalid_format"


@pytest.mark.parametrize(
    "raw",
    [
        "jos\u00e9@example.com",  # accented local part
        "user@b\u00fccher.example",  # Unicode (non-punycode) domain
        "\u0639\u0627\u0626\u0634\u06c1@example.pk",  # Urdu local part
        "user\uff20example.com",  # fullwidth at sign
        "user@example.com\u200b",  # zero-width space
    ],
)
def test_non_ascii_emails_are_unsupported(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc_info:
        normalize_email(raw)
    assert exc_info.value.code == "email.unsupported_characters"


def test_overall_length_limit() -> None:
    label = "a" * 63
    at_limit = "u" * 59 + "@" + ".".join([label, label, label]) + ".pk"  # 254 chars
    assert len(at_limit) == 254
    assert normalize_email(at_limit) == at_limit

    over_limit = "u" * 60 + "@" + ".".join([label, label, label]) + ".pk"
    with pytest.raises(NormalizationError) as exc_info:
        normalize_email(over_limit)
    assert exc_info.value.code == "field.too_long"


@pytest.mark.parametrize(
    "raw",
    ["Ayesha.Khan@Example.COM", "  user+tag@Sub.Example.pk. ", "o'neil@example.ie"],
)
def test_normalization_is_idempotent(raw: str) -> None:
    once = normalize_email(raw)
    assert normalize_email(once) == once


@pytest.mark.parametrize(
    "raw",
    ["ayesha.khan@example", "ayesha.khan@@example.com", "ayesh\u00e1.khan@example.com"],
)
def test_errors_do_not_echo_input(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc_info:
        normalize_email(raw)
    message = str(exc_info.value)
    assert "ayesha" not in message.lower()
    assert "khan" not in message.lower()
    assert "example" not in message


def test_oversized_input_is_rejected_before_normalization() -> None:
    label = "a" * 63
    domain = ".".join([label, label, label]) + ".pk"
    # 255 characters including a trailing dot normalizes to 254 and is accepted.
    with_dot = "u" * 59 + "@" + domain + "."
    assert len(with_dot) == 255
    assert normalize_email(with_dot) == with_dot[:-1]

    # 256 characters is rejected up front.
    with pytest.raises(NormalizationError) as exc_info:
        normalize_email("u" * 60 + "@" + domain + ".")
    assert exc_info.value.code == "field.too_long"


@pytest.mark.parametrize(
    "raw",
    ["a" * 1_000_000 + "@example.com", "\u00e9" * 300 + "@example.com", "@" * 10_000],
)
def test_huge_or_non_ascii_oversized_input_reports_too_long(raw: str) -> None:
    with pytest.raises(NormalizationError) as exc_info:
        normalize_email(raw)
    assert exc_info.value.code == "field.too_long"
    assert "aaaa" not in exc_info.value.message
