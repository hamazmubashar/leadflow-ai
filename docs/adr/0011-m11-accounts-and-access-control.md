# ADR 0011: M11 accounts and access control

- Status: Accepted
- Date: 2026-10-21

## Context

Up to 0.8.0, one shared operator token protected the API, and the dashboard held it, so anyone who could open the dashboard had full access and nothing recorded who did what. LeadFlow AI is presented to clients as production-minded, and the planned public deployment (M12) needs real sign-in. The project must stay free, local-first and dependency-light.

The project version is now `0.9.0`.

## Decisions

### 1. Two roles, accounts created on the command line
- `admin` can do everything; `viewer` can read everything and change nothing. More roles would add rules without a real use case.
- No sign-up page: `python -m leadflow_ai.manage_users` creates, activates, deactivates and lists accounts and sets passwords. Passwords are typed at a hidden prompt, twice, never as arguments. The tool checks the username before asking for a password.
- **Why not self-service sign-up or password reset:** both need email delivery, which the project deliberately doesn't have (nothing is ever sent).

### 2. Standard-library cryptography
- Passwords: `hashlib.scrypt` (n=2^14, r=8, p=1, random 16-byte salt), stored as `scrypt$n$r$p$salt$hash` so parameters can be raised later. Unknown users and unusable stored values still cost one full hash (a dummy), so timing doesn't reveal whether an account exists.
- Session tokens: `secrets.token_urlsafe(32)`, stored only as SHA-256. A database leak doesn't give working tokens.
- **Why not bcrypt or argon2:** both need a new dependency; scrypt is memory-hard and built in.

### 3. Server-side sessions, 8 hours, never extended
- `user_sessions` rows can be revoked: sign-out, a password change and deactivation end sessions at once. Expired rows are pruned after 7 days.
- **Why not JWTs:** a signed token can't be revoked before it expires without a server-side list anyway, and it needs a signing secret to manage.

### 4. One central authorization dependency
- Every protected router declares `dependencies=[Depends(authorize)]`: identify the caller (service key or session token), then require admin for anything but `GET`/`HEAD`/`OPTIONS`. Routes that write also take the caller (cached per request) to record it.
- Two tests make a forgotten route fail CI: the route audit checks every router depends on `authorize`, and an API test calls every served route without a token (expects `401`) and as a viewer (expects `403` for every change).

### 5. The service key stays, as an optional `system` account
- `LEADFLOW_OPERATOR_TOKEN` is now optional. When set, it acts as the reserved, inactive `system` account with admin rights, so existing scripts keep working and their changes are attributed honestly. When unset, only accounts work.
- The dashboard never sends it; it signs people in.

### 6. Audit columns: who did it (migration 0006)
- `lead_events.actor_id`, `lead_drafts.created_by_id` and `decided_by_id`, and `workflow_runs.started_by_id` reference `users`. Triggers refuse an unknown account and any later change.
- History from before 0.9.0 keeps `NULL` (unknown). **We decided not to backfill `system`:** it would claim knowledge we don't have.

### 7. Sign-in abuse: lockout after 3 failures, without revealing accounts
- Requirement: lock sign-in for 15 minutes after the third consecutive failure, show a countdown, and enforce it on the server.
- Conflict: a countdown shown only for real accounts would reveal which usernames exist. **Decision: lock the submitted username, real or not** (`UsernameLockout`, in memory). The third failure answers `429 auth.locked` with `retry_after_seconds`; later attempts are refused without a password check until it ends; a success resets the count. Attempts are counted *before* the password check, so parallel requests can't exceed three.
- The database lockout on real accounts uses the same threshold (3, 15 minutes) and survives restarts, but answers with the generic `401` (it can't show a countdown without revealing the account).
- A per-address limit (10 failed attempts per 15 minutes) slows guessing across many usernames.
- Accepted limits (see the security review): the in-memory lock is per process and resets on restart; anyone can lock a known username for 15 minutes; the dashboard is one address for the per-address limit.

### 8. Dashboard sign-in
- Only the sign-in page is shown until a tab signs in. The token lives in Streamlit's per-tab session state, so a refresh signs out (accepted). A rejected token signs the tab out with an explanation.
- Viewers get a read-only banner and disabled change controls. This is presentation only; the API enforces the role.
- The sign-in page has its own scoped styling (brand panel and card) and shows the API's lockout countdown, updating every second.

## Consequences

- Every change in the database is attributable to an account or to `system`.
- The no-password public demo requested for the portfolio is **not** a viewer login on the real database. It is planned for M12 as a private, disposable sandbox per visitor with fictional data, so visitors can upload and change things without seeing or touching anyone else's data.
- Deploying behind a proxy (M12) needs forwarded client addresses and HTTPS, and running several API processes would need a shared store for the lockout and rate limit.
