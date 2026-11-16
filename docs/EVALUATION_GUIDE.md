# Evaluation Guide

## Keyless replay

Run the deterministic evaluator without an API key:

```bash
python -m app.evaluate --input evaluation_cases/cases.json --output results.json `
  --artifacts-dir ./evaluation_output --provider replay `
  --cassettes evaluation_cases/cassettes
```

The evaluator continues after malformed or missing-file cases and records structured
per-case errors. A case may contain a pipeline run and several chat questions, so the
number of cases is not the number of questions in the per-path metrics.

## Provider modes

`--provider` accepts `auto`, `replay`, `fallback`, `mock`, `openrouter`, and `synthetic`.

- `replay` reads committed cassette responses and never calls the network.
- `fallback` uses the deterministic rule planner.
- `openrouter` uses `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`, and
  `OPENROUTER_API_KEY`; use `--record-cassettes` only for an intentional live recording.
- `auto` tries replay, then the configured live provider, then fallback.
- `mock` consumes case-supplied responses.
- `synthetic` generates deterministic fixtures and requires `--record-cassettes`.

## Output

Each case contains `dataset`, `profile_before`, `issues`, `cleaning_plan`,
`profile_after`, `validation`, `chat_evaluation`, `artifacts`, `errors`, `metrics`,
`status`, and preserved `extra` fields. Summary metrics include status counts,
pipeline pass rate, plan validity, grounding, refusal, fallback use, per-path metrics,
cassette hits/misses, and injection results.

Exact-answer accuracy is measured only over questions with an expected answer in the
seeded evaluation cases. It is not a general model-quality estimate.

## Determinism

Cassette keys include the prompt version, model, request kind, and canonical messages.
Repeated replay runs should produce the same substantive output. A prompt or model
change causes a visible cassette miss rather than silently reusing stale output.

## Scale validation

The end-to-end benchmark can be run with:

```bash
python -m scripts.benchmark_scale --rows 100000 250000
```

The 2026-11-16 run completed 100,000 rows in 84.96 seconds and 250,000 rows in
206.65 seconds on the validation machine. These are local benchmark observations, not
guarantees for every machine or for the configured five-million-row ceiling.
