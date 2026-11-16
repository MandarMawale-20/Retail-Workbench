"""Batch evaluator: `python -m app.evaluate --input cases.json --output results.json --artifacts-dir ./out`.

Uses the same `run_pipeline()` / `ask()` as the API. One bad case never aborts the batch.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.agents.prompts import PROMPT_VERSION
from app.common import canonical_json
from app.config import Settings, get_settings
from app.errors import AppError, as_app_error
from app.evaluation.cases import Case, load_cases
from app.evaluation.compare import (
    answer_is_grounded,
    check_chat_expectations,
    check_error_expectation,
    check_pipeline_expectations,
)
from app.evaluation.injection import run_suite, summarize
from app.evaluation.metrics import path_metrics
from app.ingestion.loader import validate_file
from app.llm.base import MeteredProvider
from app.llm.cassette_provider import CassetteProvider
from app.llm.factory import build_provider
from app.llm.synthetic import SessionRef, SyntheticProvider
from app.query.schemas import ChatResult
from app.schemas import PipelineOptions
from app.services.chat import ask_with_stats, create_session
from app.services.context import Services
from app.services.pipeline import run_pipeline
from app.storage.artifacts import ArtifactStore, safe_segment

PROVIDERS = ("auto", "replay", "mock", "fallback", "openrouter", "synthetic")


def _options(raw: dict[str, Any], settings: Settings) -> PipelineOptions:
    fields = PipelineOptions.model_fields
    return PipelineOptions(**{k: v for k, v in raw.items() if k in fields}, max_upload_mb=settings.max_upload_mb)


def _skeleton(case: Case) -> dict[str, Any]:
    return {"case_id": case.case_id, "status": "ok", "dataset": {}, "profile_before": {}, "issues": [], "cleaning_plan": [],
            "profile_after": {}, "validation": {}, "chat_evaluation": [], "artifacts": {}, "errors": [],
            "metrics": {"duration_ms": 0, "llm_calls": 0, "llm_provider": "none", "fallback_used": False},
            "extra": case.extra}


def run_case(case: Case, args: argparse.Namespace, settings: Settings, repo, out_root: Path,
             records: list[dict[str, Any]], cassette_stats: dict[str, int]) -> dict[str, Any]:
    started = time.perf_counter()
    result = _skeleton(case)
    try:
        if case.error:
            raise case.error
        if not case.datasets:
            raise AppError("CASE_INPUT_INVALID", "case has no datasets", stage="evaluate")
        svc = Services(settings=settings, repo=repo, store=ArtifactStore(out_root / safe_segment(case.case_id)))
        files = [validate_file(Path(d["path"]), d["name"], max_upload_mb=settings.max_upload_mb) for d in case.datasets]
        run = run_pipeline(case.owner_id, files, _options(case.options, settings), svc)
        for key in ("profile_before", "issues", "cleaning_plan", "profile_after", "validation", "artifacts"):
            result[key] = run[key]
        result["dataset"] = {"run_id": run["run_id"], "fingerprint": run["fingerprint"], "datasets": run["datasets"],
                             "data_version": run.get("data_version"), "status": run["status"], "warnings": run["warnings"]}
        result["trace"] = run.get("trace", [])
        if run["error"]:
            result["errors"].append(run["error"])
            result["status"] = "failed"
        pipe_check = check_pipeline_expectations(case.expect.get("pipeline", {}), run) if case.expect.get("pipeline") else None
        if pipe_check:
            result["pipeline_expectation_results"] = pipe_check
        if case.chat and not run["error"] and run["status"] in ("completed", "completed_with_warnings"):
            _run_chat(case, args, settings, svc, run, result, records, cassette_stats)
        if result["status"] == "ok":
            mismatched = (pipe_check and not pipe_check["matched"]) or any(
                not t["expectation_results"]["matched"] for t in result["chat_evaluation"])
            chat_errors = any(t["status"] == "error" and t["expectation_results"]["details"] == [] for t in result["chat_evaluation"])
            if mismatched or chat_errors or run["status"] == "needs_review":
                result["status"] = "partial"
    except AppError as exc:
        result["errors"].append(exc.to_dict())
        result["status"] = "error"
    except Exception as exc:   # per-case isolation: unexpected failures are data, not crashes
        result["errors"].append(as_app_error(exc, "evaluate").to_dict())
        result["status"] = "error"
    result["metrics"]["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _make_provider(case: Case, args: argparse.Namespace, settings: Settings, svc: Services, ref: SessionRef):
    mode = args.provider
    if case.mock_responses is not None:
        return build_provider("mock", settings, svc.repo, PROMPT_VERSION, case_id=case.case_id, mock_responses=case.mock_responses)
    if mode == "synthetic":
        if not args.record_cassettes:
            raise AppError("CASE_INPUT_INVALID", "--provider synthetic requires --record-cassettes", stage="evaluate")
        model = settings.openrouter_model
        inner = CassetteProvider(Path(args.record_cassettes) / f"{case.case_id}.jsonl", mode="record", prompt_version=PROMPT_VERSION,
                                 model=model, inner=SyntheticProvider(svc, case.owner_id, ref))
        return MeteredProvider(inner)
    return build_provider(mode, settings, svc.repo, PROMPT_VERSION, case_id=case.case_id,
                          cassette_dir=Path(args.cassettes) if args.cassettes else None,
                          record_dir=Path(args.record_cassettes) if args.record_cassettes else None)


def _run_chat(case: Case, args, settings: Settings, svc: Services, run: dict, result: dict, records: list, cassette_stats: dict) -> None:
    ref = SessionRef()
    ref.run_id = run["run_id"]
    provider = _make_provider(case, args, settings, svc, ref)
    session_id = create_session(svc, case.owner_id, run["run_id"])
    ref.session_id = session_id
    result["metrics"]["llm_provider"] = provider.name if provider is not None else "none"
    for turn in case.chat:
        expect = turn.get("expect") or {}
        owner = turn.get("as_owner") or case.owner_id
        entry: dict[str, Any] = {"question": turn["question"]}
        try:
            outcome = ask_with_stats(svc, owner, session_id, turn["question"], provider=provider, idem_key=turn.get("idempotency_key"))
            chat, stats, trace, tool_log = outcome.result, outcome.stats, outcome.trace, outcome.tool_log
        except AppError as exc:
            chat = ChatResult(status="error", answer_text=exc.message, error=exc.to_dict())
            from app.services.chat import QuestionStats
            stats, trace, tool_log = QuestionStats(), [], None
        checks = check_chat_expectations(expect, chat)
        extra = check_error_expectation(expect, chat)
        if extra:
            checks["details"].append(extra)
            checks["matched"] = checks["matched"] and extra["passed"]
        grounded = answer_is_grounded(chat)
        numeric_total = len(expect.get("numeric") or {})
        numeric_hit = sum(1 for d in checks["details"] if d["check"].startswith("numeric:") and d["passed"])
        entry.update({
            "plan": chat.plan.model_dump() if chat.plan else None,
            "answer": {"text": chat.answer_text, "source": chat.answer_source, "status": chat.status},
            "trace": {"trace_id": chat.trace_id, "events": trace}, "status": chat.status,
            "validation": chat.validation.model_dump() if chat.validation else None,
            "evidence": chat.evidence.model_dump() if chat.evidence else None,
            "clarification": chat.clarification, "error": chat.error,
            "tool_log": [c.model_dump() for c in tool_log.calls] if tool_log else [],
            "stats": stats.__dict__, "grounded": grounded, "expectation_results": checks})
        result["chat_evaluation"].append(entry)
        result["metrics"]["llm_calls"] += stats.llm_calls
        result["metrics"]["fallback_used"] |= stats.fallback_used
        records.append({"stats": stats.__dict__, "status": chat.status, "expect": expect, "grounded": grounded,
                        "numeric_total": numeric_total, "numeric_hit": numeric_hit, "path": stats.path})
    inner = getattr(provider, "inner", None)
    if isinstance(inner, CassetteProvider):
        cassette_stats["hits"] += inner.hits
        cassette_stats["misses"] += inner.misses
    elif provider is not None:
        cassette_stats["hits"] += sum(1 for c in provider.calls if c.ok and c.source == "replay")
        cassette_stats["misses"] += sum(c.cassette_miss for c in provider.calls)


def build_summary(results: list[dict[str, Any]], records: list[dict[str, Any]], cassette: dict[str, int]) -> dict[str, Any]:
    by_path: dict[str, list] = defaultdict(list)
    for r in records:
        by_path[r["path"]].append(r)
    produced = [r for r in records if r["stats"]["plan_produced"]]
    ok_answers = [r for r in records if r["status"] == "ok"]
    abstain = [r for r in records if r["expect"].get("clarification") or r["expect"].get("refusal")]
    handled = [r for r in abstain if r["status"] in ("clarification_required", "unsupported", "refused")]
    pipelines = [r for r in results if r["dataset"]]
    expected_error_cases = {"bad_case_missing_file", "bad_case_malformed"}
    expected_errors = sum(1 for r in results if r["case_id"] in expected_error_cases and r["status"] == "error")
    unexpected_errors = sum(1 for r in results if r["status"] == "error" and r["case_id"] not in expected_error_cases)
    return {
        "cases": len(results), "by_status": dict(Counter(r["status"] for r in results)),
        "expected_errors": expected_errors,
        "unexpected_errors": unexpected_errors,
        "pipeline_pass_rate": round(sum(1 for r in pipelines if r["dataset"]["status"] in ("completed", "completed_with_warnings")) / len(pipelines), 4) if pipelines else None,
        "plan_validity_rate": round(sum(r["stats"]["plan_valid_first"] for r in produced) / len(produced), 4) if produced else None,
        "grounding_rate": round(sum(r["grounded"] for r in ok_answers) / len(ok_answers), 4) if ok_answers else None,
        "correct_abstention_rate": round(len(handled) / len(abstain), 4) if abstain else None,
        "fallback_usage_rate": round(sum(r["stats"]["planner_source"] == "fallback" for r in records) / len(records), 4) if records else None,
        "total_llm_calls": sum(r["stats"]["llm_calls"] for r in records),
        "by_path": {p: path_metrics(p, rs) for p, rs in sorted(by_path.items())},
        "cassette": cassette, "prompt_version": PROMPT_VERSION,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evaluate", description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, default=None, help="default: EVAL_PROVIDER (auto)")
    parser.add_argument("--cassettes", default=None, help="directory with {case_id}.jsonl cassettes")
    parser.add_argument("--record-cassettes", default=None, help="record LLM responses here (with --provider openrouter|synthetic)")
    parser.add_argument("--skip-injection", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    args.provider = args.provider or settings.eval_provider
    out_root = Path(args.artifacts_dir)
    try:
        cases = load_cases(Path(args.input))
        out_root.mkdir(parents=True, exist_ok=True)
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).touch()
        svc = Services.create(settings, artifacts_dir=out_root, sqlite_path=out_root / "meta.sqlite")
    except (AppError, OSError) as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 2
    records: list[dict[str, Any]] = []
    cassette = {"hits": 0, "misses": 0}
    results = [run_case(c, args, settings, svc.repo, out_root, records, cassette) for c in cases]
    summary = build_summary(results, records, cassette)
    if not args.skip_injection:
        summary["injection"] = summarize(run_suite(out_root / "_injection", settings))
    payload = json.loads(canonical_json({"summary": summary, "cases": results}))
    try:
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"fatal: cannot write output: {exc}", file=sys.stderr)
        return 2
    print(f"evaluated {len(results)} case(s): {summary['by_status']} "
          f"(expected errors: {summary['expected_errors']}, unexpected errors: {summary['unexpected_errors']})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
