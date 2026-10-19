# Security and privacy review (M10)

- Version reviewed: `0.8.0`
- Date: 2026-10-19
- Scope: the REST API, the database and migrations, the Streamlit dashboard, the optional local-model client, configuration, the repository and CI.
- Method: a code and configuration review against the controls below, each backed by an automated test where one exists. This is a self-review by the developer, not an independent audit or a penetration test.

LeadFlow AI is designed as a **single-operator tool on one computer**. Most controls assume that: everything binds to `127.0.0.1`, and one operator token protects the API. It is not hardened for a shared server or the internet (see [Out of scope](#out-of-scope)).

## Controls in place

| Area | Control | Evidence |
|---|---|---|
| Authentication | Every `/api/*` route requires the operator bearer token; a test fails if any route lacks it. Tokens are compared in constant time (`hmac.compare_digest`) and must be at least 32 characters. | `tests/test_api_leads.py` (route audit), `tests/test_auth.py`, `tests/test_config.py` |
| Check order | Body size (413) → token (401) → content type (415) → body (422), so unauthenticated clients learn nothing from parsing errors. | `tests/test_api_limits.py`, `tests/test_api_leads.py` |
| Network exposure | The API (`scripts/dev.sh`) and the dashboard (`.streamlit/config.toml`) listen on `127.0.0.1` only. No CORS is configured, so other websites can't read API responses from a browser. | config files; README |
| No outbound communication | No email, SMS, webhook or HTTP client code exists, and a test fails if any module imports a network library. The one named exception is the local-model client. The outbox is simulated. | `tests/test_drafts_templates.py` |
| Local model (M9) | Off by default. The URL must be `http://` on `localhost`, `127.0.0.1` or `::1`; proxy variables are ignored and redirects refused; responses are size-capped. Notes are sent as delimited JSON data, model output passes deterministic checks, and every AI draft waits for human review with a visible warning. | `tests/test_config_llm.py`, `tests/test_assist_ollama.py`, `tests/test_assist_prompt.py`, ADR 0010 |
| Human approval | Only a person can approve a draft; the workflow and the AI service only create `pending_review` drafts. Database triggers make drafts and outbox items immutable and allow an outbox item only for an approved draft. | `tests/test_workflows_service.py`, `tests/test_ai_drafts_service.py`, migration tests |
| Injection | All queries go through SQLAlchemy with bound parameters; raw SQL appears only in reviewed migrations with constant text. Draft templates use concatenation, never `str.format`. The dashboard escapes every API value before rendering HTML or Markdown. | code review; `dashboard/tests` |
| Privacy in errors and logs | Errors carry fixed codes and messages, never submitted values, notes, draft text or model output. Bound SQL parameters are hidden (`hide_parameters=True`). The application writes no logs of its own; uvicorn's access log records paths, which contain ids only. | `tests/test_api_*.py`, `tests/test_ai_drafts_service.py` (log check) |
| Secrets | The token is a `SecretStr`, never shown by the dashboard. `.env` and database files are git-ignored and have never been committed (history checked on 2026-10-19). `.env.example` contains no secret. | `.gitignore`; `git log --all -- .env '*.db'` |
| Resource limits | Request bodies are capped (16 KiB, 1 MiB for CSV); list endpoints are paged; workflow runs are capped at 500 leads; one AI generation at a time. | `tests/test_api_limits.py`, `tests/test_api_assist.py` |
| Supply chain | Dependencies are locked in `uv.lock` and CI installs with `--locked`. CI runs with read-only permissions and does not persist checkout credentials. | `.github/workflows/ci.yml` |

## Residual risks (accepted, with mitigations)

1. **The dashboard has no login of its own.** It holds the operator token and trusts anyone who can open `http://127.0.0.1:8501`. That is acceptable for a single-user computer. Don't forward or expose port 8501, and lock your screen.
2. **Browser-based attacks on local services** (for example DNS rebinding against `localhost`) are not specifically defended against beyond Streamlit's own XSRF protection and the API's bearer token. The risk is low for a local tool, but it is not zero [Guessing].
3. **Data at rest is not encrypted.** `leadflow.db` and `.env` are plain files. Use full-disk encryption (FileVault on macOS) and `chmod 600 .env leadflow.db*` on shared machines.
4. **One shared token, no per-user accounts or audit of who did what.** Status changes and decisions are recorded, but not by whom.
5. **AI drafts can contain invented or inappropriate content.** In testing, the model promised a meeting time and invented a product. Only human review prevents this; the dashboard warns on every AI draft.
6. **The public OpenAPI docs** (`/docs`, `/openapi.json`) describe the API. They contain no data, and the server is local only.
7. **Commit metadata contains the author's personal email address.** The repository is private and shared on request; future commits use a GitHub no-reply address. History was deliberately not rewritten.

## Dependency vulnerability scan

Run from the project root (free; needs internet):

```sh
uv export --all-groups --no-hashes --no-emit-project -o /tmp/leadflow-requirements.txt
uvx pip-audit -r /tmp/leadflow-requirements.txt --no-deps --disable-pip
```

Result on 2026-10-19: _to be filled in when the scan is run._

## Out of scope

Deployment to a server or the internet, multi-user access, TLS, rate limiting per client, backups, and an external penetration test. Each would need its own design before LeadFlow AI is used anywhere other than one person's computer.
