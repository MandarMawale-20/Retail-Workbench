# AI Retail Data & Conversational Intelligence Workbench

This repository contains a CSV profiling and cleaning pipeline and a grounded retail chat
assistant. The pipeline keeps raw and cleaned artifacts, records issues and decisions, and
validates invariants before promoting a clean dataset. The chat path produces a typed query
plan, validates and executes it with DuckDB, turns computed results into facts and evidence,
and uses a template answer with an optional verified narrator.

**Track:** AI Engineering

**Status:** runnable submission; keyless replay and fallback paths are verified.

## 1. Quick start

The commands below are the clean-clone path. The repository currently targets Python 3.12 or
newer. `uv sync` is the preferred installer; the project also has `requirements.txt`.

```bash
uv sync
uv run pytest -q
uv run python -m scripts.make_sample_data
uv run uvicorn app.main:app --reload
```

The API and static UI are available at `http://localhost:8000`; OpenAPI is at `/docs`.

No API key and no paid service are needed for tests, replay, or fallback evaluation. The
committed replay cassettes are offline files. If `OPENROUTER_API_KEY` is set and the provider is
`openrouter`, live planner calls use OpenRouter. If it is absent, `auto` falls through to the
deterministic planner after any cassette miss. A live key is never required by the evaluator.

The required evaluator entry point is:

```bash
python -m app.evaluate --input cases.json --output results.json --artifacts-dir ./evaluation_output
```

In this repository the cases file is under `evaluation_cases`, so the directly runnable
command is:

```bash
uv run python -m app.evaluate --input evaluation_cases/cases.json --output results.json \
  --artifacts-dir ./evaluation_output --provider replay \
  --cassettes evaluation_cases/cassettes
```

The command continues after a bad case. Its exit code is zero when the batch completes; case
errors are recorded in the JSON result. `--provider fallback` gives a no-LLM comparison:

```bash
uv run python -m app.evaluate --input evaluation_cases/cases.json \
  --output results_fallback.json --artifacts-dir ./evaluation_output_fallback \
  --provider fallback
```

The recorded live-provider workflow is opt-in and requires a real key:

```bash
uv run python -m app.evaluate --input evaluation_cases/cases.json \
  --output results_openrouter.json --artifacts-dir ./evaluation_output_openrouter \
  --provider openrouter --record-cassettes evaluation_cases/cassettes
```

Do not print or commit the key. `.env.example` contains placeholders only.

## 2. What it does

### Data pipeline

1. Validates CSV input and stages it under an internal artifact name.
2. Converts input to UTF-8 and reads values as strings, retaining structural rejects.
3. Profiles raw data: row and column counts, nulls, distinct values, duplicates, parse
   failures, candidate roles, and representative values.
4. Detects whitespace/case/null-token problems, category variants, numeric/date issues,
   duplicate rows and IDs, all-null columns, negative values, non-ISO dates, header-only
   files, and structural rejects.
5. Creates a typed cleaning plan before mutation.
6. Applies registered transformations in a fixed order, writes `cleaned.csv`, and validates
   row, column, parse-failure, and idempotency invariants.
7. Builds a role catalog, relationship graph, DuckDB database, and inspectable artifacts.

### Retail intelligence implemented

The four implemented retail capabilities are:

| Capability | Implementation |
|---|---|
| Category and product normalization | Alias/fuzzy category normalization, SKU formatting, and preserved review status |
| Safe cross-dataset joins | Relationship discovery, key uniqueness/cardinality, matched ratios, and unsafe fan-out refusal |
| Sales and returns analytics | Gross/net revenue, order count, average unit price, return quantity/rate, and store/category comparisons |
| Inventory and stock analytics | Inventory on hand, low/out-of-stock filters, stock-out rate, sell-through, and stock-risk metric definitions |

Stock-risk is registered and tested as a metric definition; it is not presented as a
production risk model.

### Chat behaviours

The assistant supports aggregation, grouping, filters, top/bottom, distinct counts,
comparisons, raw-versus-clean counts, multi-turn follow-ups, clarification, unsupported
responses, unsafe-join refusal, and owner isolation. Every successful answer includes an
evidence bundle with dataset versions, stage, columns, filters, relationships, row counts,
preview, facts, and a SQL fingerprint.

Recorded replay example (`Top 5 categories by revenue`, generated on 2026-11-16 at commit
`7d4aa08d0673a03e5d27b667c2653b0632a5bdfa`):

```text
T-Shirt leads with 354,441.00.
Gross sales revenue by products.category:
1. T-Shirt: 354,441.00
2. Electronics: 177,186.00
3. Home & Kitchen: 166,610.00
4. Footwear: 151,844.00
5. Groceries: 82,495.00
Basis: Gross sales revenue: SUM(quantity x unit_price)
Evidence: clean stage, rows considered 400,
orders.product_id=products.product_id
```

The same result contained facts for each amount and evidence versions
`orders@dv_2502d2ce` and `products@dv_f4a24682`.

## 3. Architecture

```text
CSV uploads
   |
   v
loader -> all-VARCHAR reader -> raw profile/issues -> cleaning plan
                                      |                  |
                                      v                  v
                              deterministic clean -> validation
                                      |
                                      v
                       clean DuckDB + catalog + relationship graph
                                      |
question -> clarification/linker -> OpenRouter/replay/mock/fallback planner
                                      |
                                      v
                         typed QueryPlan -> validator
                                      |
                                      v
                 parameterized compiler -> read-only DuckDB executor
                                      |
                                      v
                     facts -> evidence -> template/narrator answer
```

The seven logical roles map to physical modules as follows:

| Logical role | Physical modules |
|---|---|
| Ingestion | `app/ingestion`, `app/api/routes.py` |
| Profiling and issue detection | `app/profiling`, `app/roles` |
| Cleaning and policy | `app/cleaning`, `app/services/pipeline.py` |
| Retail graph and metrics | `app/relationships`, `app/metrics`, `app/query/catalog.py` |
| Conversation and planning | `app/conversation`, `app/agents`, `app/llm` |
| Safe execution and evidence | `app/query`, `app/evidence`, `app/services/chat.py` |
| Persistence, orchestration, evaluation | `app/storage`, `app/orchestration`, `app/evaluation`, `app/evaluate.py` |

`run_pipeline()` is the shared pipeline entry point for the API and evaluator. `ask()` is
the shared chat entry point. The API adds upload staging, owner selection, jobs, and HTTP
serialization; it does not have a separate analytics implementation.

## 4. AI versus code boundary

| LLM may do | Application code does |
|---|---|
| Propose a JSON planner response or delta | Parse and reject malformed JSON |
| Select typed intents, registered metrics, columns, and relationship IDs | Bind dataset versions and owner scope from the server session |
| Request one of four inspection tools | Enforce the tool allow-list and record `ToolLog` |
| Propose narrator segments referencing fact IDs | Compile parameterized SQL, execute read-only, and compute facts |
| Suggest a cleaning category mapping when the optional advisor is enabled | Apply only registered transformations and validate confidence, risk, and invariants |

The three implemented LLM seams are planner, optional cleaning advisor, and optional
narrator. The governing rule is: **the model proposes; the application validates, executes,
and verifies.** No model output supplies SQL, Python, file paths, owner IDs, or dataset
versions.

## 5. Provider, model, and resilience

The primary live provider is OpenRouter through the OpenAI-compatible client. The configured
model is `particle-ai/particle-1:free`. The OpenRouter base URL is
`https://openrouter.ai/api/v1`. The exact model recorded in the committed root
cassettes is **`synthetic-emulator`**, produced by `app/llm/synthetic.py`; those cassettes
are deterministic test fixtures, not evidence of a live OpenRouter call. The `evaluation_cases/cassettes/openrouter` directory contains a separate legacy recording
set with an older prompt version; it is not used for the verified submission metrics.

The OpenRouter provider has a rate limiter, retry/backoff handling, circuit breaker, and SQLite
cache. Authentication and model/configuration errors are surfaced without retry. Timeout,
429, and server failures are retryable, then the chat path uses the fallback planner.
Invalid JSON receives one bounded repair attempt before fallback or refusal.

Provider selection:

| Option | Behaviour |
|---|---|
| `auto` | Replay a matching cassette, otherwise use configured live provider, otherwise fallback |
| `replay` | Read only the selected cassette directory; a miss is visible and then handled as an LLM failure |
| `fallback` | No LLM provider; deterministic rule planner |
| `openrouter` | Live OpenRouter when `OPENROUTER_API_KEY` is valid; optional cassette recording |
| `mock` | Ordered case-supplied responses, including simulated timeout/rate-limit/invalid JSON |
| `synthetic` | Deterministic cassette-generation helper; requires `--record-cassettes` |

Cassette keys hash the provider kind, prompt version, model, and complete canonical
messages. Request bodies are not stored. A changed prompt version or model causes a miss,
not a stale answer.

## 6. Data sent to the model

The planner prompt can contain:

* the question, trimmed to 500 characters;
* pruned schema metadata from `app/conversation/linker.py`;
* column IDs, inferred types, roles, null percentages, and distinct counts;
* relationship metadata and registered metric definitions;
* structured conversation state and the last three question strings;
* sanitized `list_distinct` values, fenced as untrusted `<data>`;
* for the narrator, computed facts and at most the execution preview.

Raw CSV files and raw input rows are not sent to the provider. PII-role columns and
email/phone-like values are excluded from distinct-value tool results. The narrator may
receive computed facts and a bounded query-result preview. Schema metadata is limited to
the information required for planning. This is data minimization, not a guarantee against
provider retention. `sanitize_for_prompt`
normalizes text, strips controls, truncates values, and fences data. These controls are in
`app/common.py`, `app/conversation/linker.py`, and `app/agents/planner_tools.py`.

## 7. Data pipeline details

Cleaning plan entries contain `step_id`, `issue_id`, dataset/version, columns,
transformation, parameters, reason, risk, source, confidence, status, and affected rows.
Unattended mode applies low-risk registered steps and marks ambiguous date parsing
`skipped_needs_review`. Interactive review holds proposed steps; the approval endpoint
reruns the stored raw files with approved step IDs.

The fixed order is null-token normalization, whitespace, case/SKU normalization, category
alias/fuzzy normalization, numeric parsing, date parsing, and exact deduplication. The
invariants require the same columns, accepted-row accounting, listed parse failures,
step metadata, and a second clean pass with the same profile hash. The run fingerprint is
content/config based; the same owner, files, cleaner versions, policy, and approvals reuse
the existing run unless `force` is set.

Run artifacts are under `{artifacts_dir}/{owner}/runs/{run_id}/` and include raw and cleaned
dataset files, `raw_profile.json`, `cleaned_profile.json`, `structural_rejects.json`,
`parse_failures.json`, cleaning issues and plan, validation, raw/clean relationship graphs,
`catalog.json`, `run.duckdb`, `run_result.json`, and traces. The exact paths are returned in
the evaluator result.

## 8. Relationship graph and join safety

`app/relationships/discovery.py` compares role-compatible columns and records uniqueness,
null rates, matched ratios, unmatched counts, cardinality, and reasons. A verified join
requires compatible roles and types, sufficient overlap, and a unique side. Duplicate keys
on the dimension side make the relationship `unsafe`.

The validator accepts relationship IDs, not generated join conditions. `join_policy.py`
rejects unsafe and disallowed fan-out paths. The compiler builds per-fact CTEs before
combining metrics, which prevents a returns or inventory fan-out from multiplying sales.
`unsafe_join_duplicate_products` demonstrates the refusal path.

## 9. Chat pipeline

`ask()` loads the owner-scoped session and catalog, applies the clarification gate, and
builds a bounded schema context. The planner can inspect schema, distinct values, joins,
and metric definitions. A filtered string requires `list_distinct`; a relationship requires
`preview_join`; uncertain metrics require `get_metric_definition`. Missing evidence causes
one repair with tool results injected. Fallback planner calls the same tools itself.

The typed plan is validated for scope, columns, types, relationships, PII, stage, limits,
metric roles, and tool evidence. The compiler produces parameterized SQL with catalog
identifiers. The executor uses a read-only DuckDB connection and interrupts at the query
timeout. Facts are generated from the result, evidence is assembled, and a deterministic
template answers by default. The optional narrator can add fact-referencing text; the claim
verifier rejects unknown fact IDs, invented numbers, unsupported units, and unsupported
comparisons, returning the template instead.

Follow-up deltas merge into a full plan. Unrelated topics reset filters. Sessions, turns,
evidence, traces, and jobs are persisted in SQLite with owner-scoped repository methods.

## 10. Evaluation

The case loader accepts either a top-level list or `{"cases": [...]}`, resolves dataset
paths relative to the case file, preserves unknown fields under `extra`, and emits
`CASE_INPUT_INVALID` for malformed cases while continuing.

Each case result includes `dataset`, `profile_before`, `issues`, `cleaning_plan`,
`profile_after`, `validation`, `chat_evaluation`, `artifacts`, `errors`, `metrics`, `status`,
and `extra`. The summary includes status counts, pipeline pass rate, plan validity,
grounding, abstention, fallback use, per-path metrics, cassette counts, and injection
results.

Measured run date: **2026-11-16**. Commit is recorded at submission.

Pipeline cases and chat questions are different counting units: one evaluator case can
contain a cleaning run plus multiple chat turns, and path metrics count only applicable
chat questions. Exact-answer accuracy is the proportion of evaluated questions whose
normalized expected answer matched the computed answer; it is measured on this seeded
case set, not a universal accuracy claim.

| Metric | Replay path | Fallback path |
|---|---:|---:|
| Questions | 16 | 20 |
| First-plan validity | 0.9091 | 1.0000 |
| Repair rate | 0.0625 | 0.0000 |
| Tool-use compliance | 0.9091 | 1.0000 |
| Exact-answer accuracy | 1.0000 | 1.0000 |
| Grounding rate | 1.0000 | 1.0000 |
| Clarification precision / recall | 1.0000 / 1.0000 | 1.0000 / 1.0000 |
| Correct refusal rate | 1.0000 | 1.0000 |
| Mean calls / question | 2.25 | 0.20 |
| P50 / P95 latency (ms) | 80 / 226 | 247 / 298 |
| Mean tokens / question | 5463.2 | 0 |

The replay run had 36 cassette hits and 0 misses. The full batch had 12 cases: 10 `ok`
and 2 expected `error` cases (`bad_case_missing_file`, `bad_case_malformed`), with zero
unexpected errors and pipeline pass rate 1.0.

The injection suite ran 15 attacks across six vectors. ASR was 0.0, with zero successes
and no unblocked attacks. Blocks were recorded at prompt fencing, planner schema,
validator, tool allow-list, and claim verifier. The evaluator's committed replay fixtures are synthetic. A live API smoke test used the
configured OpenRouter provider path on 2026-11-16. The pipeline completed validation and
10 chat requests returned HTTP 200. This verifies API-path execution, not production
authentication or statistically meaningful model quality; no live cassette recording or
live 100k-row OpenRouter evaluation was performed.

The repository's 100,000-row scale test was run separately with
`python -m pytest -q tests/integration/test_scale.py`: 1 test passed. The end-to-end
benchmark also completed 100,000 rows in 84.96 seconds and 250,000 rows in 206.65 seconds.
These verify the pipeline path on this machine, not the five-million-row configured ceiling.

Measured `blocked_at` distribution:

| Vector | Attacks | Blocked at |
|---|---:|---|
| `cell_to_schema` | 1 | prompt_fencing: 1 |
| `cell_to_tool_result` | 2 | prompt_fencing: 1; validator: 1 |
| `malicious_plan` | 5 | planner_output_schema: 3; validator: 2 |
| `narrator_output` | 3 | claim_verifier: 3 |
| `question` | 3 | planner_output_schema: 2; validator: 1 |
| `tool_abuse` | 1 | tool_allowlist: 1 |

To record and replay cassettes:

```bash
uv run python -m app.evaluate --input evaluation_cases/cases.json --output /tmp/record.json \
  --artifacts-dir /tmp/record_artifacts --provider openrouter \
  --record-cassettes evaluation_cases/cassettes
uv run python -m app.evaluate --input evaluation_cases/cases.json --output results.json \
  --artifacts-dir evaluation_output --provider replay \
  --cassettes evaluation_cases/cassettes
```

## 11. Testing

```bash
uv run pytest -q
uv run pytest --cov=app --cov-report=term-missing -q
uv run ruff check app tests scripts
```

The verified run collected **87 tests** and reported **84% total application coverage**.
The suite covers cleaning idempotency and normalization-then-dedup, ambiguous dates,
header-only and malformed input, role inference, relationship cardinality and unsafe
joins, metric binding, plan validation, parameterized compilation, read-only execution,
multi-turn context/reset, grounded facts, model failure fallback, cassette stale misses,
API jobs, owner isolation, path containment, secret redaction, and the 15-attack injection
suite.

## 12. Security decisions

The security boundary is typed plans plus server-owned scope, not prompt wording. SQL is
parameterized and read-only; unsafe joins, PII columns, excessive limits, foreign owners,
bad paths, and malformed plans are rejected. See
[docs/SECURITY_AND_PRIVACY.md](docs/SECURITY_AND_PRIVACY.md).

## 13. Trade-offs and limitations

* Typed plans instead of text-to-SQL: less expressive, materially safer to validate.
* Unattended cleaning skips ambiguous dates: lower coverage, no silent interpretation.
* Pandas cleans frames while DuckDB executes analytics: simple transformations and bounded
  query execution, but Python memory is still used during cleaning.
* Synthetic replay fixtures: deterministic and free, but not a live-model quality measure.

Known limitations, mismatches, and future work are listed in
[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md). Interview-ready reasoning is in
[docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).

## 14. Repository map

```text
app/
  ingestion/       CSV validation, encoding, raw registration
  profiling/       profiles, statistics, issue detection
  roles/           role catalog and inference
  cleaning/        plans, transformations, policy, validation
  relationships/   graph discovery and join policy
  metrics/         registered retail metrics and binding
  query/           plan schemas, validator, compiler, executor, catalog
  conversation/    clarification, schema linking, context merging
  agents/          LLM planner, fallback planner, tools, narrator
  llm/             OpenRouter/mock/synthetic/cassette providers
  evidence/        facts, evidence bundle, templates, claim verifier
  storage/         SQLite database, repositories, artifact paths
  services/        shared pipeline and chat entry points
  api/             FastAPI routes
  orchestration/   Run state machine and background job runner
  evaluation/      cases, metrics, comparisons, injection suite
  evaluate.py      batch evaluator CLI
tests/             unit, integration, oracle, and security tests
sample_data/       seeded retail CSVs and edge fixtures
evaluation_cases/  cases.json and cassette fixtures
config/            roles, categories, null tokens, metrics, thresholds
scripts/            sample-data, case-builder, and scale benchmark scripts
```

The seeded data plants category variants (`T-Shirt`, `Tee`, `tee`, `T-shirt `), ambiguous
dates, duplicate IDs, exact and normalized duplicates, an all-null notes column, currency
parse cases, negative quantity, unmatched join keys, prompt-injection text, a header-only
file, malformed CSV structure, and already-clean data.

## 15. Configuration reference

| Variable | Default in `.env.example` | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `none` | API provider used by the HTTP app (`openrouter`, `mock`, `none`) |
| `OPENROUTER_API_KEY` | placeholder | OpenRouter credential; never commit |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible OpenRouter endpoint |
| `OPENROUTER_MODEL` | `particle-ai/particle-1:free` | OpenRouter model override |
| `LLM_TIMEOUT_S` | `30` | Provider timeout |
| `LLM_MAX_RPM` | `20` | Client request limit |
| `LLM_MAX_RETRIES` | `2` | Provider retry count |
| `LLM_BREAKER_FAILURES` | `3` | Failures before opening breaker |
| `LLM_BREAKER_COOLDOWN_S` | `60` | Breaker cooldown |
| `USE_NARRATOR` | `true` | Enable verified narrator when a provider exists |
| `USE_ADVISOR` | `true` | Enable optional cleaning advisor |
| `PLANNER_MAX_INPUT_TOKENS` | `6000` | Planner context budget |
| `EVAL_PROVIDER` | `auto` | Evaluator provider default |
| `API_TOKENS` | empty | Optional `token=owner` bearer map |
| `API_RATE_LIMIT_RPM` | `120` | Per-owner HTTP rate limit |
| `JOB_MAX_RETRIES` | `2` | Async job retries |
| `ARTIFACTS_DIR` | `./artifacts` | Artifact root |
| `SQLITE_PATH` | `./artifacts/meta.sqlite` | Metadata database |
| `MAX_UPLOAD_MB` | `100` | Upload limit |
| `MAX_ROWS` | `5000000` | CSV read limit |
| `QUERY_TIMEOUT_S` | `15` | DuckDB query timeout |
| `DUCKDB_MEMORY_LIMIT` | `1GB` | DuckDB memory setting |
| `DUCKDB_THREADS` | `2` | DuckDB worker count |

The code defaults `LLM_PROVIDER` to `none`, `OPENROUTER_MODEL` to
`particle-ai/particle-1:free`, and `LLM_MAX_RPM` to 20 when `.env` does not override them.
This difference is intentional configuration precedence and is called out again in
[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## 16. Troubleshooting

* **OpenRouter 401:** check `OPENROUTER_API_KEY`; do not retry an authentication error.
* **OpenRouter 404:** check the `/api/v1` suffix and that `OPENROUTER_MODEL` exists for the endpoint.
* **OpenRouter 429/5xx/timeout:** the provider retries within bounds, then the planner falls back.
* **DuckDB version issue:** use the lockfile with `uv sync`; the verified environment used
  DuckDB 2.0.0. Do not mix an old system DuckDB with the project environment.
* **Stale cassette miss:** replay keys include prompt version, model, and full messages.
  Re-record after changing prompts, schemas, or model configuration, or use fallback.
* **Unexpected local credentials:** unset provider keys and set `LLM_PROVIDER=none` for
  a keyless run; never paste `.env` contents into an issue or transcript.

## 17. Further documentation

* [Architecture](docs/ARCHITECTURE.md)
* [Security and privacy](docs/SECURITY_AND_PRIVACY.md)
* [Evaluation guide](docs/EVALUATION_GUIDE.md)
* [Walkthrough](docs/WALKTHROUGH.md)
* [Design decisions](docs/DESIGN_DECISIONS.md)
* [Known limitations](docs/KNOWN_LIMITATIONS.md)
* [Submission checklist](SUBMISSION_CHECKLIST.md)
