# ADR 0001: M0 foundation decisions

- Status: Accepted
- Date: 2026-10-14

## Context

M0 sets up the LeadFlow AI codebase without any product features. Constraints: no paid services, Python 3.12 only, no database/lead logic/dashboard/LLM, no credentials in Git, tests must run offline once dependencies are installed.

## Decisions

1. **Packaging and tooling: uv, `src/` layout, hatchling.** `uv.lock` pins every dependency. `requires-python = "==3.12.*"`. The `src/` layout keeps tests running against the installed package rather than the working tree.
2. **Web framework: FastAPI** with an app factory (`create_app`). There's no module-level app, so importing the package never needs secrets, and tests can inject settings.
3. **Configuration: pydantic-settings.** Variables use the `LEADFLOW_` prefix and are read from the environment or a git-ignored `.env`. Settings are frozen. The app fails fast at startup on invalid configuration.
4. **Operator token.** It's required, at least 32 characters, and stored as `SecretStr` so it's masked in reprs and serialization. Only `.env.example` with a placeholder is committed.
5. **Authentication: static bearer token** via a reusable FastAPI dependency (`OperatorAuth`). It compares bytes with `hmac.compare_digest` and returns a uniform `401` with `WWW-Authenticate: Bearer` for every failure mode.
6. **`/health` is unauthenticated** and returns only status and version.
7. **Quality gates.** Ruff (lint + format, including the bandit `S` rules), mypy `--strict` with the pydantic plugin, and pytest with branch coverage (minimum 90%). CI runs the same commands on GitHub Actions' free tier.

## Consequences

- One shared operator token, with no per-user identity or rotation tooling. That's acceptable for M0 and should be revisited before multiple operators exist.
- Running the app needs `--factory` (`uvicorn --factory leadflow_ai.main:create_app`).
