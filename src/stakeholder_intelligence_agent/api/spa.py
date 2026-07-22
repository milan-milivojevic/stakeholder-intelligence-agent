"""Narrow same-origin hosting for the approved React production routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from stakeholder_intelligence_agent.config import PROJECT_ROOT
from stakeholder_intelligence_agent.errors import ServiceNotReadyError

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


def install_spa_routes(application: FastAPI, *, dist_root: Path = FRONTEND_DIST) -> None:
    """Serve only the approved browser routes and immutable Vite assets."""
    index_file = dist_root / "index.html"
    assets_directory = dist_root / "assets"
    application.mount(
        "/assets",
        StaticFiles(directory=assets_directory, check_dir=False),
        name="react-assets",
    )

    def index_response() -> FileResponse:
        if not index_file.is_file():
            raise ServiceNotReadyError
        return FileResponse(
            index_file,
            media_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/pm", include_in_schema=False)
    async def pm_spa() -> FileResponse:
        return index_response()

    @application.get("/s", include_in_schema=False)
    async def stakeholder_spa() -> FileResponse:
        return index_response()

    @application.get("/s/{invitation_token}", include_in_schema=False)
    async def stakeholder_activation_spa(invitation_token: str) -> FileResponse:  # noqa: ARG001
        return index_response()
