# IMPLEMENTATION_PLAN.md — Build order, hours, setup, cut list

Strategy: **thin vertical slice first, then widen.** Something runnable from a clean clone must exist by ~hour 6. Budget 16 h of the 12–18 h window; ~30% of it is slack for parsing/integration surprises.

---

## 1. Setup (do first, 30 min)

```bash
uv init retail-workbench && cd retail-workbench
uv add fastapi "uvicorn[standard]" pydantic pydantic-settings duckdb pandas pyarrow \
       charset-normalizer rapidfuzz pyyaml openai tenacity python-multipart
uv add --dev pytest pytest-cov hypothesis httpx ruff mypy
uv export --no-hashes > requirements.txt     # commit both uv.lock and requirements.txt
git init && git add -A && git commit -m "chore: project skeleton"
```
Pin by committing the lockfile (don't hand-type versions). Check installed DuckDB's `read_csv` rejects, `sniff_csv`, `interrupt`, and config setting names before writing ingestion/executor code.

### `.env.example`
```
# --- LLM (NVIDIA NIM, OpenAI-compatible) ---
LLM_PROVIDER=nim                 # nim | mock | none
NVIDIA_API_KEY=nvapi-REPLACE_ME  # from build.nvidia.com ; never commit the real key
NIM_BASE_URL=https://integrate.api.nvidia.com/v1
NIM_MODEL=meta/llama-3.3-70b-instruct   # confirm with GET /v1/models; any chat model id works
LLM_TIMEOUT_S=30
LLM_MAX_RPM=30                   # free tier is rate-limited (~40 RPM, varies); stay under it
LLM_MAX_RETRIES=2
LLM_BREAKER_FAILURES=3
LLM_BREAKER_COOLDOWN_S=60
USE_NARRATOR=true
USE_ADVISOR=true
PLANNER_MAX_INPUT_TOKENS=6000
EVAL_PROVIDER=auto             # auto | replay | fallback | nim | mock

# --- Storage / limits ---
ARTIFACTS_DIR=./artifacts
SQLITE_PATH=./artifacts/meta.sqlite
MAX_UPLOAD_MB=100
MAX_ROWS=5000000
QUERY_TIMEOUT_S=15
DUCKDB_MEMORY_LIMIT=1GB
DUCKDB_THREADS=2
```
Quick key check (manual): `curl -H "Authorization: Bearer $NVIDIA_API_KEY" https://integrate.api.nvidia.com/v1/models`. Base URL must include `/v1`.

---

## 2. Phases and hours

| Phase | Hours | Deliverable | Done when |
|---|---|---|---|
| **0 Setup** | 0.5 | skeleton, config, errors, ids/hash util, logging w/ redaction | `pytest` runs, ruff clean |
| **1 Slice A: pipeline core** | 3.0 | upload validation, raw register, DuckDB all-VARCHAR import + rejects, raw profile, issue detection (whitespace/case/null tokens/dups), 4 core transformations, clean artifact, after-profile, invariants | `run_pipeline([one csv])` produces raw+clean+profiles+plan+validation; invariants + idempotency tests green |
| **2 Slice B: evaluator + fallback chat** | 2.0 | evaluator CLI skeleton emitting PS keys; role inference v1; metric registry (units/revenue/orders); fallback planner for 4 patterns; validator v1; compiler v1 (single table); template answer; evidence v1 | `python -m app.evaluate` on a 1-dataset case returns a grounded aggregate answer — **milestone ≈ hour 6** |
| **3 Cleaning depth** | 2.0 | numeric/date parsing + ambiguity rule, category alias + fuzzy, SKU, exact dedup order, plan step ids, policy (skip needs-review), run fingerprint/idempotent re-run, `parse_failures.json` | golden cleaning cases pass |
| **4 Relationships + joins** | 2.0 | relationship discovery, cardinality, statuses, join policy, path search, fact-CTE compiler, multi-dataset metrics (return_rate variants A/B), `rows_considered` | fan-out + unsafe-join golden cases pass vs oracle |
| **5 LLM Planner (NIM) + tool-evidence** | 2.5 | provider interface; NIM provider (limiter/backoff/breaker/cache); planner protocol; 4 tools; **`ToolLog` + tool-evidence rule + repair with injected tool results**; **context budget + structured history**; injection fencing; clarification gate; multi-turn deltas; session persistence | chat works with `LLM_PROVIDER=nim`; filtered/joined questions show tool calls in the trace; forced failure degrades to fallback |
| **5b Narrator + claim verifier** | 1.0 | facts → narrator segments → deterministic renderer → verifier → template fallback | all verifier failure modes fall back to template; `answer_source` recorded |
| **6 Tests, oracle, cassettes, injection suite** | 2.5 | pandas oracle; golden fixtures + generator script; failure-path tests; isolation/security tests; **cassette record/replay provider**; **injection suite with canary tokens + `blocked_at` + ASR**; **per-path metrics (replay vs fallback)**; determinism diff test | PRD gates demonstrable; ASR = 0; per-path table generated |
| **7 API + minimal UI + docs** | 1.0 | FastAPI endpoints, job thread, trace endpoint, **one static page** (upload/status/chat/evidence + planner-loop trace view), README, `.env.example`, sample data | clean-clone walkthrough works |
| **8 Stretch (only if ahead)** | — | advisor, raw graph, review endpoint, Dockerfile | |
| **Total** | **16.5 + 1.0 slack = 17.5** | within the 12–18 h window | |

Net change vs the previous plan: +0.5 (planner depth), +1.0 (narrator promoted from stretch), +0.5 (cassettes/injection/metrics), −0.5 (UI trimmed to one page) = **+1.5 h**.

Order inside phases is test-first for: validator, compiler, join policy, claim verifier, tool-evidence rule.

### Cut order if behind
1. Stock-risk score (never started) → 2. UI becomes Swagger only → 3. Advisor (alias+fuzzy only) → 4. Raw graph → 5. Job thread (sync endpoints) → 6. Dockerfile → 7. Caching beyond `llm_cache` → 8. Narrator **last of the optional items** (it is the track's validated-structured-output showcase; cut only if everything else is green and time is gone).
**Never cut:** evaluator, idempotency, invariants, relationship validation, plan validation, metric registry, fallback planner, tool-evidence rule, cassettes (record/replay), injection suite, tests, README.

---

## 3. Module-by-module checklist

### Ingestion / profiling
- [ ] `file_validator`: extension, size, name sanitize (`[a-z0-9_]+`), resolve under staging root
- [ ] `csv_reader`: encoding detect → `sniff_csv` for header/dialect → `read_csv(all_varchar, store_rejects, header=explicit)`; copy rejects out before close
- [ ] `profiler`: DuckDB SQL stats, chunk-safe; `profile_hash`
- [ ] `issue_detector`: all issue types in DATA_CONTRACTS §3
- [ ] `role_inference`: score, thresholds, ties → ambiguous

### Cleaning
- [ ] transformations registry (pure functions on pandas Series) + tests (idempotent)
- [ ] `plan_generator`: stable ids, risk assignment, reasons
- [ ] `policy`: unattended/interactive
- [ ] `executor`: fixed order, row/col accounting, writes typed tables to `run.duckdb`, exports `cleaned.csv`, `parse_failures.json`
- [ ] `validator`: invariants + idempotency re-run
- [ ] `advisor` (stretch)

### Relationships / metrics
- [ ] discovery over role-compatible pairs; statuses; `clean_graph.json`
- [ ] `join_policy` + path search ≤3 hops
- [ ] `definitions.py`/`metrics.yaml`; `binder` with variants and `missing_roles`

### Chat
- [ ] `clarification` gate (metric/date/dimension ambiguity, defaults)
- [ ] `linker` (prune to ≤5 datasets/≤40 columns, include bridge tables)
- [ ] `fallback_planner` patterns (AGENTS.md A5)
- [ ] `planner` + `planner_tools` + `prompts` (versioned) + repair
- [ ] `ToolLog` + required-evidence computation + `TOOL_EVIDENCE_MISSING` (validator check 11); fallback calls tools itself
- [ ] context budget (`PLANNER_MAX_INPUT_TOKENS`) with documented trim order; structured history only
- [ ] `query/validator`, `compiler`, `executor` (RO conn, interrupt timer)
- [ ] `facts`, `evidence`, `templates`, `claim_verifier`, `narrator` (should-build)
- [ ] `conversation/context`: delta merge, topic reset, session persistence

### Infra
- [ ] `llm/rate_limiter` (token bucket), `nim_provider` (OpenAI SDK, `max_retries=0`, own tenacity), breaker, `llm_cache`, `mock_provider`
- [ ] `llm/cassette_provider` (record + replay, key = hash of full canonical request, miss ⇒ fallback + counted), `auto` provider chain
- [ ] `storage`: SQLite repos (owner-scoped), artifact store with hash
- [ ] `tracer`: events w/ durations, redaction
- [ ] `orchestration`: manager state machine, job runner
- [ ] `evaluate.py`: tolerant loader, per-case isolation, `--provider/--record-cassettes/--cassettes`, summary with `by_path`, `injection`, `cassette`
- [ ] `tests/security/injection_suite.py`: canary tokens, `blocked_at`, ASR; fails build if ASR > 0

---

## 4. NIM integration notes

- Client: `OpenAI(base_url=NIM_BASE_URL, api_key=NVIDIA_API_KEY, timeout=LLM_TIMEOUT_S, max_retries=0)`; call `chat.completions.create(model=NIM_MODEL, messages=…, temperature=0, max_tokens=…)`.
- Don't rely on native function-calling or `response_format` support (varies per model): use the JSON-object protocol in AGENTS.md A4, extract the first balanced `{…}`, validate with Pydantic, 1 repair.
- Reasoning-style models may emit `<think>…</think>` or a separate `reasoning_content`: strip before JSON extraction; prefer a non-reasoning instruct model for the planner.
- 401 ⇒ config error (not retryable, surface clearly). 404 ⇒ check `/v1` suffix or model id. 429/5xx/timeout ⇒ retry with backoff, then fallback.
- Treat prompts as leaving the machine; follow the data-minimization rules in ARCHITECTURE §12.
- Evaluator default is `--provider auto`: cassette hit → replay; else live NIM if a key exists (cached); else fallback planner. Committed cases use `--provider replay` so reviewers get the **real planner path** deterministically with no key. Record cassettes once with `--provider nim --record-cassettes`; re-record when `PROMPT_VERSION`, model or schemas change.
- Free-tier limits: recording the golden set (~16 questions × ≤3 planner calls + narrator) fits comfortably under 30 RPM with the limiter; do it in one sitting before the demo.

---

## 5. Commands (put verbatim in README)

```bash
# setup
uv sync                      # or: pip install -r requirements.txt
cp .env.example .env         # add NVIDIA_API_KEY only if you want live NIM

# tests (includes injection suite; fails if attack success rate > 0)
uv run pytest -q

# generate sample data (seeded; includes planted traps)
uv run python scripts/make_sample_data.py

# API + UI
uv run uvicorn app.main:app --reload       # http://localhost:8000  (docs at /docs)

# batch evaluation (required entry point) — replays recorded NIM planner responses, no key needed
uv run python -m app.evaluate --input evaluation_cases/cases.json \
    --output results.json --artifacts-dir ./evaluation_output \
    --provider replay --cassettes evaluation_cases/cassettes

# default (auto): replay if recorded, else live NIM if key present, else rule-based fallback
uv run python -m app.evaluate --input cases.json --output results.json --artifacts-dir ./evaluation_output

# record cassettes against live NIM (once, needs NVIDIA_API_KEY)
uv run python -m app.evaluate --input evaluation_cases/cases.json --output /tmp/rec.json \
    --artifacts-dir /tmp/rec --provider nim --record-cassettes evaluation_cases/cassettes

# fallback-only run (for the per-path comparison)
uv run python -m app.evaluate --input evaluation_cases/cases.json --output results_fallback.json \
    --artifacts-dir ./evaluation_output_fb --provider fallback
```

---

## 6. Day plan (calendar view)

| Day | Focus | Exit check |
|---|---|---|
| 1 | Phases 0–2 | Vertical slice milestone: evaluator returns a grounded answer on one CSV |
| 2 | Phases 3–4 | Cleaning + relationships golden cases green |
| 3 | Phases 5, 5b | NIM planner with tool-evidence rule works; narrator verified; fallback proven by forced outage |
| 4 | Phases 6–7 (+8) | Cassettes recorded, injection suite ASR = 0, per-path table, clean-clone run, README, walkthrough |

Do the **cassette recording at the start of day 4** (prompts frozen by then). Any later prompt edit means re-recording, so freeze `PROMPT_VERSION` before recording.
Record the walkthrough only after a clean-clone dry run (fresh `git clone`, fresh venv).

---

## 7. Definition of done

- [ ] All PRD §8 acceptance criteria demonstrable
- [ ] Clean clone: `uv sync && pytest && python -m app.evaluate …` succeeds with **no API key**
- [ ] Evaluator output contains every PS key; deterministic across two runs (replay provider)
- [ ] Planner trace shows inspect → plan → validate (→ repair) loop with tool calls on a filtered/joined question
- [ ] `ToolLog` rule enforced and covered by tests
- [ ] Cassettes committed; replay hits all committed cases; stale cassette ⇒ visible miss, not stale answer
- [ ] Injection suite: attack success rate 0, `blocked_at` recorded per attack
- [ ] Per-path metrics table (replay vs fallback) included in results and README
- [ ] README includes: architecture, commands, AI-vs-code boundary, **NVIDIA NIM + model id**, **data sent to model**, security decisions, trade-offs, limitations, case format
- [ ] `.env.example` has placeholders only; no secrets in git history
- [ ] Meaningful commit history (feat/test/docs prefixes)
- [ ] Can explain: architecture, data flow, AI boundary, **one trade-off** (suggested: typed QueryPlan vs raw SQL, or unattended skip policy)

---

## 8. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Hidden schema defeats role inference | High | alias YAML, distribution heuristics, clarify/unsupported; test with renamed columns |
| DuckDB API differs by version | Medium | check installed version early; thin wrapper in `csv_reader`/`executor` |
| NIM rate limits/outage during demo | Medium | cache, limiter, fallback, record a demo run beforehand |
| Time overrun on cleaning edge cases | High | slice-first; cut list; don't gold-plate parsers |
| Ambiguous-date logic wrong | Medium | simple rule (day>12 evidence); default to leaving unparsed |
| Non-determinism creeping into outputs | Medium | determinism diff test in CI of the evaluator |
| Cassettes go stale after a prompt/schema edit | High | key = hash of full request; freeze `PROMPT_VERSION` before recording; misses are counted and shown |
| Reviewer's hidden cases have no cassettes | High | `auto` chain: live NIM if key, else fallback; state this in README; fallback must stand on its own |
| Planner skips tools, wasting repair budget | Medium | tool-evidence rule + repair with injected tool results; measure tool-use compliance |
