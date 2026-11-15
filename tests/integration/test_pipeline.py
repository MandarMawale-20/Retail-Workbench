import json
import shutil
from pathlib import Path

import pytest

from app.errors import AppError
from app.ingestion.loader import validate_file
from app.schemas import PipelineOptions
from app.services.context import Services
from app.services.pipeline import run_pipeline
from tests.conftest import OWNER, SIX, make_files


def step(result, dataset, transformation, column=None):
    return next(s for s in result["cleaning_plan"] if s["dataset"] == dataset and s["transformation"] == transformation
                and (column is None or column in s["columns"]))


def test_artifacts_profiles_plan_and_invariants(main_env, oracle):
    _, result, _ = main_env
    assert result["status"] == "completed_with_warnings" and result["validation"]["passed"]
    run_dir = Path(result["artifacts"]["run_dir"])
    for name in SIX:
        assert (run_dir / "datasets" / name / "raw.csv").exists() and (run_dir / "datasets" / name / "cleaned.csv").exists()
    assert (run_dir / "run.duckdb").exists()
    assert (run_dir / "relationships" / "clean_graph.json").exists()
    assert (run_dir / "relationships" / "raw_graph.json").exists()
    rows = {d["name"]: (d["rows_raw"], d["rows_clean"]) for d in result["datasets"]}
    assert rows["orders"] == (oracle.orders_all_rows, len(oracle.orders))
    dedup = step(result, "orders", "dedup_exact_rows")
    assert dedup["rows_affected"] == oracle.orders_all_rows - len(oracle.orders) == 6       # 5 exact + 1 only after trim/case
    for s in result["cleaning_plan"]:
        assert s["step_id"].startswith("step_") and s["reason"] and s["risk"] and s["source"] and s["status"]
    checks = {c["name"]: c["passed"] for c in result["validation"]["checks"]}
    assert all(checks.values()) and any(k.startswith("idempotency") for k in checks)
    assert set(result["profile_before"]) == set(result["profile_after"]) == set(SIX)


def test_ambiguous_date_is_skipped_not_interpreted(main_env):
    _, result, catalog = main_env
    s = step(result, "orders", "parse_date_iso", "order_date")
    assert s["status"] == "skipped_needs_review" and s["risk"] == "high"
    assert catalog.column("orders.order_date").date_ambiguous and catalog.column("orders.order_date").sql_type == "VARCHAR"
    unambiguous = step(result, "returns", "parse_date_iso", "return_date")       # day > 12 present => resolved
    assert unambiguous["status"] == "applied" and catalog.column("returns.return_date").sql_type == "DATE"


def test_category_alias_applied_and_parse_failures_listed(main_env):
    _, result, _ = main_env
    assert step(result, "products", "normalize_category_alias")["status"] == "applied"
    failures = json.loads((Path(result["artifacts"]["run_dir"]) / "datasets" / "orders" / "parse_failures.json").read_text())
    assert [f["value"] for f in failures] == ["1.299,50"]


def test_issues_cover_planted_traps(main_env):
    _, result, _ = main_env
    found = {(i["dataset"], i["issue_type"]) for i in result["issues"]}
    assert {("customers", "all_null_column"), ("orders", "negative_values"), ("orders", "exact_duplicate_rows"),
            ("orders", "date_format_ambiguous"), ("products", "category_variants"), ("orders", "null_tokens")} <= found


def test_rerun_same_input_returns_same_run(main_env, data_dir):
    svc, first, _ = main_env
    again = run_pipeline(OWNER, make_files(data_dir, SIX), PipelineOptions(), svc)
    assert again["reused"] and again["run_id"] == first["run_id"]
    forced = run_pipeline(OWNER, make_files(data_dir, SIX), PipelineOptions(force=True), svc)
    assert not forced["reused"] and forced["run_id"] == first["run_id"]
    assert forced["profile_after"] == first["profile_after"] and forced["cleaning_plan"] == first["cleaning_plan"]


def test_clean_cleaned_file_is_identity(main_env, tmp_path):
    svc, result, _ = main_env
    cleaned = Path(result["artifacts"]["run_dir"]) / "datasets" / "customers" / "cleaned.csv"
    copy = tmp_path / "customers.csv"
    shutil.copy(cleaned, copy)
    again = run_pipeline("idem", [validate_file(copy, "customers", max_upload_mb=100)], PipelineOptions(), svc)
    assert again["datasets"][0]["rows_raw"] == again["datasets"][0]["rows_clean"]
    assert all(s["rows_affected"] in (0, None) or s["status"] != "applied" for s in again["cleaning_plan"])


def test_edge_files(data_dir, tmp_path):
    svc = Services.create(artifacts_dir=tmp_path)
    files = [validate_file(data_dir / f"{n}.csv", n, max_upload_mb=100) for n in ("stores_empty", "orders_bad", "clean_products")]
    result = run_pipeline("edge", files, PipelineOptions(), svc)
    assert result["status"] == "completed_with_warnings"
    types = {(i["dataset"], i["issue_type"]) for i in result["issues"]}
    assert ("stores_empty", "header_only") in types and ("orders_bad", "structural_rejects") in types
    rows = {d["name"]: d["rows_raw"] for d in result["datasets"]}
    assert rows == {"stores_empty": 0, "orders_bad": 3, "clean_products": 2}


def test_input_validation(tmp_path):
    for name, content in [("a.txt", b"x,y\n1,2\n"), ("empty.csv", b""), ("bin.csv", b"\x00\x01\x02")]:
        path = tmp_path / name
        path.write_bytes(content)
        with pytest.raises(AppError) as exc:
            validate_file(path, None, max_upload_mb=1)
        assert exc.value.code == "INVALID_FILE"
    big = tmp_path / "big.csv"
    big.write_bytes(b"a\n" + b"1\n" * 600_000)
    with pytest.raises(AppError):
        validate_file(big, None, max_upload_mb=1)
    assert validate_file(tmp_path / "x.csv" if (tmp_path / "x.csv").exists() else _write(tmp_path / "../../we ird!.csv"), None, max_upload_mb=1).logical_name


def _write(path: Path) -> Path:
    path = path.resolve()
    path.write_text("a,b\n1,2\n")
    return path


def test_latin1_and_semicolon_files(tmp_path):
    path = tmp_path / "stores.csv"
    path.write_bytes("store_id;store_name;region\nS1;Caf\u00e9 Nord;West\nS2;Bazar;East\n".encode("latin-1"))
    result = run_pipeline("enc", [validate_file(path, "stores", max_upload_mb=1)], PipelineOptions(), Services.create(artifacts_dir=tmp_path / "a"))
    prof = result["profile_before"]["stores"]
    assert prof["delimiter"] == ";" and prof["encoding"].lower() not in ("utf-8", "ascii") and prof["column_count"] == 3


def test_failed_validation_keeps_raw_and_does_not_promote(data_dir, tmp_path, monkeypatch):
    import app.services.pipeline as pipeline
    monkeypatch.setattr(pipeline, "validate_cleaning", lambda **kw: type("R", (), {
        "passed": False, "checks": [{"name": "row_invariant:x", "passed": False}], "model_dump": lambda self: {"passed": False, "checks": []}})())
    svc = Services.create(artifacts_dir=tmp_path)
    result = run_pipeline("f", make_files(data_dir, ["stores"]), PipelineOptions(), svc)
    run_dir = Path(result["artifacts"]["run_dir"])
    assert result["status"] == "failed" and result["error"]["code"] == "CLEANING_VALIDATION_FAILED"
    assert (run_dir / "datasets" / "stores" / "raw.csv").exists() and not (run_dir / "run.duckdb").exists()
    assert svc.repo.get_run("f", result["run_id"])["status"] == "failed"


def test_interactive_review_waits_then_applies_user_approval(data_dir, tmp_path):
    svc = Services.create(artifacts_dir=tmp_path)
    files = make_files(data_dir, ["orders"])
    held = run_pipeline("rev", files, PipelineOptions(policy="interactive_review"), svc)
    assert held["status"] == "needs_review"
    proposed = [s["step_id"] for s in held["cleaning_plan"] if s["status"] == "proposed"]
    assert proposed                                                    # the ambiguous date step awaits a human
    approved = run_pipeline("rev", files, PipelineOptions(policy="interactive_review", approved_step_ids=proposed), svc)
    assert approved["status"] in ("completed", "completed_with_warnings") and approved["run_id"] != held["run_id"]
    applied = next(s for s in approved["cleaning_plan"] if s["step_id"] in proposed)
    assert applied["source"] == "user"
