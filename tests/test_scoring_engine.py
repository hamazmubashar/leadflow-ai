"""Scoring engine: worked examples, factors, tiers, determinism, excluded fields, purity."""

import ast
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from leadflow_ai import scoring
from leadflow_ai.db.models import LeadRecord
from leadflow_ai.leads import LeadSource, validate_lead
from leadflow_ai.scoring.engine import Factor, ScoreResult, score_lead
from leadflow_ai.scoring.rules import SIGNALS, ScoringRules, load_rules

RULES = load_rules()
FIXED_RULE_NAMES = {f"source.{s.value}" for s in LeadSource} | set(SIGNALS)


def lead(**fields: str) -> object:
    return validate_lead({"full_name": "Test Lead", **fields})


@dataclass(frozen=True)
class Plain:
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    source: str = "other"


# --- worked examples from the approved design ---------------------------------------------


def test_best_case_lead_scores_100_hot() -> None:
    result = score_lead(
        lead(email="a@acme.pk", phone="0300-1234567", company="Acme", source="referral"),  # type: ignore[arg-type]
        RULES,
    )
    assert result == ScoreResult(
        score=100,
        max_score=100,
        tier="hot",
        rules_version="2026-10-15.1",
        factors=(
            Factor("source.referral", True, 30),
            Factor("has_email", True, 15),
            Factor("has_phone", True, 15),
            Factor("has_company", True, 15),
            Factor("non_free_email_domain", True, 25),
        ),
    )


def test_web_form_free_email_with_phone_scores_45_warm() -> None:
    result = score_lead(lead(email="b@gmail.com", phone="0300-1234567", source="web_form"), RULES)  # type: ignore[arg-type]
    assert (result.score, result.tier) == (45, "warm")
    assert result.factors == (
        Factor("source.web_form", True, 15),
        Factor("has_email", True, 15),
        Factor("has_phone", True, 15),
        Factor("has_company", False, 0),
        Factor("non_free_email_domain", False, 0),
    )


def test_imported_free_email_only_scores_20_cold() -> None:
    result = score_lead(lead(email="c@gmail.com", source="import"), RULES)  # type: ignore[arg-type]
    assert (result.score, result.tier) == (20, "cold")


# --- individual factors ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "points"),
    [("referral", 30), ("event", 20), ("web_form", 15), ("import", 5), ("other", 0)],
)
def test_every_source_contributes_its_points(source: str, points: int) -> None:
    result = score_lead(Plain(phone="+923001234567", source=source), RULES)
    assert result.factors[0] == Factor(f"source.{source}", True, points)
    assert result.score == points + 15


def test_phone_only_lead() -> None:
    result = score_lead(Plain(phone="+923001234567"), RULES)
    assert [(f.rule, f.matched) for f in result.factors[1:]] == [
        ("has_email", False),
        ("has_phone", True),
        ("has_company", False),
        ("non_free_email_domain", False),
    ]


@pytest.mark.parametrize(
    ("email", "non_free"),
    [
        ("a@gmail.com", False),
        ("a@outlook.com", False),
        ("a@acme.pk", True),
        ("a@mail.gmail.com", True),  # exact domain match only: subdomains are not on the list
        ("a.b+tag@example.co.uk", True),
        (None, False),
        ("", False),
        ("not-an-email", False),  # defensive: stored emails always contain "@"
    ],
)
def test_non_free_email_domain_signal(email: str | None, non_free: bool) -> None:
    result = score_lead(Plain(email=email), RULES)
    assert result.factors[4] == Factor("non_free_email_domain", non_free, 25 if non_free else 0)


def test_free_domain_list_comes_from_the_rules() -> None:
    custom = replace(RULES, free_email_domains=frozenset({"acme.pk"}))
    assert score_lead(Plain(email="a@acme.pk"), custom).factors[4].matched is False
    assert score_lead(Plain(email="a@gmail.com"), custom).factors[4].matched is True


def test_empty_strings_do_not_count_as_present() -> None:
    result = score_lead(Plain(email="", phone="", company=""), RULES)
    assert result.score == 0
    assert all(not f.matched for f in result.factors[1:])


# --- tiers ---------------------------------------------------------------------------------------


def _rules_scoring(points: int) -> ScoringRules:
    """Rules where a lead with no signals scores exactly ``points`` from its source."""
    return replace(
        RULES,
        source_points=tuple(
            (name, points if name == "other" else 0) for name, _ in RULES.source_points
        ),
        signal_points=tuple((name, 0) for name, _ in RULES.signal_points),
    )


@pytest.mark.parametrize(
    ("points", "tier"),
    [(0, "cold"), (39, "cold"), (40, "warm"), (69, "warm"), (70, "hot"), (100, "hot")],
)
def test_tier_boundaries(points: int, tier: str) -> None:
    result = score_lead(Plain(), _rules_scoring(points))
    assert (result.score, result.tier) == (points, tier)


# --- determinism and excluded fields -------------------------------------------------------------


def test_scoring_is_deterministic() -> None:
    subject = lead(email="a@acme.pk", phone="0300-1234567", company="Acme", source="event")
    results = {score_lead(subject, RULES) for _ in range(5)}  # type: ignore[arg-type]
    assert len(results) == 1


def test_names_notes_and_phone_country_do_not_affect_the_score() -> None:
    base = {"email": "a@acme.pk", "company": "Acme", "source": "event"}
    variants = [
        validate_lead({**base, "full_name": "Ayesha Khan", "phone": "0300-1234567", "notes": "x"}),
        validate_lead(
            {**base, "full_name": "John Smith", "phone": "+44 20 7946 0958", "notes": "VIP!!"}
        ),
        validate_lead({**base, "full_name": "Wang Fang", "phone": "+1 415 555 0123"}),
    ]
    assert len({score_lead(v, RULES) for v in variants}) == 1


def test_stored_records_and_validated_leads_score_identically() -> None:
    fields = {
        "email": "a@acme.pk",
        "phone": "+923001234567",
        "company": "Acme",
        "source": "referral",
    }
    record = LeadRecord(full_name="A", **fields)
    validated = validate_lead({"full_name": "A", **fields})
    assert score_lead(record, RULES) == score_lead(validated, RULES)


def test_unknown_source_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="not covered by the scoring rules"):
        score_lead(Plain(source="tiktok"), RULES)


def test_factors_only_ever_name_fixed_rules() -> None:
    subject = Plain(
        email="ayesha.private@acme.pk", phone="+923001234567", company="Secret Co", source="event"
    )
    result = score_lead(subject, RULES)
    assert {f.rule for f in result.factors} <= FIXED_RULE_NAMES
    rendered = repr(result)
    for value in ("ayesha", "acme.pk", "3001234567", "Secret"):
        assert value not in rendered


# --- purity --------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", ["engine.py", "rules.py"])
def test_scoring_modules_use_only_allowed_imports(module: str) -> None:
    allowed = {
        "dataclasses",
        "typing",
        "re",
        "tomllib",
        "importlib",
        "pathlib",
        "leadflow_ai",
    }
    path = Path(scoring.__file__).parent / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
        for name in names:
            assert name.split(".")[0] in allowed, f"{module} imports {name}"
            if name.startswith("leadflow_ai"):
                assert name.startswith(("leadflow_ai.scoring", "leadflow_ai.leads")), name
    # No clock, randomness, environment or network in the engine.
    source = path.read_text(encoding="utf-8")
    if module == "engine.py":
        for forbidden in ("datetime", "time", "random", "os", "socket", "open("):
            assert forbidden not in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}, (
                forbidden
            )
        assert "open(" not in source
