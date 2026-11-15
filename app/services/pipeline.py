"""`run_pipeline`: the single data-pipeline entry point shared by the API and the evaluator."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from app.cleaning.service import clean_dataset
from app.cleaning.validator import validate_cleaning
from app.common import h, new_trace_id, sha256_file
from app.config import CLEANER_VERSION, POLICY_VERSION, ROLE_CATALOG_VERSION
from app.errors import AppError, as_app_error
from app.ingestion.csv_reader import read_csv_as_strings
from app.ingestion.dataset_registry import register_raw
from app.ingestion.loader import InputFile, to_utf8
from app.observability.tracer import Tracer
from app.orchestration.manager import RunStateMachine
from app.profiling.issue_detector import detect_duplicate_ids, unresolved_role_issues
from app.profiling.profiler import profile_frame
from app.profiling.statistics import non_null_values
from app.query.catalog import CatalogDoc, ColumnInfo, DatasetInfo, quote_ident, sql_type_for
from app.relationships.discovery import discover_relationships
from app.roles.role_inference import annotate_profile, infer_roles
from app.schemas import CleaningStep, Issue, PipelineOptions
from app.services.context import Services

_TERMINAL_OK = {"completed", "completed_with_warnings", "needs_review"}


def compute_fingerprint(owner_id: str, files: list[tuple[str, str]], options: PipelineOptions) -> str:
    return h(owner_id, sorted(files), CLEANER_VERSION, ROLE_CATALOG_VERSION, POLICY_VERSION,
             {"policy": options.policy, "advisor": False, "approved": sorted(options.approved_step_ids)})


def run_pipeline(owner_id: str, files: list[InputFile], options: PipelineOptions, svc: Services) -> dict[str, Any]:
    """Run (or return the existing) pipeline for these files. Raises only for invalid input."""
    if not files:
        raise AppError("INVALID_FILE", "at least one CSV file is required", stage="ingest")
    names = [f.logical_name for f in files]
    if len(set(names)) != len(names):
        raise AppError("INVALID_FILE", "duplicate dataset names", stage="ingest")
    digests = [(f.logical_name, sha256_file(f.path)) for f in files]
    fingerprint = compute_fingerprint(owner_id, digests, options)
    run_id = "run_" + fingerprint[:12]
    existing = svc.repo.find_run_by_fingerprint(owner_id, fingerprint)
    run_dir = svc.store.run_dir(owner_id, run_id)
    if existing and existing["status"] in _TERMINAL_OK and not options.force and (run_dir / "run_result.json").exists():
        result = svc.store.read_json(run_dir / "run_result.json")
        result["reused"] = True
        return result

    tracer = Tracer(new_trace_id())
    if existing:
        svc.repo.delete_run_children(owner_id, run_id)
    svc.repo.save_run(owner_id, run_id, fingerprint, "queued", options.policy)
    machine = RunStateMachine(lambda state: svc.repo.set_run_status(owner_id, run_id, state))
    started = time.perf_counter()
    try:
        result = _execute(owner_id, run_id, run_dir, files, digests, fingerprint, options, svc, machine, tracer)
    except Exception as exc:
        err = as_app_error(exc, machine.state)
        if machine.state != "failed":
            machine.state = "failed"
        svc.repo.save_run(owner_id, run_id, fingerprint, "failed", options.policy, error=err.to_dict())
        tracer.event("pipeline", "error", error=err.message, code=err.code)
        result = {"run_id": run_id, "fingerprint": fingerprint, "status": "failed", "error": err.to_dict(),
                  "datasets": [], "profile_before": {}, "issues": [], "cleaning_plan": [], "profile_after": {},
                  "validation": {"passed": False, "checks": []}, "artifacts": {"run_dir": str(run_dir)}, "warnings": [],
                  "reused": False}
    tracer.event("pipeline", result["status"], int((time.perf_counter() - started) * 1000))
    result["trace_id"] = tracer.trace_id
    result["trace"] = tracer.events
    svc.repo.save_trace(owner_id, tracer.trace_id, tracer.events)
    svc.store.write_json(run_dir / "traces" / "pipeline_trace.json", tracer.events)
    result["artifacts"]["trace"] = str(run_dir / "traces" / "pipeline_trace.json")
    svc.store.write_json(run_dir / "run_result.json", result)
    return result


def _execute(owner_id, run_id, run_dir: Path, files, digests, fingerprint, options, svc: Services,
             machine: RunStateMachine, tracer: Tracer) -> dict[str, Any]:
    store = svc.store
    machine.advance("ingesting")
    raw: dict[str, pd.DataFrame] = {}
    versions: dict[str, str] = {}
    meta: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp, tracer.span("ingest"):
        for f in files:
            ds_dir = run_dir / "datasets" / f.logical_name
            raw_path, sha, vid = register_raw(f.path, ds_dir)
            staged = Path(tmp) / f"{f.logical_name}.csv"
            encoding = to_utf8(raw_path, staged)
            read = read_csv_as_strings(staged, max_rows=svc.settings.max_rows)
            raw[f.logical_name], versions[f.logical_name] = read.frame, vid
            meta[f.logical_name] = {"sha": sha, "encoding": encoding, "delimiter": read.delimiter,
                                    "header": read.header_detected, "rejects": read.rejects,
                                    "bytes": raw_path.stat().st_size, "raw_path": str(raw_path), "dir": ds_dir}
            store.write_json(ds_dir / "structural_rejects.json", read.rejects)

    machine.advance("profiling")
    with tracer.span("profile_raw"):
        raw_profiles = {n: profile_frame(df, dataset=n, version_id=versions[n], stage="raw", file_bytes=meta[n]["bytes"],
                                         encoding=meta[n]["encoding"], delimiter=meta[n]["delimiter"],
                                         header_detected=meta[n]["header"], reject_count=len(meta[n]["rejects"]))
                        for n, df in raw.items()}
        raw_bindings = infer_roles(raw_profiles, raw)
        for p in raw_profiles.values():
            annotate_profile(p, raw_bindings)

    machine.advance("planning")
    outcomes = {}
    with tracer.span("plan_and_clean"):
        for n, df in raw.items():
            outcomes[n] = clean_dataset(n, versions[n], df, raw_bindings, options, reject_count=len(meta[n]["rejects"]))
    steps: list[CleaningStep] = [s for o in outcomes.values() for s in o.steps]
    issues: list[Issue] = [i for o in outcomes.values() for i in o.issues]

    if options.policy == "interactive_review" and any(s.status == "proposed" for s in steps):
        machine.advance("needs_review")
        svc.repo.save_run(owner_id, run_id, fingerprint, "needs_review", options.policy)
        _write_plan_artifacts(store, run_dir, issues, steps)
        return _result(run_id, fingerprint, "needs_review", versions, raw, {}, meta, raw_profiles, {}, issues, steps,
                       {"passed": False, "checks": []}, run_dir, ["steps await review"], None)

    machine.advance("cleaning")
    clean: dict[str, pd.DataFrame] = {n: o.frame for n, o in outcomes.items()}
    failures = [f for o in outcomes.values() for f in o.failures]
    with tracer.span("write_clean"):
        for n, df in clean.items():
            df.to_csv(meta[n]["dir"] / "cleaned.csv", index=False, na_rep="")
            store.write_json(meta[n]["dir"] / "parse_failures.json", [f for f in failures if f["dataset"] == n])

    machine.advance("validating")
    with tracer.span("validate") as span:
        rerun, rerun_hash = {}, {}
        with tempfile.TemporaryDirectory() as tmp:
            for n in clean:
                staged = Path(tmp) / f"{n}.csv"
                to_utf8(meta[n]["dir"] / "cleaned.csv", staged)
                reread = read_csv_as_strings(staged, max_rows=svc.settings.max_rows).frame
                again = clean_dataset(n, versions[n], reread, raw_bindings, options).frame
                rerun[n] = again
                rerun_hash[n] = profile_frame(again, dataset=n, version_id=versions[n], stage="clean").profile_hash
        clean_hash = {n: profile_frame(df, dataset=n, version_id=versions[n], stage="clean").profile_hash
                      for n, df in clean.items()}
        unresolved = sorted({i.issue_id for i in issues if i.status in ("needs_review", "detected")})
        report = validate_cleaning(raw=raw, clean=clean, steps=steps, failures=failures, rerun=rerun,
                                   issues_unresolved=unresolved, dups_removed={n: o.exact_dups_removed for n, o in outcomes.items()},
                                   profile_hashes=(clean_hash, rerun_hash))
        span["passed"] = report.passed
    store.write_json(run_dir / "cleaning" / "validation.json", report.model_dump())
    if not report.passed:
        _write_plan_artifacts(store, run_dir, issues, steps)
        raise AppError("CLEANING_VALIDATION_FAILED", "cleaning invariants failed; clean version not promoted",
                       stage="validate", details={"failed": [c["name"] for c in report.checks if not c["passed"]]})

    with tracer.span("profile_clean_and_graph"):
        clean_profiles = {n: profile_frame(df, dataset=n, version_id=versions[n], stage="clean",
                                           file_bytes=(meta[n]["dir"] / "cleaned.csv").stat().st_size)
                          for n, df in clean.items()}
        clean_bindings = infer_roles(clean_profiles, clean)
        for p in clean_profiles.values():
            annotate_profile(p, clean_bindings)
        for n, df in clean.items():
            issues += detect_duplicate_ids(n, df, clean_bindings) + unresolved_role_issues(n, clean_bindings)
        graph = discover_relationships(clean, versions, clean_bindings)
        raw_graph = discover_relationships(raw, versions, raw_bindings)
        catalog = _write_database(run_dir, run_id, owner_id, raw, clean, clean_profiles, clean_bindings, graph, versions,
                                  issues, svc)

    _write_plan_artifacts(store, run_dir, issues, steps)
    for n in raw:
        store.write_json(meta[n]["dir"] / "raw_profile.json", raw_profiles[n].model_dump())
        store.write_json(meta[n]["dir"] / "cleaned_profile.json", clean_profiles[n].model_dump())
    store.write_json(run_dir / "relationships" / "clean_graph.json", graph.model_dump())
    store.write_json(run_dir / "relationships" / "raw_graph.json", raw_graph.model_dump())
    store.write_json(run_dir / "catalog.json", catalog.model_dump())

    warnings = _warnings(raw, meta, steps, failures, issues)
    status = "completed_with_warnings" if warnings else "completed"
    machine.advance(status)
    result = _result(run_id, fingerprint, status, versions, raw, clean, meta, raw_profiles, clean_profiles, issues, steps,
                     report.model_dump(), run_dir, warnings, graph)
    result["raw_graph"] = raw_graph.model_dump()
    result["role_bindings"] = [b.model_dump() for b in clean_bindings]
    _persist(svc, owner_id, run_id, fingerprint, options, status, warnings, versions, meta, raw_profiles, clean_profiles,
             clean, issues, steps, clean_bindings, graph, result["artifacts"])
    return result


def _warnings(raw, meta, steps, failures, issues) -> list[str]:
    out = []
    for n, df in raw.items():
        if len(df) == 0:
            out.append(f"{n}: header-only file produced a zero-row dataset")
        if meta[n]["rejects"]:
            out.append(f"{n}: {len(meta[n]['rejects'])} structural reject(s) recorded")
    if failures:
        out.append(f"{len(failures)} value(s) failed to parse and were set to NULL (see parse_failures.json)")
    skipped = [s for s in steps if s.status == "skipped_needs_review"]
    if skipped:
        out.append(f"{len(skipped)} cleaning step(s) skipped for review")
    return out


def _write_plan_artifacts(store, run_dir: Path, issues: list[Issue], steps: list[CleaningStep]) -> None:
    store.write_json(run_dir / "cleaning" / "issues.json", [i.model_dump() for i in issues])
    store.write_json(run_dir / "cleaning" / "cleaning_plan.json", [s.model_dump() for s in steps])


def _write_database(run_dir: Path, run_id, owner_id, raw, clean, clean_profiles, bindings, graph, versions, issues,
                    svc: Services) -> CatalogDoc:
    """Typed clean tables + all-VARCHAR raw tables in `run.duckdb`, plus the catalog describing them."""
    db_path = run_dir / "run.duckdb"
    if db_path.exists():
        db_path.unlink()
    roles = {b.column_id: b for b in bindings if b.status in ("verified", "candidate")}
    ambiguous_dates = {(i.dataset, i.column) for i in issues if i.issue_type == "date_format_ambiguous"}
    con = duckdb.connect(str(db_path))
    datasets: dict[str, DatasetInfo] = {}
    try:
        con.execute(f"SET memory_limit='{svc.settings.duckdb_memory_limit}'")
        con.execute(f"SET threads={svc.settings.duckdb_threads}")
        for name, df in clean.items():
            profile = {c.name: c for c in clean_profiles[name].columns}
            columns: dict[str, ColumnInfo] = {}
            casts = []
            for col in df.columns:
                b = roles.get(f"{name}.{col}")
                role = b.role if b else None
                cp = profile[col]
                amb = (name, col) in ambiguous_dates
                sql_type = "VARCHAR" if amb else sql_type_for(cp.inferred_type, non_null_values(df[col]), col, role)
                casts.append(f"TRY_CAST({quote_ident(col)} AS {sql_type}) AS {quote_ident(col)}" if sql_type != "VARCHAR"
                             else quote_ident(col))
                columns[col] = ColumnInfo(
                    column_id=f"{name}.{col}", dataset=name, name=col, inferred_type=cp.inferred_type, sql_type=sql_type,
                    role=role, role_status=b.status if b else None, role_confidence=b.confidence if b else None,
                    null_pct=cp.null_pct, distinct_count=cp.distinct_count, is_id=(role in ("product_id", "order_id", "customer_id", "store_id")),
                    date_ambiguous=amb, top_values=cp.top_values)
            con.register("tmp_clean", df.astype(object))
            con.execute(f"CREATE TABLE {quote_ident('clean_' + name)} AS SELECT {', '.join(casts) or '1'} FROM tmp_clean"
                        if len(df.columns) else f"CREATE TABLE {quote_ident('clean_' + name)}(x INT)")
            con.unregister("tmp_clean")
            con.register("tmp_raw", raw[name].astype(object))
            con.execute(f"CREATE TABLE {quote_ident('raw_' + name)} AS SELECT * FROM tmp_raw")
            con.unregister("tmp_raw")
            datasets[name] = DatasetInfo(name=name, version_id=versions[name], rows_raw=len(raw[name]), rows_clean=len(df),
                                         columns=columns, raw_columns=list(raw[name].columns))
    finally:
        con.close()
    return CatalogDoc(run_id=run_id, owner_id=owner_id, datasets=datasets, bindings=bindings, graph=graph)


def _result(run_id, fingerprint, status, versions, raw, clean, meta, raw_profiles, clean_profiles, issues, steps, validation,
            run_dir: Path, warnings, graph) -> dict[str, Any]:
    datasets = [{"name": n, "version_id": versions[n], "rows_raw": len(raw[n]),
                 "rows_clean": len(clean[n]) if n in clean else None} for n in sorted(raw)]
    artifacts: dict[str, Any] = {"run_dir": str(run_dir), "raw": {n: meta[n]["raw_path"] for n in sorted(meta)},
                                 "cleaned": {n: str(meta[n]["dir"] / "cleaned.csv") for n in sorted(clean)},
                                 "cleaning_plan": str(run_dir / "cleaning" / "cleaning_plan.json"),
                                 "raw_graph": str(run_dir / "relationships" / "raw_graph.json"),
                                 "profiles": {n: str(meta[n]["dir"]) for n in sorted(meta)}}
    return {"run_id": run_id, "fingerprint": fingerprint, "status": status, "datasets": datasets,
            "data_version": h(sorted(versions.values()))[:12],
            "profile_before": {n: p.model_dump() for n, p in sorted(raw_profiles.items())},
            "issues": [i.model_dump() for i in sorted(issues, key=lambda i: (i.dataset, i.issue_type, i.column or ""))],
            "cleaning_plan": [s.model_dump() for s in sorted(steps, key=lambda s: (s.dataset, s.step_id))],
            "profile_after": {n: p.model_dump() for n, p in sorted(clean_profiles.items())},
            "validation": validation, "artifacts": artifacts, "warnings": warnings, "error": None,
            "graph": graph.model_dump() if graph else None, "reused": False}


def _persist(svc: Services, owner_id, run_id, fingerprint, options, status, warnings, versions, meta, raw_profiles,
             clean_profiles, clean, issues, steps, bindings, graph, artifacts) -> None:
    run_dir = Path(artifacts["run_dir"])
    version_rows = []
    for n, vid in versions.items():
        version_rows.append({"version_id": vid, "name": n, "stage": "raw", "artifact_path": meta[n]["raw_path"],
                             "sha256": meta[n]["sha"], "row_count": raw_profiles[n].row_count, "parent_version_id": None})
        cleaned = meta[n]["dir"] / "cleaned.csv"
        version_rows.append({"version_id": vid, "name": n, "stage": "clean", "artifact_path": str(cleaned),
                             "sha256": sha256_file(cleaned), "row_count": len(clean[n]), "parent_version_id": vid})
    art_rows = []
    for kind, path in [("cleaning_plan", run_dir / "cleaning/cleaning_plan.json"), ("issues", run_dir / "cleaning/issues.json"),
                       ("validation", run_dir / "cleaning/validation.json"), ("graph", run_dir / "relationships/clean_graph.json"),
                       ("database", run_dir / "run.duckdb")]:
        art_rows.append({"kind": kind, "path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    svc.repo.save_run(owner_id, run_id, fingerprint, status, options.policy, warnings=warnings)
    svc.repo.save_documents(
        owner_id, run_id, versions=version_rows, artifacts=art_rows,
        profiles=[p.model_dump() for p in [*raw_profiles.values(), *clean_profiles.values()]],
        issues=[i.model_dump() for i in issues], bindings=[b.model_dump() for b in bindings],
        steps=[s.model_dump() for s in steps], graph=graph.model_dump())
