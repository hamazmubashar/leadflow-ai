# LeadFlow AI

- **M0, foundation:** a FastAPI service with a health endpoint, typed configuration, reusable operator authentication, and quality tooling.
- **M1, lead validation:** pure-Python validation and normalization of lead input.

There's no database, dashboard, or LLM yet.

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

## Lead validation

The `leadflow_ai.leads` package validates and normalizes raw lead data. It's pure Python with no network access.

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

Error messages never include the submitted values. Checks are structural only: a valid-looking email or phone number may still not exist. See [ADR 0002](docs/adr/0002-m1-lead-validation.md) for the full rules and limitations.

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
src/leadflow_ai/   application package (leads/: M1 validation)
tests/             unit and integration tests
docs/adr/          architectural decision records
```
