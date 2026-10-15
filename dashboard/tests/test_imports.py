from dashboard.api_client import ApiError, ApiErrorKind
from dashboard.imports import COLUMNS, TEMPLATE_CSV, describe_import_error


def coded(code: str, **detail: object) -> ApiError:
    return ApiError(
        ApiErrorKind.HTTP_ERROR,
        "API message",
        status_code=422,
        code=code,
        detail={"code": code, **detail},
    )


def test_template_has_the_api_columns_and_only_example_data() -> None:
    header, *rows = TEMPLATE_CSV.strip().splitlines()
    assert tuple(header.split(",")) == COLUMNS
    assert len(rows) == 2
    assert "example" in TEMPLATE_CSV


def test_rejected_rows_become_a_table_without_lead_values() -> None:
    failure = describe_import_error(
        coded(
            "import.rejected",
            row_errors=[
                {"row": 3, "field": "email", "code": "email.invalid_format", "message": "Bad"},
                {
                    "row": 4,
                    "field": "phone",
                    "code": "row.duplicate_in_file",
                    "message": "Same as earlier",
                    "duplicate_of_row": 2,
                },
                {
                    "row": 5,
                    "field": "email",
                    "code": "lead.duplicate",
                    "message": "Exists",
                    "lead_id": "2f1c0000-aaaa",
                },
                {"row": 6, "code": "row.column_count", "message": "Expected 3 values"},
            ],
            truncated=True,
        )
    )
    assert "Nothing was imported" in failure.summary
    assert [(r.row, r.field, r.reference) for r in failure.rows] == [
        ("3", "Email", ""),
        ("4", "Phone", "same as row 2"),
        ("5", "Email", "existing lead 2f1c0000…"),
        ("6", "—", ""),
    ]
    assert failure.truncated
    assert not failure.outcome_unknown


def test_unreadable_file_names_columns_by_number() -> None:
    failure = describe_import_error(
        coded(
            "import.invalid_file",
            errors=[
                {"code": "header.unknown_columns", "message": "Unknown", "columns": [2, 4]},
                {"code": "file.encoding", "message": "Not UTF-8"},
            ],
        )
    )
    assert [(r.field, r.problem) for r in failure.rows] == [
        ("columns 2, 4", "Unknown"),
        ("file", "Not UTF-8"),
    ]


def test_timeout_means_the_outcome_is_unknown() -> None:
    failure = describe_import_error(ApiError(ApiErrorKind.TIMEOUT, "timed out"))
    assert failure.outcome_unknown
    assert "Check the Leads page" in failure.summary


def test_too_large_and_other_errors() -> None:
    too_large = ApiError(
        ApiErrorKind.HTTP_ERROR,
        "Request body must be at most",
        status_code=413,
        code="request.too_large",
    )
    assert "1 MiB" in describe_import_error(too_large).summary
    unauthorized = ApiError(ApiErrorKind.UNAUTHORIZED, "Authentication failed", status_code=401)
    assert describe_import_error(unauthorized).summary == "Authentication failed"
