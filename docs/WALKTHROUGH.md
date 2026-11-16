# 3–5 minute walkthrough

This script uses the keyless path and should be recorded only after the clean-clone dry run.
It assumes the server is started from the repository root.

## 0:00–0:30 — Clean-clone dry run

```bash
uv sync
uv run pytest -q
uv run python -m scripts.make_sample_data
uv run python -m app.evaluate --input evaluation_cases/cases.json --output results.json \
  --artifacts-dir evaluation_output --provider replay \
  --cassettes evaluation_cases/cassettes
```

Expected terminal result:

```text
evaluated 12 case(s): {'ok': 10, 'error': 2} (expected errors: 2, unexpected errors: 0)
```

Open `results.json` and show `summary.cassette.misses = 0` and
`summary.injection.asr = 0.0`.

## 0:30–1:15 — Start the app and run one cleaning job

```bash
uv run uvicorn app.main:app --reload
```

Open `http://localhost:8000`. Leave the `Owner` field as `demo`, click the file chooser,
multi-select `sample_data/products.csv`, `stores.csv`, `orders.csv`, `returns.csv`,
`inventory.csv`, and `customers.csv`, then click `Create run`.

Expected result: a completed or completed-with-warnings run. Expand `Issues` and
`Cleaning plan (reason / risk / source / status)`.
The sample run has clean rows: products 40, stores 6, orders 400, returns 60, inventory
240, customers 40. Show `category_variants`, `exact_duplicate_rows`, `all_null_column`,
`negative_values`, and `date_format_ambiguous`.

The API alternative is `POST /api/runs` with multipart `files`, optional `policy`, and
`X-Owner-Id: demo`. The response contains `run_id`, status, warnings, validation, and trace.

## 1:15–1:45 — Show an ambiguous decision

In the expanded cleaning plan, find the `orders.order_date` step.
Expected result: `parse_date_iso` is `skipped_needs_review`; the ambiguous date column is
not silently interpreted.

For interactive review, click the `Approve` button beside a pending step. Approval creates
a new run; it does not silently alter the original unattended result.

## 1:45–2:30 — Ask a retail question

The page creates a session automatically after the run. In the Chat input, type the
question and click `Ask`:

```text
Top 5 categories by revenue
```

Expected answer begins with `T-Shirt leads with 354,441.00` and states
`Revenue basis: gross sales revenue`. Expand `Evidence`: show clean stage, products and
orders versions, selected columns, the verified relationship
`orders.product_id=products.product_id`, 400 rows considered, five result rows, and facts.

The evidence preview contains T-Shirt 354441.0, Electronics 177186.0, Home & Kitchen
166610.0, Footwear 151844.0, and Groceries 82495.0.

## 2:30–3:15 — Multi-turn state and planner loop

Ask:

```text
only stores in the West region
among those, which category has the highest return rate?
among those, which is lowest on stock?
```

Expected result: follow-up turns preserve the West filter and add the relevant metric and
joins. Expand `Planner-loop trace` and show:

```text
question -> planner/tool calls -> validation -> compile -> execute -> facts -> final
```

Replay traces report planner source `replay`; fallback traces report `fallback`. Both use
the same validator and executor.

## 3:15–3:45 — Ambiguity and refusal

Ask `Which products performed best?`. Expected result: targeted clarification because
“best” has multiple supported meanings. Ask `What is our profit margin?`. Expected result:
unsupported/refused response naming missing `unit_cost`; no evidence query is executed.

## 3:45–4:30 — Failure and recovery

Run:

```bash
uv run python -m app.evaluate --input evaluation_cases/cases.json \
  --output failure_results.json --artifacts-dir failure_artifacts --provider mock
```

Invalid-JSON, timeout, rate-limit, and malicious-plan cases remain isolated. The batch
continues; planner failures use fallback or a structured error, and failed cleaning
validation does not promote a clean artifact.

If live OpenRouter fails, switch to replay. If no cassette is available, use `--provider fallback`.
Do not present a live error as a successful model result.

## Recording checklist

- [ ] Fresh clone or clean worktree used for the dry run.
- [ ] No real key appears on screen or in terminal history.
- [ ] `pytest` passes before recording.
- [ ] Replay has zero cassette misses.
- [ ] Cleaning plan and validation artifact are visible.
- [ ] Evidence facts are shown, not just prose.
- [ ] Trace includes tools, validation, execution, and final answer.
- [ ] One clarification, one missing-data refusal, and one failure path are shown.
- [ ] Synthetic replay is not described as a live OpenRouter call.
