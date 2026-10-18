# ADR 0010: M9 AI-assisted drafts (optional, local model only)

- Status: Accepted
- Date: 2026-10-18

## Context

M6 drafts come from fixed templates that use only the lead's name and company. Operators asked for drafts that read more naturally and can draw on the lead's notes. The project must stay free and local-first, and M6's rules must keep holding: a person approves every draft, and nothing is ever sent.

The notes are the only field that makes a draft more relevant, and they are also the riskiest field. They are written for internal use ("chose another vendor", "no budget this year") and could contain text that reads like instructions. Giving a model the notes is therefore a deliberate change to M6's rule that notes never reach a message. It was approved with the safeguards below.

The project version is now `0.8.0`.

## Decisions

### 1. Local model only, off by default
- The model runs in Ollama on the operator's own machine. `LEADFLOW_LLM_URL` is unset by default (feature off), and only `http://` URLs on `localhost`, `127.0.0.1` or `::1` are accepted; anything else stops startup.
- The client (`assist/ollama.py`) uses only the standard library, ignores proxy environment variables, refuses redirects and caps the response size. It is the single module allowed to import network libraries, and a test fails if that exception grows.
- **Why not a hosted API:** it would cost money and send lead notes to a third party.

### 2. Only the body is generated
- Subject and recipient come from the M6 template; the greeting and sign-off are fixed text added by code. The model writes one to three paragraphs in between.
- The lead's name, company and notes are passed as JSON inside a delimited block, with `<` and `>` escaped so no value can close it. The system prompt says the block is data, never instructions.

### 3. Deterministic output checks, and an honest limit
- The text is rejected (nothing saved, `llm.invalid_output`) if it contains a link, an `@`, a phone-like number, markup or placeholder characters, control characters, too many paragraphs, or is too short or too long. A truncated answer is rejected too.
- **Prompt instructions are not a guarantee.** In the first live test, `qwen2.5:3b` asked *"How does tomorrow afternoon work for you?"*, a time commitment the prompt forbids. We decided **not** to add heuristic date or promise detection: words like "tomorrow" or weekday names also occur in acceptable text (the M6 template says "next week"), so such a filter would reject good drafts while still missing paraphrases. The control is human review, made explicit in the UI (decision 6).

### 4. Provenance in the database (migration 0005)
- `lead_drafts` gains `generator` (`template` or `local_llm`), `model` and `prompt_version`, added with `ALTER TABLE ... ADD COLUMN` (no table rebuild; existing drafts become `template`, which is true).
- An insert trigger allows exactly two shapes, and a `local_llm` draft can only be inserted as `pending_review`. The content-immutability trigger is recreated to cover the new columns.
- Downgrade is refused while any `local_llm` draft exists, so generated drafts can never be silently relabelled as template drafts.

### 5. No lock while generating; one generation at a time
- The service ends its read transaction before calling the model, then re-checks eligibility and the one-pending-draft rule under SQLite's write lock before saving. A lead closed, or a draft created, during generation wins.
- The API allows one generation per process; a second request gets `503 llm.busy` immediately instead of queueing minute-long calls on a laptop.
- Every failure maps to a fixed `llm.*` code and message. No prompt, note or model text appears in an error or a log.

### 6. Dashboard: offered only when enabled, reviewed like any draft
- The **Draft with local AI** button on Lead Detail appears only when `GET /api/v1/assist/status` says the feature is enabled. That endpoint never contacts the model and never exposes the URL.
- The click is recorded in a callback, so the run that sends the request already shows the button disabled, with a "this can take up to a minute" message. The request is handed out once (`take_unsent`); an interrupted page run never resends it, and nothing is retried automatically. The real duplicate protection stays in the API (one pending draft per lead, `llm.busy`).
- The dashboard waits longer (330 s) than the API's maximum model timeout (300 s), so the API decides when generation failed. If the dashboard does stop waiting, it says the draft may or may not exist and to check before trying again.
- Every AI-assisted draft shows its model and prompt version and the persistent warning *"Written by a local AI model from internal notes. Check for internal remarks, dates or promises before approving."*, whatever its status. Approval, rejection, the audit history and the simulated outbox are unchanged; rejecting never creates an outbox item.

### 7. The M8 workflow stays template-only
- The workflow keeps calling the M6 staging function without a generated body, so its results stay deterministic and `WORKFLOW_VERSION` is unchanged. A test checks it never imports the model code.

## Consequences

- Drafts can use the notes, at the cost of non-deterministic text that a person must read carefully. The feature helps with wording, not judgement.
- A 3B model on a laptop gives fluent but fairly generic text, and generation can take up to about a minute. Larger models work through `LEADFLOW_LLM_MODEL` at the cost of speed.
- CI never needs Ollama: tests use a fake model and a loopback test server.

## Verification

- `tests/test_db_draft_provenance_migration.py`: columns, defaults, the provenance and immutability triggers, exact downgrade, refused downgrade, all-or-nothing upgrade, offline SQL.
- `tests/test_assist_prompt.py` and `tests/test_assist_ollama.py`: prompt delimiting, every output check, and the client against a loopback server (timeouts, 404, redirects refused, proxies ignored, oversized or truncated answers).
- `tests/test_config_llm.py`: off by default, loopback-only URLs.
- `tests/test_ai_drafts_service.py` and `tests/test_api_assist.py`: pending only, no transaction during generation, races, fixed error responses, `llm.busy`, auth, nothing private in errors or logs.
- `tests/test_drafts_templates.py`: the single network-library exception.
- `dashboard/tests/test_assist.py` and client tests: when the button is offered, the warning and provenance, error mapping, and the send-once guard.
- A manual live run against `qwen2.5:3b` produced a `pending_review` draft with correct provenance and no outbox row. No timing benchmark was recorded.
