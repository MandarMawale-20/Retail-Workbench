# PRD.md — AI Retail Data & Conversational Intelligence Workbench

Source: assessment DQ-AI-CHAT-03 · Track: **AI Engineering** · Timebox: 4 days / 12–18 h · Score: 100

---

## 1. Problem
Retail teams get messy CSVs (products, orders, customers, inventory, stores, returns). They need (1) a trustworthy pipeline that profiles and cleans them without silent damage and (2) a chat assistant that answers questions with **computational evidence**, not persuasive prose.

## 2. Goals
- G1. Ingest, profile, clean CSVs safely with a full audit trail and stable IDs.
- G2. Demonstrate retail understanding: **category normalization**, **safe cross-dataset joins**, **sales/inventory analytics** (≥2 required; all 3 targeted).
- G3. Grounded multi-turn chat: plan → validate → execute → evidence → answer.
- G4. Safe AI engineering: constrained plans, bounded tool loop, fallbacks, injection defenses, eval harness.
- G5. Reviewer can run `python -m app.evaluate` from a clean clone with no paid service.

## 3. Non-goals
Vector search · unrestricted SQL/Python · multi-user auth system · real-time streaming · fiscal calendars/multi-currency · fancy UI · stock-risk scoring (stretch).

## 4. Users
| User | Need |
|---|---|
| **Reviewer (primary)** | Run evaluator on hidden datasets, inspect artifacts, verify grounded answers, see failures handled |
| Retail analyst | Upload CSVs, see issues and cleaning decisions, ask questions, trust numbers |

## 5. User stories
1. Upload one or more CSVs and get a run with status, profiles, issues, plan, cleaned artifact.
2. See *why* each cleaning step exists (reason, risk, source, status) and which were skipped for review.
3. Ask "top 5 categories by revenue" and see answer + dataset/version + columns + filters + joins + row count + preview.
4. Follow up "only West region", then "among those, which category has the highest return rate?"
5. Get a targeted clarification when a question is ambiguous, and a clear refusal when data is missing.
6. Inspect any past chat turn: question → plan → validation → execution → answer.
7. Run the batch evaluator; one bad case never aborts the rest.

## 6. Functional requirements

### Data pipeline
| ID | Requirement | Priority |
|---|---|---|
| FR-01 | Accept ≥1 CSV; validate extension, size, path, encoding; internal artifact names | Must |
| FR-02 | Persist run: status, metadata, raw artifact, cleaned artifact, hashes | Must |
| FR-03 | Import as all-VARCHAR with explicit header handling; capture structural rejects; header-only → zero-row dataset | Must |
| FR-04 | Profile before/after: schema, rows/cols, nulls, duplicates, cardinality, parse failures, distributions | Must |
| FR-05 | Detect: mixed types, inconsistent dates/numbers, whitespace/case, duplicate rows & IDs, category variants, all-null columns | Must |
| FR-06 | Cleaning plan before mutation; each step: stable id, reason, fields, risk, source (rule/llm/user), status | Must |
| FR-07 | Deterministic registered transformations; fixed order; normalize-then-dedup | Must |
| FR-08 | Validate invariants: `rows_clean = rows_accepted − exact_dups`, same column set, idempotency | Must |
| FR-09 | Unattended policy: apply low-risk, skip ambiguous/high-risk as `skipped_needs_review`; interactive mode supported via endpoint | Must / Should |
| FR-10 | Ambiguous dates are never silently interpreted | Must |
| FR-11 | Re-upload of same files+config returns the same run (fingerprint) | Must |
| FR-12 | Column role inference with confidence; works on unseen schemas | Must |
| FR-13 | Optional LLM cleaning advisor with cached output; deterministic without it | Should |

### Retail intelligence
| ID | Requirement | Priority |
|---|---|---|
| FR-20 | Category/product normalization with alias table + fuzzy + confidence, preserving uncertainty | Must |
| FR-21 | Relationship graph with key uniqueness, overlap, cardinality, status; fan-out detection | Must |
| FR-22 | Metric registry: units_sold, gross/net revenue, order_count, AOV, return_rate, inventory_on_hand; role-based, multi-variant | Must |
| FR-23 | Revenue basis default stated in every revenue answer | Must |
| FR-24 | Sell-through / stock-out / inventory ageing | Could |
| FR-25 | Explainable stock-risk score | Cut first |

### Chat
| ID | Requirement | Priority |
|---|---|---|
| FR-30 | Filter, sort, group, aggregate, compare, top/bottom, distinct, describe | Must |
| FR-31 | Multi-turn state with plan deltas merged into full explicit plans | Must |
| FR-32 | Validated cross-dataset joins via relationship ids only | Must |
| FR-33 | Every answer carries evidence: dataset/version/stage, columns, filters, joins, operations, `rows_considered`, preview ≤20 | Must |
| FR-34 | All numbers computed by code (facts); template answer default | Must |
| FR-35 | Clarify on real ambiguity; refuse on unanswerable; never invent columns/facts | Must |
| FR-36 | Persist sessions: question → plan → execution → answer; no hidden prompts/secrets exposed | Must |
| FR-37 | One chat flow combining data + retail context (return-rate investigation / stock-out / store comparison) | Must |
| FR-38 | Narrator (fact-referencing segments) with claim verification and template fallback — track-critical | Should |
| FR-39 | Planner via NVIDIA NIM with bounded tools; fallback rule planner when LLM unavailable | Must |
| FR-49 | Tool-evidence rule: filtered/joined plans must be backed by `list_distinct` / `preview_join` in the per-question `ToolLog`; violation ⇒ one repair with tool results injected | Must |
| FR-50 | Planner context budget with documented trim order; structured (non-text) conversation history | Must |
| FR-51 | Cassette record/replay of real LLM responses; `--provider auto\|replay\|mock\|fallback\|nim` | Must |
| FR-52 | Prompt-injection suite with canary tokens, `blocked_at`, attack success rate (target 0) | Must |
| FR-53 | Per-path chat-quality metrics (replay vs fallback): validity, repair, tool-use compliance, accuracy, grounding, clarification precision/recall, tokens/latency | Must |

### Reliability, security, evaluation
| ID | Requirement | Priority |
|---|---|---|
| FR-40 | `python -m app.evaluate --input --output --artifacts-dir`; same production services; per-case isolation; continue on failure | Must |
| FR-41 | Output includes PS keys: `dataset, profile_before, issues, cleaning_plan, profile_after, validation, chat_evaluation[], artifacts` (+ extensions) | Must |
| FR-42 | Deterministic output given fixed data/model responses; `mock_responses` per case supported | Must |
| FR-43 | Structured errors `{code,message,retryable,stage}`; retries/backoff for LLM; circuit breaker; rate limiter | Must |
| FR-44 | Owner isolation on every resource; server-owned scope | Must |
| FR-45 | Injection defenses; PII masking; documented data sent to NIM | Must |
| FR-46 | Query limits/timeouts; read-only analytics connection | Must |
| FR-47 | Async run execution (single worker + jobs table) for API | Should |
| FR-48 | Minimal UI (upload, status, chat, evidence/trace view) | Could |

## 7. Non-functional requirements
- **Scale:** 100k+ rows per file without loading everything in Python memory (DuckDB scans); evaluator case ≤ 60 s typical.
- **Determinism:** IDs are content hashes; JSON sorted; no timestamps in compared fields.
- **Cost:** no paid service required; NIM free tier optional; ≤3 planner calls + optional narrator per question.
- **Clean clone:** `uv sync` (or `pip install -r requirements.txt`) → `pytest` → `python -m app.evaluate …` works.
- **Privacy:** no raw rows/PII to LLM; documented.

## 8. Acceptance criteria (release gates)
1. Clean input → raw + cleaned artifacts, before/after profiles, plan with stable ids.
2. Applied steps all have id/reason/risk/source/status; ambiguous steps skipped and reported.
3. `clean(clean(x)) == clean(x)`; re-run with same input returns same logical result.
4. Row/column invariants enforced; failure keeps raw and does not promote clean.
5. Ambiguous date stays unresolved; date queries on it ask for clarification.
6. Duplicate dimension keys ⇒ relationship `unsafe`; fan-out query blocked or correct vs oracle.
7. Missing roles ⇒ `UNSUPPORTED` listing them; no invented return rate.
8. Ambiguous metric ⇒ targeted clarification (or documented default stated).
9. Planner emits typed plan only; no SQL/Python reaches execution.
10. Owner and dataset versions come from server session.
11. Every number in an answer maps to a computed fact (match oracle in tests).
12. Follow-up modifies previous plan correctly; unrelated topic resets filters.
13. Evaluator CLI works, continues after failed case, emits required keys.
14. LLM off / invalid JSON / timeout ⇒ fallback planner or safe error; data state uncorrupted.
15. README, `.env.example`, sample data, tests, walkthrough present.
16. A filtered or joined question shows required tool calls in its trace; a plan lacking them is repaired or refused.
17. Replaying committed cassettes reproduces the evaluator output deterministically with no API key and no network.
18. Injection suite reports attack success rate 0 with a `blocked_at` stage for every attack.
19. Results include per-path metrics (NIM replay vs fallback) and the gap is discussed in the README.

## 9. Scoring map
| Area | Pts | Delivered by |
|---|---|---|
| Profiling + cleaning | 18 | FR-02…FR-13 |
| Retail intelligence | 14 | FR-20…FR-24 |
| Chat grounding + multi-turn | 20 | FR-30…FR-39 |
| Safe plan execution + AI eng | 12 | Planner, validator, compiler, fallback, prompt-injection tests |
| Batch evaluator + reliability | 10 | FR-40…FR-43 |
| Architecture + code quality | 10 | Layered modules, shared services |
| Security/privacy/isolation | 6 | FR-44…FR-46 |
| Tests | 5 | Invariant, golden, oracle tests |
| README + reasoning | 5 | README sections below |

Realistic target: high 80s – low 90s. Largest risk: hidden-dataset generality and time.

## 10. Demo script (3–5 min)
1. Upload `products/orders/returns/inventory` → show issues + cleaning plan (risk, source).
2. Show **one ambiguous decision** (ambiguous date or conflicting category) skipped for review.
3. Show before/after profile + invariants + idempotency.
4. Show relationship graph: verified many-to-one **and** unsafe duplicate-key relationship.
5. Chat: "Which categories have the highest return rate?" → evidence + **planner-loop trace (tool calls → plan → validation → answer)**; follow-up "only West region"; "among those, which is lowest on stock?"
6. Refusal: "What is our profit margin?" (no cost data) → missing roles.
7. Failure/recovery: break the NIM key/timeouts → fallback planner answers; evaluator continues past a bad case.
8. Show the injection suite summary (attack success rate 0, `blocked_at` per attack) and the replay-vs-fallback metrics table.

## 11. README must contain
Architecture + diagram · exact run/evaluate/test commands · AI-vs-code boundary · provider/model (NVIDIA NIM, model id, rate-limit handling) · **data sent to the model** · security decisions · trade-offs · known limitations · sample datasets description · evaluator case format.

## 12. Assumptions & open questions
- Assumed `cases.json` schema is unknown → loader tolerant, unknown fields preserved.
- Currency unit defaults to `unspecified`/INR display only if column indicates; never converts.
- Region/store hierarchy detected from roles; if absent, geographic questions return `UNSUPPORTED`.
- NIM model availability/rate limits vary → configurable; project must pass without it.

## 13. Risks
| Risk | Mitigation |
|---|---|
| Hidden schemas break role inference | Alias YAML + distribution heuristics + clarify/unsupported path |
| Time overrun | Vertical slice first; cut list (stock-risk, UI, caching, narrator) |
| NIM rate limit/outage | Limiter, backoff, cache, fallback planner |
| Silent wrong numbers | Metric registry, fact-based answers, pandas oracle tests |
| Injection via cells | Fencing, truncation, validator allow-list |
