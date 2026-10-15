# ADR 0009: M8 workflow runs (fixed new-lead outreach workflow)

- Status: Accepted
- Date: 2026-10-15

## Context

LeadFlow AI is a workflow-automation project, but until M7 every step was a manual click. Operators want the routine part automated: score new leads and prepare first-contact drafts for the promising ones, with run history and clear success and failure reporting.

**Constraints:**
- Human approval stays mandatory (M6). Automation may prepare drafts, never approve, reject or send.
- Zero budget and local-first: no job queue, scheduler, background worker or new dependency.
- M0–M7 behavior preserved; migrations additive only.

The project version is now `0.7.0`.

## Decisions

### 1. One fixed, versioned workflow; no workflow builder
- `workflows/outreach.py` (pure) defines **new-lead outreach**: candidates are `new` leads not yet processed by a live run of this version; each is scored with the current rules; hot leads with an email get an *introduction* draft; everything else is skipped with a reason code.
- `WORKFLOW_VERSION` is stored with every run and result. Changing the steps or the rule means a new version, and leads are then processed again under it.
- **Why not configurable workflows:** a builder (user-defined steps, triggers, schedules) is a much larger and riskier surface. One well-tested workflow shows the automation pattern end to end.

### 2. Preview and live are distinct everywhere
- `mode` is **required** in the API (no default), so a live run never starts by accident. The dashboard also asks for an explicit confirmation before a live run.
- Preview results say `would_create_draft` and can never reference a draft (CHECK constraint). Previews never count as processing.

### 3. Mandatory human approval
- The workflow service only calls the M6 *stage draft* function, so M6 eligibility and the one-pending-draft rule still apply. It has no reference to approve, reject, the outbox or any network module (a test scans its source).
- Run results expose `drafts_need_human_review`; the drafts appear on the normal review screens.

### 4. Database-level safeguards (migration 0004)
| Risk | Safeguard |
|---|---|
| Two runs at once | Partial unique index: one `running` run per workflow (`409 workflow.run_in_progress`) |
| A lead processed twice | Partial unique index on `(workflow_version, lead_id)` for live, non-failed results |
| A draft without its result (or vice versa) | `create_draft` split into *stage* + commit; the draft and its result commit in **one** transaction |
| Results added to a stopped run | Trigger: results only for a `running` run of the same mode and version |
| A result pointing at someone else's draft | Trigger: the draft must be a pending draft of the same lead |
| Claiming completion falsely | Trigger: `completed` only when the result count equals `planned_count`; counts are always derived from result rows, never stored |
| History tampering | Triggers: finished runs and all results are immutable and can't be deleted |

### 5. Interruption without a background worker
- A run executes synchronously inside its request. Every recorded lead refreshes the run's `heartbeat_at` with a compare-and-set that also takes SQLite's write lock.
- If the process dies, the run stays `running` until a later start finds its heartbeat older than **5 minutes** and marks it `interrupted` (`stale_heartbeat`). The new run then processes the leads the old one didn't reach.
- If a run discovers it was marked interrupted, it stops immediately and never marks itself completed.

### 6. Failures are per lead and retried
- A lead's error is stored as a `failed` result with a fixed code (`scoring_error`, `database_busy`, `concurrent_update`, `unexpected_error`), never an exception message or lead value. The run continues. Failed leads are excluded from the processed-once index, so the next live run retries them.
- If a result itself can't be stored, the run ends as `failed` (`recording_failed`) rather than continuing without a record.

### 7. Privacy
- Results hold ids, outcome, reason, score and tier only; no names, emails or phones. Model `repr`s exclude everything else.

## Consequences

- Automation is visible and auditable: every decision is a stored, immutable result with its rules and workflow version.
- A run of up to 500 leads executes within one HTTP request. That's fine locally; a production deployment with thousands of leads would need a job queue (out of scope, documented).
- Changing the decision rule requires a version bump; old results remain readable.

## Verification

- `tests/test_db_workflow_migration.py`: schema, constraints, partial indexes and triggers, reversibility, all-or-nothing upgrade, offline SQL.
- `tests/test_workflows_service.py`: preview writes nothing, live creates pending drafts only, processed-once and retries, the cap, races (status change, manual draft, second run, interruption), index backstops with service checks bypassed, atomic draft and result, recording failure, refused false completion.
- `tests/test_api_workflows.py`: required mode, exact responses, `409`, `404`, auth, no contact data.
- `dashboard/tests/test_workflows.py` and client tests for the Workflows page.
- Full suite at 100% coverage, strict mypy and Ruff.
