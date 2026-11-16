# Design decisions

## 1. Typed `QueryPlan` instead of generated SQL

**Context:** Planner output must be executable without allowing arbitrary SQL or Python.

**Options:** text-to-SQL, a SQL allow-list, or a typed plan compiled by code.

**Choice:** `QueryPlan` and `PlanDelta` are Pydantic models with `extra="forbid"`. The
compiler owns SQL generation.

**Consequences:** The validator can check columns, metrics, joins, limits, stage, scope,
and PII before execution. The trade-off is less expressive queries and a deliberately small
intent vocabulary.

## 2. Unattended cleaning skips ambiguous dates

**Context:** `01/02/2024` can represent different dates.

**Options:** choose a locale, ask for a global setting, or preserve the source text.

**Choice:** preserve the column as text and mark `parse_date_iso` as
`skipped_needs_review`; interactive approval is a separate rerun.

**Consequences:** Date questions may clarify or be unsupported, but the unattended path
does not silently corrupt dates. The policy is in `app/cleaning/policy.py` and
`app/services/pipeline.py`.

## 3. Normalize before deduplication

**Context:** Rows that differ only by whitespace or case are duplicates after cleaning.

**Options:** deduplicate raw rows, deduplicate after each transformation, or normalize all
registered fields and deduplicate once.

**Choice:** fixed transformation order followed by exact deduplication.

**Consequences:** Row accounting matches the clean representation and the second clean pass
is idempotent. It also means the plan and validation must record the order and duplicate
count.

## 4. Relationship IDs instead of free-form joins

**Context:** Retail facts span orders, products, returns, inventory, and stores; a naive
join can multiply facts.

**Options:** let the model write join conditions, infer joins at compile time, or require
discovered relationship IDs.

**Choice:** plans contain relationship IDs. Discovery records uniqueness, overlap,
cardinality, and status; `join_policy.py` allows only safe paths.

**Consequences:** unsafe duplicate dimensions are refused instead of answered. The
fact-CTE compiler preserves zero-return categories and reduces fan-out risk, at the cost of
refusing some queries that a human might resolve manually.

## 5. Template answer first, narrator second

**Context:** A fluent answer is not evidence that its numbers are correct.

**Options:** always ask an LLM to write prose, use templates only, or permit a narrator
behind a verifier.

**Choice:** render facts with deterministic templates, then optionally prepend narrator text
only when fact IDs, numbers, units, and comparisons verify.

**Consequences:** A provider outage still gives a grounded answer. Narration can improve
readability, but a failed narrator always falls back to the template.

## 6. Replay cassettes plus fallback

**Context:** Reviewers need a no-key deterministic run, while the AI track needs a real
provider seam.

**Options:** require a live key, use mocks only, or support replay/live/fallback paths.

**Choice:** `CassetteProvider` keys the full canonical request; `auto` tries replay, then
live provider, then fallback. The root committed cassettes are synthetic fixtures and are
labelled as such.

**Consequences:** Evaluation is reproducible and free. It does not measure live OpenRouter quality
until a key-backed cassette is recorded, and prompt/model changes intentionally invalidate
old cassettes.

## 7. Server-owned owner and dataset scope

**Context:** An LLM or request body must not choose another user's data.

**Options:** trust request fields, pass owner/version through the plan, or bind them from a
server session.

**Choice:** `ExecutionContext` supplies owner, active versions, stage, and graph version.
Repositories require owner IDs and the validator checks the active catalog.

**Consequences:** The current `X-Owner-Id` header is only a development stand-in for real
authentication, but a foreign owner cannot read an existing run, session, evidence, trace,
or job through repository methods.

## 8. Pandas cleaning and DuckDB analytics

**Context:** Cleaning needs clear, testable transformations while analytics should avoid
loading all query results into application memory.

**Options:** pandas for everything, DuckDB for everything, or pandas for deterministic
cleaning and DuckDB for persisted analytics.

**Choice:** pandas frames are used for cleaning; typed/raw tables are written to DuckDB and
queries execute through a read-only connection.

**Consequences:** The implementation is straightforward and tested at 100k rows, but
cleaning still materializes frames in Python. A production version would need chunked
cleaning or a larger worker boundary.

## Likely reviewer questions

### Why a typed QueryPlan instead of generated SQL?

Because `QueryPlan` makes the validator's security and semantic checks explicit. The LLM
cannot supply SQL, and `app/query/compiler.py` is the only layer that generates it.

### How is idempotency guaranteed?

Transformations are designed to be idempotent, the order is fixed, and validation rereads
`cleaned.csv` and compares profile hashes. Run fingerprints reuse the same logical run for
the same owner, files, policy, and versions.

### What happens if the LLM returns invalid JSON?

`LLMPlanner` strips permitted wrapper noise and extracts JSON, then validates
`PlannerOutput`. A single repair is attempted; on timeout, invalid JSON, or unrepaired
validation failure, the deterministic fallback planner or a structured refusal is used.

### How do you prevent fan-out?

Relationship discovery marks duplicate keys on the one side unsafe. `join_policy.py` rejects
unsafe paths, and the compiler uses per-fact CTEs before combining metrics.

### What if the model skips required tools?

Code computes required evidence. The validator emits `TOOL_EVIDENCE_MISSING`; the planner
gets one repair with tool results injected. Fallback performs the required tools itself.

### How do you avoid leaking PII?

PII roles and email/phone-like values are excluded from distinct-value tool results. The
plan validator rejects PII selection, and prompts never receive raw rows or API keys.

### What would you change for production?

Replace `X-Owner-Id` with authenticated identity and authorization, move jobs to a durable
queue, use chunked/distributed cleaning, add provider policy and retention controls, and
record live OpenRouter evaluation separately from synthetic replay.

### What is the most important limitation of the recorded metrics?

The root cassettes are synthetic emulator responses, so replay metrics measure the
planner/validator/executor path deterministically, not live OpenRouter model quality. A live OpenRouter
run is still unverified in this environment.
