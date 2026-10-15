# ADR 0004: M3 all-or-nothing CSV lead import

- Status: Accepted
- Date: 2026-10-15

## Context

Operators need to load existing lead lists, usually spreadsheet exports, without posting leads one at a time.

**M3 must:**
- Reuse M1 validation and M2 storage, authentication and duplicate rules unchanged.
- Stay local and zero-cost.
- Add no dependencies, schema changes or migrations.
- Never echo personal data in errors.

The project version is now `0.3.0`.

## Decisions

### 1. Endpoint and upload transport
- **Endpoint:** `POST /api/v1/leads/import` on the existing leads router, so the router-level operator token check applies. The OpenAPI-based test that every `/api` route requires auth includes it.
- **Body:** the **raw CSV bytes** with `Content-Type: text/csv`, matched case-insensitively. A `charset` parameter, if present, must be `utf-8`.
  - Multipart uploads and JSON are rejected with 415 `request.unsupported_media_type` ("Content-Type must be text/csv (UTF-8)").
  - Raw bodies avoid the `python-multipart` dependency that FastAPI's file uploads need.
- **Order of checks:** the same as M2: **413 → 401 → 415 → 422**. The body is read by a dependency that runs after authentication and the media-type check.
- **Safe handling:** the upload is processed **in memory only**. It's never written to disk, and no filename is used.

### 2. Limits
| Limit | Value | Result when exceeded |
|---|---|---|
| Body size, import route only | **1 MiB** (1048576 bytes) | 413 `request.too_large` |
| Body size, every other route | **16 KiB**, unchanged | 413 `request.too_large` |
| Lead rows per file | **1,000** (blank rows not counted) | `file.too_many_rows` |
| Row errors returned | **100** | `truncated: true` |
| A single cell | 131,072 characters, the `csv` module default | `file.malformed` |

- **How the 1 MiB exception works:** the M2 body-size middleware got an exact-path limit map; the 413 message reports the limit that applied. The 1 MiB limit applies before authentication, like the 16 KiB one, so it also bounds what an unauthenticated client can send.
- **Why the cell limit stays at the default:** it's process-wide state, so it isn't changed.

### 3. CSV format (`imports/csv_reader.py`, standard library only, no I/O)
- **Encoding:** UTF-8, with an optional byte-order mark (Excel's "CSV UTF-8" adds one). Bytes that aren't valid UTF-8 are `file.encoding`. Excel's default "CSV" is Windows-1252: an ASCII-only file is also valid UTF-8 and is accepted, but non-ASCII content (accented or non-Latin letters) is rejected. Users are told to export "CSV UTF-8".
- **Empty files:** no data, only a byte-order mark or whitespace, only blank rows, or a header with no lead rows are all `file.empty`.
- **Structure:**
  - NUL characters are rejected explicitly, because Python 3.12's `csv` module accepts them.
  - Quoting errors are caught by strict mode.
  - Both are `file.malformed`.
- **Header:**
  - The first non-blank row. Names match M1's `ALLOWED_FIELDS`, ignoring case and surrounding spaces, in any order.
  - Unknown names (including blank header cells) are `header.unknown_columns`; repeated names are `header.duplicate_columns`.
  - A missing `full_name`, or missing both `email` and `phone`, is `header.missing_required`.
  - All header problems are reported together.
- **Rows:**
  - Fully blank rows (no cells, or only empty cells such as `,,,`) are skipped.
  - Row numbers are spreadsheet row numbers: the first line of the file is row 1, and every line counts, including blank lines before the header.
  - A row with the wrong number of cells is `row.column_count`; the rest of the file is still read.
  - Empty cells are passed to M1, which treats them as missing.

### 4. All-or-nothing import (`services/imports.py`)
1. **Validate every row** with the unchanged M1 `validate_lead`.
2. **Duplicates within the file:** a normalized email or phone equal to an earlier row's is `row.duplicate_in_file`, with `duplicate_of_row`.
3. **Duplicates against stored leads:** **one batched query** for the whole file (at most 2,000 parameters; SQLite allows 32,766). A match is `lead.duplicate`, with `lead_id` and the same message as the API's 409.
4. **Any error means nothing is written.** The read transaction is rolled back, and `ImportRejectedError` carries the row errors sorted by row, the first 100, and an exact `truncated` flag.
5. **Otherwise** every record is added in file order and committed **once**. The ids are returned in file order.
6. **A conflict at commit time:** the UNIQUE constraints remain authoritative. If another request stores the same contact between the lookup and the commit, the commit fails, **the whole batch is rolled back**, the duplicates are looked up again, and the conflict is reported as `lead.duplicate`. Other integrity errors are re-raised, with parameters hidden by the engine.
7. **Never merged:** existing leads are never updated or merged.

**Why all or nothing:** the outcome is always "imported N" or "imported 0". Fixing and re-uploading a file is always safe, and there's never a half-imported file whose imported rows would then fail as duplicates on the next upload.

### 5. Responses (the contract)
**Success:**
```json
201 {"imported": 3, "lead_ids": ["…", "…", "…"]}
```
The ids are in file order.

**Rejected rows** (`import.rejected`):
```json
422 {"detail": {"code": "import.rejected", "message": "No leads were imported",
                "row_errors": [...], "truncated": false}}
```
Each row error has `row`, `code` and `message`; `field` when a field is involved; `lead_id` for `lead.duplicate`; and `duplicate_of_row` for `row.duplicate_in_file`.

**File-level problems** (`import.invalid_file`):
```json
422 {"detail": {"code": "import.invalid_file", "errors": [...]}}
```
- Each error has `code` and `message`, plus `columns` (1-based column numbers) for header column problems.
- `message` was added to the originally proposed example (which showed only `code` and `columns`). It's fixed text that never contains submitted data, and it carries the actionable tip for `file.encoding`.

**Privacy:**
- Errors never contain cell values or header text.
- Bad header columns are reported **by number only**, because a forgotten header row would make the first data row (names, emails) look like the header.

## Known limitations and future concerns

- **CSV formula injection (relevant to any future export).** Values are stored exactly as validated, including ones starting with `=`, `+`, `-`, `@`, tab or carriage return. Spreadsheet programs may run such cells as formulas when a CSV is opened, for example `=HYPERLINK(...)`. M3 has **no export**, so nothing is affected today. **Any future CSV export must neutralize these cells** (for example, prefix them with `'`) and document that.
- **Size and duration:** imports are capped at 1,000 rows and 1 MiB, with no streaming or background jobs. One import holds the SQLite write lock for its single transaction; other writers wait up to the 5 s busy timeout, as in M2.
- **Strictness:** only UTF-8 is accepted, unknown or blank header columns reject the file, and there's no preview or "import valid rows only" mode.
- **Timestamps:** leads from one import get nearly or exactly identical `created_at` values, so the list endpoint's newest-first order among them falls back to `id`. The import response gives the ids in file order.
- **Multiple errors per row:** a row can carry several errors, for example both `row.duplicate_in_file` and `lead.duplicate`, so the error count can exceed the number of bad rows.
- **Error responses before authentication:** `GET /api/v1/leads/import` is a lead lookup for the id `import` and returns 404. Unauthenticated clients can send up to 1 MiB to the import path before receiving 401.

## Consequences

Lead lists can be imported safely in one request with clear, privacy-safe feedback, using only the standard library and the existing M1/M2 code. Supporting other encodings, larger files or partial imports would need new decisions about streaming, transaction size and duplicate semantics.
