"""Read an uploaded CSV file into rows of lead fields. Standard library only, no I/O.

The reader checks the *file* (encoding, structure, header, size). It does not
validate lead values; every row is later passed unchanged to M1's
``validate_lead()``. Errors never contain cell values or header text: a header
can be a data row if the user forgot it, so bad columns are reported by number.
"""

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass

from leadflow_ai.leads.validation import ALLOWED_FIELDS

MAX_DATA_ROWS = 1000
UTF8_BOM = b"\xef\xbb\xbf"
_REQUIRED = "full_name"
_CONTACT = ("email", "phone")


@dataclass(frozen=True, slots=True)
class FileError:
    """A problem with the file as a whole. ``columns`` are 1-based column numbers."""

    code: str
    message: str
    columns: tuple[int, ...] = ()


class CsvFileError(ValueError):
    """The file cannot be imported at all. Holds every file-level problem found."""

    def __init__(self, errors: list[FileError]) -> None:
        self.errors: tuple[FileError, ...] = tuple(errors)
        super().__init__("Invalid CSV file (" + ", ".join(e.code for e in self.errors) + ")")


@dataclass(frozen=True, slots=True)
class CsvRow:
    """One data row. ``row`` is the spreadsheet row number (the first line is row 1)."""

    row: int
    values: dict[str, str]


@dataclass(frozen=True, slots=True)
class RowProblem:
    """A structural problem with one row (e.g. the wrong number of cells)."""

    row: int
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ParsedCsv:
    columns: tuple[str, ...]
    rows: tuple[CsvRow, ...]
    problems: tuple[RowProblem, ...]


def read_leads_csv(data: bytes, *, max_rows: int = MAX_DATA_ROWS) -> ParsedCsv:
    """Parse ``data`` (UTF-8, optional BOM) into lead rows, or raise ``CsvFileError``.

    - The first non-blank row is the header. Column names are matched to M1's
      fields ignoring case and surrounding spaces; order does not matter.
      ``full_name`` and at least one of ``email``/``phone`` are required.
    - Fully blank rows (no cells, or only empty/space cells) are skipped but
      still counted in row numbers, so numbers match a spreadsheet.
    - Rows with the wrong number of cells are reported as ``RowProblem``s and
      the rest of the file is still read.
    - More than ``max_rows`` data rows rejects the whole file.
    """
    text = _decode(data)
    records = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header_row, columns = _read_header(records)
        rows, problems = _read_rows(records, header_row, columns, max_rows)
    except csv.Error:
        raise CsvFileError(
            [FileError("file.malformed", "The file is not valid CSV (check quoting)")]
        ) from None
    if not rows and not problems:
        raise CsvFileError([FileError("file.empty", "The file has a header but no lead rows")])
    return ParsedCsv(columns=columns, rows=tuple(rows), problems=tuple(problems))


def _decode(data: bytes) -> str:
    if data.startswith(UTF8_BOM):
        data = data[len(UTF8_BOM) :]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise CsvFileError(
            [FileError("file.encoding", "The file must be UTF-8 (in Excel: Save As 'CSV UTF-8')")]
        ) from None
    if "\x00" in text:
        raise CsvFileError([FileError("file.malformed", "The file contains NUL characters")])
    if not text.strip():
        raise CsvFileError([FileError("file.empty", "The file is empty")])
    return text


def _is_blank(record: list[str]) -> bool:
    return all(not cell.strip() for cell in record)


def _read_header(records: Iterator[list[str]]) -> tuple[int, tuple[str, ...]]:
    for row_number, record in enumerate(records, start=1):
        if _is_blank(record):
            continue
        return row_number, _check_header(record)
    # Reached when every row is blank, e.g. a file containing only ",,,".
    raise CsvFileError([FileError("file.empty", "The file is empty")])


def _check_header(record: list[str]) -> tuple[str, ...]:
    names = tuple(cell.strip().lower() for cell in record)
    errors: list[FileError] = []

    unknown = tuple(i for i, name in enumerate(names, start=1) if name not in ALLOWED_FIELDS)
    if unknown:
        errors.append(
            FileError(
                "header.unknown_columns",
                f"Unknown column names; allowed columns are: {', '.join(ALLOWED_FIELDS)}",
                unknown,
            )
        )
    seen: set[str] = set()
    repeated: list[int] = []
    for i, name in enumerate(names, start=1):
        if name in ALLOWED_FIELDS and name in seen:
            repeated.append(i)
        seen.add(name)
    if repeated:
        errors.append(
            FileError(
                "header.duplicate_columns", "Each column may appear only once", tuple(repeated)
            )
        )
    if _REQUIRED not in names:
        errors.append(FileError("header.missing_required", "The header must include full_name"))
    if not any(field in names for field in _CONTACT):
        errors.append(
            FileError("header.missing_required", "The header must include email or phone")
        )
    if errors:
        raise CsvFileError(errors)
    return names


def _read_rows(
    records: Iterator[list[str]], header_row: int, columns: tuple[str, ...], max_rows: int
) -> tuple[list[CsvRow], list[RowProblem]]:
    rows: list[CsvRow] = []
    problems: list[RowProblem] = []
    data_rows = 0
    for row_number, record in enumerate(records, start=header_row + 1):
        if _is_blank(record):
            continue
        data_rows += 1
        if data_rows > max_rows:
            raise CsvFileError(
                [
                    FileError(
                        "file.too_many_rows",
                        f"The file has more than {max_rows} lead rows; split it into smaller files",
                    )
                ]
            )
        if len(record) != len(columns):
            problems.append(
                RowProblem(
                    row_number,
                    "row.column_count",
                    f"Expected {len(columns)} values, found {len(record)}",
                )
            )
            continue
        rows.append(CsvRow(row_number, dict(zip(columns, record, strict=True))))
    return rows, problems
