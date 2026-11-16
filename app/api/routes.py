"""HTTP API. Owner scope comes from the server-side `X-Owner-Id` header (stand-in for real auth), never from the body."""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agents.prompts import PROMPT_VERSION
from app.errors import AppError
from app.ingestion.loader import InputFile, validate_file
from app.llm.base import MeteredProvider
from app.llm.openrouter_provider import CachingProvider, OpenRouterProvider
from app.schemas import PipelineOptions
from app.services.chat import ask, create_session, reset_session
from app.services.context import Services
from app.services.pipeline import compute_fingerprint, run_pipeline
from app.storage.artifacts import safe_segment

router = APIRouter(prefix="/api")
_STATUS = {"INVALID_FILE": 400, "ISOLATION_VIOLATION": 404, "CASE_INPUT_INVALID": 400, "UNSUPPORTED_QUERY": 422,
           "CLARIFICATION_REQUIRED": 422}


class ChatBody(BaseModel):
    question: str = Field(max_length=2000)


class SessionBody(BaseModel):
    run_id: str


class ApproveBody(BaseModel):
    step_ids: list[str]


def _svc(request: Request) -> Services:
    return request.app.state.svc


def _owner(request: Request, x_owner_id: str | None) -> str:
    if owner := getattr(request.state, "auth_owner", None):
        return owner
    return safe_segment(x_owner_id or "demo")


def error_response(exc: AppError) -> JSONResponse:
    return JSONResponse({"error": exc.to_dict()}, status_code=_STATUS.get(exc.code, 500))


def _provider(request: Request) -> MeteredProvider | None:
    state = request.app.state
    settings = state.svc.settings
    if getattr(state, "provider_ready", False):
        return state.provider
    state.provider_ready = True
    state.provider = None
    if settings.llm_provider == "openrouter" and settings.has_openrouter_key:
        state.provider = MeteredProvider(CachingProvider(OpenRouterProvider(settings), state.svc.repo, PROMPT_VERSION, settings.openrouter_model))
    return state.provider


async def _stage(files: list[UploadFile], svc: Services) -> tuple[list[InputFile], Path]:
    staging = svc.store.root / "_staging" / uuid.uuid4().hex[:12]
    staging.mkdir(parents=True, exist_ok=True)
    out: list[InputFile] = []
    for i, up in enumerate(files):
        if Path(up.filename or "").suffix.lower() != ".csv":
            shutil.rmtree(staging, ignore_errors=True)
            raise AppError("INVALID_FILE", "only .csv files are accepted", stage="ingest")
        target = staging / f"upload_{i}.csv"          # internal artifact name; the client filename is never used as a path
        limit = svc.settings.max_upload_mb * 1024 * 1024
        written = 0
        with target.open("wb") as fh:
            while chunk := await up.read(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise AppError("INVALID_FILE", f"file exceeds {svc.settings.max_upload_mb} MB limit", stage="ingest")
                fh.write(chunk)
        out.append(validate_file(target, Path(up.filename or f"dataset_{i}").stem, max_upload_mb=svc.settings.max_upload_mb))
    return out, staging


@router.post("/runs")
async def create_run(request: Request, files: Annotated[list[UploadFile], File()], policy: Annotated[str, Form()] = "unattended",
                     force: Annotated[bool, Form()] = False, run_async: Annotated[bool, Form()] = False,
                     x_owner_id: Annotated[str | None, Header()] = None):
    svc, owner = _svc(request), _owner(request, x_owner_id)
    try:
        staged, staging = await _stage(files, svc)
        options = PipelineOptions(policy=policy, force=force, max_upload_mb=svc.settings.max_upload_mb)  # type: ignore[arg-type]
        if run_async:
            from app.common import sha256_file
            fp = compute_fingerprint(owner, [(f.logical_name, sha256_file(f.path)) for f in staged], options)
            job_id = request.app.state.jobs.submit(owner, "run_" + fp[:12], staged, options)
            return JSONResponse({"job_id": job_id, "run_id": "run_" + fp[:12], "status": "queued"}, status_code=202)
        result = run_pipeline(owner, staged, options, svc)
        shutil.rmtree(staging, ignore_errors=True)
        return _public(result)
    except AppError as exc:
        if "staging" in locals():
            shutil.rmtree(staging, ignore_errors=True)
        return error_response(exc)


def _public(result: dict) -> dict:
    return {k: result[k] for k in ("run_id", "status", "fingerprint", "datasets", "warnings", "validation", "error", "trace_id", "reused")
            if k in result}


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str, x_owner_id: Annotated[str | None, Header()] = None):
    try:
        job = _svc(request).repo.get_job(_owner(request, x_owner_id), job_id)
        return {k: job[k] for k in ("job_id", "run_id", "status", "attempts", "error_json")}
    except AppError as exc:
        return error_response(exc)


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str, x_owner_id: Annotated[str | None, Header()] = None):
    svc, owner = _svc(request), _owner(request, x_owner_id)
    try:
        run = svc.repo.get_run(owner, run_id)
        result = svc.store.read_json(svc.store.run_dir(owner, run_id) / "run_result.json") if run["status"] != "queued" else {}
        return {**_public(result), "status": run["status"], "run_id": run_id}
    except (AppError, FileNotFoundError) as exc:
        return error_response(exc) if isinstance(exc, AppError) else JSONResponse({"error": {"code": "ISOLATION_VIOLATION"}}, 404)


def _doc(request: Request, run_id: str, x_owner_id: str | None, key: str):
    svc, owner = _svc(request), _owner(request, x_owner_id)
    try:
        svc.repo.get_run(owner, run_id)
        return svc.store.read_json(svc.store.run_dir(owner, run_id) / "run_result.json").get(key)
    except AppError as exc:
        return error_response(exc)


for _name, _key in (("issues", "issues"), ("plan", "cleaning_plan"), ("profiles", "profile_after"),
                    ("graph", "graph"), ("raw-graph", "raw_graph")):
    def _make(key=_key):
        def endpoint(request: Request, run_id: str, x_owner_id: Annotated[str | None, Header()] = None):
            return _doc(request, run_id, x_owner_id, key)
        return endpoint
    router.add_api_route(f"/runs/{{run_id}}/{_name}", _make(), methods=["GET"], name=f"run_{_name}")


@router.post("/runs/{run_id}/approve")
def approve(request: Request, run_id: str, body: ApproveBody, x_owner_id: Annotated[str | None, Header()] = None):
    """Interactive review: re-run the stored files with the approved step ids (creates a new, distinct run)."""
    svc, owner = _svc(request), _owner(request, x_owner_id)
    try:
        svc.repo.get_run(owner, run_id)
        run_dir = svc.store.run_dir(owner, run_id)
        raw = sorted((run_dir / "datasets").glob("*/raw.csv"))
        files = [InputFile(logical_name=p.parent.name, path=p) for p in raw]
        options = PipelineOptions(policy="interactive_review", approved_step_ids=body.step_ids,
                                  max_upload_mb=svc.settings.max_upload_mb)
        return _public(run_pipeline(owner, files, options, svc))
    except AppError as exc:
        return error_response(exc)


@router.post("/sessions")
def new_session(request: Request, body: SessionBody, x_owner_id: Annotated[str | None, Header()] = None):
    try:
        return {"session_id": create_session(_svc(request), _owner(request, x_owner_id), body.run_id)}
    except AppError as exc:
        return error_response(exc)


@router.post("/sessions/{session_id}/chat")
def chat(request: Request, session_id: str, body: ChatBody, x_owner_id: Annotated[str | None, Header()] = None,
         idempotency_key: Annotated[str | None, Header()] = None):
    try:
        result = ask(_svc(request), _owner(request, x_owner_id), session_id, body.question, provider=_provider(request), idem_key=idempotency_key)
        return result.model_dump()
    except AppError as exc:
        return error_response(exc)


@router.post("/sessions/{session_id}/reset")
def reset(request: Request, session_id: str, x_owner_id: Annotated[str | None, Header()] = None):
    try:
        reset_session(_svc(request), _owner(request, x_owner_id), session_id)
        return {"session_id": session_id, "status": "reset"}
    except AppError as exc:
        return error_response(exc)


@router.get("/sessions/{session_id}/turns")
def turns(request: Request, session_id: str, x_owner_id: Annotated[str | None, Header()] = None):
    try:
        return _svc(request).repo.list_turns(_owner(request, x_owner_id), session_id)
    except AppError as exc:
        return error_response(exc)


@router.get("/traces/{trace_id}")
def trace(request: Request, trace_id: str, x_owner_id: Annotated[str | None, Header()] = None):
    try:
        return _svc(request).repo.get_trace(_owner(request, x_owner_id), trace_id)
    except AppError as exc:
        return error_response(exc)


@router.get("/evidence/{evidence_id}")
def evidence(request: Request, evidence_id: str, x_owner_id: Annotated[str | None, Header()] = None):
    try:
        return _svc(request).repo.get_evidence(_owner(request, x_owner_id), evidence_id)
    except AppError as exc:
        return error_response(exc)
