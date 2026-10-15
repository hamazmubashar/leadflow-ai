# LeadFlow AI

A local-first service for collecting and validating sales leads.

- **M0, foundation:** a FastAPI service with a health endpoint, typed configuration, reusable operator authentication, and quality tooling.
- **M1, lead validation:** pure-Python validation and normalization of lead input.
- **M2, storage and API:** a local SQLite database (SQLAlchemy and Alembic) and an authenticated REST API to create and read leads.
- **M3, CSV import:** upload a CSV file of leads; every row is validated and the whole file is imported, or nothing is.
- **M4, lead scoring:** a 0–100 score with a hot/warm/cold tier, calculated on request from versioned TOML rules, with a per-rule explanation.
- **M5, lead lifecycle:** each lead has a status (new, contacted, qualified, won, lost), changed through allowed transitions, with an append-only activity history.
- **M6, response drafts:** generate an email draft for a lead from a fixed template, have a person approve or reject it, and record approved drafts in a **simulated** outbox.

Everything runs on your machine, and it uses no paid services or external APIs. There's no dashboard, machine learning, or LLM.

> **LeadFlow AI does not send anything.** No emails, SMS, or other messages are ever sent. M6's outbox is **simulated**: approving a draft only records that it *would* be sent. There's no sending code, mail server setting, or delivery worker, and nothing contacts an external service.

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
| `LEADFLOW_SCORING_RULES_PATH` | no | Path to your own scoring rules TOML file; the built-in rules are used when it's unset or empty (see [Lead scoring](#lead-scoring-get-apiv1leadsidscore)) |

- **Relative path:** the default path `./leadflow.db` is relative to the directory you run commands from. Run `alembic` and `uvicorn` from the same place, normally the project root.
- **Accepted URLs:** only local SQLite file URLs (`sqlite:///relative.db` or `sqlite:////absolute/path.db`). Other databases, in-memory databases, hosts, credentials, and query parameters are rejected at startup.
- **Masking:** the token is held as a `SecretStr`, and rejected settings values are never echoed in error messages.
- **Git:** `leadflow.db` and its `-wal`, `-shm`, and `-journal` files are git-ignored.
- **Permissions:** on a shared machine, restrict them with `chmod 600 leadflow.db*`.

## Create the database

Alembic creates and upgrades the schema. The app never creates tables itself.

```sh
uv run alembic upgrade head     # creates ./leadflow.db (or LEADFLOW_DATABASE_URL) and its tables
uv run alembic current          # shows the applied revision, e.g. "0003 (head)"
```

- **Token:** migrations read only `LEADFLOW_DATABASE_URL`, so they don't need the operator token.
- **After an upgrade:** run `uv run alembic upgrade head` again after pulling a version with new migrations.
- **Undo:** `uv run alembic downgrade base` removes the schema and **deletes all leads**.
- **Back up before upgrading.** Stop the server and copy `leadflow.db` (and any `leadflow.db-wal` / `leadflow.db-shm` files) before running `alembic upgrade head` on a database that has data.
- **Upgrades are all-or-nothing.** Each upgrade runs in a single transaction: if any step fails, the database is left exactly as it was and `alembic current` still shows the previous revision.
- **Upgrading to 0.5.0 (migration `0002`)** gives every existing lead the status `new` and one `lead_created` history event. Those events are **reconstructed** from each lead's stored `created_at`, because no history was recorded before M5. `uv run alembic downgrade 0001` removes the lifecycle data but keeps the leads.
- **Upgrading to 0.6.0 (migration `0003`)** only adds the `lead_drafts` and `outbox_messages` tables; existing tables and data are unchanged. `uv run alembic downgrade 0002` **permanently deletes all drafts and outbox items** and keeps leads and their history, so back up first.

## Run the server

```sh
uv run uvicorn --factory leadflow_ai.main:create_app
```

Add `--reload` while developing. The server listens on `http://127.0.0.1:8000`, which only your machine can reach. If port 8000 is already in use, uvicorn exits with an "address already in use" error. Add `--port 8001` (or any free port) and use that port in the URLs below.

```sh
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"0.6.0"}
```

**`/health` doesn't check the database.** It only shows that the process is running. The app opens the database on the first lead request, so a healthy server can still answer lead requests with `503` (see [Database errors](#database-errors)).

The OpenAPI docs at `/docs` and `/openapi.json` are public. They describe the API but contain no lead data.

## Use the API

Load the token into your shell first. `set -a` exports every variable defined in `.env`:

```sh
set -a; . ./.env; set +a
```

Every `/api/v1/...` request needs `Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN`. Requests that send a body also need `Content-Type: application/json`.

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

### Lead scoring: `GET /api/v1/leads/{id}/score`

```sh
curl http://127.0.0.1:8000/api/v1/leads/<id>/score \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN"
```

```json
{"lead_id": "2f1c...", "score": 45, "max_score": 100, "tier": "warm", "rules_version": "2026-10-15.1",
 "factors": [{"rule": "source.web_form", "matched": true, "points": 15},
             {"rule": "has_email", "matched": true, "points": 15},
             {"rule": "has_phone", "matched": true, "points": 15},
             {"rule": "has_company", "matched": false, "points": 0},
             {"rule": "non_free_email_domain", "matched": false, "points": 0}]}
```

**What it is:**
- **A heuristic, not a prediction.** The score is a prioritization aid based on configurable rules. It has **not** been validated against real conversions, so treat it as a sorting hint, not a forecast.
- **Explained.** `factors` always lists the same five rules in the same order (the lead's source, then four signals), showing which matched and the points each added. The score is their sum.
- **Calculated on request.** The score is never stored, so it always reflects the lead's current data and the current rules. `rules_version` tells you which ruleset produced it.
- **Deterministic.** The same lead and rules always give the same score. Nothing depends on the time.
- **Privacy:** the response contains rule names and points only, never the lead's email, domain, phone, or other values.
- **No scores elsewhere:** other responses (create, get, list, import) don't include a score.

**Default rules** (0–100):

| Rule | Points | Matched when |
|---|---|---|
| `source.*` | referral 30, event 20, web_form 15, import 5, other 0 | always; the lead's source |
| `has_email` | 15 | the lead has an email address |
| `has_phone` | 15 | the lead has a phone number |
| `has_company` | 15 | the lead has a company name |
| `non_free_email_domain` | 25 | the email's domain is **not** on the free-email list (`gmail.com`, `outlook.com`, ...) |

**Tiers:** `hot` at 70 or above, `warm` at 40 or above, otherwise `cold`.

**Fields that are never scored:** names, phone country codes, notes, and creation time. They could introduce bias (names, countries), are unpredictable free text (notes), or would make the score change over time (creation time).

**Limits of `non_free_email_domain`:** it only checks that the domain isn't on a short list of free providers. It does **not** verify that the lead works for a business, or for the company the domain suggests. A personal custom domain or a regional free provider missing from the list also matches, so add the providers your leads use.

**Custom rules:** copy `src/leadflow_ai/scoring/default_rules.toml`, edit it, and set `LEADFLOW_SCORING_RULES_PATH` to the copy. Change `rules_version` whenever you change the rules.
- **Validation:** every key, type and range is checked when the server starts. Whole numbers only (no `true`/`false` or decimals), every lead source listed exactly once, `hot > warm > cold = 0`, valid lowercase domains, at most 64 KiB, and the highest source score plus all signal points no more than 100.
- **Invalid rules stop the server from starting**, with a list of every problem by key path. The server **never** silently falls back to the built-in rules.
- **Changes take effect after a restart.**

### Lead lifecycle: `POST /api/v1/leads/{id}/status` and `GET /api/v1/leads/{id}/activity`

Every lead has a **status**. New and imported leads start as `new`. Allowed changes:

| From | To |
|---|---|
| `new` | `contacted`, `qualified`, `lost` |
| `contacted` | `qualified`, `lost` |
| `qualified` | `won`, `lost` |
| `lost` | `contacted` (reopen) |
| `won` | none (final) |

Changing to the same status isn't allowed.

**Change a status.** Send the new `status` **and the `expected_status`** (the status you last saw). An optional `note` of up to 500 characters is stored with the change.

```sh
curl -X POST http://127.0.0.1:8000/api/v1/leads/<id>/status \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"status": "contacted", "expected_status": "new", "note": "Called, asked for a demo"}'
```

```json
{"lead_id": "2f1c...", "status": "contacted",
 "event": {"id": "9b3e...", "sequence": 2, "type": "status_changed", "from_status": "new",
           "to_status": "contacted", "note": "Called, asked for a demo", "occurred_at": "2026-10-15T12:00:00.123456Z"}}
```

- **The status and its history event are saved together,** in one transaction: either both are stored or neither is.
- **`expected_status` prevents lost updates.** If the lead's status isn't what you expected, because someone else changed it first, nothing is written and you get `409 lead.status_conflict` with `current_status`. Reload, decide again, and retry. When two changes arrive at the same moment, exactly one succeeds.
- **A change the table doesn't allow** returns `422 lead.invalid_transition` with `from_status`, `to_status`, and the `allowed` next statuses.
- **A missing or invalid field** (status, expected status, note, unknown fields) returns `422 lead.invalid` with the same per-field `errors` as lead creation. An unknown status value has the code `status.invalid_choice`; status values ignore case.

**View the history.** Events are returned **oldest first**, ordered by `sequence` (1, 2, 3, ... per lead). `limit` and `offset` work as for the lead list.

```sh
curl "http://127.0.0.1:8000/api/v1/leads/<id>/activity?limit=50&offset=0" \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN"
# {"lead_id": "...", "status": "contacted", "items": [{"sequence": 1, "type": "lead_created", ...},
#   {"sequence": 2, "type": "status_changed", ...}], "limit": 50, "offset": 0}
```

- **Timestamps:** `occurred_at` is set by the server, in UTC. `sequence`, not the timestamp, defines the order.
- **Append-only:** events can't be edited or deleted.
- **Leads that existed before 0.5.0** have a reconstructed `lead_created` event (see [Create the database](#create-the-database)).
- **Notes are visible to anyone with the operator token.** They may contain personal data, so keep them short and factual. They never appear in error responses.
- **No per-person attribution:** with one shared token, the history can't record *who* made a change.
- **Not shown elsewhere:** existing lead responses (create, get, list, import) don't include the status. Use the activity endpoint.

### Response drafts and the simulated outbox

A **draft** is an email for one lead, rendered from a fixed template. A person reviews it, then **approves** or **rejects** it. Approving records an item in the **simulated outbox**; nothing is sent.

| Status | Meaning | Next |
|---|---|---|
| `pending_review` | awaiting review (new drafts start here) | `approved` or `rejected` |
| `approved` | approved; has exactly one outbox item | none (final) |
| `rejected` | rejected, with an optional reason | none (final) |

**Create a draft.** `template` is `introduction` or `follow_up`.

```sh
curl -X POST http://127.0.0.1:8000/api/v1/leads/<id>/drafts \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"template": "introduction"}'
```

```json
{"id": "5d0a...", "lead_id": "2f1c...", "template": "introduction", "template_version": "2026-10-15.1",
 "channel": "email", "recipient": "ayesha.khan@example.com", "subject": "Introduction for Acme Traders",
 "body": "Hello Ayesha Khan,\n\nThank you for getting in touch on behalf of Acme Traders. ...",
 "status": "pending_review", "rejection_reason": null, "created_at": "2026-10-15T12:00:00.123456Z", "decided_at": null}
```

- **Deterministic text:** the same lead and template always give the same text. Templates are fixed in code; `template_version` records which version produced a draft.
- **Only the lead's name and company are used.** Notes, phone, source, status and scores never appear in a draft. Email is the only channel: a lead without an email gets `422 draft.no_email`.
- **Eligible leads:** the lead's status must be `new`, `contacted` or `qualified`. For `won` or `lost` leads you get `409 draft.lead_not_eligible` with `lead_status`.
- **One draft awaiting review per lead:** a second one gets `409 draft.pending_exists`. Approve or reject the first.
- **Drafts can't be edited.** The stored text is exactly what you review and approve. To change it, reject the draft and create a new one. No signature or sender is added.

**Review it.**

```sh
curl http://127.0.0.1:8000/api/v1/drafts/<draft_id> -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN"
curl "http://127.0.0.1:8000/api/v1/leads/<id>/drafts?limit=50&offset=0" -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN"   # newest first

curl -X POST http://127.0.0.1:8000/api/v1/drafts/<draft_id>/approve \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN"
# {"draft": {..., "status": "approved", "decided_at": "..."},
#  "outbox_message": {"id": "...", "draft_id": "...", "lead_id": "...", "status": "simulated",
#                     "recipient": "...", "subject": "...", "body": "...", "created_at": "..."}}

curl -X POST http://127.0.0.1:8000/api/v1/drafts/<draft_id>/reject \
  -H "Authorization: Bearer $LEADFLOW_OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Too formal"}'     # or '{}' for no reason; reason is at most 500 characters
```

- **Approval checks the lead again.** If the lead is no longer `new`, `contacted` or `qualified` (for example it was marked `lost` after the draft was created), approval returns `409 draft.lead_not_eligible`, the draft stays `pending_review`, and no outbox item is created. Reject the draft, or reopen the lead (`lost` → `contacted`) and approve again.
- **Each draft is decided once.** Approving or rejecting a decided draft returns `409 draft.already_decided` with its `status`. When two decisions arrive at the same moment, exactly one succeeds.
- **Approval is all-or-nothing:** the draft's new status and its outbox item are saved in one transaction. A rejected or failed approval never creates an outbox item.
- **Rejection works for any lead status**, so a pending draft for a `won` or `lost` lead can always be cleared.
- **Approval doesn't change the lead's status** or its activity history.

**The outbox.** `GET /api/v1/outbox?limit&offset` lists items newest first; `GET /api/v1/outbox/{id}` returns one. Every item's `status` is `simulated`. Its recipient, subject and body are those of the approved draft, which can never change afterwards: the database itself rejects edits to draft text, to decided drafts and to outbox items, and rejects deleting outbox items.

### Errors

Most errors have the form `{"detail": {"code": "...", ...}}`. Depending on the error, `detail` also has a `message`, a list of `errors`, `row_errors`, or `conflicts`. Errors never include the values you sent.

| Status | `detail.code` | When |
|---|---|---|
| `401` | none (`detail` is the string `"Invalid or missing bearer token"`) | missing or wrong token |
| `404` | `lead.not_found` | no lead with that id (also for malformed ids) |
| `404` | `draft.not_found` / `outbox.not_found` | no draft or outbox item with that id (also for malformed ids) |
| `404` | none (`"Not Found"`) | unknown path, including a trailing slash such as `/api/v1/leads/` |
| `409` | `lead.duplicate` | email or phone already stored |
| `409` | `lead.status_conflict` | `expected_status` doesn't match the lead's current status |
| `409` | `draft.lead_not_eligible` | creating or approving a draft for a lead that isn't `new`, `contacted` or `qualified`; includes `lead_status` and `eligible` |
| `409` | `draft.pending_exists` | the lead already has a draft awaiting review |
| `409` | `draft.already_decided` | the draft was already approved or rejected; includes `status` |
| `413` | `request.too_large` | body over 16 KiB (16384 bytes); over 1 MiB (1048576 bytes) for CSV import |
| `415` | `request.unsupported_media_type` | `Content-Type` missing or wrong: `application/json` for JSON bodies (creating leads and drafts, status changes, rejecting a draft), `text/csv` (UTF-8) for the import |
| `422` | `lead.invalid` | field validation failed; `errors` lists each `field` and `code` |
| `422` | `lead.invalid_transition` | the lifecycle doesn't allow that status change |
| `422` | `draft.no_email` | the lead has no email address (drafts are email only) |
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
| `database.not_migrated` | A table doesn't exist, for example the draft tables before migration `0003` | Run `uv run alembic upgrade head`. No restart needed: the next request works. |
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
- **Draft text, recipients and rejection reasons** are returned only to requests with the operator token. They never appear in error responses, and printing a draft or outbox object shows only its id (and a draft's status).

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
  services/        lead create/get/list, duplicate handling (M2), CSV batch import (M3),
                   status changes (M5), drafts and the simulated outbox (M6)
  imports/         CSV file reading and header checks (M3)
  scoring/         scoring rules (default_rules.toml), validation and the pure scoring engine (M4)
  lifecycle/       lead statuses and the allowed transitions (M5)
  drafts/          fixed email templates and draft statuses (M6)
  leads/           pure-Python validation and normalization (M1)
  main.py          application factory
alembic.ini        Alembic configuration (the database URL comes from settings)
tests/             unit, database, and API tests
docs/adr/          architectural decision records
```

Design decisions: [ADR 0001](docs/adr/0001-m0-foundation.md) (foundation), [ADR 0002](docs/adr/0002-m1-lead-validation.md) (validation), [ADR 0003](docs/adr/0003-m2-database-and-api.md) (database and API), [ADR 0004](docs/adr/0004-m3-csv-import.md) (CSV import), [ADR 0005](docs/adr/0005-m4-lead-scoring.md) (lead scoring), [ADR 0006](docs/adr/0006-m5-lead-lifecycle.md) (lead lifecycle), [ADR 0007](docs/adr/0007-m6-drafts-and-simulated-outbox.md) (drafts and simulated outbox).
