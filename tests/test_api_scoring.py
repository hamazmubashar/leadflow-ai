"""Score endpoint, startup rule loading, privacy and M0-M3 contract regression."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from leadflow_ai.config import Settings, get_settings
from leadflow_ai.main import create_app
from leadflow_ai.scoring.rules import ScoringRulesError
from tests.conftest import AUTH_HEADERS, TEST_TOKEN
from tests.scoring_helpers import default_doc, toml_bytes

LEADS = "/api/v1/leads"
LEAD_FIELDS = {"id", "full_name", "email", "phone", "company", "source", "notes", "created_at"}


def create(api: TestClient, **fields: str) -> str:
    response = api.post(LEADS, json={"full_name": "Ayesha Khan", **fields}, headers=AUTH_HEADERS)
    assert response.status_code == 201, response.text
    lead_id: str = response.json()["id"]
    return lead_id


# --- the endpoint ----------------------------------------------------------------------------


def test_score_response_shape(api: TestClient) -> None:
    lead_id = create(
        api, email="a@acme.pk", phone="0300-1234567", company="Acme", source="referral"
    )
    response = api.get(f"{LEADS}/{lead_id}/score", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "lead_id": lead_id,
        "score": 100,
        "max_score": 100,
        "tier": "hot",
        "rules_version": "2026-10-15.1",
        "factors": [
            {"rule": "source.referral", "matched": True, "points": 30},
            {"rule": "has_email", "matched": True, "points": 15},
            {"rule": "has_phone", "matched": True, "points": 15},
            {"rule": "has_company", "matched": True, "points": 15},
            {"rule": "non_free_email_domain", "matched": True, "points": 25},
        ],
    }


@pytest.mark.parametrize(
    ("fields", "score", "tier"),
    [
        ({"email": "b@gmail.com", "phone": "0300-1234567", "source": "web_form"}, 45, "warm"),
        ({"email": "c@gmail.com", "source": "import"}, 20, "cold"),
        ({"phone": "+44 20 7946 0958"}, 15, "cold"),
    ],
)
def test_scores_are_calculated_from_stored_fields(
    api: TestClient, fields: dict[str, str], score: int, tier: str
) -> None:
    body = api.get(f"{LEADS}/{create(api, **fields)}/score", headers=AUTH_HEADERS).json()
    assert (body["score"], body["tier"]) == (score, tier)


@pytest.mark.parametrize(
    "lead_id", [str(uuid.uuid4()), "not-a-uuid", "00000000-0000-0000-0000-000000000000"]
)
def test_missing_lead_returns_404(api: TestClient, lead_id: str) -> None:
    response = api.get(f"{LEADS}/{lead_id}/score", headers=AUTH_HEADERS)
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "lead.not_found", "message": "Lead not found"}}


def test_uppercase_id_returns_404(api: TestClient) -> None:
    lead_id = create(api, email="a@acme.pk")
    assert api.get(f"{LEADS}/{lead_id.upper()}/score", headers=AUTH_HEADERS).status_code == 404


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"}]
)
def test_score_requires_the_operator_token(api: TestClient, headers: dict[str, str]) -> None:
    lead_id = create(api, email="a@acme.pk")
    response = api.get(f"{LEADS}/{lead_id}/score", headers=headers)
    assert response.status_code == 401
    assert "score" not in response.text


def test_score_response_contains_no_personal_data(api: TestClient) -> None:
    lead_id = create(
        api,
        email="ayesha.private@acme-secret.pk",
        phone="0300-1234567",
        company="SecretCo",
        notes="VIP",
    )
    text = api.get(f"{LEADS}/{lead_id}/score", headers=AUTH_HEADERS).text
    for value in ("Ayesha", "ayesha", "acme-secret", "3001234567", "SecretCo", "VIP"):
        assert value not in text


def test_score_reflects_current_data_without_storing_anything(api: TestClient) -> None:
    lead_id = create(api, email="a@gmail.com")
    first = api.get(f"{LEADS}/{lead_id}/score", headers=AUTH_HEADERS).json()
    second = api.get(f"{LEADS}/{lead_id}/score", headers=AUTH_HEADERS).json()
    assert first == second
    assert set(api.get(f"{LEADS}/{lead_id}", headers=AUTH_HEADERS).json()) == LEAD_FIELDS


# --- regression: existing contracts carry no score ----------------------------------------------


def test_existing_responses_are_unchanged(api: TestClient) -> None:
    created = api.post(LEADS, json={"full_name": "A", "email": "a@acme.pk"}, headers=AUTH_HEADERS)
    assert set(created.json()) == LEAD_FIELDS
    assert set(api.get(LEADS, headers=AUTH_HEADERS).json()) == {"items", "limit", "offset"}
    assert set(api.get(LEADS, headers=AUTH_HEADERS).json()["items"][0]) == LEAD_FIELDS
    imported = api.post(
        f"{LEADS}/import",
        content=b"full_name,email\r\nB,b@acme.pk\r\n",
        headers={**AUTH_HEADERS, "content-type": "text/csv"},
    )
    assert set(imported.json()) == {"imported", "lead_ids"}


# --- startup: rules are loaded and validated once ----------------------------------------------


def _settings(database_url: str, rules_path: Path | None) -> Settings:
    return Settings(
        _env_file=None,
        operator_token=SecretStr(TEST_TOKEN),
        database_url=database_url,
        scoring_rules_path=rules_path,
    )


def test_default_rules_are_used_when_no_override_is_configured(api_settings: Settings) -> None:
    assert api_settings.scoring_rules_path is None
    app = create_app(api_settings)
    assert app.state.scoring_rules.rules_version == "2026-10-15.1"
    assert app.state.database.engine is None  # loading rules never touches the database


def test_valid_override_changes_scores_and_version(
    alembic_config: object, database_url: str, tmp_path: Path
) -> None:
    from alembic import command

    command.upgrade(alembic_config, "head")  # type: ignore[arg-type]
    doc = default_doc()
    doc["rules_version"] = "custom-2"
    doc["points"]["source"]["other"] = 40
    doc["points"]["source"]["referral"] = 10
    doc["points"]["signals"]["non_free_email_domain"] = 10  # keeps the maximum at 95 (<= 100)
    rules_path = tmp_path / "custom.toml"
    rules_path.write_bytes(toml_bytes(doc))
    with TestClient(create_app(_settings(database_url, rules_path))) as client:
        lead_id = client.post(
            LEADS, json={"full_name": "A", "email": "a@gmail.com"}, headers=AUTH_HEADERS
        ).json()["id"]
        body = client.get(f"{LEADS}/{lead_id}/score", headers=AUTH_HEADERS).json()
    assert (body["rules_version"], body["score"], body["tier"]) == ("custom-2", 55, "warm")


def test_invalid_override_stops_startup_without_fallback(database_url: str, tmp_path: Path) -> None:
    doc = default_doc()
    doc["points"]["signals"]["has_email"] = True
    rules_path = tmp_path / "bad.toml"
    rules_path.write_bytes(toml_bytes(doc))
    with pytest.raises(ScoringRulesError) as exc_info:
        create_app(_settings(database_url, rules_path))
    assert [p.path for p in exc_info.value.problems] == ["points.signals.has_email"]


def test_missing_override_file_stops_startup(database_url: str, tmp_path: Path) -> None:
    with pytest.raises(ScoringRulesError, match="Rules file not found"):
        create_app(_settings(database_url, tmp_path / "does-not-exist.toml"))


def test_override_path_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, database_url: str, tmp_path: Path
) -> None:
    rules_path = tmp_path / "env.toml"
    doc = default_doc()
    doc["rules_version"] = "from-env"
    rules_path.write_bytes(toml_bytes(doc))
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("LEADFLOW_SCORING_RULES_PATH", str(rules_path))
    get_settings.cache_clear()
    assert create_app().state.scoring_rules.rules_version == "from-env"


# --- LEADFLOW_SCORING_RULES_PATH: unset/empty means defaults; anything else must be valid --------


@pytest.fixture
def env_app(monkeypatch: pytest.MonkeyPatch, database_url: str) -> pytest.MonkeyPatch:
    """Environment for create_app() to read settings exactly as in production."""
    monkeypatch.setenv("LEADFLOW_OPERATOR_TOKEN", TEST_TOKEN)
    get_settings.cache_clear()
    return monkeypatch


@pytest.mark.parametrize(
    "value", [None, "", " ", "   \t  "], ids=["unset", "empty", "space", "whitespace"]
)
def test_unset_or_blank_rules_path_uses_packaged_defaults(
    env_app: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        env_app.delenv("LEADFLOW_SCORING_RULES_PATH", raising=False)
    else:
        env_app.setenv("LEADFLOW_SCORING_RULES_PATH", value)
    assert get_settings().scoring_rules_path is None
    assert create_app().state.scoring_rules.rules_version == "2026-10-15.1"


def test_blank_rules_path_in_env_file_uses_packaged_defaults(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LEADFLOW_OPERATOR_TOKEN={TEST_TOKEN}\nLEADFLOW_SCORING_RULES_PATH=\n", encoding="utf-8"
    )
    settings = Settings(_env_file=env_file)
    assert settings.scoring_rules_path is None
    assert create_app(settings).state.scoring_rules.rules_version == "2026-10-15.1"


def test_valid_rules_path_from_env_is_used(env_app: pytest.MonkeyPatch, tmp_path: Path) -> None:
    doc = default_doc()
    doc["rules_version"] = "env-override"
    rules_path = tmp_path / "rules.toml"
    rules_path.write_bytes(toml_bytes(doc))
    env_app.setenv("LEADFLOW_SCORING_RULES_PATH", str(rules_path))
    assert create_app().state.scoring_rules.rules_version == "env-override"


@pytest.mark.parametrize(
    ("kind", "expected_message"),
    [
        ("missing", "Rules file not found or not a regular file"),
        ("directory", "Rules file not found or not a regular file"),
        ("dot", "Rules file not found or not a regular file"),
        ("padded", "Rules file not found or not a regular file"),
        ("invalid-toml", "Not valid TOML"),
        ("invalid-rules", "Must be a whole number (not a boolean, float or string)"),
    ],
)
def test_non_empty_invalid_rules_path_stops_startup(
    env_app: pytest.MonkeyPatch, tmp_path: Path, kind: str, expected_message: str
) -> None:
    valid = tmp_path / "valid.toml"
    valid.write_bytes(toml_bytes(default_doc()))
    bad_rules = default_doc()
    bad_rules["points"]["signals"]["has_email"] = True
    targets = {
        "missing": str(tmp_path / "missing.toml"),
        "directory": str(tmp_path),
        "dot": ".",  # non-empty, so it is an explicit (invalid) override, not "unset"
        "padded": f"  {valid}  ",  # surrounding spaces are kept, never trimmed into a fallback
        "invalid-toml": str(tmp_path / "broken.toml"),
        "invalid-rules": str(tmp_path / "bad.toml"),
    }
    (tmp_path / "broken.toml").write_bytes(b"schema_version = = 1\n")
    (tmp_path / "bad.toml").write_bytes(toml_bytes(bad_rules))
    env_app.setenv("LEADFLOW_SCORING_RULES_PATH", targets[kind])
    with pytest.raises(ScoringRulesError) as exc_info:
        create_app()
    assert expected_message in str(exc_info.value)


def test_unmigrated_database_returns_503(api_settings: Settings) -> None:
    with TestClient(create_app(api_settings)) as client:
        response = client.get(f"{LEADS}/{uuid.uuid4()}/score", headers=AUTH_HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database.not_migrated"
