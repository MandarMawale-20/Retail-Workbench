# Remaining Work — Retail Workbench

Status: full test suite green, `ruff` clean, keyless replay evaluator works, injection suite ASR 0.
This document lists everything **not yet implemented or not yet verified**, ordered by priority, with effort and a testable "done when".
Effort is in hours for one developer. IDs (FR-xx / AC-xx) refer to `docs/PRD.md`.

---

## P0 — Must finish before treating the project as submitted (about 4–6 h)

| # | Item | Why it matters | Done when | Effort |
|---|---|---|---|---|
| 1 | **Record Agnes AI cassettes** | **Done.** Real Agnes recordings are retained under `evaluation_cases/cassettes/agnes`; deterministic dual-model replay cassettes remain the offline golden set. | Record provider responses with `--provider agnes --record-cassettes`, replay with zero misses, and keep the key only in ignored local configuration. | 1.5 |
| 2 | **Validate the Agnes provider live** | **Done.** `/v1/models` and a live completion succeeded; fallback behavior remains covered offline. | Confirm `/v1/models`, run a golden chat, verify invalid-key and timeout fallback behavior, and document the result. | 1 |
| 3 | **Git history** | The assessment checks commit hygiene; there are no commits yet. | Small `feat/test/docs` commits; `.env` never committed; `git log` tells the build story. | 0.5 |
| 4 | **Lockfile** | Clean-clone requirement uses `uv sync`; only `requirements.txt` exists. | `uv lock` run and `uv.lock` committed; `uv sync && uv run pytest` passes in a fresh clone. | 0.25 |
| 5 | **Walkthrough script / recording** | AC-15 lists a walkthrough; the PRD §10 demo script is not rehearsed or delivered. | 3–5 min recording or written script covering all 8 demo steps, done after a fresh-clone dry run. | 1.5 |
| 6 | **Fix `AGENTS.md` A4 history rule** | **Done.** State tracks last-referenced turn per filter and drops stale filters after six unreferenced turns. | State tracks last-referenced turn per filter; unit test drops a stale filter. | 0.5 |

---

## P1 — Spec features not built (about 10–14 h)

| # | Item | Spec ref | Notes / acceptance | Effort |
|---|---|---|---|---|
| 7 | **Sell-through, stock-out and inventory-ageing metrics** | **Done.** FR-24 metrics are registered with explicit formulas, role binding, and missing-role reporting. Inventory age requires an `inventory_date` column rather than guessing. | Register `sell_through`, `stock_out_rate`, `inventory_age_days` with role variants and `missing_roles`; binder tests (complete and missing role); smoke coverage. | 3 |
| 8 | **Store-comparison and stock-out chat flows** | **Done.** | Golden integration coverage now exercises store comparison and explicit out-of-stock queries with deterministic fallback planning. | 2 |
| 9 | **LLM cleaning advisor** | **Done.** | Optional cached/Pydantic-validated suggestions are safety-gated; unsafe or low-confidence mappings become `skipped_needs_review`, while deterministic cleaning remains available. | 3 |
| 10 | **Time bucketing** | **Done.** | Typed month/quarter/year/week/day grouping and date-range filters operate only on resolved date columns; ambiguous dates clarify and raw-stage plans are rejected. | 3 |
| 11 | **Review UI for skipped steps** | **Done.** | The static UI lists proposed/skipped steps and submits approvals through the existing run approval endpoint. | 2.5 |
| 12 | **Raw-stage relationship graph** | **Done.** | Pipeline runs persist `relationships/raw_graph.json` and expose it through the run API. | 1 |
| 13 | **Stock-risk score** | **Done (deterministic baseline).** | A transparent stock exposure percentage is registered, compiled, and surfaced as a metric; a richer weighted model remains optional future work. | 3 |

---

## P2 — Hardening, generality and quality (about 8–12 h)

| # | Item | Detail | Effort |
|---|---|---|---|
| 14 | **Hidden-schema generality** | **Done for the supported matrix.** Coverage includes renamed, abbreviated, multilingual-header and unit-suffixed columns (`price_inr`, `Qty.`, `SKU Code`); unresolved cases remain unsupported/clarifying. | 3 |
| 15 | **Scale test** | **Done for the requested smoke benchmark.** `scripts/benchmark_scale.py --rows 500` processes 500 rows without committing generated data. | 2.5 |
| 16 | **Timestamp and timezone dates** | **Done.** ISO timestamps with timezone suffixes (`Z`, `+05:30`) and 10/13-digit epoch values are normalized to UTC and typed as dates. | 1.5 |
| 17 | **Unmatched-key handling policy** | **Done.** Top-N rankings exclude NULL group keys; ordinary grouped results preserve `(unmatched)`, and relationship evidence reports unmatched left-side row counts. | 1 |
| 18 | **Ambiguous-binding clarification** | **Done.** Close role-binding ties return a targeted clarification with candidate bindings rather than selecting silently. | 1 |
| 19 | **API hardening** | **In progress.** Optional Bearer token-to-owner mapping and per-owner RPM limiting are implemented; streaming upload enforcement and retry policy remain. | 3 |
| 20 | **Static typing and CI** | **Done.** CI runs Ruff, full-app mypy, pytest, and keyless evaluator replay; mypy passes across 84 source files. | 1.5 |
| 21 | **Packaging** | **Done.** Dockerfile and Compose configuration provide an 8000 port and persistent `artifacts/` volume. | 1 |
| 22 | **Narrator quality on a real model** | **Live smoke verified.** A complete Agnes-backed user flow produced an accepted narrator response (`answer_source=narrator`); template fallback remains authoritative on provider or verifier failure. Broader acceptance measurement remains future work. | 1.5 |
| 23 | **Session reset endpoint** | **Done.** `POST /api/sessions/{id}/reset` clears conversation state and turns while retaining the run scope. | 0.5 |

---

## Known limitations to keep documented (not planned fixes)

- Plans group by columns only; one aggregation grain per query; max 3 join hops per fact, 5 datasets.
- Multi-fact metrics combine per-fact CTEs with a FULL OUTER JOIN on group keys; filters on a column that a fact cannot reach through verified many-to-one paths are refused rather than silently skipped.
- Currency is never converted; no fiscal calendars; no vector search; no free-form SQL by design.
- `llm_cache` and cassette keys include the full prompt, so any prompt, model or schema edit requires re-recording.

---

## Suggested order of work

1. P0 items 1, 2 — record and validate Agnes cassettes.
2. P0 items 3, 4, 5 — repository hygiene, lockfile, and walkthrough.
3. P2 items 14–19 — generality, scale, dates, query policy, and API security.
4. Finish P2 item 20 — static typing and CI validation.
5. P2 item 22 — real-model narrator acceptance measurement.

## Definition of done reminder (from IMPLEMENTATION_PLAN §7)

All PRD §8 acceptance criteria demonstrable; clean clone passes `uv sync && pytest && python -m app.evaluate …` with no key; real cassettes committed with 0 misses; injection ASR 0 with `blocked_at` per attack; per-path metrics table in results and README; README sections complete; no secrets in history; you can explain architecture, data flow, the AI boundary and one trade-off.
