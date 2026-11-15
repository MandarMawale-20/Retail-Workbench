"""Expectation checking, grounding checks and determinism helpers for the evaluator."""
from __future__ import annotations

from typing import Any

from app.evidence.claim_verifier import ungrounded_numbers
from app.query.schemas import ChatResult

VOLATILE_KEYS = {"trace", "trace_id", "turn_id", "ts", "duration_ms", "metrics", "latency_ms", "recorded_at", "artifacts",
                 "run_dir", "session_id", "tool_log_ms", "latency_p50_ms", "latency_p95_ms"}


def strip_volatile(obj: Any) -> Any:
    """Drop wall-clock / random / path fields so two runs can be diffed."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj


def answer_is_grounded(result: ChatResult) -> bool:
    if result.status != "ok" or result.evidence is None:
        return True
    ev = result.evidence
    allowed = {float(n) for n in range(0, 21)} | {float(ev.rows_considered), float(ev.result_row_count)}
    for f in ev.filters:
        for v in f["value"] if isinstance(f["value"], list) else [f["value"]]:
            if isinstance(v, int | float) and not isinstance(v, bool):
                allowed.add(float(v))
    if result.plan:
        allowed.add(float(result.plan.limit))
    return not ungrounded_numbers(result.answer_text, ev.facts, allowed)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(b))


def check_chat_expectations(expect: dict[str, Any], result: ChatResult) -> dict[str, Any]:
    """Compare one chat turn against its (all optional) expectations."""
    details: list[dict] = []

    def record(name: str, ok: bool, expected: Any, actual: Any) -> None:
        details.append({"check": name, "passed": ok, "expected": expected, "actual": actual})

    if "status" in expect:
        record("status", result.status == expect["status"], expect["status"], result.status)
    for needle in expect.get("answer_contains", []):
        record(f"answer_contains:{needle}", needle.lower() in result.answer_text.lower(), needle, None)
    if "refusal" in expect:
        refused = result.status in ("unsupported", "refused")
        record("refusal", refused == expect["refusal"], expect["refusal"], refused)
    if "clarification" in expect:
        asked = result.status == "clarification_required"
        record("clarification", asked == expect["clarification"], expect["clarification"], asked)
    if "missing_roles" in expect:
        got = (result.error or {}).get("details", {}) or {}
        record("missing_roles", set(expect["missing_roles"]) <= set(got.get("missing_roles", [])), expect["missing_roles"], got.get("missing_roles"))
    ev = result.evidence
    if "row_count" in expect:
        record("row_count", ev is not None and ev.result_row_count == expect["row_count"], expect["row_count"], ev.result_row_count if ev else None)
    if "columns_used" in expect:
        record("columns_used", ev is not None and set(expect["columns_used"]) <= set(ev.selected_columns), expect["columns_used"],
               ev.selected_columns if ev else None)
    if "relationships_used" in expect:
        labels = {r["label"] for r in ev.relationships} if ev else set()
        swapped = {"=".join(reversed(label.split("="))) for label in labels}
        record("relationships_used", all(r in labels or r in swapped for r in expect["relationships_used"]),
               expect["relationships_used"], sorted(labels))
    if "stage" in expect:
        record("stage", ev is not None and ev.dataset_stage == expect["stage"], expect["stage"], ev.dataset_stage if ev else None)
    for key, wanted in (expect.get("numeric") or {}).items():
        metric, _, label = key.partition(":")
        label = label or "overall"
        actual = None
        if ev:
            for f in ev.facts:
                name_label, _, col = f.name.partition("|")
                if name_label.lower() == label.lower() and (metric in ("", "*") or metric.lower() in col.lower()) \
                        and not isinstance(f.value, str):
                    actual = f.value
                    break
        record(f"numeric:{key}", actual is not None and _close(float(actual), float(wanted)), wanted, actual)
    return {"matched": all(d["passed"] for d in details), "details": details}


def check_pipeline_expectations(expect: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    details: list[dict] = []

    def record(name: str, ok: bool, expected: Any, actual: Any) -> None:
        details.append({"check": name, "passed": ok, "expected": expected, "actual": actual})

    clean_rows = {d["name"]: d["rows_clean"] for d in result.get("datasets", [])}
    if "row_count_clean" in expect:
        wanted = expect["row_count_clean"]
        actual = clean_rows if isinstance(wanted, dict) else sum(v or 0 for v in clean_rows.values())
        record("row_count_clean", actual == wanted, wanted, actual)
    types = {i["issue_type"] for i in result.get("issues", [])}
    if "issues_include" in expect:
        record("issues_include", set(expect["issues_include"]) <= types, expect["issues_include"], sorted(types))
    if "idempotent" in expect:
        idem = all(c["passed"] for c in result.get("validation", {}).get("checks", []) if c["name"].startswith("idempotency"))
        record("idempotent", idem == expect["idempotent"], expect["idempotent"], idem)
    if "status" in expect:
        record("status", result.get("status") == expect["status"], expect["status"], result.get("status"))
    if "skipped_steps" in expect:
        skipped = [s["transformation"] for s in result.get("cleaning_plan", []) if s["status"] == "skipped_needs_review"]
        record("skipped_steps", set(expect["skipped_steps"]) <= set(skipped), expect["skipped_steps"], skipped)
    return {"matched": all(d["passed"] for d in details), "details": details}


def check_error_expectation(expect: dict[str, Any], result: ChatResult) -> dict[str, Any] | None:
    if "error_code" not in expect:
        return None
    actual = (result.error or {}).get("code")
    ok = actual == expect["error_code"]
    return {"check": "error_code", "passed": ok, "expected": expect["error_code"], "actual": actual}
