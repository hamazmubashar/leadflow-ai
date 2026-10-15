"""Lead API: auth, create/get/list, request bodies, duplicates, 404s and privacy."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from leadflow_ai.api.leads import router as leads_router
from leadflow_ai.auth import require_operator
from leadflow_ai.main import create_app
from tests.conftest import AUTH_HEADERS, TEST_TOKEN

LEADS = "/api/v1/leads"
AYESHA = {
    "full_name": "  Ayesha   Khan ",
    "email": "Ayesha.Khan@Example.COM",
    "phone": "0300-1234567",
    "company": "Acme Traders",
    "source": "Referral",
    "notes": "Prefers WhatsApp",
}
LEAD_FIELDS = {"id", "full_name", "email", "phone", "company", "source", "notes", "created_at"}


def _create(api: TestClient, body: Mapping[str, object]) -> dict[str, object]:
    response = api.post(LEADS, json=body, headers=AUTH_HEADERS)
    assert response.status_code == 201, response.text
    data: dict[str, object] = response.json()
    return data


# --- authentication ---------------------------------------------------------------

ROUTES = [
    ("POST", LEADS),
    ("GET", LEADS),
    ("GET", f"{LEADS}/{uuid.uuid4()}"),
    ("POST", f"{LEADS}/import"),
]
BAD_HEADERS = [
    {},
    {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"},
    {"Authorization": f"Basic {TEST_TOKEN}"},
    {"Authorization": TEST_TOKEN},
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize("headers", BAD_HEADERS)
def test_every_lead_route_requires_the_operator_token(
    api: TestClient, method: str, path: str, headers: dict[str, str]
) -> None:
    response = api.request(method, path, json=AYESHA, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"detail": "Invalid or missing bearer token"}


def test_unauthenticated_requests_never_reach_body_parsing_or_storage(api: TestClient) -> None:
    malformed = api.post(LEADS, content=b"{not json", headers={"content-type": "application/json"})
    assert malformed.status_code == 401  # not 422: auth runs before the body is read
    assert api.post(LEADS, json=AYESHA).status_code == 401
    assert api.get(LEADS, headers=AUTH_HEADERS).json()["items"] == []


def test_all_api_routes_declare_operator_auth(api: TestClient) -> None:
    # Every operation the app serves under /api (per its OpenAPI schema) must be
    # a leads-router route, and every leads-router route must depend on auth.
    served = {
        (method.upper(), path)
        for path, operations in api.get("/openapi.json").json()["paths"].items()
        if path.startswith("/api/")
        for method in operations
    }
    routed = {
        (method, route.path)
        for route in leads_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods or set()
    }
    assert (
        served
        == routed
        == {
            ("POST", LEADS),
            ("GET", LEADS),
            ("GET", f"{LEADS}/{{lead_id}}"),
            ("POST", f"{LEADS}/import"),
        }
    )
    for route in leads_router.routes:
        assert isinstance(route, APIRoute)
        calls = {dependency.call for dependency in route.dependant.dependencies}
        assert require_operator in calls, route.path


# --- create and retrieve ------------------------------------------------------------


def test_create_returns_normalized_lead_with_location(api: TestClient) -> None:
    response = api.post(LEADS, json=AYESHA, headers=AUTH_HEADERS)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == LEAD_FIELDS
    assert uuid.UUID(body["id"]).version == 4
    assert response.headers["Location"] == f"{LEADS}/{body['id']}"
    assert {k: body[k] for k in LEAD_FIELDS - {"id", "created_at"}} == {
        "full_name": "Ayesha Khan",
        "email": "ayesha.khan@example.com",
        "phone": "+923001234567",
        "company": "Acme Traders",
        "source": "referral",
        "notes": "Prefers WhatsApp",
    }
    created_at = datetime.fromisoformat(body["created_at"])
    assert body["created_at"].endswith("Z")
    assert created_at.tzinfo is not None
    assert abs((datetime.now(UTC) - created_at).total_seconds()) < 60


def test_location_resolves_to_the_same_lead(api: TestClient) -> None:
    response = api.post(LEADS, json=AYESHA, headers=AUTH_HEADERS)
    fetched = api.get(response.headers["Location"], headers=AUTH_HEADERS)
    assert fetched.status_code == 200
    assert fetched.json() == response.json()


def test_minimal_lead_defaults(api: TestClient) -> None:
    body = _create(api, {"full_name": "Bilal", "phone": "+44 20 7946 0958"})
    assert (body["email"], body["company"], body["notes"], body["source"]) == (
        None,
        None,
        None,
        "other",
    )
    assert body["phone"] == "+442079460958"


# --- invalid lead data (M1 validation) ------------------------------------------------


def test_invalid_lead_returns_m1_error_codes(api: TestClient) -> None:
    response = api.post(
        LEADS,
        json={"full_name": " ", "email": "bad@", "phone": "021-12345678", "source": "tiktok"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "lead.invalid"
    assert [(e["field"], e["code"]) for e in detail["errors"]] == [
        ("email", "email.invalid_format"),
        ("phone", "phone.national_format_unsupported"),
        ("source", "source.invalid_choice"),
        ("full_name", "field.required"),
    ]


@pytest.mark.parametrize("field", ["id", "created_at", "score", "city"])
def test_unknown_or_server_owned_fields_are_rejected(api: TestClient, field: str) -> None:
    response = api.post(LEADS, json={**AYESHA, field: "x"}, headers=AUTH_HEADERS)
    assert response.status_code == 422
    assert [e["code"] for e in response.json()["detail"]["errors"]] == ["field.unknown"]


def test_non_string_values_are_rejected(api: TestClient) -> None:
    response = api.post(LEADS, json={"full_name": "A", "phone": 3001234567}, headers=AUTH_HEADERS)
    assert response.status_code == 422
    assert [(e["field"], e["code"]) for e in response.json()["detail"]["errors"]] == [
        ("phone", "field.wrong_type")
    ]


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{not json",
        b"[]",
        b'["full_name", "A"]',
        b'"a string"',
        b"42",
        b"null",
        b"\xff\xfe\x00garbage",
        b'{"full_name": "A", "full_name": "B", "email": "a@example.com"}',
        b"[" * 5000 + b"]" * 5000,
    ],
    ids=[
        "empty",
        "malformed",
        "array",
        "array-of-strings",
        "string",
        "number",
        "null",
        "invalid-utf8",
        "duplicate-keys",
        "deep-nesting",
    ],
)
def test_body_must_be_one_json_object(api: TestClient, content: bytes) -> None:
    response = api.post(
        LEADS, content=content, headers={**AUTH_HEADERS, "content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "request.invalid_body",
            "message": "Request body must be a single JSON object",
        }
    }


# --- Content-Type: application/json only (order 413 -> 401 -> 415 -> 422) ------------------

VALID_BODY = b'{"full_name": "A", "email": "a@example.com"}'
UNSUPPORTED_MEDIA_TYPE = {
    "detail": {
        "code": "request.unsupported_media_type",
        "message": "Content-Type must be application/json",
    }
}


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
        "application/jsonp",
        "application/json-patch+json",
        "",
    ],
)
def test_unsupported_content_type_returns_415_without_writing(
    api: TestClient, content_type: str | None
) -> None:
    headers = dict(AUTH_HEADERS)
    if content_type is not None:
        headers["content-type"] = content_type
    response = api.post(LEADS, content=VALID_BODY, headers=headers)
    assert response.status_code == 415
    assert response.json() == UNSUPPORTED_MEDIA_TYPE
    assert api.get(LEADS, headers=AUTH_HEADERS).json()["items"] == []


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "Application/JSON",
        "application/json; charset=utf-8",
        "APPLICATION/JSON ; Charset=UTF-8",
    ],
)
def test_json_content_type_is_case_insensitive_and_allows_parameters(
    api: TestClient, content_type: str
) -> None:
    response = api.post(
        LEADS, content=VALID_BODY, headers={**AUTH_HEADERS, "content-type": content_type}
    )
    assert response.status_code == 201


def test_unauthenticated_unsupported_type_returns_401_before_415(api: TestClient) -> None:
    response = api.post(LEADS, content=VALID_BODY, headers={"content-type": "text/plain"})
    assert response.status_code == 401
    response = api.post(LEADS, content=VALID_BODY)  # no Content-Type at all
    assert response.status_code == 401


def test_unsupported_type_returns_415_before_body_is_parsed(api: TestClient) -> None:
    response = api.post(
        LEADS, content=b"{not json", headers={**AUTH_HEADERS, "content-type": "text/plain"}
    )
    assert response.status_code == 415  # not 422: the body is never read


def test_oversized_body_returns_413_before_auth_and_media_type(api: TestClient) -> None:
    response = api.post(LEADS, content=b"x" * 20_000, headers={"content-type": "text/plain"})
    assert response.status_code == 413


def test_read_routes_do_not_require_a_content_type(api: TestClient) -> None:
    lead = _create(api, {"full_name": "A", "email": "a@example.com"})
    assert api.get(LEADS, headers=AUTH_HEADERS).status_code == 200
    assert api.get(f"{LEADS}/{lead['id']}", headers=AUTH_HEADERS).status_code == 200


# --- no trailing-slash redirects (they would echo path/query in Location) ------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{LEADS}/"),
        ("GET", f"{LEADS}/?email=secret.person@example.com"),
        ("GET", f"{LEADS}/{uuid.uuid4()}/"),
        ("GET", f"{LEADS}/secret.person@example.com/"),
        ("POST", f"{LEADS}/"),
        ("GET", "/health/"),
    ],
)
@pytest.mark.parametrize("authenticated", [True, False])
def test_trailing_slash_is_404_without_location(
    api: TestClient, method: str, path: str, authenticated: bool
) -> None:
    headers = {**AUTH_HEADERS, "content-type": "application/json"} if authenticated else {}
    response = api.request(
        method, path, content=VALID_BODY, headers=headers, follow_redirects=False
    )
    assert response.status_code == 404
    assert "location" not in response.headers
    assert "secret.person" not in response.text


# --- privacy ------------------------------------------------------------------------------


def test_validation_errors_never_echo_submitted_values(api: TestClient) -> None:
    secret_body = {
        "full_name": "Ayesha Khan\u0007",
        "email": "ayesha.khan@private-example",
        "phone": "0300-98765 ext 4",
        "company": "SecretCo" * 20,
        "notes": "CNIC 35202-1234567-1",
        "ayesha.private@example.com": "key that is personal data",
    }
    response = api.post(LEADS, json=secret_body, headers=AUTH_HEADERS)
    assert response.status_code == 422
    for secret in ("Ayesha", "ayesha", "Khan", "private", "98765", "SecretCo", "35202"):
        assert secret not in response.text


def test_malformed_body_is_not_echoed(api: TestClient) -> None:
    response = api.post(
        LEADS,
        content=b'{"email": "ayesha@example.com", oops}',
        headers={**AUTH_HEADERS, "content-type": "application/json"},
    )
    assert response.status_code == 422
    assert "ayesha" not in response.text
    assert "oops" not in response.text


# --- duplicates -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("second", "field"),
    [
        ({"full_name": "B", "email": "AYESHA.KHAN@example.com"}, "email"),
        ({"full_name": "B", "phone": "+92 300 1234567"}, "phone"),
        ({"full_name": "B", "phone": "0092 300 123 4567"}, "phone"),
    ],
)
def test_duplicate_identity_returns_409_without_writing(
    api: TestClient, second: dict[str, object], field: str
) -> None:
    first = _create(api, AYESHA)
    response = api.post(LEADS, json=second, headers=AUTH_HEADERS)
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "lead.duplicate",
            "message": "A lead with this email or phone already exists",
            "conflicts": [{"field": field, "lead_id": first["id"]}],
        }
    }
    assert "ayesha" not in response.text.lower()
    assert "3001234567" not in response.text
    assert len(api.get(LEADS, headers=AUTH_HEADERS).json()["items"]) == 1


def test_conflicts_with_two_leads_are_both_reported(api: TestClient) -> None:
    by_email = _create(api, {"full_name": "A", "email": "a@example.com"})
    by_phone = _create(api, {"full_name": "B", "phone": "0300-1234567"})
    response = api.post(
        LEADS,
        json={"full_name": "C", "email": "a@example.com", "phone": "03001234567"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["conflicts"] == [
        {"field": "email", "lead_id": by_email["id"]},
        {"field": "phone", "lead_id": by_phone["id"]},
    ]


def test_duplicate_never_modifies_the_existing_lead(api: TestClient) -> None:
    first = _create(api, AYESHA)
    api.post(LEADS, json={**AYESHA, "full_name": "Changed", "notes": "x"}, headers=AUTH_HEADERS)
    assert api.get(f"{LEADS}/{first['id']}", headers=AUTH_HEADERS).json() == first


# --- 404 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lead_id",
    [str(uuid.uuid4()), "not-a-uuid", "00000000-0000-0000-0000-000000000000", "x" * 300],
)
def test_missing_lead_returns_404(api: TestClient, lead_id: str) -> None:
    _create(api, AYESHA)
    response = api.get(f"{LEADS}/{lead_id}", headers=AUTH_HEADERS)
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "lead.not_found", "message": "Lead not found"}}


def test_non_canonical_id_returns_404(api: TestClient) -> None:
    lead = _create(api, AYESHA)
    response = api.get(f"{LEADS}/{str(lead['id']).upper()}", headers=AUTH_HEADERS)
    assert response.status_code == 404


# --- list and pagination ------------------------------------------------------------------------


def test_list_is_newest_first_with_defaults(api: TestClient) -> None:
    ids = [
        _create(api, {"full_name": f"L{i}", "phone": f"0300-12345{i:02d}"})["id"] for i in range(3)
    ]
    response = api.get(LEADS, headers=AUTH_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert (body["limit"], body["offset"]) == (50, 0)
    assert [item["id"] for item in body["items"]] == list(reversed(ids))
    assert all(set(item) == LEAD_FIELDS for item in body["items"])


def test_pagination_pages_do_not_overlap(api: TestClient) -> None:
    ids = [
        _create(api, {"full_name": f"L{i}", "phone": f"0300-12345{i:02d}"})["id"] for i in range(5)
    ]
    pages = [
        api.get(LEADS, params={"limit": 2, "offset": offset}, headers=AUTH_HEADERS).json()
        for offset in (0, 2, 4, 6)
    ]
    assert [len(page["items"]) for page in pages] == [2, 2, 1, 0]
    assert [item["id"] for page in pages for item in page["items"]] == list(reversed(ids))
    assert [(page["limit"], page["offset"]) for page in pages] == [(2, 0), (2, 2), (2, 4), (2, 6)]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "1"},
        {"limit": "100"},
        {"offset": str(2**63 - 1)},
    ],
)
def test_pagination_bounds_are_accepted(api: TestClient, params: dict[str, str]) -> None:
    assert api.get(LEADS, params=params, headers=AUTH_HEADERS).status_code == 200


@pytest.mark.parametrize(
    ("params", "location"),
    [
        ({"limit": "0"}, ["query", "limit"]),
        ({"limit": "101"}, ["query", "limit"]),
        ({"limit": "-1"}, ["query", "limit"]),
        ({"limit": "abc"}, ["query", "limit"]),
        ({"limit": "1.5"}, ["query", "limit"]),
        ({"offset": "-1"}, ["query", "offset"]),
        ({"offset": str(2**63)}, ["query", "offset"]),
        ({"offset": "secret-value"}, ["query", "offset"]),
    ],
)
def test_invalid_pagination_returns_422_without_echo(
    api: TestClient, params: dict[str, str], location: list[str]
) -> None:
    response = api.get(LEADS, params=params, headers=AUTH_HEADERS)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "request.invalid"
    assert [error["location"] for error in detail["errors"]] == [location]
    assert "input" not in response.text
    assert "secret-value" not in response.text
    assert str(2**63) not in response.text


def test_default_page_limit_is_50(api: TestClient) -> None:
    for i in range(55):
        _create(api, {"full_name": f"L{i}", "phone": f"+92300123{i:04d}"})
    assert len(api.get(LEADS, headers=AUTH_HEADERS).json()["items"]) == 50
    assert len(api.get(LEADS, params={"limit": 100}, headers=AUTH_HEADERS).json()["items"]) == 55


# --- OpenAPI ----------------------------------------------------------------------------------


def test_openapi_is_public_but_lead_data_is_not(api: TestClient) -> None:
    schema = api.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert {"/api/v1/leads", "/api/v1/leads/{lead_id}"} <= set(paths)
    request_schema = paths["/api/v1/leads"]["post"]["requestBody"]["content"]["application/json"]
    assert request_schema["schema"]["additionalProperties"] is False
    assert api.get(LEADS).status_code == 401


def test_app_factory_with_default_settings_has_lead_routes(settings: object) -> None:
    app = create_app(settings)  # type: ignore[arg-type]
    assert {"/health", LEADS, f"{LEADS}/{{lead_id}}"} <= set(app.openapi()["paths"])
    assert app.state.database.engine is None  # building the app never opens the database
