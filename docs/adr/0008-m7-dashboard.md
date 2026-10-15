# ADR 0008: M7 Streamlit operator dashboard

- Status: Accepted
- Date: 2026-10-15

## Context

Operators need a visual way to run the M0–M6 workflow: see the pipeline, manage leads, import files, review drafts and check the simulated outbox.

**Constraints:**
- Zero budget and local-first: open-source tools only, no cloud service.
- The FastAPI backend stays the single source of truth. M0–M6 behavior and contracts are unchanged; this milestone changes no backend file.
- Nothing may be sent (M6), and lead data must not leak into logs or errors.

## Decisions

### 1. A separate HTTP client, not a second backend
- **`dashboard/` is a top-level package,** outside `src/`. It talks to the API over HTTP only and never imports backend modules or opens SQLite.
- **One client (`api_client.py`)** owns every request: explicit connect/read timeouts, no redirects (the token is never forwarded), **no retries** (a retried write could duplicate a lead or an import). Every failure becomes one `ApiError` kind: unreachable, timeout, unauthorized, HTTP error, invalid response. Exception details are dropped.
- **Typed responses (`models.py`)** mirror the API schemas, ignore unknown fields (additive API changes can't break the UI), and turn malformed data into "invalid response" without echoing values.
- **Dependencies:** `streamlit` and `requests` (plus `types-requests` for strict mypy) in a separate `ui` dependency group, so the API's runtime dependencies are unchanged.

### 2. No business rules in the frontend
- Scores, tiers, status transitions, draft eligibility and approval rules are never recalculated or pre-checked. The UI offers the choices and shows the API's answer, including its structured refusal (`allowed` next statuses, `current_status`, `lead_status`).
- The dashboard only counts and groups values the API returned (KPIs, charts, filters).

### 3. Data loading without aggregate endpoints
- The API has no summary, filter or global-feed endpoints, and lead responses omit `status` (ADR 0006 kept the M2 contract). The dashboard therefore reads all lead pages, then each lead's activity, score and drafts: **3 requests per lead**.
- **Cached for 60 s** (`st.cache_data`) and cleared after every write or on **Refresh**. A failed request fails the whole load rather than showing a partial, misleading table.
- **Capped at 500 leads,** with a visible warning above that.
- **Trade-off:** fine for a local demo (tens of leads load in well under a second), slow for thousands. The fix is backend work: `status` in lead responses and summary/list endpoints for drafts and activity (planned follow-up).

### 4. Safe writes
- **Fresh reads before decisions:** the lead detail reads the status from the API before offering a status change, and sends it as `expected_status`; the draft review reads the draft fresh before approve/reject. Conflicts (`409`) are shown with what changed; nothing is retried or overwritten.
- **Deliberate approval:** Approve and Reject are disabled until the reviewer confirms they read the draft. A double click is harmless: the API rejects a second decision (`draft.already_decided`).
- **Ambiguous outcomes:** a timed-out create or import is reported as "outcome unknown, check before retrying". Re-uploading an imported file is rejected by the API as duplicates, so nothing can be imported twice.

### 5. Security and privacy
- The token is a `SecretStr` read from `.env`; it is never stored in session state, displayed or logged. Plain `http://` API URLs are accepted only for localhost.
- `.streamlit/config.toml` binds to `127.0.0.1` (Streamlit's default listens on all interfaces) and disables usage statistics (an external call).
- Every API string is HTML- or Markdown-escaped before display; message bodies are shown in read-only text areas.

### 6. Honest replacements for the design mockup
| Mockup element | Built instead | Why |
|---|---|---|
| "↑12% vs last week" on KPI cards | Leads added in the last 7 days; each tier's share | Scores/tiers are not stored over time |
| "Score calculated" activity events | Not shown | Scores are calculated on request, never recorded |
| CSV "100 imported / 15 duplicates / 5 failed" | "Imported N", or the rejected-rows table | Imports are all or nothing (ADR 0004) |
| Outbox Pending / Sent / Failed tabs | One list labelled "Simulated — nothing is sent" | Nothing is ever sent (ADR 0007) |
| Edit/delete lead icons, user avatar, Settings | Not shown | No such endpoints; one operator token, no accounts |

## Consequences

- The workflow is usable end to end from a browser, with no backend changes.
- Load cost grows with lead count (3 requests per lead); acceptable locally, documented, and capped.
- Possible backend follow-ups (each a separate, approved change): `status` in lead responses with a status filter; global drafts and activity endpoints; stored score snapshots (would enable week-over-week trends); a lead edit endpoint with history.

## Verification

- `dashboard/tests/`: the API client (success, timeouts, unreachable, 401, coded and uncoded errors, invalid responses, no retries, no token leaks), loading and paging, filters, counts, error wording and escaping. Network-free (fake HTTP session).
- `uv run --group ui mypy dashboard` (strict) and Ruff.
- Manual: `bash scripts/dev.sh`, then each page against a local database.
