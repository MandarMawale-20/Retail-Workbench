# Submission checklist

Verification date: 2026-11-16  
Commit: recorded at submission

Only boxes verified in this session are checked. A checked evaluator item refers to the
keyless replay/fallback run unless explicitly marked as live.

## Miopenrouterum engineering gate

- [x] Clean disposable environment installed the project and dev dependencies.
- [x] `pytest -q` passed: 87 collected tests, exit code 0.
- [x] `ruff check app tests scripts` passed.
- [x] Sample-data generation ran and wrote 10 files.
- [x] Evaluator ran on the committed cases without an API key.
- [x] Evaluator completed 12 cases: 10 `ok`, two expected input errors, zero unexpected errors.
- [x] Cleaned artifacts and validation were inspected through evaluator output.
- [x] Grounded answer was inspected: revenue facts and evidence matched the recorded output.
- [x] Failure cases were isolated: invalid/missing input cases did not abort the batch.
- [x] Cleaning invariants and failure-preserves-raw tests passed.
- [x] Injection suite ran: 15 attacks, ASR 0.0, no unblocked attacks.
- [x] Live OpenRouter smoke run verified: pipeline validation passed and 10 authenticated
  chat requests returned HTTP 200; this was not a full live evaluator or load test.
- [ ] Browser/UI walkthrough verified interactively — note: server startup was not used as a
  verification command in this session.
- [x] 100,000-row scale test passed: `python -m pytest -q tests/integration/test_scale.py`
  (`1 passed`); this is not evidence for the five-million-row ceiling.

## Assessment scoring areas

- [x] Profiling and cleaning — `app/profiling`, `app/cleaning`, `tests/integration/test_pipeline.py`.
- [x] Retail intelligence — `app/metrics`, `app/relationships`, `tests/integration/test_chat.py`.
- [x] Grounded multi-turn chat — `app/services/chat.py`, `app/evidence`,
  `tests/integration/test_chat.py`.
- [x] Safe plan execution and AI boundary — `app/query`, `app/agents`,
  `tests/unit/test_validator_compiler.py`.
- [x] Batch evaluator and reliability — `app/evaluate.py`,
  `tests/integration/test_api_and_evaluator.py`.
- [x] Architecture and code quality — `docs/ARCHITECTURE.md`, module separation,
  Ruff result above.
- [x] Security, privacy, and isolation — `docs/SECURITY_AND_PRIVACY.md`,
  `tests/security/test_injection_and_security.py`.
- [x] Tests and oracle comparisons — `tests/oracle/oracle.py`, 87-test run above.
- [x] README and reasoning — `README.md`, `docs/DESIGN_DECISIONS.md`,
  `docs/KNOWN_LIMITATIONS.md`.

## Required deliverables

- [x] Repository code and configuration.
- [x] Primary `README.md`.
- [x] `sample_data/` fixtures and generator.
- [x] `evaluation_cases/cases.json`.
- [x] `evaluation_cases/cassettes/`.
- [x] `tests/`.
- [x] `.env.example` with placeholders only.
- [x] `docs/ARCHITECTURE.md`.
- [x] `docs/SECURITY_AND_PRIVACY.md`.
- [x] `docs/EVALUATION_GUIDE.md`.
- [x] `docs/WALKTHROUGH.md`.
- [x] `docs/DESIGN_DECISIONS.md`.
- [x] `docs/KNOWN_LIMITATIONS.md`.
- [ ] 3–5 minute recording — note: walkthrough script is present; recording was not created.

## Final pre-submit command block

```bash
uv sync
uv run pytest -q
uv run ruff check app tests scripts
uv run python -m scripts.make_sample_data
uv run python -m app.evaluate --input evaluation_cases/cases.json --output results.json \
  --artifacts-dir evaluation_output --provider replay \
  --cassettes evaluation_cases/cassettes
```

Before submitting, inspect:

```bash
git status --short
git diff --check
```

Confirm that no `.env` or real credential is staged, and that the replay result reports
zero cassette misses and injection ASR 0.0. The submission metrics in the README and
evaluation guide are tied to the verification date and commit above.
