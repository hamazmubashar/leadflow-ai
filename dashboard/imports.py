"""CSV import helpers (no Streamlit): the template file and readable error tables.

The API validates every byte of the file; nothing here checks or parses it. Import is
all or nothing: a file is either fully imported or fully rejected with row errors.
"""

from dataclasses import dataclass

from dashboard.api_client import ApiError, ApiErrorKind
from dashboard.leads import FIELD_LABELS

MAX_FILE_BYTES = 1024 * 1024  # shown to the operator; the API enforces it
MAX_ROWS = 1000
COLUMNS = ("full_name", "email", "phone", "company", "source", "notes")
TEMPLATE_CSV = (
    "full_name,email,phone,company,source,notes\n"
    "Example Person,example.person@example.com,,Example Co,referral,Met at expo\n"
    "Second Example,,+44 20 7946 0958,,event,\n"
)


@dataclass(frozen=True)
class RowProblem:
    row: str
    field: str
    problem: str
    reference: str


@dataclass(frozen=True)
class ImportFailure:
    """Why an import failed, ready to display. ``outcome_unknown`` means: check before retrying."""

    summary: str
    rows: tuple[RowProblem, ...] = ()
    truncated: bool = False
    outcome_unknown: bool = False


def describe_import_error(error: ApiError) -> ImportFailure:
    detail = error.detail or {}
    if error.kind is ApiErrorKind.TIMEOUT:
        return ImportFailure(
            "The API did not answer in time, so it is unknown whether the file was imported. "
            "Check the Leads page before uploading it again (a repeat upload of an imported "
            "file is rejected as duplicates, so nothing can be imported twice).",
            outcome_unknown=True,
        )
    if error.code == "import.rejected":
        row_problems: list[RowProblem] = []
        for item in detail.get("row_errors") or []:
            if not isinstance(item, dict):
                continue
            reference = ""
            if item.get("duplicate_of_row") is not None:
                reference = f"same as row {item['duplicate_of_row']}"
            elif item.get("lead_id"):
                reference = f"existing lead {str(item['lead_id'])[:8]}…"
            field = item.get("field")
            row_problems.append(
                RowProblem(
                    row=str(item.get("row", "")),
                    field=FIELD_LABELS.get(str(field), str(field)) if field else "—",
                    problem=str(item.get("message", item.get("code", ""))),
                    reference=reference,
                )
            )
        return ImportFailure(
            "Nothing was imported. Fix these rows and upload the whole file again.",
            rows=tuple(row_problems),
            truncated=bool(detail.get("truncated")),
        )
    if error.code == "import.invalid_file":
        file_problems: list[RowProblem] = []
        for item in detail.get("errors") or []:
            if not isinstance(item, dict):
                continue
            columns = item.get("columns")
            where = (
                "columns " + ", ".join(str(c) for c in columns)
                if isinstance(columns, list) and columns
                else "file"
            )
            file_problems.append(RowProblem("—", where, str(item.get("message", "")), ""))
        return ImportFailure(
            "The file could not be read. Nothing was imported.", rows=tuple(file_problems)
        )
    if error.code == "request.too_large":
        return ImportFailure("The file is larger than 1 MiB. Split it and import each part.")
    return ImportFailure(error.message)
