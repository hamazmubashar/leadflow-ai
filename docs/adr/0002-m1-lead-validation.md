# ADR 0002: M1 lead validation and normalization

- Status: Accepted
- Date: 2026-10-14

## Context

M1 turns raw lead input, such as a parsed web form or CSV row, into a clean, normalized record. The constraints: standard library only, no new dependencies, Python 3.12, no database, API endpoints or LLM, and no network access. Pakistan (`PK`, +92) is the default region, following the original project plan.

## Decisions

### 1. A pure-Python `leads` package
`src/leadflow_ai/leads/` imports only the standard library (`re`, `unicodedata`, `dataclasses`, `enum`, `collections`) and does no I/O. `tests/test_leads_purity.py` enforces this. The public API is `validate_lead()`, `Lead`, `LeadSource`, `LeadValidationError`, `FieldError`, `NormalizationError`, `normalize_email()`, `normalize_phone()`, `clean_text()`, and the constants `DEFAULT_REGION` (`"PK"`) and `SUPPORTED_REGIONS` (`frozenset({"PK"})`).

### 2. Input rules
`validate_lead(data, *, default_region="PK")` takes a mapping of strings.

| Field | Required | Rules |
|---|---|---|
| `full_name` | yes | NFC, whitespace collapsed, no control characters, 1-100 characters, at least one letter or digit |
| `email` | email or phone | see 3 |
| `phone` | email or phone | see 4 |
| `company` | no | as `full_name`, at most 120 characters |
| `source` | no | `web_form`, `referral`, `event`, `import`, `other` (case-insensitive); default `other` |
| `notes` | no | trimmed, line breaks kept (CRLF/CR become LF), other control characters rejected, at most 1000 characters |

- `None`, `""` and whitespace-only values count as missing.
- Non-string values are `field.wrong_type`.
- Unknown keys are rejected rather than ignored.
- If both email and phone are missing, the error is `lead.contact_required`. An invalid one reports its own error instead.

### 3. Email normalization (structural only)
- Trimmed, then capped at 255 characters before any other work (254 plus one trailing domain dot).
- ASCII only: `email.unsupported_characters`.
- Exactly one `@`. The local part is a dot-atom of 1-64 characters.
- The domain has at least two labels of `a-z 0-9 -`, each 1-63 characters with no hyphen at the ends. The top-level domain is letters only or `xn--`.
- One trailing dot is dropped and the whole address is lowercased. The final result is at most 254 characters.
- Quoted local parts and IP-address domains are rejected.
- Gmail-style dots and `+tags` are kept as typed.

### 4. Phone normalization to E.164 (structural only)
- Trimmed input is capped at 32 characters.
- Allowed characters are ASCII digits, spaces and `- . ( ) /`, plus a leading `+`. Letters and extensions are rejected.
- `00` at the start is treated as `+`.
- **International numbers** have 8-15 digits, and the country code can't start with 0. For `+92`, the national number can't start with 0, and a number starting with `3` must be `3` followed by 9 digits (mobile).
- **National numbers** are accepted only for `PK` mobiles (`03XX-XXXXXXX`, normalized to `+923XXXXXXXXX`).
  - PK landlines and every other national format get `phone.national_format_unsupported` and must be entered with `+countrycode`. Their lengths vary by area, and per-area rules can't be tested reliably without numbering-plan data.
  - With `default_region=None`, only international input is accepted.
- "Structurally valid" never means the number exists, is assigned or is reachable.

### 5. Errors and privacy
- `validate_lead` collects every problem and raises one `LeadValidationError` with a tuple of `FieldError(field, code, message)`.
- Error codes are stable strings, listed below.
- Messages are fixed text and never include submitted values. Unknown keys aren't named in the error, because key names come from the input.
- `repr(Lead)` shows only the source and whether an email and phone are present.
- A non-mapping input or an unsupported region is a programming mistake and raises `TypeError` or `ValueError`.

Error codes:
- General: `field.required`, `field.wrong_type`, `field.too_long`, `field.invalid_characters`, `field.no_readable_text`, `field.unknown`
- Email: `email.invalid_format`, `email.unsupported_characters`
- Phone: `phone.invalid_characters`, `phone.invalid_length`, `phone.invalid_format`, `phone.invalid_pk_mobile`, `phone.national_format_unsupported`, `phone.region_required`
- Other: `source.invalid_choice`, `lead.contact_required`

### 6. Text that is only invisible characters
Names and company names must contain at least one Unicode letter (`L*`) or digit (`N*`), in any script. This rejects values made only of zero-width characters, direction marks, soft hyphens, lone combining marks, punctuation or emoji. Joiners such as ZWNJ/ZWJ between letters, as used in Urdu and Persian, are kept unchanged. Notes are exempt, so a note of just an emoji is allowed.

### 7. Duplicates
`Lead.identity_keys()` returns `{"email:<email>", "phone:<e164>"}` for whichever contacts are present. Leads sharing a key probably refer to the same contact. M1 does no storage, matching, fuzzy comparison or merging.

## Known limitations

- **Structural checks only.** There's no DNS, mailbox or carrier lookup. Valid-looking emails and numbers may not exist.
- **Phone coverage.** National format works only for PK mobiles. Other countries and PK landlines get only the 8-15 digit check, so plausible but wrong numbers can pass. Numbers shorter than 8 digits (a few tiny numbering plans) are rejected. PK mobiles are checked as `3` plus 9 digits, with no operator-prefix list.
- **Digits must be ASCII.** Urdu/Arabic-Indic and fullwidth digits in phone numbers are rejected, not converted.
- **Emails must be ASCII.** Internationalized addresses are rejected.
- **Values must be strings.** Callers reading CSV or spreadsheets must convert numbers to text first.
- **Unknown-field errors list the allowed fields** but not the offending keys.
- **Invisible characters at the edges of names are kept.** Invisible characters next to visible text, such as a leading zero-width space, aren't stripped, so two names that look the same can differ. Names aren't used in identity keys.
- **Duplicates are matched exactly on identity keys only.** Aliases and multiple numbers per person aren't detected.

## Consequences

The validation rules are explicit, deterministic and fully unit-tested, with no third-party dependencies. Broader phone coverage or internationalized email support would need a reviewed dependency, such as `phonenumbers` or `email-validator`, in a future milestone.
