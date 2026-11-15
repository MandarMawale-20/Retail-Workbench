"""Per-path chat-quality metrics (EVALUATION.md §9)."""
from __future__ import annotations

import math
from typing import Any


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def path_metrics(path: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """`records`: one per chat question with keys stats (dict), status, expect (dict), matched checks, grounded."""
    n = len(records)
    # questions designed to be rejected/clarified are scored on abstention metrics, not on first-plan validity
    produced = [r for r in records if r["stats"]["plan_produced"]
                and not (r["expect"].get("clarification") or r["expect"].get("refusal"))]
    numeric = [(r["numeric_total"], r["numeric_hit"]) for r in records if r["numeric_total"]]
    issued = [r for r in records if r["status"] == "clarification_required"]
    expected_clar = [r for r in records if r["expect"].get("clarification") is True]
    expected_refusal = [r for r in records if r["expect"].get("refusal") is True]
    ok_answers = [r for r in records if r["status"] == "ok"]
    attempted = sum(r["stats"]["narrator_attempted"] for r in records)
    return {
        "path": path, "n_questions": n,
        "plan_validity_rate": _rate(sum(r["stats"]["plan_valid_first"] for r in produced), len(produced)),
        "repair_rate": _rate(sum(r["stats"]["repaired"] for r in records), n),
        "tool_use_compliance": _rate(sum(r["stats"]["tool_compliant"] for r in produced), len(produced)),
        "tools_per_question_mean": round(sum(r["stats"]["tools_used"] for r in records) / n, 3) if n else 0.0,
        "tools_per_question_max": max((r["stats"]["tools_used"] for r in records), default=0),
        "invalid_json_rate": _rate(sum(r["stats"]["invalid_json"] for r in records), n),
        "exact_answer_accuracy": _rate(sum(h for _, h in numeric), sum(t for t, _ in numeric)),
        "grounding_rate": _rate(sum(r["grounded"] for r in ok_answers), len(ok_answers)),
        "clarification_precision": _rate(sum(1 for r in issued if r["expect"].get("clarification") is True), len(issued)),
        "clarification_recall": _rate(sum(1 for r in expected_clar if r["status"] == "clarification_required"), len(expected_clar)),
        "correct_refusal_rate": _rate(sum(1 for r in expected_refusal if r["status"] in ("unsupported", "refused")), len(expected_refusal)),
        "narrator_acceptance_rate": _rate(sum(r["stats"]["narrator_accepted"] for r in records), attempted),
        "fallback_usage_rate": _rate(sum(r["stats"]["planner_source"] == "fallback" for r in records), n),
        "llm_calls_per_question": round(sum(r["stats"]["llm_calls"] for r in records) / n, 3) if n else 0.0,
        "tokens_per_question": round(sum(r["stats"]["tokens"] for r in records) / n, 1) if n else 0.0,
        "latency_p50_ms": _pct([r["stats"]["latency_ms"] for r in records], 0.5),
        "latency_p95_ms": _pct([r["stats"]["latency_ms"] for r in records], 0.95),
    }
