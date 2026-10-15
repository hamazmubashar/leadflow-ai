"""Scoring rules: the shipped defaults and strict validation of TOML rule files."""

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from leadflow_ai.leads import LeadSource
from leadflow_ai.scoring.rules import (
    MAX_RULES_FILE_BYTES,
    SIGNALS,
    RuleProblem,
    ScoringRulesError,
    load_rules,
    parse_rules,
)
from tests.scoring_helpers import DEFAULT_RULES_BYTES, default_doc, toml_bytes


def problems(data: bytes) -> list[tuple[str, str]]:
    with pytest.raises(ScoringRulesError) as exc_info:
        parse_rules(data)
    return [(p.path, p.message) for p in exc_info.value.problems]


def paths(data: bytes) -> list[str]:
    return [path for path, _ in problems(data)]


def with_change(path: str, value: Any) -> bytes:
    """Default rules with one dotted key set (value=...) or deleted (value=DELETE)."""
    doc = default_doc()
    *parents, last = path.split(".")
    table = doc
    for part in parents:
        table = table[part]
    if value is DELETE:
        del table[last]
    else:
        table[last] = value
    return toml_bytes(doc)


DELETE = object()
WHOLE_NUMBER = "Must be a whole number (not a boolean, float or string)"


# --- the shipped default rules -------------------------------------------------------------------


def test_default_rules_are_valid_and_match_the_approved_design() -> None:
    rules = load_rules()
    assert rules.rules_version == "2026-10-15.1"
    assert dict(rules.source_points) == {
        "web_form": 15,
        "referral": 30,
        "event": 20,
        "import": 5,
        "other": 0,
    }
    assert rules.signal_points == (
        ("has_email", 15),
        ("has_phone", 15),
        ("has_company", 15),
        ("non_free_email_domain", 25),
    )
    assert (rules.hot_min, rules.warm_min, rules.max_score) == (70, 40, 100)
    assert "gmail.com" in rules.free_email_domains


def test_default_rules_cover_every_lead_source_in_enum_order() -> None:
    assert [name for name, _ in load_rules().source_points] == [s.value for s in LeadSource]


def test_default_file_round_trips_through_the_parser() -> None:
    assert parse_rules(DEFAULT_RULES_BYTES) == load_rules()
    assert parse_rules(toml_bytes(default_doc())) == load_rules()


def test_rules_are_immutable() -> None:
    rules = load_rules()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rules.hot_min = 1  # type: ignore[misc]


def test_signal_names_cannot_be_read_as_verified_business_affiliation() -> None:
    assert "non_free_email_domain" in SIGNALS
    assert not any("business" in name for name in SIGNALS)


# --- loading from a path -------------------------------------------------------------------------


def test_override_file_is_loaded(tmp_path: Path) -> None:
    doc = default_doc()
    doc["rules_version"] = "custom-1"
    path = tmp_path / "rules.toml"
    path.write_bytes(toml_bytes(doc))
    assert load_rules(path).rules_version == "custom-1"


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_override_path_must_be_an_existing_file(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "missing.toml" if kind == "missing" else tmp_path
    with pytest.raises(ScoringRulesError) as exc_info:
        load_rules(path)
    assert exc_info.value.problems == (
        RuleProblem("file", "Rules file not found or not a regular file"),
    )


def test_oversized_override_file_is_rejected_before_reading(tmp_path: Path) -> None:
    path = tmp_path / "big.toml"
    path.write_bytes(DEFAULT_RULES_BYTES + b"#" * MAX_RULES_FILE_BYTES)
    with pytest.raises(ScoringRulesError) as exc_info:
        load_rules(path)
    assert [p.message for p in exc_info.value.problems] == [
        "Rules file must be at most 65536 bytes"
    ]


# --- file-level problems -------------------------------------------------------------------------


def test_oversized_bytes_are_rejected() -> None:
    assert problems(b"#" * (MAX_RULES_FILE_BYTES + 1)) == [
        ("file", "Rules file must be at most 65536 bytes")
    ]


def test_non_utf8_is_rejected() -> None:
    assert problems(b'rules_version = "caf\xe9"\n') == [("file", "Rules file must be UTF-8")]


def test_invalid_toml_is_rejected() -> None:
    [(path, message)] = problems(b"schema_version = = 1\n")
    assert path == "file"
    assert message.startswith("Not valid TOML: ")


# --- keys ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "expected_path"),
    [
        ("extra", 1, "extra"),
        ("tiers.lukewarm", 50, "tiers.lukewarm"),
        ("points.bonus", 5, "points.bonus"),
        ("points.source.tiktok", 5, "points.source.tiktok"),
        ("points.signals.business_email", 5, "points.signals.business_email"),
        ("free_email_domains.extra", ["a.com"], "free_email_domains.extra"),
    ],
)
def test_unknown_keys_are_rejected(key: str, value: Any, expected_path: str) -> None:
    assert problems(with_change(key, value)) == [(expected_path, "Unknown key")]


@pytest.mark.parametrize(
    "key",
    [
        "schema_version",
        "rules_version",
        "tiers",
        "points",
        "free_email_domains",
        "tiers.hot",
        "tiers.cold",
        "points.source",
        "points.signals",
        "points.source.other",
        "points.signals.has_phone",
        "free_email_domains.domains",
    ],
)
def test_missing_required_keys_are_rejected(key: str) -> None:
    assert (key, "Missing required key") in problems(with_change(key, DELETE))


@pytest.mark.parametrize(
    "key", ["tiers", "points", "free_email_domains", "points.source", "points.signals"]
)
def test_tables_must_be_tables(key: str) -> None:
    assert (key, "Must be a table") in problems(with_change(key, 5))


# --- versions ------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [2, 0, "1", True, 1.0])
def test_schema_version_must_be_integer_1(value: Any) -> None:
    assert problems(with_change("schema_version", value)) == [
        ("schema_version", "Must be the integer 1")
    ]


@pytest.mark.parametrize("value", ["", "has space", "x" * 65, "v1/2", 5, True])
def test_rules_version_format(value: Any) -> None:
    assert paths(with_change("rules_version", value)) == ["rules_version"]


def test_rules_version_allowed_characters() -> None:
    assert parse_rules(with_change("rules_version", "v1.2_beta-3")).rules_version == "v1.2_beta-3"


# --- points --------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["points.source.referral", "points.signals.has_email"])
@pytest.mark.parametrize(
    ("value", "message"),
    [
        (True, WHOLE_NUMBER),
        (False, WHOLE_NUMBER),
        (1.5, WHOLE_NUMBER),
        (10.0, WHOLE_NUMBER),
        ("10", WHOLE_NUMBER),
        (-1, "Must be between 0 and 100"),
        (101, "Must be between 0 and 100"),
    ],
)
def test_points_must_be_whole_numbers_0_to_100(key: str, value: Any, message: str) -> None:
    assert (key, message) in problems(with_change(key, value))


def test_maximum_possible_score_must_not_exceed_100() -> None:
    assert problems(with_change("points.signals.has_email", 16)) == [
        ("points", "Highest source score plus all signal points must be at most 100")
    ]


def test_maximum_below_100_is_allowed() -> None:
    rules = parse_rules(with_change("points.source.referral", 20))
    assert rules.max_score == 90


# --- tiers ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"tiers.hot": 40}, ("tiers", "Must satisfy hot > warm > cold")),
        ({"tiers.hot": 30}, ("tiers", "Must satisfy hot > warm > cold")),
        ({"tiers.warm": 0}, ("tiers.warm", "Must be between 1 and 100")),
        (
            {"tiers.cold": 5},
            ("tiers.cold", "Must be the integer 0 (cold is every score below warm)"),
        ),
        (
            {"tiers.cold": False},
            ("tiers.cold", "Must be the integer 0 (cold is every score below warm)"),
        ),
        ({"tiers.hot": True}, ("tiers.hot", WHOLE_NUMBER)),
        ({"tiers.warm": 40.5}, ("tiers.warm", WHOLE_NUMBER)),
    ],
)
def test_tier_rules(changes: dict[str, Any], expected: tuple[str, str]) -> None:
    [(key, value)] = changes.items()
    assert expected in problems(with_change(key, value))


def test_hot_threshold_must_be_reachable() -> None:
    doc = default_doc()
    doc["points"]["signals"]["non_free_email_domain"] = 0  # max score becomes 75
    doc["tiers"]["hot"] = 80
    assert problems(toml_bytes(doc)) == [
        ("tiers.hot", "Must not exceed the maximum possible score")
    ]


# --- free email domains --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "domain",
    ["Gmail.com", "gmail", "-x.com", "x-.com", "", "a..com", "gmail.com.", "x.c", "x.123", 5, True],
)
def test_domains_must_be_lowercase_domain_names(domain: Any) -> None:
    assert problems(with_change("free_email_domains.domains", ["gmail.com", domain])) == [
        ("free_email_domains.domains[1]", "Must be a lowercase domain name such as gmail.com")
    ]


def test_domains_must_be_a_list() -> None:
    assert problems(with_change("free_email_domains.domains", "gmail.com")) == [
        ("free_email_domains.domains", "Must be a list of domain names")
    ]


def test_domains_must_not_repeat() -> None:
    assert problems(with_change("free_email_domains.domains", ["gmail.com", "gmail.com"])) == [
        ("free_email_domains.domains", "Must not contain duplicates")
    ]


def test_domain_list_size_limit() -> None:
    domains = [f"d{i}.example.com" for i in range(501)]
    assert problems(with_change("free_email_domains.domains", domains)) == [
        ("free_email_domains.domains", "Must contain at most 500 domains")
    ]
    assert (
        len(
            parse_rules(with_change("free_email_domains.domains", domains[:500])).free_email_domains
        )
        == 500
    )


def test_empty_domain_list_is_allowed() -> None:
    assert (
        parse_rules(with_change("free_email_domains.domains", [])).free_email_domains == frozenset()
    )


# --- reporting -----------------------------------------------------------------------------------


def test_all_problems_are_reported_together() -> None:
    doc = default_doc()
    doc["schema_version"] = 2
    doc["tiers"]["cold"] = 1
    doc["points"]["source"]["referral"] = True
    doc["free_email_domains"]["domains"] = ["BAD"]
    doc["surprise"] = 1
    error_paths = paths(toml_bytes(doc))
    assert set(error_paths) == {
        "surprise",
        "schema_version",
        "tiers.cold",
        "points.source.referral",
        "free_email_domains.domains[0]",
    }


def test_error_summary_lists_paths_and_never_values() -> None:
    doc = default_doc()
    doc["rules_version"] = "SENTINEL VALUE"
    doc["free_email_domains"]["domains"] = ["SENTINEL-DOMAIN"]
    with pytest.raises(ScoringRulesError) as exc_info:
        parse_rules(toml_bytes(doc))
    text = str(exc_info.value)
    assert text.startswith("Invalid scoring rules (2 problems): rules_version: ")
    assert "SENTINEL" not in text
