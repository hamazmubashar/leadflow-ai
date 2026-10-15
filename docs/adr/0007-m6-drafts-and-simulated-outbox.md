# ADR 0007: M6 deterministic response drafts, human approval and a simulated outbox

- Status: Accepted
- Date: 2026-10-15

## Context

Operators want help writing a first response to a lead, with a person deciding what goes out.

**Constraints:**
- Zero budget and local-first: no LLM, paid service, network access or new dependency.
- M0–M5 behavior and response contracts preserved.
- Nothing may be sent, and nothing may contact an external service.

**Repository facts this builds on:**
- Migration head `0002`; every migration runs in one transaction (ADR 0006).
- A lead's name, email and company never change after it's created. Its status is the only field that changes (M5).
- M1 collapses names and companies to a single line.
- `lead_events.event_type` only allows M5's two event types, so recording draft events there would mean rebuilding that table.

The project version is now `0.6.0`.

## Decisions

### 1. Fixed, versioned templates (`drafts/templates.py`, pure)
- **Two templates**, `introduction` and `follow_up`, fixed in code. `TEMPLATE_VERSION` (`2026-10-15.1`) is stored with every draft and must change whenever any template text changes.
- **Inputs:** only `full_name` and `company`. The input `Protocol` exposes nothing else. Notes, phone, source, status and scores never appear in a draft (tested).
- **Deterministic:** rendering is a pure function with no clock, randomness or I/O. Values are inserted by concatenation, never `str.format`, so braces in a name can't change the template.
- **Email only:** the recipient is the lead's stored (lowercase) email. A lead without an email can't get a draft.
- **No signature or sender identity,** so no new setting is needed.
- **Why not TOML, like the scoring rules:** that needs a validator and loader for a feature with two fixed texts. Revisit if operators need custom templates.

### 2. Draft lifecycle, eligibility and errors
| From | Allowed to |
|---|---|
| `pending_review` (initial) | `approved`, `rejected` |
| `approved`, `rejected` | none (final) |

- **Stored as rendered:** approval covers exactly the reviewed text, which is never re-rendered.
- **No editing:** reject and create a new draft instead.
- **Eligibility:** a lead must be `new`, `contacted` or `qualified` both to get a draft and when its draft is approved. Otherwise **409 `draft.lead_not_eligible`** with `lead_status` and `eligible`. It's 409, not 422, because it conflicts with the lead's current state, and one code means the answer is the same whether the check fails early or in a race.
  - A failed approval leaves the draft `pending_review`; it's never rejected automatically.
  - Rejection doesn't check eligibility, so a pending draft for a won or lost lead can always be cleared.
- **One pending draft per lead:** **409 `draft.pending_exists`**, with the same body whether the explicit check or the database index catches it, and no `draft_id`.
- **Each draft is decided once:** **409 `draft.already_decided`** with `status`. It takes precedence over `draft.lead_not_eligible`.
- **No email:** **422 `draft.no_email`**. A lead's email can't change, so this is a property of the lead, not a conflict.
- **Lead status and history are unaffected:** approval doesn't change the lead's status and adds no `lead_events`.

### 3. Concurrency: take SQLite's write lock first, then check
Each write starts with a statement that takes SQLite's write lock, and every check that matters runs after it.

- **Lead guard (`_lock_eligible_lead`):** `UPDATE leads SET status = status WHERE id = :id AND status IN ('new','contacted','qualified')`. It changes nothing, but executing it takes the write lock even when it matches no row. A concurrent status change therefore either committed before it (no match) or waits until this transaction ends.
- **Draft compare-and-set (`_mark_decided`):** `UPDATE lead_drafts SET status = ..., decided_at = ... WHERE id = :id AND status = 'pending_review'`. Of two concurrent decisions, exactly one matches.
- **Verified first, not assumed:** a probe on this machine's SQLite (3.53.1) and Python 3.12 driver confirmed that:
  - a zero-row guard still holds the write lock (another connection gets "database is locked");
  - a plain `SELECT` doesn't open a transaction, so the guard always reads the latest committed status.
- **Nothing else changes:** neither the runtime engine nor any M5 code. No `BEGIN IMMEDIATE`.

**Create** (one commit):
1. Read the lead: 404 if missing, 422 if it has no email.
2. Render the text.
3. Run the guard. No match: read the status under the lock, roll back, 409 `draft.lead_not_eligible`.
4. Under the lock, check for a pending draft: 409 `draft.pending_exists`.
5. Insert. An `IntegrityError` from the partial unique index rolls back and returns 409 `draft.pending_exists`; any other `IntegrityError` is re-raised.

**Approve** (one commit):
1. Read the draft: 404, or 409 `draft.already_decided` if it's decided, without writing.
2. Compare-and-set the draft. No match: 409 `draft.already_decided`.
3. Run the lead guard. No match: roll back (undoing step 2) and return 409 `draft.lead_not_eligible`.
4. Insert the outbox row. A `UNIQUE(draft_id)` collision rolls back steps 2–3 and returns 409.

Step 2 comes before step 3, which is what makes `already_decided` win. Eligibility holds at the moment of commit; a status change after that doesn't undo the approval.

**Reject:** read the draft, compare-and-set it, commit. It never touches the outbox.

**Waiting:** a write that waits more than 5 s returns 503 `database.busy`, as before.

### 4. Schema: migration `0003_drafts_and_outbox`
**`lead_drafts`:** `id`, `lead_id` (foreign key), `template`, `template_version`, `channel`, `recipient`, `subject` (≤ 200), `body` (≤ 2000), `status`, `rejection_reason` (≤ 500), `created_at`, `decided_at`.
- **CHECKs:** template, `channel = 'email'`, lowercase recipient, status, and a shape check: `decided_at` is NULL exactly when pending, and only rejected drafts may have a reason.
- **Indexes:** partial unique `uq_lead_drafts_one_pending_per_lead (lead_id) WHERE status = 'pending_review'`, and `ix_lead_drafts_lead_id_created_at`.

**`outbox_messages`:** `id`, `draft_id` (UNIQUE, foreign key), `lead_id` (foreign key), `status` (CHECK `= 'simulated'`), `created_at` (indexed).
- **No content columns:** recipient, subject and body are read from the approved draft.
- **No delivery fields:** no `sent_at`, attempts or other state.

**Triggers:** immutability is enforced by the database, not only by the application. Each trigger aborts the statement.

| Trigger | Blocks |
|---|---|
| `trg_lead_drafts_content_immutable` | any UPDATE naming `id`, `lead_id`, `template`, `template_version`, `channel`, `recipient`, `subject`, `body` or `created_at`, even with an unchanged value |
| `trg_lead_drafts_decided_immutable` | any UPDATE of an approved or rejected draft |
| `trg_lead_drafts_no_delete_with_outbox` | deleting a draft that has an outbox row |
| `trg_outbox_messages_require_approved_draft` | inserting an outbox row unless its draft is approved **and** belongs to the same lead |
| `trg_outbox_messages_immutable` | any UPDATE of an outbox row |
| `trg_outbox_messages_no_delete` | any DELETE of an outbox row (the outbox is append-only) |

- **Beyond the approved design:** two small additions strengthen these guarantees, and neither weakens anything.
  - `trg_outbox_messages_require_approved_draft` makes "a rejected or failed approval never creates an outbox item" hold even for SQL written outside the app.
  - `id` is on the content trigger's list.
- **Triggers apply regardless of `PRAGMA foreign_keys`,** so they also protect a plain `sqlite3` session (tested).

**Migration properties:**
- **Low risk:** it only creates tables, indexes and triggers. Nothing is rebuilt, and existing rows are untouched (tested).
- **Atomic:** a failure injected at the end of `0003` leaves the schema identical to `0002` (tested).
- **Offline scripts** (`--sql`) include the partial index and the triggers, wrapped in `BEGIN;` / `COMMIT;`.
- **Downgrade** drops the triggers, indexes and both tables. This **permanently deletes all drafts and outbox items** and is documented in the README.
- **Model comparison:** `compare_metadata` doesn't compare an index's `WHERE` clause or triggers, so dedicated tests check both against the expected SQL.

### 5. API
Two new routers with router-level operator authentication. The auth-coverage test now covers every router serving `/api`.

| Route | Success |
|---|---|
| `POST /api/v1/leads/{id}/drafts` `{"template"}` | 201, with `Location` |
| `GET /api/v1/leads/{id}/drafts?limit&offset` | 200, newest first |
| `GET /api/v1/drafts/{id}` | 200 |
| `POST /api/v1/drafts/{id}/approve` (no body; any body is ignored) | 200 `{draft, outbox_message}` |
| `POST /api/v1/drafts/{id}/reject` (a JSON object, optional `reason`) | 200 |
| `GET /api/v1/outbox?limit&offset` | 200, newest first |
| `GET /api/v1/outbox/{id}` | 200 |

- **404s:** `lead.not_found`, `draft.not_found`, `outbox.not_found`; malformed ids are treated as not found.
- **Body errors:** `422 lead.invalid` with M1's per-field format (`template.invalid_choice` for an unknown template); 415 for a wrong content type. Check order is unchanged: 413 → 401 → 415 → 422.
- **Existing responses are unchanged.**

### 6. Privacy and "simulated only"
- **Message content** (recipient, subject, body, rejection reason) is returned only to authenticated requests. It's never in error responses or `repr`, and never logged (tested with sensitive values).
- **No network-capable code:** every M6 module may only import from an allowlist of packages. A test also checks that no module in the package imports `smtplib`, `socket`, `ssl`, `http`, `urllib`, `email`, `asyncio`, `subprocess`, or the HTTP client libraries. The test suite's network guard stays on.

## Verification
- **Deterministic race tests:** hooks around `_lock_eligible_lead` and `_mark_decided` commit a competing change through a separate engine at an exact point. They cover:
  - a status change before the create guard;
  - the write lock being held from the guard to the commit, for both create and approve;
  - a status change, or a decision, between the draft read and the compare-and-set;
  - precedence, with the hook firing before whichever lock statement comes first.
- **Barrier races,** 20 runs each: create against create, approve against approve, and approve against reject.
- **Safeguards:** the partial unique index (with the explicit pending check bypassed) and `UNIQUE(draft_id)` (with a stray outbox row) each roll back everything.
- **Deliberate breaks, all caught by the tests (13 of 13):** removing either guard, replacing the guard with a plain `SELECT`, removing the compare-and-set, committing the draft before the guard, reordering the approval steps, removing the partial index, and removing each of the six triggers.

## Known limitations

- **Nothing is ever sent.** Delivering approved drafts would need a new decision about channels, credentials and consent.
- **No per-person attribution:** one shared token means decisions don't record who made them.
- **Fixed templates and English-only text;** no custom templates or editing.
- **Downgrading to `0002` deletes all drafts and outbox items.**

## Consequences

Operators get reviewable, reproducible drafts with a strict approval step and an auditable simulated outbox. The database itself guarantees that approved text can't change and that an outbox item exists only for an approved draft. Real sending, custom templates, editing, or draft events in the lead history would each need new decisions.
