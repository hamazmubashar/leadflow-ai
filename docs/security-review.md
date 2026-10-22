# Security and privacy review (M11–M12)

- Version reviewed: `0.9.0`
- Date: 2026-10-23, M12 public deployment (previous: 2026-10-21 for M11; 2026-10-19 for `0.8.0`)
- Scope: the public deployment (Docker, Caddy, the server, the demo sandboxes), the REST API, the database and migrations, the Streamlit dashboard, accounts and sign-in, the optional local-model client, configuration, the repository and CI.
- Method: a code and configuration review against the controls below, each backed by an automated test where one exists. This is a self-review by the developer, not an independent audit or a penetration test.

LeadFlow AI runs either **on one computer** (everything binds to `127.0.0.1`) or, since M12, **on one public server** behind HTTPS, with a no-password demo. It has personal accounts with roles, so several people can share it with separate, audited access.

## Controls in place

| Area | Control | Evidence |
|---|---|---|
| Accounts | Personal accounts created only on the command line (no sign-up). Passwords are 12–128 characters, not common and not containing the username, and are stored as scrypt hashes (n=2^14, r=8, p=1, random 16-byte salt). The reserved `system` account can never sign in. | `tests/test_accounts_rules.py`, `tests/test_accounts_service.py`, `tests/test_manage_users.py` |
| Sessions | Sign-in returns a random 256-bit token. Only its SHA-256 hash is stored. Sessions last 8 hours, are never extended, and end at once on sign-out, password change or deactivation. | `tests/test_auth.py`, `tests/test_api_auth.py` |
| Authorization | One central dependency on every protected router: identify the caller, then require the admin role for anything but `GET`/`HEAD`/`OPTIONS` (viewers get `403 auth.forbidden`). Tests fail if any served route is reachable without a token, or if a viewer can change anything. | `tests/test_api_leads.py` (route audit), `tests/test_api_auth.py` |
| Sign-in abuse | Every failure gets the same answer after the same password-hashing work, so a response never reveals whether a username exists. **3 consecutive failures lock a username for 15 minutes** (`429 auth.locked`), real or not; attempts are counted before the password check, so parallel guesses can't exceed the limit. Real accounts are also locked in the database (survives restarts). An address gets at most 10 failed attempts per 15 minutes. | `tests/test_login_limit.py`, `tests/test_api_auth.py`, `tests/test_accounts_service.py` |
| Audit trail | Lead events, draft creation and decisions, and workflow runs record the acting account (`system` for the service key). Database triggers refuse an unknown account and any later change to who did it. | `tests/test_audit_actors.py`, `tests/test_db_accounts_migration.py` |
| Service key | `LEADFLOW_OPERATOR_TOKEN` is optional (scripts only), at least 32 characters, compared in constant time, and recorded as `system`. The dashboard never sends it. | `tests/test_config.py`, `tests/test_auth.py` |
| Dashboard | Shows only the sign-in page until signed in. The token lives in the tab's server-side session state (no cookie, URL or log); the password is not kept. Viewers see a read-only banner and disabled change controls. A rejected session returns the tab to sign-in. | `dashboard/tests/test_session.py`, `dashboard/tests/test_api_client.py` |
| Check order | Body size (413) → token (401) → role (403) → content type (415) → body (422), so unauthenticated or unauthorized clients learn nothing from parsing errors. | `tests/test_api_limits.py`, `tests/test_api_leads.py`, `tests/test_api_auth.py` |
| Network exposure | The API (`scripts/dev.sh`) and the dashboard (`.streamlit/config.toml`) listen on `127.0.0.1` only. No CORS is configured. | config files; README |
| No outbound communication | No email, SMS, webhook or HTTP client code exists, and a test fails if any module imports a network library (the one named exception is the local-model client). The outbox is simulated. | `tests/test_drafts_templates.py` |
| Local model (M9) | Off by default; loopback `http://` only; proxies ignored, redirects refused, responses size-capped. Notes are delimited data; output passes deterministic checks; every AI draft waits for human review with a visible warning. | `tests/test_config_llm.py`, `tests/test_assist_*.py`, ADR 0010 |
| Human approval | Only a person (an admin) can approve a draft; workflows and the AI service only create `pending_review` drafts. Triggers make drafts and the outbox immutable. | `tests/test_workflows_service.py`, `tests/test_ai_drafts_service.py`, migration tests |
| Injection | All queries use SQLAlchemy with bound parameters; raw SQL only in reviewed migrations with constant text. The dashboard escapes every API value before rendering HTML or Markdown. | code review; `dashboard/tests` |
| Privacy in errors and logs | Errors carry fixed codes and messages, never submitted values, passwords, tokens, notes or model output. Bound SQL parameters are hidden. The application writes no logs of its own; uvicorn's access log records paths, which contain ids only. | `tests/test_api_*.py` |
| Secrets | `.env` and database files are git-ignored and have never been committed (history checked on 2026-10-19). `.env.example` contains no secret. | `.gitignore`; `git log --all -- .env '*.db'` |
| Public demo (M12) | Each visitor gets a private copy of a fictional dataset in its own SQLite file; demo tokens never reach the main database and main tokens never reach a sandbox. Sandboxes are deleted on sign-out, after 2 hours (a sweep runs every 5 minutes) and on restart. Limits: 30 at once, 5 new per address per hour, 300 leads, 100 rows per import, no AI. | `tests/test_demo_sandboxes.py` |
| Server and containers (M12) | Only Caddy is published (80/443) with automatic HTTPS, HSTS, a content security policy and other security headers. Containers run as a non-root user with a read-only filesystem, all Linux capabilities dropped and `no-new-privileges`. The server allows SSH keys only (no passwords, no root login), applies security updates automatically, and has two firewalls (cloud security list and the server's own). | `docker-compose.yml`, `deploy/Caddyfile`, `deploy/server-setup.sh`; CI `docker` job |
| Password help (M12) | "Forgot password?" shows administrator-assisted instructions only. It has no form and no endpoint, so it reveals nothing about which accounts exist, and there is no reset token to steal. | `dashboard/tests/test_sign_in_page.py` |
| Resource limits | Request bodies are capped (16 KiB, 1 MiB for CSV); lists are paged; workflow runs are capped at 500 leads; one AI generation at a time; the rate-limit and lockout tables are capped at 10,000 entries. | `tests/test_api_limits.py`, `tests/test_login_limit.py` |
| Supply chain | Dependencies are locked in `uv.lock`; CI installs with `--locked`, runs with read-only permissions and does not persist checkout credentials. | `.github/workflows/ci.yml` |

## Residual risks (accepted, with mitigations)

1. **The username lockout lives in the API's memory.** It survives page refreshes and browser restarts, not an API restart, and each API process has its own. The database lockout on real accounts (same threshold) survives restarts but answers with the generic error, without a countdown. Running more than one API process (M12) needs a shared store.
2. **Anyone who knows a username can lock it out for 15 minutes** by failing 3 times. This is inherent in account lockout. An admin can clear the database lock with `set-password`; restarting the API clears the in-memory one.
3. **The address rate limit trusts the proxy's forwarded address.** Behind the bundled Caddy (M12) each visitor is limited separately. Run locally without a proxy, all dashboard users share `127.0.0.1`, so 10 failed sign-ins briefly block everyone. Never set `LEADFLOW_TRUST_PROXY=true` without a proxy that overwrites `X-Forwarded-For`.
4. **Refreshing the dashboard signs you out.** The session token is deliberately kept only in the tab's memory; the API session simply expires after 8 hours.
5. **No multi-factor authentication** and no self-service password reset. Accounts are few and managed by an admin on the command line.
6. **Inside the server, containers talk over plain HTTP** on a private Docker network that is not published. Everything from the internet arrives over HTTPS at Caddy.
7. **Data at rest is not encrypted.** `leadflow.db` and `.env` are plain files. Use full-disk encryption (FileVault on macOS) and `chmod 600 .env leadflow.db*` on shared machines.
8. **History from before 0.9.0 has no recorded actor** (stored as empty, shown as unknown). It is deliberately not guessed.
9. **AI drafts can contain invented or inappropriate content.** In testing, the model promised a meeting time and invented a product. Only human review prevents this; the dashboard warns on every AI draft.
10. **The sign-in page styling targets Streamlit's internal element names** (version 1.66, locked). An upgrade could drop some styling; it cannot change any security behaviour.
11. **Commit metadata contains the author's personal email address.** The repository is private and shared on request; newer commits use a GitHub no-reply address. History was deliberately not rewritten.
12. **Demo sandboxes and lockouts live in the API's memory.** An API restart deletes all open demos (visitors simply start a new one) and resets the in-memory lockout.
13. **Single server, backups on the same disk.** Nightly backups (7 kept) protect against mistakes, not against losing the server. Copy a backup off the server regularly.
14. **Free-tier reclamation.** Oracle may reclaim an idle Always Free server; see the README's free-tier notes.
15. **The dashboard's content security policy allows `unsafe-inline` and `unsafe-eval`,** which Streamlit needs. Mitigated by escaping every API value before rendering.

## Dependency vulnerability scan

Run from the project root (free; needs internet):

```sh
uv export --all-groups --no-hashes --no-emit-project -o /tmp/leadflow-requirements.txt
uvx pip-audit -r /tmp/leadflow-requirements.txt --no-deps --disable-pip
```

Last result, on 2026-10-19 (0.8.0): **no known vulnerabilities** in the 65 locked packages. M11 added no dependencies (password hashing uses the standard library's `hashlib.scrypt`). This is a point-in-time result; rerun it before each release.

## Out of scope

A shared store for rate limits and lockouts (needed only for more than one API process), multi-factor authentication, self-service password reset (needs email delivery), off-server backups, and an external penetration test or automated scan (planned: OWASP ZAP, pip-audit and gitleaks in CI).
