# ADR 0005: M4 deterministic, rule-based lead scoring

- Status: Accepted
- Date: 2026-10-15

## Context

Operators need a simple, explainable way to decide which leads to contact first.

**Constraints:**
- Zero budget and local-first.
- No new dependencies, schema changes or migrations.
- Existing M0–M3 behavior and response contracts preserved.
- Scoring rules versioned in TOML.

**Available data:** `LeadRecord` holds only `full_name`, `email`, `phone`, `company`, `source`, `notes` and `created_at`. There's no job title, budget, company size or engagement data, and no conversion history to validate a model against.

The project version is now `0.4.0`.

## Decisions

### 1. What may influence the score
| Used | Reason |
|---|---|
| `source` | acquisition channel quality |
| email present | reachability |
| phone present | reachability |
| company present | a business-lead signal |
| email domain **not** on a configurable free-email list (`non_free_email_domain`) | a business-lead signal |

**Never used:**
- `full_name`: a proxy for ethnicity, gender and nationality.
- The phone country code: a national-origin proxy, which the PK default region would also skew.
- `notes`: unpredictable free text that may hold sensitive data.
- `created_at`: it would make scores change over time.

The engine's input `Protocol` exposes only the four fields that are used. A test confirms that names, notes and phone countries don't change scores.

**Naming:** the email signal is deliberately called `non_free_email_domain`, not `business_email`. Passing it doesn't verify any business affiliation. The default domain list is short and English-centric, and it lives in the rules file so operators can extend it.

### 2. Formula, range and tiers
- **Formula:** `score = source points + Σ points of matched signals`, using whole numbers only.
- **Range:** the rules must keep the highest source score plus all signal points at **100 or less**, so a score never needs capping. The range is 0 to `max_score`.

**Defaults:**

| Rule | Points |
|---|---|
| `source`: referral / event / web_form / import / other | 30 / 20 / 15 / 5 / 0 |
| `has_email`, `has_phone`, `has_company` | 15 each |
| `non_free_email_domain` | 25 |
| Maximum | 100 |

**Tiers:** `hot` at 70 or above, `warm` at 40 or above, otherwise `cold`.

**These are prioritization heuristics, not validated predictions of conversion.** The README and the response model say so.

**Determinism:** the engine (`scoring/engine.py`) is a pure function of the lead and the rules. No clock, randomness, I/O or environment is involved, and a test enforces the allowed imports.

**Explanations:** `factors` always has five entries in a fixed order: the source factor, then the four signals in rules order. Each is `{rule, matched, points}`. Rule names come from a fixed set, so explanations never contain lead values.

### 3. Versioned TOML rules
- **Defaults:** `src/leadflow_ai/scoring/default_rules.toml` is tracked in Git and ships inside the wheel. A test builds the wheel, offline when possible, and checks that the file is present, identical to the source and valid.
- **Format:**
  - `schema_version = 1` (the file format)
  - `rules_version` (identifies the ruleset; returned with every score)
  - `[tiers]`
  - `[points.source]`
  - `[points.signals]`
  - `[free_email_domains] domains = [...]`
- **Parsing:** standard-library `tomllib`. Validation collects **every** problem with its key path and never repeats the file's values. Rejected:
  - unknown or missing keys, at every level
  - a `schema_version` other than integer 1
  - a `rules_version` that isn't 1–64 characters of `[A-Za-z0-9._-]`
  - points that aren't whole numbers 0–100 (booleans and floats are rejected: the check is `type(v) is int`)
  - source keys that aren't exactly the `LeadSource` values
  - signal keys that aren't exactly the four known signals
  - a maximum possible score over 100
  - tiers other than exactly `hot`, `warm`, `cold`, or not satisfying `cold == 0 < warm < hot ≤ max possible score`
  - domains that aren't lowercase, well-formed and unique, or more than 500 of them
  - files over 64 KiB, not UTF-8, or not valid TOML

### 4. Loading and failure handling
- **When:** rules are loaded **once**, in `create_app`, before the app serves anything, and kept in `app.state`. The database is still not opened at startup.
- **Which file:** `LEADFLOW_SCORING_RULES_PATH`, which belongs to the API `Settings` only, so migrations don't need it, selects an override file. When it's unset, **empty or whitespace-only** (for example a blank `LEADFLOW_SCORING_RULES_PATH=` line in `.env`), the packaged defaults are used. Any other value, even `.` or a path with stray spaces, is an explicit override and must be valid.
- **Invalid explicit override:** if the file is missing or invalid, `ScoringRulesError` **stops startup**. There's **no silent fallback** to the defaults, which would quietly produce scores from rules the operator didn't choose.
- **Changes** take effect after a restart.

### 5. Calculated on request, not stored
Scores are calculated per request by `GET /api/v1/leads/{id}/score`, an authenticated route on the existing leads router. Unknown and malformed ids return 404 `lead.not_found`, and an un-migrated database returns 503, as on the other lead routes.

**Why not store them:**
- It needs no schema change or migration.
- Scores can never go stale against the current rules.
- No change to M2/M3 writes.
- The cost is negligible, since it's a pure function.

`rules_version` in each response records which ruleset produced the score. Create, get, list and import responses are unchanged and contain no score.

## Known limitations

- **Unvalidated weights.** There are no outcome data, so the default points are judgment, not evidence. They should be revisited if conversion data becomes available.
- **Coarse signals.** With so few inputs, many leads share the same score. There's no sorting or filtering by score.
- **Domain list.** `non_free_email_domain` uses an exact match against a short list. Subdomains of free providers and unlisted regional providers count as "non-free".
- **No punycode (IDN) domains in the list.** The rules validator accepts only letter-only top-level domains, so internationalized domains such as `mail.xn--p1ai` can't be listed, although M1 accepts such email addresses. Leads at those providers therefore always count as "non-free". Supporting them would mean allowing M1's `xn--` top-level-domain form in `scoring/rules.py`.
- **No history.** Scores aren't stored, so changing the rules changes every lead's score at once. Only `rules_version` records which rules applied to a given response.

## Consequences

Scoring is transparent, reproducible and cheap, and it's fully configurable without code changes. Storing scores, sorting by them, using engagement or outcome data, or any learned model would each need new decisions, about migrations, data collection and validation respectively.
