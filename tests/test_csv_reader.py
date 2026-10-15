"""CSV reader: encoding, structure, header rules, row numbering, limits and privacy."""

import ast
from pathlib import Path

import pytest

from leadflow_ai.imports import csv_reader
from leadflow_ai.imports.csv_reader import (
    MAX_DATA_ROWS,
    UTF8_BOM,
    CsvFileError,
    CsvRow,
    FileError,
    RowProblem,
    read_leads_csv,
)

SECRET = "Ayesha Khan"
SECRET_EMAIL = "ayesha.private@example.com"


def csv_bytes(*lines: str, newline: str = "\r\n") -> bytes:
    return (newline.join(lines) + newline).encode("utf-8")


def file_errors(data: bytes, **kwargs: int) -> list[FileError]:
    with pytest.raises(CsvFileError) as exc_info:
        read_leads_csv(data, **kwargs)
    return list(exc_info.value.errors)


def codes(data: bytes, **kwargs: int) -> list[str]:
    return [error.code for error in file_errors(data, **kwargs)]


def assert_no_secrets(data: bytes, *secrets: str) -> None:
    with pytest.raises(CsvFileError) as exc_info:
        read_leads_csv(data)
    rendered = f"{exc_info.value} {exc_info.value!r} {exc_info.value.errors}"
    for secret in secrets:
        assert secret not in rendered


# --- valid files ------------------------------------------------------------------


def test_reads_rows_with_spreadsheet_row_numbers() -> None:
    parsed = read_leads_csv(
        csv_bytes(
            "full_name,email,phone",
            f"{SECRET},{SECRET_EMAIL},0300-1234567",
            "Bilal,,+44 20 7946 0958",
        )
    )
    assert parsed.columns == ("full_name", "email", "phone")
    assert parsed.rows == (
        CsvRow(2, {"full_name": SECRET, "email": SECRET_EMAIL, "phone": "0300-1234567"}),
        CsvRow(3, {"full_name": "Bilal", "email": "", "phone": "+44 20 7946 0958"}),
    )
    assert parsed.problems == ()


def test_header_is_case_insensitive_trimmed_and_unordered() -> None:
    parsed = read_leads_csv(csv_bytes(" Phone ,FULL_NAME,Notes", "0300-1234567,A,hi"))
    assert parsed.columns == ("phone", "full_name", "notes")
    assert parsed.rows[0].values == {"phone": "0300-1234567", "full_name": "A", "notes": "hi"}


def test_all_six_columns_are_accepted() -> None:
    parsed = read_leads_csv(
        csv_bytes("full_name,email,phone,company,source,notes", "A,a@example.com,,Acme,event,x")
    )
    assert set(parsed.rows[0].values) == {
        "full_name",
        "email",
        "phone",
        "company",
        "source",
        "notes",
    }


@pytest.mark.parametrize("contact", ["email", "phone"])
def test_one_contact_column_is_enough(contact: str) -> None:
    parsed = read_leads_csv(csv_bytes(f"full_name,{contact}", "A,x"))
    assert parsed.rows[0].values == {"full_name": "A", contact: "x"}


def test_cell_values_are_passed_unchanged_for_m1_to_normalize() -> None:
    parsed = read_leads_csv(csv_bytes("full_name,email", "  A   B ,  A@Example.COM "))
    assert parsed.rows[0].values == {"full_name": "  A   B ", "email": "  A@Example.COM "}


def test_utf8_bom_is_accepted() -> None:
    parsed = read_leads_csv(UTF8_BOM + csv_bytes("full_name,email", "A,a@example.com"))
    assert parsed.columns == ("full_name", "email")


def test_non_ascii_utf8_values_are_decoded() -> None:
    urdu = "".join(chr(cp) for cp in (0x0639, 0x0627, 0x0626, 0x0634, 0x06C1))
    parsed = read_leads_csv(csv_bytes("full_name,phone", f"{urdu},0300-1234567"))
    assert parsed.rows[0].values["full_name"] == urdu


@pytest.mark.parametrize("newline", ["\r\n", "\n", "\r"])
def test_line_ending_styles(newline: str) -> None:
    parsed = read_leads_csv(csv_bytes("full_name,email", "A,a@example.com", newline=newline))
    assert parsed.rows == (CsvRow(2, {"full_name": "A", "email": "a@example.com"}),)


def test_quoted_cells_with_commas_quotes_and_newlines() -> None:
    parsed = read_leads_csv(
        csv_bytes(
            "full_name,email,notes",
            '"Khan, Ayesha",a@example.com,"Said ""call me""',
            'later"',
            "Bilal,b@example.com,plain",
        )
    )
    assert parsed.rows[0] == CsvRow(
        2,
        {"full_name": "Khan, Ayesha", "email": "a@example.com", "notes": 'Said "call me"\r\nlater'},
    )
    assert parsed.rows[1].row == 3  # a multi-line cell is still one spreadsheet row


def test_blank_lines_are_skipped_but_counted_in_row_numbers() -> None:
    parsed = read_leads_csv(
        csv_bytes("", "full_name,email", "", ",", " , ", "A,a@example.com", "", "B,b@example.com")
    )
    assert [row.row for row in parsed.rows] == [6, 8]
    assert parsed.problems == ()


# --- encoding and structure -----------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"full_name,email\r\nJos" + bytes([0xE9]) + b",a@example.com\r\n",  # Windows-1252 export
        "full_name,email\r\nA,a@example.com\r\n".encode("utf-16"),
        b"full_name,email\r\n" + bytes([0xFF, 0xFE, 0x80]) + b"\r\n",
    ],
    ids=["windows-1252", "utf-16", "invalid-bytes"],
)
def test_non_utf8_is_rejected(data: bytes) -> None:
    errors = file_errors(data)
    assert [e.code for e in errors] == ["file.encoding"]
    assert "CSV UTF-8" in errors[0].message


def test_nul_character_is_rejected() -> None:
    assert codes(csv_bytes("full_name,email", "A" + chr(0) + ",a@example.com")) == [
        "file.malformed"
    ]


@pytest.mark.parametrize(
    "line",
    ['"A"B,a@example.com', '"unterminated,a@example.com'],
    ids=["text-after-closing-quote", "unterminated-quote"],
)
def test_malformed_quoting_is_rejected(line: str) -> None:
    assert codes(csv_bytes("full_name,email", line)) == ["file.malformed"]


def test_oversized_cell_is_rejected_as_malformed() -> None:
    huge = "x" * 200_000  # above the csv module's field size limit
    assert codes(csv_bytes("full_name,email", f"{huge},a@example.com")) == ["file.malformed"]


@pytest.mark.parametrize(
    "data",
    [b"", UTF8_BOM, b"   \r\n\r\n  \n", b",,,\r\n , ,\r\n"],
    ids=["no-bytes", "only-bom", "only-whitespace", "only-blank-cells"],
)
def test_empty_files_are_rejected(data: bytes) -> None:
    assert codes(data) == ["file.empty"]


def test_header_without_rows_is_rejected() -> None:
    errors = file_errors(csv_bytes("full_name,email", "", ",,"))
    assert [(e.code, e.message) for e in errors] == [
        ("file.empty", "The file has a header but no lead rows")
    ]


# --- header rules -----------------------------------------------------------------------


def test_unknown_columns_are_reported_by_number() -> None:
    errors = file_errors(csv_bytes("full_name,City,email,score", "A,x,a@example.com,1"))
    assert errors == [
        FileError(
            "header.unknown_columns",
            "Unknown column names; allowed columns are: full_name, email, phone, company, "
            "source, notes",
            (2, 4),
        )
    ]


def test_blank_header_cell_is_an_unknown_column() -> None:
    errors = file_errors(csv_bytes("full_name,email,", "A,a@example.com,"))
    assert [(e.code, e.columns) for e in errors] == [("header.unknown_columns", (3,))]


def test_duplicate_columns_are_reported_by_number_ignoring_case() -> None:
    errors = file_errors(csv_bytes("full_name,Email,phone,EMAIL ,email", "A,a,b,c,d"))
    assert [(e.code, e.columns) for e in errors] == [("header.duplicate_columns", (4, 5))]


@pytest.mark.parametrize(
    ("header", "messages"),
    [
        ("email,phone", ["The header must include full_name"]),
        ("full_name,company", ["The header must include email or phone"]),
        (
            "company,notes",
            ["The header must include full_name", "The header must include email or phone"],
        ),
    ],
)
def test_missing_required_columns(header: str, messages: list[str]) -> None:
    errors = file_errors(csv_bytes(header, "x,y"))
    assert [(e.code, e.message) for e in errors] == [
        ("header.missing_required", message) for message in messages
    ]


def test_all_header_problems_are_reported_together() -> None:
    assert codes(csv_bytes("City,email,EMAIL", "x,a,b")) == [
        "header.unknown_columns",
        "header.duplicate_columns",
        "header.missing_required",
    ]


def test_forgotten_header_row_does_not_echo_the_data() -> None:
    # The first data row is taken as the header: its values must not appear in errors.
    data = csv_bytes(f"{SECRET},{SECRET_EMAIL},0300-1234567", "Bilal,b@example.com,0311-7654321")
    errors = file_errors(data)
    assert errors[0] == FileError(
        "header.unknown_columns",
        "Unknown column names; allowed columns are: full_name, email, phone, company, "
        "source, notes",
        (1, 2, 3),
    )
    assert_no_secrets(data, SECRET, SECRET_EMAIL, "0300-1234567", "Bilal")


# --- row width ----------------------------------------------------------------------


def test_rows_with_wrong_width_are_reported_and_others_kept() -> None:
    parsed = read_leads_csv(
        csv_bytes(
            "full_name,email",
            "A,a@example.com",
            f"{SECRET},{SECRET_EMAIL},extra",
            "Bilal",
            "C,c@example.com",
        )
    )
    assert [row.row for row in parsed.rows] == [2, 5]
    assert parsed.problems == (
        RowProblem(3, "row.column_count", "Expected 2 values, found 3"),
        RowProblem(4, "row.column_count", "Expected 2 values, found 1"),
    )
    rendered = repr(parsed.problems)
    assert SECRET not in rendered and SECRET_EMAIL not in rendered


def test_file_with_only_bad_width_rows_is_not_reported_as_empty() -> None:
    parsed = read_leads_csv(csv_bytes("full_name,email", "A"))
    assert parsed.rows == ()
    assert [p.code for p in parsed.problems] == ["row.column_count"]


# --- row limit ------------------------------------------------------------------------


def _rows(count: int) -> bytes:
    return csv_bytes("full_name,phone", *(f"L{i},+92300{i:07d}" for i in range(count)))


def test_exactly_the_row_limit_is_accepted() -> None:
    parsed = read_leads_csv(_rows(MAX_DATA_ROWS))
    assert len(parsed.rows) == MAX_DATA_ROWS == 1000


def test_one_row_over_the_limit_is_rejected() -> None:
    errors = file_errors(_rows(MAX_DATA_ROWS + 1))
    assert [(e.code, e.message) for e in errors] == [
        ("file.too_many_rows", "The file has more than 1000 lead rows; split it into smaller files")
    ]


def test_blank_lines_do_not_count_toward_the_limit_but_bad_rows_do() -> None:
    blanks = csv_bytes("full_name,phone", "", "A,1", ",", "B,2", "")
    assert len(read_leads_csv(blanks, max_rows=2).rows) == 2
    assert codes(csv_bytes("full_name,phone", "A,1", "bad", "C,3"), max_rows=2) == [
        "file.too_many_rows"
    ]


# --- privacy and purity -------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        csv_bytes(f"full_name,{SECRET_EMAIL}", f"{SECRET},x"),
        csv_bytes("full_name,email", f'"{SECRET},{SECRET_EMAIL}'),
        csv_bytes("full_name,email", f"{SECRET}{chr(0)},{SECRET_EMAIL}"),
        f"full_name,email\r\n{SECRET},{SECRET_EMAIL}\r\n".encode("utf-16"),
    ],
    ids=["unknown-column-name", "malformed", "nul", "encoding"],
)
def test_file_errors_never_contain_submitted_text(data: bytes) -> None:
    assert_no_secrets(data, SECRET, SECRET_EMAIL, "Ayesha", "private")


def test_csv_file_error_summary_lists_codes_only() -> None:
    error = CsvFileError([FileError("file.empty", "msg"), FileError("file.malformed", "msg")])
    assert str(error) == "Invalid CSV file (file.empty, file.malformed)"


def test_reader_imports_only_stdlib_and_m1() -> None:
    tree = ast.parse(Path(csv_reader.__file__).read_text(encoding="utf-8"))
    modules = {
        alias.name if isinstance(node, ast.Import) else (node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert modules == {
        "csv",
        "io",
        "collections.abc",
        "dataclasses",
        "leadflow_ai.leads.validation",
    }
