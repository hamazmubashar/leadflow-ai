# LeadFlow AI

Milestone **M0**: the project foundation. It has a FastAPI service with a health endpoint, typed configuration, reusable operator authentication, and quality tooling. M0 has no database, lead logic, dashboard, or LLM.

## Requirements

- Python 3.12 (pinned in `.python-version`)
- [uv](https://docs.astral.sh/uv/) 0.13+

## Setup

```sh
uv sync                      # installs runtime + dev dependencies from uv.lock
cp .env.example .env         # .env is git-ignored
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into .env
```

## Run

```sh
uv run uvicorn --factory leadflow_ai.main:create_app --reload
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"0.1.0"}
```

The app won't start if `LEADFLOW_OPERATOR_TOKEN` is missing or shorter than 32 characters.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `LEADFLOW_OPERATOR_TOKEN` | yes | Bearer token for operator endpoints, at least 32 characters |

The token is held as a `SecretStr`, so it's masked in `repr`, logs, and serialized output.

## Authentication

Protect a route by adding the `OperatorAuth` dependency:

```python
from leadflow_ai.auth import OperatorAuth


@app.get("/admin/thing")
def thing(_: OperatorAuth) -> dict[str, str]: ...
```

Requests must send `Authorization: Bearer <token>`. The token is compared using `hmac.compare_digest`.

## Quality checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest                # includes coverage, fails under 90%
```

The tests make no network calls.

## Layout

```
src/leadflow_ai/   application package
tests/             unit and integration tests
docs/adr/          architectural decision records
```
