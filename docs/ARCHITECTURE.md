# ARCHITECTURE.md — Governed AI Retail Intelligence Workbench

> Principle: **The LLM may interpret and propose. Application code owns data, policy, execution, calculations and evidence.**
> Track: AI Engineering. Hard MVP boundary: the whole system works end-to-end **without any LLM, queue, UI or vector DB**.

---

## 1. Final tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | `python -m app.evaluate` required |
| Deps | `uv` + `pyproject.toml` + lockfile; export `requirements.txt` | Clean-clone path with either tool |
| API | FastAPI + Uvicorn | Swagger at `/docs` |
| Schemas/config | Pydantic v2 + pydantic-settings | Every contract is a model |
| CLI | argparse (stdlib) | Evaluator only |
| Analytics + storage | DuckDB | Per-run `.duckdb` file; joins, aggregates |
| Cleaning logic | pandas + pyarrow | Column-wise parse/normalize |
| Encoding | charset-normalizer | Mixed-encoding edge case |
| Fuzzy matching | rapidfuzz | Category suggestions with confidence |
| Config data | PyYAML | Role aliases, category aliases, null tokens |
| Metadata DB | stdlib `sqlite3`, WAL, hand-written repositories | `BEGIN IMMEDIATE`, `busy_timeout` |
| Artifacts | Local filesystem, SHA-256 hashes | Immutable raw files |
| Background work | `ThreadPoolExecutor(1)` + `jobs` table | Evaluator bypasses it |
| LLM provider | **NVIDIA NIM** via `openai` SDK, `base_url=https://integrate.api.nvidia.com/v1` | OpenAI-compatible Chat Completions |
| Model | `NIM_MODEL` env var, default `meta/llama-3.3-70b-instruct` | Verify availability with `GET /v1/models`; swappable |
| Structured output | Own JSON protocol + Pydantic validation + 1 repair | Function-calling quality varies by NIM model, so do **not** depend on native tool-calling |
| Retries | tenacity (backoff + jitter), client-side rate limiter, circuit breaker | LLM calls only |
| Agent framework | **None** — custom bounded loop | LangGraph/LangChain rejected: fixed flow |
| Tests | pytest, pytest-cov, hypothesis, httpx | pandas oracle lives in `tests/` only |
| Lint | ruff (+ mypy on `query/`, `cleaning/` if time) | |
| UI | One static HTML page + vanilla JS served by FastAPI | Built last, cuttable |
| Container | Minimal Dockerfile (`python:3.12-slim`) | Optional |

**Rejected:** vector DB/embeddings, SQLAlchemy, Celery/Redis/Postgres, Polars, Great Expectations, React/Streamlit.

---

## 2. System overview

```
        API (FastAPI)  |  CLI evaluator  |  static UI
                         │
                         ▼
          services.pipeline.run_pipeline()     services.chat.ask()
                         │                              │
   ┌─────────────────────┘                              └───────────────────────┐
   ▼                                                                            ▼
DATA PIPELINE (100% deterministic)                       CHAT PIPELINE
 ingest → profile → roles → issues →                      clarification gate → schema linker →
 cleaning plan → policy → execute →                       PLANNER (LLM | fallback) → validator →
 validate invariants → relationship graph                 metric binder → compiler → DuckDB (RO) →
   ▲  optional: Cleaning Advisor (LLM, cached)            facts → evidence → template answer
                                                            ▲ optional: Narrator (LLM, verified)
                         │                              │
                         └──────── SQLite metadata + artifacts + DuckDB + trace store ────┘
```

Three LLM seams only: **Cleaning Advisor**, **Planner**, **Narrator**. Everything else is deterministic.

### Seven logical roles → physical modules

| Role | Module(s) | LLM |
|---|---|---|
| Manager/Orchestrator | `orchestration/` | No |
| Data Quality | `profiling/`, `roles/` | No |
| Cleaning | `cleaning/` | Optional advisor |
| Retrieval/Planner | `conversation/linker`, `agents/planner` | Yes (with fallback) |
| Validation/Guard | `query/validator`, `relationships/join_policy` | No |
| Analytics | `metrics/`, `query/compiler`, `query/executor` | No |
| Conversation | `conversation/`, `evidence/` | Optional narrator |

Retail Intelligence = **Metric Registry + role catalog + retail rules** (not an agent).

---

## 3. Data pipeline (`run_pipeline`)

1. **Upload validation** — extension (`.csv`), size cap, path sanitization, encoding detect, non-empty bytes. Internal artifact names only.
2. **Register raw** — copy to `artifacts/{owner}/{run}/datasets/{name}/raw.csv`, SHA-256, dataset version `dv_<hash8>`.
3. **Import as strings** — DuckDB `read_csv(..., all_varchar=true, store_rejects=true, header=<explicit>)`. Header via `sniff_csv` first; fall back to `header=true`. Copy reject tables to `structural_rejects.json` **before** closing the ingestion connection (they are temporary). Header-only file → zero-row dataset, status `completed_with_warnings`. Never let the sniffer choose types or date formats.
4. **Raw profile** — rows, columns, nulls, distinct, duplicates (exact + candidate-key), min/max, top values, parse-failure counts, reject count.
5. **Role inference** — see §6.
6. **Issue detection** — whitespace, case variants, null tokens, mixed numeric/currency, mixed/ambiguous dates, exact duplicate rows, duplicate IDs, category variants (fuzzy clusters), all-null columns, suspicious outliers (negative qty/price).
7. **Cleaning plan** — one `CleaningStep` per issue group, stable `step_id = hash(op, dataset, columns, params)`.
8. **Policy** — `unattended` (default, evaluator): apply low-risk; skip medium/high/ambiguous as `skipped_needs_review`. `interactive_review`: pause in `needs_review` (supported by endpoint; no UI required).
9. **Execute** — registered Python transformations only. **Order is fixed:** null-token → trim/collapse whitespace → case → alias/category → numeric parse → date parse → SKU format → **exact dedup last (on normalized values)**. Normalize-then-dedup is what makes `clean(clean(x)) == clean(x)`.
10. **Clean artifact + clean profile** — typed tables written to the per-run `run.duckdb`; `cleaned.csv` exported.
11. **Validation invariants** (hard-fail → clean version not promoted, raw preserved):
    - `rows_clean == rows_accepted − exact_dups_removed`
    - `columns_clean == columns_raw` (same names, same order)
    - Every applied step has id/reason/risk/source/status
    - Parse failures are listed in `parse_failures.json` (never silent)
    - **Idempotency check:** re-run cleaner on `cleaned.csv`; output profile hash must equal.
12. **Relationship graph** on clean versions (§5).
13. **Finalize** — persist profiles, plan, validation, trace; status `completed | completed_with_warnings | failed`.

### Parsing policy

| Data | Policy |
|---|---|
| Null tokens (`""`, `N/A`, `null`, `-`) | → NULL, count recorded (low risk) |
| Numeric strings / currency / thousand separators | Parse; failures → NULL + `parse_failures.json`; column stays typed only if ≥ configured success ratio, else stays VARCHAR + issue |
| ISO dates | Parse |
| Non-ISO, column-consistent unambiguous (any value with day>12 fixes the format) | Parse with detected format |
| Ambiguous dates (`01/02/2024` and no disambiguating value) | **Leave VARCHAR**, issue `date_format_ambiguous`, step `skipped_needs_review`; date filters/metrics on that column are rejected with a clarification |
| Duplicate IDs | Flag only; never delete |
| Missing values | Preserve + report |

---

## 4. Idempotency, versioning, lineage

- **Run fingerprint** = `sha256(owner_id + sorted[(logical_name, file_sha256)] + cleaner_version + role_catalog_version + policy_version + advisor_config_hash)`. Same fingerprint ⇒ return existing completed run (unless `force=true`).
- **Step ID** = `sha256(op + dataset_logical_name + sorted(columns) + canonical(params))[:12]`.
- **Advisor cache key** = `sha256(schema_hash + sorted_value_set_hash + prompt_version + model_id + output_schema_version)`. Executed mapping is stored **in the cleaning plan**, so a later different LLM answer cannot change a re-run.
- Raw artifacts immutable; cleaned artifact always derived from raw, never from a prior clean.
- Every plan, execution and evidence row references `dataset_version_id` + `graph_version`. Sessions **pin** versions.
- Chat requests accept `Idempotency-Key`; repeated key returns stored turn.

---

## 5. Relationship graph and join policy

Built after cleaning (MVP: clean-only). For each candidate column pair (role-compatible, type-compatible): non-null rates, distinct counts, key-uniqueness per side, duplicate-key count, value overlap, unmatched count, cardinality (`one_to_one | many_to_one | one_to_many | many_to_many`).

| Status | Meaning |
|---|---|
| `verified` | Role + statistical evidence, safe cardinality |
| `candidate` | Plausible, not usable automatically |
| `ambiguous` | Multiple plausible pairs |
| `unsafe` | Fan-out / integrity problem (e.g. duplicate dimension keys) |
| `rejected` | Disallowed |

| Situation | Policy |
|---|---|
| Verified many-to-one / one-to-one | Permit |
| Facts at different grains | **Pre-aggregate each fact to the group-by grain, then merge** (fact-CTE pattern) |
| Duplicate dimension keys, conflicting rows | Block → clarification |
| Many-to-many without aggregation semantics | Block |
| No verified path | `UNSUPPORTED` |

Never use `DISTINCT` after a join to hide fan-out. Planner may only reference `relationship_id`s; join keys are never invented. Path search (max 3 hops) is deterministic code.

---

## 6. Roles and Metric Registry

**Canonical roles** (YAML aliases): `product_id, order_id, customer_id, store_id, quantity, unit_price, line_amount, category, order_date, available_stock, reorder_level, returned_quantity, return_amount, return_status, region, store_name`.

Role score = alias (0.5) + type compatibility (0.2) + distribution (0.2) + relationship evidence (0.1). `verified ≥ 0.8`, `candidate 0.5–0.8`, `ambiguous` if top two within 0.1, else unresolved. Never silently assign.

**Metrics** (versioned; declare required **roles**, multiple binding variants):

| metric_id | Formula |
|---|---|
| `units_sold` | SUM(quantity) |
| `gross_sales_revenue` (**default revenue basis**) | SUM(line_amount) or SUM(quantity×unit_price) |
| `net_sales_revenue` | gross − return_amount (needs return_amount) |
| `order_count` | COUNT(DISTINCT order_id) |
| `average_order_value` | gross_sales_revenue / order_count |
| `return_rate` | Variant A: SUM(returned_quantity)/SUM(quantity) via returns dataset. Variant B: rows matching return-status values / total rows (or qty) |
| `inventory_on_hand` | SUM(available_stock) |

Binder returns a complete binding or `UNSUPPORTED{missing_roles}`. Default revenue basis is stated in every revenue answer.

---

## 7. Chat flow (`ask`)

```
question + session
 1. Clarification gate (code)  → CLARIFY | continue
 2. Schema linker (code)       → pruned candidate datasets/columns/paths
 3. Planner                    → NIM → (invalid/timeout) → fallback rule planner → clarify/unsupported
      loop ≤ 2 rounds, ≤ 4 metadata tool calls, ≤ 1 repair; tool-evidence rule (§17)
 4. Plan validator (code)      → fail: 1 repair (LLM path only) else structured refusal
 5. Metric binder, compiler    → parameterized DuckDB SQL
 6. Executor (RO, limits)      → rows
 7. Fact generator             → totals, deltas, pct, shares, ranks (all computed)
 8. Evidence bundle            → persisted
 9. Template answer            → default; optional Narrator then claim-verify, else template
10. Persist turn: question → plan → validation → SQL fingerprint → execution → evidence → answer
```

**Clarify only when** ≥2 supported interpretations exist and no documented default. Otherwise apply the default and **state the assumption** (e.g. "gross sales revenue, before returns"). One clarification round per question; then `UNSUPPORTED`.

**Multi-turn:** `ConversationState` holds pinned versions, last plan, active filters/metrics, `pending_clarification`. The planner (or fallback) emits a **plan delta** (`add_filter`, `replace_group_by`, `replace_metric`, `reset`); code merges it into a **complete explicit QueryPlan**. Unrelated topic ⇒ filters are not carried. "Among those" ⇒ previous filter context is carried.

**Stage:** `dataset_stage` defaults `clean`. Raw stage is allowed for single-table count/distinct/filter-eq/group only (raw is all-VARCHAR); joins and numeric aggregates on raw are rejected. Evidence always names stage + version.

### Compiler pattern for multi-fact questions
For each fact table needed (orders, returns, inventory): join to the dimension through a verified many-to-one relationship, aggregate to the group-by grain in its own CTE, then join CTEs on the group-by keys. This prevents fan-out inflation and is what makes "return rate by category AND stock" safe.

---

## 8. Execution boundary

- LLM never emits SQL, Python, paths, `owner_id` or dataset versions. Plans use logical dataset names and `column_id = "dataset.column"`.
- Catalog maps `column_id → physical identifier`; SQL identifiers come only from the catalog; literals are bound parameters; functions/operators from an allow-list; no `SELECT *`.
- **Two DuckDB connections:**
  - *Ingestion* (write): reads staged files only, writes `run.duckdb`; memory/thread limits.
  - *Analytics* (read): `read_only=True`, `enable_external_access=false`, `memory_limit`, bounded threads, `lock_configuration=true`, no extension loading. Opened only after the ingestion connection closes. Verify setting names against the installed DuckDB version.
- Timeout: timer thread calls the connection's `interrupt()`; interrupted ⇒ `QUERY_TIMEOUT`. One connection per request.
- Limits: `limit ≤ 1000`, preview ≤ 20 rows, ≤ 3 joins, ≤ 5 datasets, ≤ 40 candidate columns.
- Filter values for `eq/in` must exist in the column's actual values (case/space-normalized); near matches are returned as suggestions → clarification. Never silently substitute.

---

## 9. Evidence and answers

`EvidenceBundle`: dataset versions + stage, graph version, relationships (with cardinality), metric definitions + role bindings, columns, filters, operations, aggregation grain, null policy, **`rows_considered`** (after filters/joins, before aggregation), `result_row_count`, preview (≤20), `facts[]`, SQL fingerprint (hash), trace id.

Facts are precomputed by code. **The template renderer is the default answer path.** The optional Narrator returns `segments` (`text` | `fact{fact_id}`); the renderer injects formatted values; the verifier rejects unknown fact ids, numbers in free text that do not match facts, wrong units, and comparatives without a comparison fact. Any failure ⇒ discard narration, use the template.

---

## 10. Persistence

**SQLite tables:** `runs, datasets, dataset_versions, artifacts, profiles, issues, role_bindings, cleaning_plans, cleaning_steps, relationship_graphs, relationships, sessions, chat_turns, query_plans, executions, evidence, traces, jobs, advisor_cache, llm_cache`.

**Artifact layout**
```
artifacts/{owner_id}/{run_id}/
  datasets/{name}/ raw.csv cleaned.csv raw_profile.json cleaned_profile.json structural_rejects.json parse_failures.json
  cleaning/ issues.json cleaning_plan.json validation.json
  relationships/ clean_graph.json
  run.duckdb
  traces/ pipeline_trace.json
  evaluation/ case_result.json
```

**Run states:** `queued → ingesting → profiling → planning → cleaning → validating → completed | completed_with_warnings | failed` (+ `needs_review`, interactive only). A stage failure never marks the run completed; raw and prior successful artifacts are preserved.

**Jobs:** single worker thread; claim via `BEGIN IMMEDIATE`; `busy_timeout`; startup reconciliation re-queues interrupted jobs; the pipeline is idempotent so re-run is safe. No leases/heartbeats in the MVP.

---

## 11. Reliability

| Code | Meaning | Behaviour |
|---|---|---|
| `INVALID_FILE` | Bad type/size/content | Reject, no run |
| `SCHEMA_INFERENCE_FAILED` | Roles unresolved | Warn; chat limited to generic ops |
| `CLEANING_VALIDATION_FAILED` | Invariant broken | Don't promote; keep raw |
| `UNKNOWN_RELATIONSHIP` / `UNSAFE_JOIN` | No/unsafe join | Reject / clarify; no retry on same relationship |
| `INVALID_QUERY_PLAN` | Validator fail | ≤1 repair |
| `UNSUPPORTED_QUERY` | Missing roles/data | Explain what's missing |
| `CLARIFICATION_REQUIRED` | Ambiguity | Targeted question |
| `QUERY_TIMEOUT` | Exceeded limit | Interrupt, record |
| `LLM_TIMEOUT` / `LLM_RATE_LIMITED` / `INVALID_LLM_JSON` | Provider issues | Retry once → fallback planner |
| `ISOLATION_VIOLATION` | Wrong owner | Deny, log |

Error shape everywhere: `{code, message, retryable, stage, details?}`. Never turn errors into empty "successful" results.

**NIM-specific handling** (the free tier is rate-limited, roughly 40 RPM, and varies by model and load — treat it as unreliable):
- Client-side token bucket `LLM_MAX_RPM` (default 30).
- On 429: honour `Retry-After` if present, else exponential backoff + jitter, max `LLM_MAX_RETRIES` (default 2).
- Timeout `LLM_TIMEOUT_S` (default 30); circuit breaker opens after 3 consecutive failures for 60 s ⇒ straight to the fallback planner.
- `temperature=0`, bounded `max_tokens`.
- `llm_cache` keyed by `sha256(request_kind + prompt_version + model + canonical_input)`; evaluator and repeated demos reuse it.
- The rule-based fallback planner means **LLM outage ≠ chat outage**.

---

## 12. Security and privacy

| Risk | Control |
|---|---|
| Malicious filename/path | Generated internal names; resolve + verify under artifact root |
| Oversized/malformed upload | Size cap, row/col caps, reject tables |
| Prompt injection in cells | Cells enter prompts only as fenced, truncated (≤64 chars), control-char-stripped JSON string values labelled `data`; tool results are data; the validator is the real defense |
| LLM-generated SQL | Not possible: no SQL channel |
| Cross-user access | `owner_id` from server session; owner check on every dataset/run/session/artifact/query |
| Expensive queries | Timeout, memory cap, limits |
| Secrets | `.env` only; redaction filter on logs; never in traces |
| PII | Column-name + regex detection (email, phone, names, addresses) ⇒ masked/omitted from prompts; no inference of protected traits |

**Data sent to NVIDIA NIM (document in README):** column names, inferred types/roles, aggregate profile stats, relationship metadata, metric definitions, bounded distinct values for non-PII categorical columns (sanitized), conversation turn text, tool results, and verified facts (narrator). **Never sent:** raw files, full rows, PII columns, owner IDs, file paths, API keys. The free hosted endpoint means all prompts leave your system.

---

## 13. Observability

Trace per request: `trace_id` → events `{ts, component, status, duration_ms, input_ref, output_ref, dataset_versions, error, model, tokens_in/out}`. Events: question, linker output, planner request/response (redacted), tool calls, validation result, compile fingerprint, execution, facts, narrator + verification, final. Stored in SQLite; exposed by `GET /api/traces/{id}` and included in evaluator output.

---

## 14. Repo layout

```
app/
  main.py config.py
  api/        uploads.py runs.py datasets.py chat.py traces.py
  orchestration/ manager.py state.py job_runner.py
  ingestion/  loader.py csv_reader.py reject_handler.py dataset_registry.py
  profiling/  profiler.py statistics.py issue_detector.py
  roles/      role_catalog.py role_inference.py role_binder.py
  cleaning/   plan_generator.py advisor.py transformations.py executor.py policy.py validator.py
  relationships/ discovery.py graph.py cardinality.py join_policy.py
  conversation/ service.py context.py clarification.py linker.py
  agents/     planner.py planner_tools.py fallback_planner.py prompts.py narrator.py
  metrics/    registry.py binder.py definitions.py
  query/      schemas.py validator.py compiler.py executor.py
  evidence/   generator.py facts.py templates.py claim_verifier.py
  llm/        base.py nim_provider.py mock_provider.py rate_limiter.py
  storage/    database.py repositories.py artifacts.py
  observability/ tracer.py events.py
  services/   pipeline.py chat.py
  evaluate.py
config/       roles.yaml categories.yaml null_tokens.yaml metrics.yaml
tests/ unit/ integration/ golden/ security/ oracle/
sample_data/ evaluation_cases/ .env.example pyproject.toml README.md
```

---

## 15. Design trade-offs (for review)

1. **Typed QueryPlan vs raw SQL** — less expressive, but safe and validatable; generic aggregations plus the metric registry cover the required question types.
2. **Own JSON protocol vs native tool-calling** — portable across NIM models and the mock; costs some prompt engineering.
3. **Clean-only relationship graph** — fewer moving parts; raw-vs-clean join provenance deferred.
4. **Unattended skip policy** — never hangs and never guesses; some ambiguous fixes stay unapplied.
5. **Failures → NULL + rejects artifact** — column set stays identical; lineage lives in a side artifact.
6. **Single worker thread** — no distributed coordination; adequate for assessment scale.

## 16. Known limitations (state in README)

Single-process; clean-only graph; heuristic role inference may leave roles unresolved on exotic schemas; the fallback planner handles a fixed pattern set; free-tier NIM rate limits; no multi-currency or fiscal-calendar logic; no vector/unstructured retrieval.

---

## 17. AI-engineering evidence layer (track: AI Engineering)

Makes the AI work visible, enforced and measured.

| Track requirement | Mechanism | Where it is measured |
|---|---|---|
| Tool/agent orchestration | Planner loop with 4 read-only tools; **tool-evidence rule**: string filters need `list_distinct`, joins need `preview_join` (checked by the validator against a per-question `ToolLog`); violation ⇒ one repair with tool results injected; fallback planner calls the same tools | tool-use compliance, tools/question |
| Validated structured output | `extra="forbid"` plans/deltas; JSON protocol with one repair; narrator `segments` + claim verifier with template fallback | plan validity, invalid-JSON rate, narrator acceptance |
| Context management | Schema linker pruning; `PLANNER_MAX_INPUT_TOKENS` with documented trim order and explicit failure; **structured history** (no free-text transcript, last 3 questions fenced) | context-budget tests, prompt-leak test |
| Retries/fallbacks | limiter, backoff, breaker, cache, fallback planner, template answers | fallback usage rate, failure-path tests |
| Prompt-injection defenses | fencing + truncation, scope bound by server, validator allow-list, tool allow-list, claim verifier | **injection suite: canary tokens, `blocked_at`, attack success rate** |
| Eval harness for chat quality | golden questions + pandas oracle; **cassette record/replay** of the real NIM planner path; per-path metrics (replay vs fallback) | `results.json → summary.by_path / injection / cassette` |

**Why cassettes:** the evaluator must be deterministic and key-free, but must not silently measure only the rule-based fallback. Recorded NIM responses (keyed by the hash of the full request incl. fenced tool results) exercise the real planner, repair and failure branches offline. A stale or missing recording is a counted miss that falls back — never a stale replay.

**Provider chain for evaluation (`--provider auto`):** cassette hit → replay; else live NIM if `NVIDIA_API_KEY` is set (cached); else rule-based fallback. Hidden reviewer datasets will normally hit the last two branches, so the fallback must stand on its own and the README must say so.

See EVALUATION.md §7–§10 for formats, attack vectors and metric definitions.
