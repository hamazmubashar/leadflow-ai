# LeadFlow AI

A local-first service for collecting and validating sales leads.

- **M0, foundation:** a FastAPI service with a health endpoint, typed configuration, reusable operator authentication, and quality tooling.
- **M1, lead validation:** pure-Python validation and normalization of lead input.
- **M2, storage and API:** a local SQLite database (SQLAlchemy and Alembic) and an authenticated REST API to create and read leads.

Everything runs on your machine, and it uses no paid services or external APIs. There's no dashboard, scoring, or LLM yet.

> **LeadFlow AI does not send anything.** No emails, SMS, or other messages are sent, and there's no sending feature in M0–M2, real or simulated. It only stores and returns the leads you submit.

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
# {"status":"ok","version":"0.2.0"}
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

### Errors

Most errors have the form `{"detail": {"code": "...", ...}}`. Depending on the error, `detail` also has a `message`, a list of `errors`, or a list of `conflicts`. Errors never include the values you sent.

| Status | `detail.code` | When |
|---|---|---|
| `401` | none (`detail` is the string `"Invalid or missing bearer token"`) | missing or wrong token |
| `404` | `lead.not_found` | no lead with that id (also for malformed ids) |
| `404` | none (`"Not Found"`) | unknown path, including a trailing slash such as `/api/v1/leads/` |
| `409` | `lead.duplicate` | email or phone already stored |
| `413` | `request.too_large` | body over 16 KiB (16384 bytes) |
| `415` | `request.unsupported_media_type` | `Content-Type` missing or not `application/json` on `POST` |
| `422` | `lead.invalid` | field validation failed; `errors` lists each `field` and `code` |
| `422` | `request.invalid_body` | body isn't a single JSON object (bad JSON, an array, duplicate keys, ...) |
| `422` | `request.invalid` | bad query parameter, such as `limit=0` |
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
  services/        lead create/get/list and duplicate handling (M2)
  leads/           pure-Python validation and normalization (M1)
  main.py          application factory
alembic.ini        Alembic configuration (the database URL comes from settings)
tests/             unit, database, and API tests
docs/adr/          architectural decision records
```

Design decisions: [ADR 0001](docs/adr/0001-m0-foundation.md) (foundation), [ADR 0002](docs/adr/0002-m1-lead-validation.md) (validation), [ADR 0003](docs/adr/0003-m2-database-and-api.md) (database and API).
