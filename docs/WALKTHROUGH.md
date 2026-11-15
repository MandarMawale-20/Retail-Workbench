# Retail Workbench Walkthrough

## 1. Install and start

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/`.

## 2. Upload retail data

Use the UI or `POST /api/runs` with `products.csv`, `stores.csv`, and
`orders.csv`. The response contains the run status, detected roles, cleaning
warnings, and relationship graph metadata.

## 3. Review cleaning

Inspect proposed and skipped cleaning steps. Approve only steps that are
supported by the displayed evidence, then submit the approval through the
review action.

## 4. Inspect the relationship graph

Open the run details and review matched-key ratio, cardinality, duplicate-key
safety, and unmatched left-side rows before asking cross-dataset questions.

## 5. Ask a revenue question

Ask: `Top 3 categories by revenue`.

The answer includes the executed plan, result preview, formula facts, selected
columns, and the query fingerprint.

## 6. Compare stores

Ask: `Compare revenue by store region`.

The planner uses only verified relationships and returns a grouped comparison.

## 7. Ask a stock-out question

Ask: `Which products are out of stock?`.

If the schema lacks the required inventory role, the workbench clarifies or
returns an explicit unsupported response instead of guessing.

## 8. Validate offline

```powershell
$env:LLM_PROVIDER = "none"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m mypy app
.\.venv\Scripts\python.exe -m app.evaluate `
  --input evaluation_cases\cases.json `
  --output evaluation_output\results.json `
  --artifacts-dir evaluation_output\artifacts `
  --provider replay `
  --cassettes evaluation_cases\cassettes `
  --skip-injection
Remove-Item Env:LLM_PROVIDER
```

The offline path requires no provider key and must report zero cassette misses.
