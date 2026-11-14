"""CUA Server — standalone Computer Use Agent service.

Mounts ONLY the browser task routes (no platform baggage). Run:

    uvicorn cua_server.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cua_server.routes import browser_task
from cua_server.utils.logger import setup_logging

WEB_DIR = Path(__file__).parent / "web"


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="Computer Use Agent Server",
        description=(
            "Task-oriented browser automation: teach a flow once, then let the "
            "agent replay it against a CDP-connected Chrome. SSE task lifecycle, "
            "deterministic CDP driver layer, per-task tab binding, audit trail."
        ),
        version="1.0.0",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(browser_task.router, prefix="/agent/browser", tags=["tasks"])

    @app.get("/health")
    async def health() -> dict:
        from cua_server.services.browser_driver import get_browser_driver

        driver = get_browser_driver()
        browser = await driver.health_check()
        return {"ok": True, "service": "cua-server", "browser": browser}

    # Web console (single-file, no build step)
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()
