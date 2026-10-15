# LeadFlow AI

A local-first service for collecting and validating sales leads.

- **M0, foundation:** a FastAPI service with a health endpoint, typed configuration, reusable operator authentication, and quality tooling.
- **M1, lead validation:** pure-Python validation and normalization of lead input.
- **M2, storage and API:** a local SQLite database (SQLAlchemy and Alembic) and an authenticated REST API to create and read leads.
- **M3, CSV import:** upload a CSV file of leads; every row is validated and the whole file is imported, or nothing is.

Everything runs on your machine, and it uses no paid services or external APIs. There's no dashboard, scoring, or LLM yet.

> **LeadFlow AI does not send anything.** No emails, SMS, or other messages are sent, and there's no sending feature in M0–M3, real or simulated. It only stores and returns the leads you submit.

## Requirements

- Python 3.12 (pinned in `.python-version`)
- [uv](https://docs.astral.sh/uv/) 0.13+

## Setup

Run these commands from the project root:

```sh
uv sync                  # creates .venv and installs dependencies from uv.lock
cp .env.example .env     # .env is git-ignored; never commit it
```

Generate a random operator token and put it in `.env`:

```sh
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```sh
# .env
LEADFLOW_OPERATOR_TOKEN=<paste the generated value here>
```

The token must be at least 32 characters. The app won't start without one, and `.env.example` leaves it blank on purpose. Treat it like a password: anyone who has it can read every lead.

## Configuration

Settings come from environment variables or from `.env` in the directory you run commands from.

| Variable | Required | Description |
|---|---|---|
| `LEADFLOW_OPERATOR_TOKEN` | yes | Bearer token for the lead API, at least 32 characters |
| `LEADFLOW_DATABASE_URL` | no | Local SQLite file URL (default `sqlite:///./leadflow.db`); other databases are rejected |

- **Relative path:** the default path `./leadflow.db` is relative to the directory you run commands from. Run `alembic` and `uvicorn` from the same place, normally the project root.
- **Accepted URLs:** only local SQLite file URLs (`sqlite:///relative.db` or `sqlite:////absolute/path.db`). Other databases, in-memory databases, hosts, credentials, and query parameters are rejected at startup.
- **Masking:** the token is held as a `SecretStr`, and rejected settings values are never echoed in error messages.
- **Git:** `leadflow.db` and its `-wal`, `-shm`, and `-journal` files are git-ignored.
- **Permissions:** on a shared machine, restrict them with `chmod 600 leadflow.db*`.

## Create the database

Alembic creates and upgrades the schema. The app never creates tables itself.

```sh
uv run alembic upgrade head     # creates ./leadflow.db (or LEADFLOW_DATABASE_URL) and the leads table
uv run alembic current          # shows the applied revision, e.g. "0001 (head)"
```

- **Token:** migrations read only `LEADFLOW_DATABASE_URL`, so they don't need the operator token.
- **After an upgrade:** run `uv run alembic upgrade head` again after pulling a version with new migrations.
- **Undo:** `uv run alembic downgrade base` removes the schema and **deletes all leads**.

## Run the server

```sh
uv run uvicorn --factory leadflow_ai.main:create_app
```

Add `--reload` while developing. The server listens on `http://127.0.0.1:8000`, which only your machine can reach. If port 8000 is already in use, uvicorn exits with an "address already in use" error. Add `--port 8001` (or any free port) and use that port in the URLs below.

```sh
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"0.3.0"}
```

**`/health` doesn't check the database.** It only shows that the process is running. The app opens the database on the first lead request, so a healthy server can still answer lead requests with `503` (see [Database errors](#database-errors)).

The OpenAPI docs at `/docs` and `/openapi.json` are public. They describe the API but contain no lead data.

## Use the API

Load the token into your shell first. `set -a` exports every variable defined in `.env`:

```sh
set -a; . ./.env; set +a
```

Every `/api/v1/leads` request needs `Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN`. Requests that send a body also need `Content-Type: application/json`.

### Create a lead: `POST /api/v1/leads`

```sh
curl -i -X POST http://127.0.0.1:8000/api/v1/leads \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"full_name": "  Ayesha   Khan ", "email": "Ayesha.Khan@Example.COM", "phone": "0300-1234567", "source": "web_form"}'
```

```
HTTP/1.1 201 Created
location: /api/v1/leads/2f1c...-...

{"id":"2f1c...","full_name":"Ayesha Khan","email":"ayesha.khan@example.com","phone":"+923001234567",
 "company":null,"source":"web_form","notes":null,"created_at":"2026-10-14T12:15:16.031993Z"}
```

**Fields:**

| Field | Required | Notes |
|---|---|---|
| `full_name` | yes | up to 100 characters; must contain a letter or digit |
| `email` | email or phone | stored lowercased |
| `phone` | email or phone | stored as E.164 (see below) |
| `company` | no | up to 120 characters |
| `source` | no | `web_form`, `referral`, `event`, `import`, `other` (default `other`) |
| `notes` | no | up to 1000 characters |

**Field rules:**
- All values must be strings, and any other field is rejected (including `id` and `created_at`, which the server sets).
- **Phone numbers:** Pakistani mobiles can be entered as `03XX-XXXXXXX`. All other numbers, including Pakistani landlines, need an international prefix (`+44 20 7946 0958`).
- **Validity:** checks are structural, so a well-formed email or number may still not exist.
- **Full rules:** see [ADR 0002](docs/adr/0002-m1-lead-validation.md).

### Get one lead: `GET /api/v1/leads/{id}`

```sh
curl http://127.0.0.1:8000/api/v1/leads/<id> \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN"
```

Use the exact `id` (or the `location` header) from the create response. IDs are lowercase UUIDs. Any other form, including uppercase, returns `404`.

### List leads: `GET /api/v1/leads`

```sh
curl "http://127.0.0.1:8000/api/v1/leads?limit=20&offset=0" \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN"
# {"items":[...],"limit":20,"offset":0}
```

- **Order:** newest first.
- **Pages:** `limit` is 1–100 (default 50). `offset` is how many leads to skip, from 0 to 9223372036854775807 (default 0). Fetch the next page with `offset = offset + limit`, and stop when `items` comes back shorter than `limit`.
- **Out-of-range values** return `422`.

### Duplicates

Each email address and each phone number can belong to only one lead. Comparison uses the normalized values, so `Ayesha.Khan@Example.COM` and `ayesha.khan@example.com`, or `0300-1234567` and `+92 300 1234567`, count as the same.

A duplicate returns `409`. The response names the field and the existing lead's id, but never the value:

```json
{"detail": {"code": "lead.duplicate",
            "message": "A lead with this email or phone already exists",
            "conflicts": [{"field": "email", "lead_id": "2f1c..."}]}}
```

The existing lead is never changed or merged, and nothing is written. This holds even when two requests arrive at the same moment, because the database's unique constraints decide.

### Import leads from CSV: `POST /api/v1/leads/import`

Send a CSV file as the raw request body with `Content-Type: text/csv`. The bearer token is required, as for every lead request.

```sh
curl -X POST http://127.0.0.1:8000/api/v1/leads/import \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN" \
  -H "Content-Type: text/csv" \
  --data-binary @leads.csv
```

Use `--data-binary`, which sends the file exactly as it is. `-d` strips line breaks, and `-F` (a multipart form upload) is rejected with `415`.

**File format:**

```
full_name,email,phone,company,source,notes
Ayesha Khan,ayesha@example.com,0300-1234567,Acme Traders,referral,Met at expo
Bilal,,+44 20 7946 0958,,,
```

- **Header row:** required. The columns are the same fields as a single lead: `full_name` plus `email` and/or `phone` are required, and the rest are optional.
  - Column names ignore case and surrounding spaces, and can be in any order.
  - An unknown or repeated column rejects the file.
- **Cells:** an empty cell means "not provided". Every row goes through the same validation as a single lead.
- **Blank lines** are skipped. Row numbers in errors are spreadsheet row numbers: the first line of the file is row 1, and blank lines (including any before the header) are counted.
- **Encoding:** must be UTF-8. In Excel, use **File > Save As > "CSV UTF-8 (Comma delimited)"**. Plain "CSV" in Excel uses Windows-1252: a file containing only ASCII characters is also valid UTF-8 and is accepted, but accented or non-Latin letters (for example `é` or Urdu text) make it fail with `file.encoding`. Always choose "CSV UTF-8".
- **Limits:**
  - at most **1 MiB** (1048576 bytes) per file (other API requests are limited to 16 KiB)
  - at most **1,000 lead rows** per file; split larger lists into several files

**All or nothing:**
- **Success:** if every row is valid and new, all rows are imported in one step and the response lists their ids in file order.
- **Any failure:** if any row is invalid or a duplicate, **nothing is imported**. Fix the file and upload it again.

```
HTTP/1.1 201 Created

{"imported": 2, "lead_ids": ["2f1c...", "8a7d..."]}
```

Fetch each imported lead with `GET /api/v1/leads/{id}`.

**Duplicates are rejected, never merged.** A row is rejected if its email or phone matches an earlier row in the same file (`row.duplicate_in_file`, with `duplicate_of_row`) or an existing lead (`lead.duplicate`, with that lead's `lead_id`). Re-uploading a file that was already imported is therefore rejected as a whole.

**Rejected rows** return `422 import.rejected`. Row errors are the single-lead validation codes, the two duplicate codes above, and `row.column_count` (a row with more or fewer cells than the header). At most 100 row errors are listed, and `truncated` is `true` when there were more:

```json
{"detail": {"code": "import.rejected", "message": "No leads were imported",
            "row_errors": [
              {"row": 3, "field": "email", "code": "email.invalid_format", "message": "Not a valid email address"},
              {"row": 4, "field": "email", "code": "row.duplicate_in_file", "message": "Same email or phone as an earlier row in this file", "duplicate_of_row": 2},
              {"row": 5, "field": "phone", "code": "lead.duplicate", "message": "A lead with this email or phone already exists", "lead_id": "2f1c..."},
              {"row": 6, "code": "row.column_count", "message": "Expected 3 values, found 4"}],
            "truncated": false}}
```

**A file that can't be read at all** returns `422 import.invalid_file`. Each entry in `errors` has a `code` and a fixed `message` that never contains your data, plus `columns` for header column problems. Problem columns are identified **by column number, not name**, because a missing header row would otherwise echo your data:

```json
{"detail": {"code": "import.invalid_file",
            "errors": [{"code": "header.unknown_columns",
                        "message": "Unknown column names; allowed columns are: full_name, email, phone, company, source, notes",
                        "columns": [2, 4]}]}}
```

| `errors[].code` | Meaning |
|---|---|
| `file.empty` | no data, or a header with no lead rows |
| `file.encoding` | not UTF-8 (save as "CSV UTF-8") |
| `file.malformed` | broken quoting, NUL characters, or a single cell over 131,072 characters |
| `file.too_many_rows` | more than 1,000 lead rows |
| `header.unknown_columns` | column names that aren't lead fields (blank header cells included) |
| `header.duplicate_columns` | the same column more than once |
| `header.missing_required` | no `full_name` column, or neither `email` nor `phone` |

### Errors

Most errors have the form `{"detail": {"code": "...", ...}}`. Depending on the error, `detail` also has a `message`, a list of `errors`, `row_errors`, or `conflicts`. Errors never include the values you sent.

| Status | `detail.code` | When |
|---|---|---|
| `401` | none (`detail` is the string `"Invalid or missing bearer token"`) | missing or wrong token |
| `404` | `lead.not_found` | no lead with that id (also for malformed ids) |
| `404` | none (`"Not Found"`) | unknown path, including a trailing slash such as `/api/v1/leads/` |
| `409` | `lead.duplicate` | email or phone already stored |
| `413` | `request.too_large` | body over 16 KiB (16384 bytes); over 1 MiB (1048576 bytes) for CSV import |
| `415` | `request.unsupported_media_type` | `Content-Type` missing or wrong: `application/json` for `POST /api/v1/leads`, `text/csv` (UTF-8) for the import |
| `422` | `lead.invalid` | field validation failed; `errors` lists each `field` and `code` |
| `422` | `request.invalid_body` | body isn't a single JSON object (bad JSON, an array, duplicate keys, ...) |
| `422` | `request.invalid` | bad query parameter, such as `limit=0` |
| `422` | `import.rejected` | CSV import: some rows are invalid or duplicates; nothing was imported |
| `422` | `import.invalid_file` | CSV import: the file or its header can't be used; nothing was imported |
| `503` | `database.busy` / `database.not_migrated` / `database.unavailable` | see [Database errors](#database-errors) |
| `500` | none (plain text) | unexpected server error |

**Check order:** body size (`413`), then token (`401`), then content type (`415`), then body content (`422`). For example, an unauthenticated oversized request gets `413`, and an unauthenticated `text/plain` request gets `401`.

`curl -d` sends `application/x-www-form-urlencoded` by default, so always add `-H "Content-Type: application/json"`.

### Database errors

| `detail.code` | Meaning | What to do |
|---|---|---|
| `database.not_migrated` | The leads table doesn't exist | Run `uv run alembic upgrade head`. No restart needed: the next request works. |
| `database.busy` | Another process held the write lock for over 5 seconds | Retry. The response has `Retry-After: 1`, and the server doesn't retry for you. |
| `database.unavailable` | The database file can't be opened (missing or read-only directory) | Check `LEADFLOW_DATABASE_URL` and the directory permissions. The response never shows the path. |

**Before you migrate:** the first lead request against a database file that doesn't exist yet creates an empty `leadflow.db` (plus `-wal` and `-shm` files) and returns `database.not_migrated`. Running the migration then fixes it in place.

**Logging:** the app doesn't log these `503`s; uvicorn's access log still records the status code.

## Privacy

- **No personal data in URLs.** uvicorn's access log records every request's path and query string. The API never needs personal data there (look leads up by id), and you shouldn't add it. For example, don't append `?email=...`.
- **Validation and duplicate errors** report field names, codes, and lead ids, never the submitted values.
- **CSV import errors** report row and column numbers, never cell values or header text. Uploaded files are processed in memory and never saved to disk.
- **Database error messages** hide SQL parameter values, and printing a stored lead object in a log or traceback shows only its id.
- **Lead data** is returned only to requests with the operator token. Anyone with the token can read every lead.

## Lead validation in Python

The `leadflow_ai.leads` package can also be used directly, without the API:

```python
from leadflow_ai.leads import LeadValidationError, validate_lead

lead = validate_lead(
    {
        "full_name": "  Ayesha   Khan ",
        "email": "Ayesha.Khan@Example.COM",
        "phone": "0300-1234567",  # PK mobile; other numbers need +countrycode
        "source": "Web_Form",
    }
)
lead.email  # 'ayesha.khan@example.com'
lead.phone  # '+923001234567'
lead.identity_keys()  # frozenset({'email:ayesha.khan@example.com', 'phone:+923001234567'})

try:
    validate_lead({"full_name": "", "phone": "021-12345678"})
except LeadValidationError as exc:
    for error in exc.errors:
        print(error.field, error.code)
    # phone phone.national_format_unsupported
    # full_name field.required
```

## Protecting new routes

Add the `OperatorAuth` dependency to require the bearer token:

```python
from leadflow_ai.auth import OperatorAuth


@app.get("/admin/thing")
def thing(_: OperatorAuth) -> dict[str, str]: ...
```

The token is compared using `hmac.compare_digest`.

## Quality checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest                # includes coverage, fails under 90%
```

- **Isolation:** the tests block network access and use temporary SQLite databases. They never touch `leadflow.db`.
- **Coverage:** includes the Alembic migration files.

## Layout

```
src/leadflow_ai/
  api/             HTTP routes, request limits, error responses (M2)
  db/              SQLAlchemy models, engine/session, Alembic migrations (M2)
  services/        lead create/get/list, duplicate handling (M2), CSV batch import (M3)
  imports/         CSV file reading and header checks (M3)
  leads/           pure-Python validation and normalization (M1)
  main.py          application factory
alembic.ini        Alembic configuration (the database URL comes from settings)
tests/             unit, database, and API tests
docs/adr/          architectural decision records
```

Design decisions: [ADR 0001](docs/adr/0001-m0-foundation.md) (foundation), [ADR 0002](docs/adr/0002-m1-lead-validation.md) (validation), [ADR 0003](docs/adr/0003-m2-database-and-api.md) (database and API), [ADR 0004](docs/adr/0004-m3-csv-import.md) (CSV import).
