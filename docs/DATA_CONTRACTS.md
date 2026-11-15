# DATA_CONTRACTS.md — Schemas, IDs, errors, SQLite DDL

All contracts are Pydantic v2 models in `app/**/schemas.py`. Unknown fields in *inputs from evaluator cases* are preserved; unknown fields in *LLM output* are **rejected** (`extra="forbid"`).

---

## 1. IDs and hashing

```python
def h(*parts) -> str:            # canonical content hash
    return sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

dataset_version_id = "dv_" + file_sha256[:8]
run_id             = "run_" + run_fingerprint[:12]
step_id            = "step_" + h(op, dataset_name, sorted(columns), params)[:12]
issue_id           = "iss_"  + h(dataset_name, issue_type, sorted(columns))[:12]
relationship_id    = "rel_"  + h(left_col_id, right_col_id, graph_inputs)[:12]
plan_id            = "qp_"   + h(plan_json)[:12]
evidence_id        = "ev_"   + h(plan_id, dataset_versions, sql_fingerprint)[:12]
fact_id            = "f_"    + h(evidence_id, name, key)[:10]
trace_id           = "tr_"   + uuid4 hex     # the only random id; excluded from comparisons
```
`column_id = "<dataset_logical_name>.<column_name>"` (names normalized: trimmed; original name kept in catalog).

`run_fingerprint = h(owner_id, sorted[(logical_name, file_sha256)], CLEANER_VERSION, ROLE_CATALOG_VERSION, POLICY_VERSION, advisor_config_hash)`

---

## 2. Errors

```python
class AppError(Exception):
    code: ErrorCode; message: str; retryable: bool; stage: str; details: dict | None
ErrorCode = Literal[
 "INVALID_FILE","SCHEMA_INFERENCE_FAILED","CLEANING_VALIDATION_FAILED","UNKNOWN_RELATIONSHIP",
 "UNSAFE_JOIN","INVALID_QUERY_PLAN","UNSUPPORTED_QUERY","CLARIFICATION_REQUIRED","QUERY_TIMEOUT",
 "LLM_TIMEOUT","LLM_RATE_LIMITED","INVALID_LLM_JSON","ISOLATION_VIOLATION","INTERNAL_ERROR","CASE_INPUT_INVALID"]
```
Serialized: `{"code","message","retryable","stage","details"}`.

---

## 3. Pipeline contracts

```python
class ExecutionContext(BaseModel):       # server-owned, never from LLM
    owner_id: str; session_id: str | None
    active_dataset_version_ids: list[str]
    dataset_stage: Literal["clean","raw"] = "clean"
    graph_version: str | None
    policy_version: str; trace_id: str

class PipelineOptions(BaseModel):
    policy: Literal["unattended","interactive_review"] = "unattended"
    force: bool = False
    use_advisor: bool = True
    max_upload_mb: int = 100

class InputFile(BaseModel):
    logical_name: str                    # sanitized: [a-z0-9_]+
    path: Path                           # staged by server
```

```python
class ColumnProfile(BaseModel):
    name: str; inferred_type: Literal["integer","decimal","string","date","boolean","mixed","empty"]
    null_count: int; null_pct: float; distinct_count: int; duplicate_value_count: int
    min: str | None; max: str | None
    numeric: dict | None                 # mean, std, p25/p50/p75 over parseable values
    top_values: list[tuple[str,int]]     # ≤10, sanitized
    parse_failures: dict[str,int]        # {"numeric": n, "date": n}
    candidate_key: bool; role: str | None; role_confidence: float | None

class DatasetProfile(BaseModel):
    dataset: str; dataset_version_id: str; stage: Literal["raw","clean"]
    row_count: int; column_count: int; file_bytes: int
    encoding: str; delimiter: str; header_detected: bool
    structural_reject_count: int; exact_duplicate_rows: int
    columns: list[ColumnProfile]; profile_hash: str
```

```python
class RoleBinding(BaseModel):
    role: str; dataset_version_id: str; column_id: str; confidence: float
    inference_source: list[Literal["alias","type","distribution","relationship"]]
    status: Literal["verified","candidate","ambiguous","rejected"]

class Issue(BaseModel):
    issue_id: str; dataset: str; column: str | None
    issue_type: Literal["whitespace","case_variants","null_tokens","mixed_numeric","numeric_parse_failures",
        "date_format_ambiguous","date_parse_failures","exact_duplicate_rows","duplicate_ids",
        "category_variants","all_null_column","negative_values","structural_rejects","header_only","unresolved_role"]
    severity: Literal["low","medium","high"]; count: int; examples: list[str]   # ≤5, sanitized
    status: Literal["detected","planned","resolved","needs_review"]

class CleaningStep(BaseModel):
    step_id: str; issue_id: str; dataset_version_id: str; dataset: str
    columns: list[str]; transformation: str; parameters: dict
    reason: str; risk: Literal["low","medium","high"]
    source: Literal["rule","llm","user"]; confidence: float | None
    status: Literal["proposed","approved","applied","skipped_needs_review","rejected","failed"]
    rows_affected: int | None

class ValidationReport(BaseModel):
    passed: bool
    checks: list[dict]      # {name, passed, expected, actual}
    # required checks: row_invariant, column_invariant, step_metadata, parse_failures_listed, idempotency
    rows_accepted: int; rows_clean: int; exact_dups_removed: int; unresolved_issues: list[str]
```

### Registered transformations (`cleaning/transformations.py`)
| name | risk | idempotent by | notes |
|---|---|---|---|
| `normalize_null_tokens` | low | tokens → NULL | tokens from `null_tokens.yaml` |
| `trim_whitespace` | low | strip+collapse | |
| `normalize_case` | low | title/upper per role | only categorical/id roles |
| `normalize_category_alias` | low (alias/fuzzy ≥0.92 or advisor ≥0.90) / medium otherwise | canonical→canonical | mapping persisted in params |
| `parse_numeric` | low | typed column stays typed | currency symbols/thousands removed; failures → NULL + `parse_failures.json` |
| `parse_date_iso` | low (unambiguous) | ISO→ISO | ambiguous ⇒ not generated as applicable |
| `format_sku` | low | upper, strip internal spaces/dashes policy | |
| `dedup_exact_rows` | low | after normalization | counts `D_e` |
| `flag_duplicate_ids` | n/a | no mutation | issue only |

Execution order fixed: null_tokens → whitespace → case → category → numeric → date → sku → dedup.

---

## 4. Relationship graph

```python
class Relationship(BaseModel):
    relationship_id: str; graph_version: str
    left_column_id: str; right_column_id: str
    left_dataset_version_id: str; right_dataset_version_id: str
    cardinality: Literal["one_to_one","many_to_one","one_to_many","many_to_many"]
    left_null_rate: float; right_null_rate: float
    left_duplicate_keys: int; right_duplicate_keys: int
    matched_key_ratio: float; unmatched_left: int
    role_compatible: bool
    status: Literal["verified","candidate","ambiguous","unsafe","rejected"]
    reasons: list[str]
```
`verified` requires: role-compatible, type-compatible, matched ratio ≥ 0.5 (configurable), and one side unique (≥ 99.9% unique, 0 duplicates). Duplicate keys on the "one" side ⇒ `unsafe`.

---

## 5. Metrics

```python
class MetricDefinition(BaseModel):
    metric_id: str; name: str; description: str; unit: str
    variants: list[MetricVariant]; null_policy: str; definition_version: str
    default_grain: str | None

class MetricVariant(BaseModel):
    variant_id: str; required_roles: list[str]; optional_roles: list[str] = []
    formula_id: str; requires_relationship_between: list[tuple[str,str]] = []   # role pairs
```
`metrics/binder.py` returns `MetricBinding{metric_id, variant_id, role_to_column: dict[str,str], definition_version}` or raises `UNSUPPORTED_QUERY` with `details.missing_roles`.

---

## 6. QueryPlan (LLM output + internal)

```python
Scalar = str | int | float | bool

class Filter(BaseModel, extra="forbid"):
    column_id: str
    op: Literal["eq","neq","gt","gte","lt","lte","in","between","contains","is_null","not_null"]
    value: Scalar | list[Scalar] | None = None

class DateGrouping(BaseModel, extra="forbid"):
    column_id: str
    grain: Literal["month","quarter","year","week","day"]

class DateRangeFilter(BaseModel, extra="forbid"):
    column_id: str
    start: date
    end: date

class Aggregation(BaseModel, extra="forbid"):
    function: Literal["count","sum","avg","min","max","distinct_count"]
    column_id: str | None = None          # None only for count(*)
    alias: str = Field(pattern=r"^[a-z][a-z0-9_]{0,40}$")

class MetricRef(BaseModel, extra="forbid"):
    metric_id: str
    alias: str | None = None

class SortSpec(BaseModel, extra="forbid"):
    field: str                            # alias or column_id
    direction: Literal["asc","desc"] = "desc"

class QueryPlan(BaseModel, extra="forbid"):
    intent: Literal["aggregate","list","distinct","describe","top_n","compare"]
    datasets: list[str]                   # logical names; versions bound by server
    metrics: list[MetricRef] = []
    aggregations: list[Aggregation] = []
    group_by: list[str] = []              # column_ids
    date_groupings: list[DateGrouping] = []
    filters: list[Filter] = []
    date_ranges: list[DateRangeFilter] = []
    relationship_ids: list[str] = []
    sort: list[SortSpec] = []
    limit: int = Field(default=100, ge=1, le=1000)
    select_columns: list[str] = []        # for intent=list/distinct
    dataset_stage: Literal["clean","raw"] = "clean"

class PlanDelta(BaseModel, extra="forbid"):    # follow-ups
    op: Literal["add_filter","remove_filter","replace_filter","replace_group_by","replace_metric",
                "add_metric","replace_sort","replace_limit","reset"]
    filter: Filter | None = None
    column_ids: list[str] = []
    metrics: list[MetricRef] = []
    sort: list[SortSpec] = []
    limit: int | None = None
    carry_context: bool = True            # "among those" ⇒ True; new topic ⇒ False

class PlannerOutput(BaseModel, extra="forbid"):  # parsed protocol
    type: Literal["tool_call","final_plan","final_delta","clarify","unanswerable"]
    calls: list[ToolCall] = []; plan: QueryPlan | None = None; delta: list[PlanDelta] | None = None
    question: str | None = None; options: list[str] = []
    reason: str | None = None; missing: list[str] = []
```
Plan **must not** contain: sql, code, path, owner, version, join conditions. `extra="forbid"` rejects them.

```python
class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationIssue] = []    # {code, path, message, suggestion?}
    warnings: list[str] = []
    bound_dataset_versions: dict[str,str] = {}
    bound_relationships: list[str] = []
    bound_metrics: list[MetricBinding] = []
```

---

## 7. Execution, facts, evidence

```python
class ExecutionResult(BaseModel):
    columns: list[str]; rows: list[list]            # ≤ limit
    rows_considered: int                            # after filters/joins, before aggregation
    result_row_count: int; duration_ms: int
    sql_fingerprint: str                            # hash of compiled SQL text + param types (not values)
    truncated: bool

class Fact(BaseModel):
    fact_id: str; name: str; value: float | int | str; unit: Literal["count","currency","percent","ratio","text"]
    formula_id: str; source_ref: str                # row index / metric id

class EvidenceBundle(BaseModel):
    evidence_id: str; trace_id: str
    dataset_versions: dict[str,str]; dataset_stage: str; graph_version: str | None
    relationships: list[dict]                       # id, cardinality, status
    metric_definitions: list[dict]; role_bindings: list[dict]
    selected_columns: list[str]; filters: list[dict]; operations: list[dict]
    aggregation_grain: list[str]; null_policy: str | None
    rows_considered: int; result_row_count: int; result_preview: list[dict]   # ≤20
    facts: list[Fact]; assumptions: list[str]       # e.g. "gross_sales_revenue used as revenue basis"
    sql_fingerprint: str

class ChatResult(BaseModel):
    status: Literal["ok","clarification_required","unsupported","refused","error"]
    answer_text: str; answer_source: Literal["template","narrator","none"]
    plan: QueryPlan | None; validation: ValidationResult | None
    evidence: EvidenceBundle | None; clarification: dict | None
    error: dict | None; trace_id: str; turn_id: str
```

### Narrator
```python
class NarratorSegment(BaseModel, extra="forbid"):
    type: Literal["text","fact"]; text: str | None = None; fact_id: str | None = None
class NarratorOutput(BaseModel, extra="forbid"): segments: list[NarratorSegment]
```

---

## 8. Conversation state

```python
class ConversationState(BaseModel):
    session_id: str; owner_id: str
    active_dataset_version_ids: list[str]; graph_version: str | None
    previous_plan: QueryPlan | None; previous_evidence_id: str | None
    active_filters: list[Filter] = []; active_metric_ids: list[str] = []
    pending_clarification: dict | None
    turn_count: int
```

---

## 9. LLM provider interface

```python
class LLMProvider(Protocol):
    name: str
    def complete_json(self, *, kind: Literal["planner","advisor","narrator"],
                      messages: list[dict], max_tokens: int, trace_id: str) -> LLMResponse: ...

class LLMResponse(BaseModel):
    text: str; model: str; tokens_in: int | None; tokens_out: int | None
    latency_ms: int; cached: bool
```
`NimProvider`: `OpenAI(base_url=NIM_BASE_URL, api_key=NVIDIA_API_KEY, timeout=LLM_TIMEOUT_S, max_retries=0)`; our own limiter/retry wraps the call. `MockProvider(fixtures: dict[str, str])` returns recorded text keyed by `h(kind, canonical_messages)`; evaluator cases may pass `mock_responses` (ordered list) instead.

---

## 10. SQLite DDL (abridged)

```sql
PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;

CREATE TABLE runs(run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
  status TEXT NOT NULL, policy TEXT, created_at TEXT, finished_at TEXT, error_json TEXT, warnings_json TEXT,
  UNIQUE(owner_id, fingerprint));
CREATE TABLE dataset_versions(version_id TEXT PRIMARY KEY, run_id TEXT, owner_id TEXT, name TEXT, stage TEXT,
  artifact_path TEXT, sha256 TEXT, row_count INT, parent_version_id TEXT);
CREATE TABLE artifacts(artifact_id TEXT PRIMARY KEY, run_id TEXT, owner_id TEXT, kind TEXT, path TEXT, sha256 TEXT, bytes INT);
CREATE TABLE profiles(profile_id TEXT PRIMARY KEY, version_id TEXT, profile_json TEXT, profile_hash TEXT);
CREATE TABLE issues(issue_id TEXT, run_id TEXT, issue_json TEXT, PRIMARY KEY(issue_id, run_id));
CREATE TABLE role_bindings(run_id TEXT, version_id TEXT, role TEXT, column_id TEXT, confidence REAL, status TEXT);
CREATE TABLE cleaning_steps(step_id TEXT, run_id TEXT, step_json TEXT, status TEXT, PRIMARY KEY(step_id, run_id));
CREATE TABLE relationship_graphs(graph_version TEXT PRIMARY KEY, run_id TEXT, graph_json TEXT);
CREATE TABLE sessions(session_id TEXT PRIMARY KEY, owner_id TEXT, run_id TEXT, state_json TEXT, created_at TEXT);
CREATE TABLE chat_turns(turn_id TEXT PRIMARY KEY, session_id TEXT, idem_key TEXT, question TEXT, status TEXT,
  plan_json TEXT, validation_json TEXT, execution_json TEXT, evidence_id TEXT, answer_json TEXT, trace_id TEXT,
  UNIQUE(session_id, idem_key));
CREATE TABLE evidence(evidence_id TEXT PRIMARY KEY, evidence_json TEXT);
CREATE TABLE traces(trace_id TEXT, seq INT, ts TEXT, component TEXT, status TEXT, duration_ms INT, event_json TEXT,
  PRIMARY KEY(trace_id, seq));
CREATE TABLE jobs(job_id TEXT PRIMARY KEY, run_id TEXT, status TEXT, attempts INT DEFAULT 0,
  retryable INT, error_json TEXT, created_at TEXT, started_at TEXT, finished_at TEXT);
CREATE TABLE advisor_cache(cache_key TEXT PRIMARY KEY, response_json TEXT);
CREATE TABLE llm_cache(cache_key TEXT PRIMARY KEY, response_text TEXT, model TEXT, created_at TEXT);
```
Every query on these tables filters by `owner_id` (repositories require it as an argument).

---

## 11. Config files (`config/`)

- `roles.yaml`: `role -> {aliases: [...], types: [...], negative_aliases: [...]}`
- `categories.yaml`: canonical → aliases (`T-Shirt: [tee, tshirt, t shirt, t-shirts]`)
- `null_tokens.yaml`: `["", "n/a", "na", "null", "none", "-", "--", "?"]`
- `metrics.yaml`: synonyms (`revenue → gross_sales_revenue`, `sales → gross_sales_revenue`, `aov`, `return rate`…), return-status values (`returned, return, refunded`)
- `thresholds.yaml`: role thresholds, fuzzy thresholds, numeric parse success ratio (default 0.95), limits


---

## 12. AI-track additions

```python
class ToolCallRecord(BaseModel):
    seq: int; tool: Literal["get_schema","list_distinct","preview_join","get_metric_definition"]
    args: dict; result_hash: str; duration_ms: int; source: Literal["llm_requested","code_injected","fallback"]

class ToolLog(BaseModel):                 # per question, persisted in the trace
    calls: list[ToolCallRecord] = []
    def has(self, tool: str, **args) -> bool: ...

class RequiredEvidence(BaseModel):        # computed from the plan by code
    list_distinct: list[str] = []         # column_ids with eq/in/contains on string columns
    preview_join: list[str] = []          # relationship_ids in the plan
    get_metric_definition: list[str] = [] # metrics flagged binding_uncertain
```
Validator issue code added: `TOOL_EVIDENCE_MISSING` (`path` = plan field, `message` names the missing tool). It is a `ValidationIssue.code`, not an `AppError`.

```python
class CassetteEntry(BaseModel):
    key: str; kind: Literal["planner","narrator","advisor"]; seq: int
    response_text: str | None = None
    simulate: Literal["timeout","rate_limited","invalid_json"] | None = None
    model: str; tokens_in: int | None; tokens_out: int | None; latency_ms: int | None
    recorded_at: str | None               # excluded from determinism diffs

class InjectionAttackResult(BaseModel):
    attack_id: str; vector: str; canary: str
    blocked_at: Literal["prompt_fencing","planner_output_schema","tool_allowlist","validator",
                        "executor_limits","claim_verifier","none"]
    attack_success: bool; evidence: dict

class PathMetrics(BaseModel):
    path: Literal["replay","fallback","nim_live","mock"]
    n_questions: int; plan_validity_rate: float; repair_rate: float; tool_use_compliance: float
    tools_per_question_mean: float; invalid_json_rate: float; exact_answer_accuracy: float
    grounding_rate: float; clarification_precision: float; clarification_recall: float
    correct_refusal_rate: float; narrator_acceptance_rate: float; fallback_usage_rate: float
    llm_calls_per_question: float; tokens_per_question: float; latency_p50_ms: int; latency_p95_ms: int
```
New config: `PLANNER_MAX_INPUT_TOKENS` (default 6000), `EVAL_PROVIDER` (default `auto`), `PROMPT_VERSION` (string constant in `agents/prompts.py`, part of every cassette/cache key).
