"""FastAPI application: `uvicorn app.main:app`."""
from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
from time import monotonic

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.orchestration.job_runner import JobRunner
from app.services.context import Services

STATIC = Path(__file__).parent / "static"


def create_app(svc: Services | None = None) -> FastAPI:
    app = FastAPI(title="Retail Data & Conversational Intelligence Workbench", version="0.1.0")
    app.state.svc = svc or Services.create()
    app.state.jobs = JobRunner(app.state.svc)
    app.state.rate_windows = defaultdict(deque)

    @app.middleware("http")
    async def api_guard(request, call_next):
        settings = app.state.svc.settings
        if request.url.path.startswith("/api") and settings.api_tokens.strip():
            token_map = {}
            for entry in settings.api_tokens.split(","):
                if "=" in entry:
                    token, owner = entry.split("=", 1)
                    token_map[token.strip()] = owner.strip()
            authorization = request.headers.get("authorization", "")
            token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
            owner = token_map.get(token)
            if not owner:
                return JSONResponse({"error": {"code": "AUTH_REQUIRED"}}, status_code=401)
            request.state.auth_owner = owner
            now = monotonic()
            window = app.state.rate_windows[owner]
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= settings.api_rate_limit_rpm:
                return JSONResponse({"error": {"code": "RATE_LIMITED"}}, status_code=429)
            window.append(now)
        return await call_next(request)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
