# Security and privacy

This document describes controls that are implemented and tested. It does not claim
production authentication or compliance certification.

## Threat controls

| Threat | Control | Code location | Test/evidence |
|---|---|---|---|
| Path traversal or unsafe upload name | Extension, size, containment, internal `upload_<n>.csv` names | `app/api/routes.py`, `app/ingestion/loader.py` | `test_path_containment`, bad-upload API test |
| Oversized input | Streaming upload limit and `MAX_UPLOAD_MB` | `app/api/routes.py` | `test_api_rejects_bad_uploads` |
| Malformed CSV | All-VARCHAR reader, structural reject capture | `app/ingestion/csv_reader.py` | edge-file pipeline test |
| Silent cleaning damage | Dry-run plan, risk/status metadata, validation before promotion | `app/cleaning`, `app/services/pipeline.py` | invariant and failed-validation tests |
| Ambiguous dates | Column remains text; step is `skipped_needs_review` | `app/cleaning/transformations.py`, `policy.py` | `test_ambiguous_date_is_skipped_not_interpreted` |
| SQL injection | Typed plan, parameter values, catalog identifiers only; no raw SQL field | `app/query/schemas.py`, `validator.py`, `compiler.py` | compiler and malicious-plan tests |
| Unsafe join fan-out | Relationship uniqueness/cardinality and unsafe status | `app/relationships`, `join_policy.py` | duplicate-product refusal test |
| Planner tool abuse | Four-tool allow-list | `app/agents/planner_tools.py` | injection suite tool-abuse attack |
| Prompt injection in cells | Sanitization, truncation, `<data>` fencing, untrusted-data instruction | `app/common.py`, `planner_tools.py`, `prompts.py` | injection suite cell attacks |
| Narrator hallucination | Fact IDs and numeric/unit claim verification; template fallback | `app/evidence/claim_verifier.py`, `agents/narrator.py` | narrator verifier tests and three injection attacks |
| Cross-owner access | Owner comes from server/header mapping, repositories require owner | `app/api/routes.py`, `storage/repositories.py` | isolation integration tests |
| Secret leakage | Keys are not included in prompts or cassette bodies; trace redaction | `app/observability/tracer.py`, `common.py` | `test_secrets_are_redacted_from_traces` |
| Expensive query | Plan limit, DuckDB memory/thread settings, read-only connection, interrupt timeout | `app/query/validator.py`, `executor.py` | executor limit tests |
| Stale model response | Cassette key includes full canonical request, model, and prompt version | `app/llm/cassette_provider.py` | stale cassette test |

## Data sent to OpenRouter or another provider

The planner receives:

* a question trimmed to 500 characters;
* bounded schema metadata: logical dataset/column IDs, inferred types, roles, null and
  distinct counts;
* relationship metadata and registered metric definitions;
* structured conversation state and recent question strings;
* sanitized distinct values when the planner asks for them;
* computed facts and a maximum 20-row result preview for the optional narrator.

`PlannerTools` omits PII roles and email/phone-like values from distinct results. Values are
normalized, control characters are removed, values are bounded, and returned data is fenced
as `<data>`. The application never sends raw CSV files, arbitrary raw rows, SQL text,
Python, API keys, owner IDs, or the authority to choose dataset versions.

This is minimization, not a guarantee that a provider will not retain data. The live provider
path is opt-in and should be configured according to the provider's data policy.

## Injection defenses

The committed suite runs 15 attacks over six vectors:

* cell content to tool result;
* poisoned schema/header;
* question injection;
* malicious model plans;
* disallowed tool requests;
* narrator output.

The evaluator run on 2026-11-16 at commit
`7d4aa08d0673a03e5d27b667c2653b0632a5bdfa` reported 15 attacks, 0 successes, ASR 0.0,
and no `blocked_at: none`. Blocks occurred at `prompt_fencing`, `planner_output_schema`,
`validator`, `tool_allowlist`, and `claim_verifier`. The suite treats a canary appearing in
the plan, SQL parameters, answer, or evidence as success.

## Isolation and authentication boundary

The API uses `X-Owner-Id` as a development stand-in. If `API_TOKENS` is configured, the
middleware maps bearer tokens to server-owned owners and rejects unknown tokens with 401.
The header is not a production identity system. Every repository read/write still receives
the selected owner and rejects foreign resources as `ISOLATION_VIOLATION`.

## Query and storage limits

The default settings are 5,000,000 input rows, 100 MB upload size, 15 seconds query timeout,
1 GB DuckDB memory, and two DuckDB threads. Plans are bounded to limits from 1 through 1,000.
The executor uses a separate read-only DuckDB connection with external access disabled and
interrupts an overdue query. SQLite metadata is owner-scoped; the DuckDB run database is
under the owner/run artifact directory.

## Secrets handling

`.env.example` contains placeholders. Real `.env` files are ignored and must not be copied
into an artifact directory or pasted into an issue. Cassettes store response text and request
hashes, not API keys or request bodies. The current committed root cassettes are synthetic
fixtures, so they do not prove live-provider privacy behaviour. A separate live smoke run
made 10 authenticated chat requests; it did not inspect or retain provider payloads.
