"""CSV import endpoint: auth, media type, size limits, responses, privacy, all-or-nothing."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from leadflow_ai.api.limits import MAX_IMPORT_BODY_BYTES, MAX_REQUEST_BODY_BYTES
from leadflow_ai.config import Settings
from leadflow_ai.imports.csv_reader import UTF8_BOM
from leadflow_ai.main import create_app
from tests.conftest import AUTH_HEADERS

LEADS = "/api/v1/leads"
IMPORT = f"{LEADS}/import"
CSV = {**AUTH_HEADERS, "content-type": "text/csv"}
UNSUPPORTED = {
    "detail": {
        "code": "request.unsupported_media_type",
        "message": "Content-Type must be text/csv (UTF-8)",
    }
}
SECRETS = ("Ayesha", "ayesha", "3001234567", "private", "Acme")


def csv_bytes(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def post(api: TestClient, data: bytes, headers: dict[str, str] = CSV) -> Response:
    return api.post(IMPORT, content=data, headers=headers)


def stored_count(api: TestClient) -> int:
    return len(api.get(LEADS, params={"limit": 100}, headers=AUTH_HEADERS).json()["items"])


VALID = csv_bytes("full_name,email", "A,a@example.com")


# --- authentication and check order (413 -> 401 -> 415 -> 422) -----------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "text/csv"},
        {"content-type": "text/csv", "Authorization": "Bearer wrong-token-wrong-token-wrong"},
        {"content-type": "application/json"},  # 401 comes before 415
    ],
)
def test_import_requires_the_operator_token(api: TestClient, headers: dict[str, str]) -> None:
    response = post(api, VALID, headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing bearer token"}
    assert stored_count(api) == 0


def test_unsupported_type_returns_415_before_the_file_is_read(api: TestClient) -> None:
    response = post(api, b"\x00not csv", {**AUTH_HEADERS, "content-type": "text/plain"})
    assert response.status_code == 415  # not 422


def test_oversized_import_returns_413_before_auth(api: TestClient) -> None:
    response = post(api, b"x" * (MAX_IMPORT_BODY_BYTES + 1), {"content-type": "text/plain"})
    assert response.status_code == 413


def test_import_body_over_16_kib_still_requires_auth(api: TestClient) -> None:
    response = post(api, b"x" * (500 * 1024), {"content-type": "text/csv"})
    assert response.status_code == 401  # under the 1 MiB import limit, so auth decides


# --- media types ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    [
        "text/csv",
        "Text/CSV",
        "text/csv; charset=utf-8",
        'text/csv; Charset="UTF-8"',
        "text/csv;charset=utf-8; header=present",
        "text/csv; header=present; charset=utf-8",  # charset after another parameter
        "text/csv; header=present",  # parameters without a charset
    ],
)
def test_csv_media_type_is_case_insensitive_with_optional_utf8_charset(
    api: TestClient, content_type: str
) -> None:
    response = post(api, VALID, {**AUTH_HEADERS, "content-type": content_type})
    assert response.status_code == 201


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "",
        "application/json",
        "multipart/form-data; boundary=x",
        "text/plain",
        "application/csv",
        "text/csv; charset=latin-1",
        "text/csv; charset=utf-16",
        "text/csv; charset=",
    ],
)
def test_other_media_types_return_415(api: TestClient, content_type: str | None) -> None:
    headers = dict(AUTH_HEADERS)
    if content_type is not None:
        headers["content-type"] = content_type
    response = post(api, VALID, headers)
    assert response.status_code == 415
    assert response.json() == UNSUPPORTED
    assert stored_count(api) == 0


def test_multipart_file_upload_is_rejected(api: TestClient) -> None:
    response = api.post(
        IMPORT, files={"file": ("leads.csv", VALID, "text/csv")}, headers=AUTH_HEADERS
    )
    assert response.status_code == 415
    assert stored_count(api) == 0


def test_json_route_keeps_its_own_415_message(api: TestClient) -> None:
    response = api.post(LEADS, content=b"{}", headers={**AUTH_HEADERS, "content-type": "text/csv"})
    assert response.json()["detail"]["message"] == "Content-Type must be application/json"


# --- size limits: 1 MiB for the import route only ------------------------------------------------


def _exactly(size: int) -> bytes:
    """A valid 1,000-row CSV padded (within M1 field limits) to exactly ``size`` bytes."""
    rows = [[f"L{i:04d}", f"+92300{i:07d}", "", ""] for i in range(1000)]

    def render() -> bytes:
        return csv_bytes("full_name,phone,company,notes", *(",".join(r) for r in rows))

    remaining = size - len(render())
    for row in rows:
        add_notes = min(1000, remaining)
        row[3] = "n" * add_notes
        remaining -= add_notes
        add_company = min(120, remaining)
        row[2] = "c" * add_company
        remaining -= add_company
    data = render()
    assert len(data) == size
    return data


def test_import_at_the_size_and_row_limits_is_accepted(api: TestClient) -> None:
    response = post(api, _exactly(MAX_IMPORT_BODY_BYTES))
    assert response.status_code == 201
    assert response.json()["imported"] == 1000


def test_import_over_1_mib_returns_413(api: TestClient) -> None:
    response = post(api, b"x" * (MAX_IMPORT_BODY_BYTES + 1))
    assert response.status_code == 413
    assert response.json() == {
        "detail": {
            "code": "request.too_large",
            "message": "Request body must be at most 1048576 bytes",
        }
    }


def test_chunked_import_over_1_mib_returns_413(api: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(MAX_IMPORT_BODY_BYTES // 65536 + 1):
            yield b"x" * 65536

    assert post(api, chunks()).status_code == 413  # type: ignore[arg-type]


def test_import_between_16_kib_and_1_mib_is_accepted(api: TestClient) -> None:
    rows = [f"L{i},+92300{i:07d},{'n' * 500}" for i in range(200)]
    data = csv_bytes("full_name,phone,notes", *rows)
    assert MAX_REQUEST_BODY_BYTES < len(data) < MAX_IMPORT_BODY_BYTES
    assert post(api, data).json()["imported"] == 200


@pytest.mark.parametrize("path", [LEADS, f"{IMPORT}/", "/health"])
def test_other_routes_keep_the_16_kib_limit(api: TestClient, path: str) -> None:
    response = api.post(path, content=b"x" * (MAX_REQUEST_BODY_BYTES + 1), headers=CSV)
    assert response.status_code == 413
    assert response.json()["detail"]["message"] == "Request body must be at most 16384 bytes"


# --- success and fetching imported leads ---------------------------------------------------------


def test_import_returns_ids_in_file_order_and_leads_are_fetchable(api: TestClient) -> None:
    response = post(
        api,
        csv_bytes(
            "Full_Name,Email,Phone,Source",
            "  Ayesha   Khan ,Ayesha@Example.COM,0300-1234567,Referral",
            "Bilal,,+44 20 7946 0958,",
            "Chen,chen@example.com,,event",
        ),
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"imported", "lead_ids"}
    assert body["imported"] == 3
    fetched = [
        api.get(f"{LEADS}/{lead_id}", headers=AUTH_HEADERS).json() for lead_id in body["lead_ids"]
    ]
    assert [(f["full_name"], f["email"], f["phone"], f["source"]) for f in fetched] == [
        ("Ayesha Khan", "ayesha@example.com", "+923001234567", "referral"),
        ("Bilal", None, "+442079460958", "other"),
        ("Chen", "chen@example.com", None, "event"),
    ]
    assert stored_count(api) == 3


def test_utf8_bom_from_excel_is_accepted(api: TestClient) -> None:
    assert post(api, UTF8_BOM + VALID).json()["imported"] == 1


# --- import.rejected: nothing is written ---------------------------------------------------------


def test_rejected_import_reports_row_errors_and_writes_nothing(api: TestClient) -> None:
    existing = api.post(
        LEADS, json={"full_name": "Old", "email": "taken@example.com"}, headers=AUTH_HEADERS
    ).json()
    response = post(
        api,
        csv_bytes(
            "full_name,email,phone",
            "Good,good@example.com,",
            "Bad,not-an-email,",
            "Dup,good@example.com,",
            "Stored,taken@example.com,",
            "Wide,w@example.com,,extra",
        ),
    )
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "import.rejected",
            "message": "No leads were imported",
            "row_errors": [
                {
                    "row": 3,
                    "field": "email",
                    "code": "email.invalid_format",
                    "message": "Not a valid email address",
                },
                {
                    "row": 4,
                    "field": "email",
                    "code": "row.duplicate_in_file",
                    "message": "Same email or phone as an earlier row in this file",
                    "duplicate_of_row": 2,
                },
                {
                    "row": 5,
                    "field": "email",
                    "code": "lead.duplicate",
                    "message": "A lead with this email or phone already exists",
                    "lead_id": existing["id"],
                },
                {"row": 6, "code": "row.column_count", "message": "Expected 3 values, found 4"},
            ],
            "truncated": False,
        }
    }
    assert stored_count(api) == 1  # only the lead created before the import


def test_row_errors_are_capped_at_100_with_truncated(api: TestClient) -> None:
    response = post(api, csv_bytes("full_name,email", *(f"L{i},bad@" for i in range(150))))
    detail = response.json()["detail"]
    assert (len(detail["row_errors"]), detail["truncated"]) == (100, True)
    assert detail["row_errors"][0]["row"] == 2 and detail["row_errors"][-1]["row"] == 101
    assert stored_count(api) == 0


# --- import.invalid_file -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", [{"code": "file.empty", "message": "The file is empty"}]),
        (
            b"full_name,email\r\nJos\xe9,a@example.com\r\n",
            [
                {
                    "code": "file.encoding",
                    "message": "The file must be UTF-8 (in Excel: Save As 'CSV UTF-8')",
                }
            ],
        ),
        (
            b'full_name,email\r\n"A"B,a@example.com\r\n',
            [{"code": "file.malformed", "message": "The file is not valid CSV (check quoting)"}],
        ),
        (
            csv_bytes("full_name,City,email,Score", "A,x,a@example.com,1"),
            [
                {
                    "code": "header.unknown_columns",
                    "message": "Unknown column names; allowed columns are: "
                    "full_name, email, phone, company, source, notes",
                    "columns": [2, 4],
                }
            ],
        ),
        (
            csv_bytes("full_name,email,EMAIL", "A,a,b"),
            [
                {
                    "code": "header.duplicate_columns",
                    "message": "Each column may appear only once",
                    "columns": [3],
                }
            ],
        ),
        (
            csv_bytes("company,notes", "A,b"),
            [
                {"code": "header.missing_required", "message": "The header must include full_name"},
                {
                    "code": "header.missing_required",
                    "message": "The header must include email or phone",
                },
            ],
        ),
        (
            csv_bytes("full_name,phone", *(f"L{i},+92300{i:07d}" for i in range(1001))),
            [
                {
                    "code": "file.too_many_rows",
                    "message": "The file has more than 1000 lead rows; split it into smaller files",
                }
            ],
        ),
    ],
    ids=[
        "empty",
        "encoding",
        "malformed",
        "unknown-columns",
        "duplicate-columns",
        "missing-required",
        "too-many-rows",
    ],
)
def test_invalid_files_return_import_invalid_file(
    api: TestClient, data: bytes, expected: list[dict[str, object]]
) -> None:
    response = post(api, data)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "import.invalid_file", "errors": expected}}
    assert stored_count(api) == 0


# --- privacy -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        csv_bytes(
            "Ayesha Khan,ayesha.private@example.com,0300-1234567", "B,b@example.com,0311-1111111"
        ),
        csv_bytes("full_name,email,company", "Ayesha Khan,ayesha.private@example,Acme"),
        csv_bytes(
            "full_name,email",
            "Ayesha Khan,ayesha.private@example.com",
            "Ayesha Two,ayesha.private@example.com",
        ),
        csv_bytes(
            "full_name,email", '"Ayesha Khan,ayesha.private@example.com'
        ),  # unterminated quote
    ],
    ids=["forgotten-header", "invalid-values", "in-file-duplicate", "malformed"],
)
def test_import_errors_never_echo_cells_or_headers(api: TestClient, data: bytes) -> None:
    response = post(api, data)
    assert response.status_code == 422
    for secret in SECRETS:
        assert secret not in response.text


# --- database state ------------------------------------------------------------------------------


def test_unmigrated_database_returns_503(api_settings: Settings) -> None:
    with TestClient(create_app(api_settings)) as client:
        response = client.post(IMPORT, content=VALID, headers=CSV)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database.not_migrated"


def test_get_on_import_path_is_a_missing_lead(api: TestClient) -> None:
    response = api.get(IMPORT, headers=AUTH_HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "lead.not_found"
