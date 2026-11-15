# AI Retail Data & Conversational Intelligence Workbench

A CSV pipeline that profiles and cleans retail data **without silent damage**, plus a chat assistant that answers with
**computational evidence** instead of persuasive prose. Spec documents are in `docs/` (PRD, ARCHITECTURE, DATA_CONTRACTS, AGENTS, EVALUATION, IMPLEMENTATION_PLAN).

## Commands (clean clone, no API key needed)

```bash
uv sync                                   # or: pip install -r requirements.txt   (Python >= 3.12)
cp .env.example .env                      # add a provider key only for live model calls
uv run pytest -q                          # full regression suite, including oracle, idempotency and injection coverage
uv run python scripts/make_sample_data.py # seeded sample data with planted traps -> sample_data/
uv run uvicorn app.main:app --reload      # UI at http://localhost:8000, Swagger at /docs

# required entry point: replays committed cassettes, no key, no network
uv run python -m app.evaluate --input evaluation_cases/cases.json --output results.json \
    --artifacts-dir ./evaluation_output --provider replay --cassettes evaluation_cases/cassettes
# default (auto): cassette hit -> replay; else configured live provider; else rule-based fallback
uv run python -m app.evaluate --input cases.json --output results.json --artifacts-dir ./evaluation_output
# fallback-only run (per-path comparison)
uv run python -m app.evaluate --input evaluation_cases/cases.json --output results_fb.json --artifacts-dir ./out_fb --provider fallback
# record Agnes cassettes (do once after freezing PROMPT_VERSION)
uv run python -m app.evaluate --input evaluation_cases/cases.json --output results_agnes.json --artifacts-dir evaluation_output_agnes \
    --provider agnes --record-cassettes evaluation_cases/cassettes
# regenerate cases with oracle-computed expectations
uv run python scripts/build_eval_cases.py
```

## Architecture

```
CSV -> loader -> DuckDB all-VARCHAR import (+rejects) -> profile -> role inference -> issues -> cleaning plan (dry-run)
   -> policy (low risk applied, ambiguous skipped_needs_review) -> deterministic transformations (fixed order)
   -> invariants + idempotency (re-run on cleaned.csv) -> typed run.duckdb + relationship graph + catalog.json

question -> clarification gate -> schema linker (token budget) -> PLANNER (NIM loop | rule fallback)
   -> typed QueryPlan -> VALIDATOR (11 checks incl. tool evidence) -> COMPILER (parameterised fact-CTE SQL)
   -> read-only EXECUTOR (timeout) -> facts -> evidence -> template answer (+ optional verified narrator)
```
Layers: `ingestion/ profiling/ roles/ cleaning/ relationships/ metrics/ query/ conversation/ agents/ evidence/ llm/ storage/ services/ orchestration/ api/ evaluation/`.
`run_pipeline()` and `ask()` are shared by the API and the evaluator.

## AI-vs-code boundary
The model **proposes** a typed plan (JSON protocol; no SQL/Python/paths/owner/versions). Code validates, joins (only verified
many-to-one paths), executes, computes every number as a `Fact`, and verifies narration. Owner and dataset versions are server-owned (`ExecutionContext`).

## Provider / model
The app supports Agnes AI and other OpenAI-compatible providers. Agnes uses
`https://apihub.agnes-ai.com/v1` with `agnes-3.0-flash`, which Agnes currently lists
as free for its Angel User Trial. Configure `LLM_PROVIDER=agnes` and keep
`AGNES_API_KEY` only in the ignored local `.env`.
Own token-bucket limiter (`LLM_MAX_RPM`), exponential backoff with jitter honouring `Retry-After` (<= 2 retries), circuit breaker, SQLite response cache,
`<think>` stripping. 401/404 are non-retryable config errors. Chain: live provider -> (timeout / 429 / invalid JSON after one repair / breaker open) -> deterministic fallback -> clarify/unsupported.

## Data sent to the model
Only: the question (<= 500 chars); a pruned schema context (<= 5 datasets, <= 40 columns: ids, types, roles, null%, distinct counts), relationship ids with cardinality,
metric definitions, a structured conversation summary (active filters/metrics, last plan), the last 3 question strings, `list_distinct` values (sanitised: NFKC, control chars removed,
<= 64 chars, fenced in `<data>`; personal-data columns and e-mail/phone-like values never returned), and, for the narrator, computed facts plus a <= 20-row preview.
**No raw table rows, no PII columns.** Prompts are versioned (`PROMPT_VERSION`) and never exposed.

## Security decisions
Typed plans with `extra=forbid`; tool allow-list (4 read-only tools); validator is the real defence (scope, columns, types, values must exist, relationship status, PII columns, limits);
parameterised SQL with catalog-derived identifiers only; read-only DuckDB connection with `enable_external_access=false`, timeout via `interrupt()`; owner-scoped repositories
(`ISOLATION_VIOLATION`, no existence leak); upload checks (extension on the client name, size, binary sniffing, internal artifact names, path containment); secret redaction in traces;
suspicious values are echoed as "the requested value". Injection suite: 15 attacks, 6 vectors, canary tokens, `blocked_at` per attack, **ASR 0**.

## Cleaning behaviour worth knowing
Fixed order: null tokens -> trim -> case/SKU canonicalisation -> category alias/fuzzy (>= 0.92 applied, 0.80-0.92 `skipped_needs_review`) -> numeric -> dates -> dedup (normalise-then-dedup).
Ambiguous `DD/MM` vs `MM/DD` is **never guessed**: the step is `skipped_needs_review`, the column stays text, and date questions ask for clarification. Parse failures become NULL and are listed in `parse_failures.json`.
Duplicate ids are flagged, never deleted; duplicate keys on a dimension make the relationship `unsafe` and joins are refused (fan-out blocked by construction).

## Results (this repo, sample data)
Per-path chat metrics from evaluator output (`summary.by_path`). Offline replay is the
submission baseline; Agnes is optional for live provider experiments.

| metric | Agnes live | fallback |
|---|---|---|
| plan_validity_rate (first plan) | 1.0 | 1.0 |
| repair_rate | 0.0 | 0.0 |
| tool_use_compliance | 1.0 | 1.0 |
| exact_answer_accuracy (vs pandas oracle) | 0.0 | 1.0 |
| grounding_rate | 1.0 | 1.0 |
| clarification precision / recall | 0.2 / 1.0 | 1.0 / 1.0 |
| correct_refusal_rate | 1.0 | 1.0 |
| narrator_acceptance_rate | live smoke: 1.0 (1/1) | template fallback |
| llm_calls / tokens per question | 1.375 / 3,226.2 | 0.2 / 0 |

Evaluator: 12 cases -> 10 ok and 2 intentionally bad (missing file, malformed case) reported as expected `error` cases without aborting. The replay has zero cassette misses. A live Agnes user-flow smoke test produced an accepted narrator response; template fallback remains authoritative whenever verification or provider calls fail.

## Cassettes: what is and isn't real
Real Agnes recordings are retained in `evaluation_cases/cassettes/agnes`. The root
`evaluation_cases/cassettes` directory is the deterministic offline golden set and
contains entries for both configured model keys, so replay remains keyless and
network-free. Provider failures are recorded as deterministic simulated
timeout/rate-limit entries.

Provider keys remain only in the ignored local `.env`. The local configuration uses
Agnes `agnes-3.0-flash` with the 40 RPM client limiter. Live Agnes calls receive
the bounded schema/statistics context described above; offline replay requires no key.

## Evaluator case format
```json
{"cases": [{"case_id": "x", "type": "pipeline|pipeline_chat", "owner_id": "u1",
  "datasets": [{"name": "orders", "path": "orders.csv"}], "options": {"policy": "unattended|interactive_review", "force": true},
  "chat": ["question", {"question": "...", "as_owner": "intruder", "expect": {"status": "ok", "numeric": {"revenue:Electronics": 123.4},
     "columns_used": [], "relationships_used": ["orders.product_id=products.product_id"], "clarification": true, "refusal": true, "missing_roles": [], "error_code": ""}}],
  "mock_responses": ["scripted LLM outputs, or SIMULATE:timeout|rate_limited"],
  "expect": {"pipeline": {"row_count_clean": 10, "issues_include": [], "idempotent": true, "skipped_steps": []}}}]}
```
Unknown fields are preserved under `extra`. Output keys per case: `dataset, profile_before, issues, cleaning_plan, profile_after, validation, chat_evaluation[], artifacts` (+ `metrics`, `errors`, `status`); plus `summary` (`by_path`, `injection`, `cassette`).

## Trade-offs
Typed QueryPlan instead of text-to-SQL (safer, less flexible: one time grain per query). Unattended skip-policy favours correctness over coverage. Pandas for cleaning
(simple, fine for 100k rows) with DuckDB for storage/analytics. Multi-fact metrics use FULL OUTER JOIN of per-fact CTEs, so zero-return categories still appear.

## Known limitations
No live-NIM run was possible while building (no key): the NIM provider is tested with a fake transport only. No date bucketing ("by month"); ambiguous dates require the review endpoint to re-run.
Role inference is alias/type driven (unseen schemas degrade to clarify/unsupported). Advisor LLM, stock-risk score, Dockerfile and `mypy` pass are not built. `ruff` clean.
Contract extensions beyond DATA_CONTRACTS.md: issue type `non_iso_dates`; `MetricVariant.terms/expression`; `MetricBinding.term_datasets/term_columns/binding_uncertain`; `PlanDelta.relationship_ids`;
`LLMResponse.source`; `owner_id` columns on `chat_turns`/`evidence`/`traces`; `ValidationIssue.details`.
