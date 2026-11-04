"""HTTP API (ECS `api` service): CRM routes under /v1, channel webhooks, health check."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from arifaa_pack.config import app_settings
from arifaa_pack.container import build
from kx.automation.webhooks import build_router as webhook_router
from kx.core.errors import install_problem_handlers
from kx.core.logging import configure_logging
from kx.core.settings import get_settings
from kx.crm.api import build_router as crm_router


def create_app() -> FastAPI:
    core = get_settings()
    configure_logging(core.log_level, core.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with build() as c:
            app.include_router(crm_router(c.databases, c.crm_deps), prefix="/v1")
            app.include_router(webhook_router(c.webhook_deps))
            app.state.container = c
            yield

    app = FastAPI(title="Kairox CRM API", version="1.0.0", lifespan=lifespan)
    install_problem_handlers(app)  # also turns bad webhook signatures into 401s
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app_settings().cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
