"""Single-worker job runner: pipeline runs execute off the request thread; status lives in the jobs table."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

from app.errors import as_app_error
from app.ingestion.loader import InputFile
from app.schemas import PipelineOptions
from app.services.context import Services
from app.services.pipeline import run_pipeline


class JobRunner:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")

    def submit(self, owner_id: str, run_id: str, files: list[InputFile], options: PipelineOptions) -> str:
        job_id = "job_" + uuid.uuid4().hex[:12]
        self.svc.repo.create_job(owner_id, job_id, run_id)
        self.pool.submit(self._work, owner_id, job_id, files, options)
        return job_id

    def _work(self, owner_id: str, job_id: str, files: list[InputFile], options: PipelineOptions) -> None:
        for attempt in range(self.svc.settings.job_max_retries + 1):
            self.svc.repo.update_job(job_id, "running")
            try:
                result = run_pipeline(owner_id, files, options, self.svc)
                error = result.get("error")
                retryable = bool((error or {}).get("retryable"))
                if retryable and attempt < self.svc.settings.job_max_retries:
                    continue
                self.svc.repo.update_job(job_id, "failed" if result["status"] == "failed" else "completed", error, retryable)
                return
            except Exception as exc:
                err = as_app_error(exc, "job")
                if err.retryable and attempt < self.svc.settings.job_max_retries:
                    continue
                self.svc.repo.update_job(job_id, "failed", err.to_dict(), err.retryable)
                return
