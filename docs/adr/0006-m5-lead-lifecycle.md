# ADR 0006: M5 lead lifecycle and activity history

- Status: Accepted
- Date: 2026-10-15

## Context

Operators need to track where each lead stands and see an auditable history of how it got there.

**Constraints:**
- Zero budget, local-first, and no new dependencies.
- Existing M0–M4 response contracts and behavior preserved.
- A status change and its history must never get out of step.

**Repository facts this builds on:**
- One migration (`0001`).
- SQLite with `PRAGMA foreign_keys=ON`.
- Single-commit write paths in `create_lead` and `import_leads`.
- Tests pinning the exact lead response fields.
- ADR 0003's known limitation that **SQLite migrations weren't transactional**.

The project version is now `0.5.0`.

## Decisions

### 1. Statuses and transitions (`lifecycle/statuses.py`, pure, fixed in code)
`new` (initial) · `contacted` · `qualified` · `won` · `lost`

| From | Allowed to |
|---|---|
| `new` | `contacted`, `qualified`, `lost` |
| `contacted` | `qualified`, `lost` |
| `qualified` | `won`, `lost` |
| `lost` | `contacted` (reopen) |
| `won` | none (final) |

Same-status changes are invalid. The table isn't configurable, and an exhaustive test checks all 25 status pairs against it.

### 2. Storage: current status on `leads`, append-only history in `lead_events`
Migration `0002_lead_lifecycle` makes three changes.

**1. On `leads`:** adds `status VARCHAR(20) NOT NULL DEFAULT 'new'` with `ck_leads_status_allowed`. SQLite's `ADD COLUMN` handles this without rebuilding the table.

**2. A new `lead_events` table:**

| Column | Type and constraints |
|---|---|
| `id` | UUID4 primary key |
| `lead_id` | foreign key to `leads.id` |
| `sequence` | `INTEGER ≥ 1`, `UNIQUE (lead_id, sequence)` |
| `event_type` | `lead_created` or `status_changed` |
| `from_status`, `to_status` | |
| `note` | ≤ 500 characters |
| `occurred_at` | UTC (the existing `UTCDateTime` type) |

CHECK constraints also enforce each event's shape:
- `lead_created` must have no `from_status` and `to_status = 'new'`.
- `status_changed` must have a `from_status` that differs from `to_status`.

**3. Backfill:** one `lead_created` event per existing lead, with `sequence 1`. Its **`occurred_at` is reconstructed from the lead's stored `created_at`, copied verbatim**, because no history existed before M5. It's a single `INSERT … SELECT` that generates UUID4s in SQLite, so `alembic upgrade head --sql` keeps working. An earlier draft that read rows in Python broke offline mode, and a test caught it.

**Ordering:** history is ordered by **`sequence`**, never by timestamp. Sequences are gap-free per lead, because failed transactions roll back.

**Downgrade:** `0002 → 0001` drops `lead_events`, then `status`, and keeps all lead data (tested).

### 3. Migrations are now all-or-nothing
- **Evidence first.** A spike ran a multi-step migration with an injected failure against the **previous** `env.py`. It left the new column and new table in place while `alembic_version` still said `0001`, an inconsistent database. With the change below, the same failure left the database exactly as before.
- **The change (`env.py`, migration engine only):** SQLAlchemy's documented pysqlite recipe, which turns off the driver's implicit transactions (`isolation_level = None`), plus a `begin` listener that issues `BEGIN` and `transactional_ddl=True`. All schema changes, the backfill and the version stamp then commit together. **The app's runtime engine isn't changed.**
- **Tests:**
  - Failures are injected into a copy of the **real** `env.py`, `0001` and `0002`: once after the column is added, and once after every step. Each time, the list of tables, the `leads` columns, every lead row and `alembic_version` are identical to before, and the real migration then applies cleanly.
  - A control run with the recipe removed shows the partial state, which proves the test can tell the two apart.
  - Deliberately removing the recipe fails the tests.
- **Offline scripts (`alembic upgrade … --sql`)** are configured the same way (`transactional_ddl=True`), so a generated script starts with `BEGIN;` and ends with `COMMIT;`.
  - **Verified by tests:** the first and last statements of the generated `0001:head` script are `BEGIN;` and `COMMIT;`. Applied by hand with `sqlite3` to a `0001` database with data, the script upgrades it to `0002` with the backfill. With a statement that fails just before `COMMIT;`, the database is left identical to before.
- **Foreign keys during migrations.** SQLite's documented table-rebuild procedure, which Alembic batch mode uses, needs foreign key enforcement **off**, and `PRAGMA foreign_keys` has no effect inside a transaction. With the new `lead_events → leads` key, a batch rebuild of `leads` would otherwise always fail. A probe showed that even `PRAGMA defer_foreign_keys=ON` doesn't help.
  - **The change (migration engine only):**
    - `PRAGMA foreign_keys=OFF` is set when the connection opens, before `BEGIN`.
    - After all migrations, still inside the transaction, `PRAGMA foreign_key_check` runs. Any violation raises `ForeignKeyViolationError`, and the whole migration rolls back. The message names only tables and a count, never row contents.
    - The app's runtime engine still enforces foreign keys on every connection.
  - **Verified by tests**, against copies of the real `env.py`, `0001` and `0002`, plus a test-only `0003`:
    - An Alembic batch rebuild (`recreate="always"`) of `leads` succeeds while `lead_events` rows reference it. Lead rows and events are unchanged, `foreign_key_check` is empty, and the runtime engine still rejects an orphaned event afterwards.
    - A control without `foreign_keys=OFF` fails the same rebuild with `FOREIGN KEY constraint failed`.
    - A migration that rebuilds `leads` **and** leaves an orphaned event is rolled back entirely: no new column, no orphan row, the version stays `0002`.
    - Deliberately removing the check, or the `OFF` pragma, fails the tests.
  - **Offline caveat:** offline scripts don't include the `foreign_key_check` step. `sqlite3` applies scripts with enforcement off by default, so review generated scripts before applying them by hand, or prefer online `alembic upgrade`.
- **Scope:** this lifts ADR 0003's "migrations aren't transactional" limitation for every future migration, and ADR 0003's batch-rebuild approach keeps working for tables that other tables reference.

### 4. Atomic, concurrency-safe status changes (`services/lifecycle.py`)
`change_status` uses one transaction and one commit:
1. If `expected_status` (required) doesn't equal the lead's status: **409 `lead.status_conflict`** with `current_status`. If the lifecycle doesn't allow the change: **422 `lead.invalid_transition`** with `from_status`, `to_status` and `allowed`.
2. **Compare-and-set:** `UPDATE leads SET status=:to WHERE id=:id AND status=:expected`. If no row matches, roll back and return 409. That catches stale reads and concurrent changes.
3. Insert the event with `MAX(sequence)+1`, safe because SQLite's write lock is held after step 2. `UNIQUE (lead_id, sequence)` is the final safeguard: an IntegrityError rolls back **both** the status and the event, then returns 409.
4. Commit.

**Tests:**
- Exactly one commit per change, and a stale in-memory read is rejected by the compare-and-set.
- An injected sequence collision rolls back the status UPDATE too.
- **Two threads held at a barrier** after both read `new`: exactly one wins, the loser gets 409 naming the winner's status, and exactly one `status_changed` event exists (20 out of 20 repeated runs).
- Deliberately removing the compare-and-set, or committing the status separately, fails the tests.

**Creation events:** `create_lead` and `import_leads` flush the new leads (SQLite checks the foreign key immediately), then add their `lead_created` events, **inside their existing single commit**. A CSV import therefore stays all-or-nothing, events included. A rejected import or duplicate writes **no** events (tested). Their responses are unchanged.

### 5. API
**Authenticated routes on the leads router** (the auth-coverage test includes them):
- `POST /{id}/status` with body `{status, expected_status, note?}` returns **200** `{lead_id, status, event}`.
- `GET /{id}/activity?limit&offset` returns `{lead_id, status, items (oldest first), limit, offset}`. Pagination bounds are the same as for the lead list.

**Body validation** reuses M1's error format: **`422 lead.invalid`** with per-field `errors`.
- Missing, blank or wrong-type `status` / `expected_status`.
- An unknown status: `status.invalid_choice`. Status values are matched case-insensitively.
- `note`: optional, ≤ 500 characters, cleaned by M1's `clean_text` with line breaks kept.
- Unknown fields: reported without naming them.

**Consistent activity reads:** the activity response's `status` and `items` come from **one SELECT** (events joined to their lead). SQLite reads a single statement from a single snapshot, so the status can't straddle a concurrent change relative to the events; the runtime engine isn't changed. A page past the last event reads the status alone, since it has no items to disagree with.
- **Verified by a deterministic test:** a hook commits a status change through a separate engine immediately after the endpoint's lead lookup and before the activity read, with no sleeps or thread timing. The response's `status` matches its newest event, while the old two-read code reported the stale status.

**Other responses:**
- Malformed JSON: 422 `request.invalid_body`.
- Wrong content type: 415.
- Unknown or malformed id: 404 `lead.not_found`.
- Database problems: 503, as before.

**Existing contracts are unchanged:** `status` isn't added to create, get, list or import responses, and the tests pinning their fields are untouched. Scoring is unaffected by status (tested).

### 6. Time and privacy
- **Time:** `occurred_at` is set by the server in UTC and returned in ISO 8601 with `Z`.
- **Notes:** written by the operator, so they may contain personal data.
  - Returned only to authenticated requests.
  - Never included in any error response (tested with sensitive values).
  - Never in a record's text form (`repr`).
- **Statuses, sequence numbers and timestamps** aren't personal data, so they may appear in errors.

## Known limitations

- **No per-person attribution:** one shared operator token means events can't record who made a change. No actor field is faked.
- **Reconstructed history:** `lead_created` events for leads that existed before 0.5.0 are reconstructed from `created_at`. No earlier history exists.
- **Status isn't in existing responses**, and there's no filtering or sorting by status. Use the activity endpoint.
- **Append-only:** events can't be edited or deleted, including notes. Operators should keep notes minimal.
- **Fixed lifecycle:** the lifecycle isn't configurable, and there are no bulk status changes.
- **Writes are serialized** by SQLite's write lock, as in M2. A change waiting more than 5 s returns 503 `database.busy`.

## Consequences

Status changes are atomic, auditable and safe under concurrent updates. Migrations, including all future ones, now apply all or nothing on SQLite. Exposing status in other responses, per-user identity, filtering by status or configurable workflows would each need new decisions.
