"""HTTP API (ECS `api` service): CRM routes under /v1, channel webhooks, health check."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from arifaa_pack.config import app_settings
from arifaa_pack.container import build
from arifaa_pack.observability import init_error_tracking
from kx.agents.website import build_router as website_router
from kx.automation.admin_api import build_router as channels_admin_router
from kx.automation.cms import build_router as cms_router
from kx.automation.unsubscribe import build_router as unsubscribe_router
from kx.automation.webhooks import build_router as webhook_router
from kx.core.errors import Unavailable, install_problem_handlers
from kx.core.logging import configure_logging
from kx.core.settings import get_settings
from kx.crm.api import build_router as crm_router


def create_app() -> FastAPI:
    core = get_settings()
    configure_logging(core.log_level, core.log_json)
    init_error_tracking(app_settings(), core.env.value, "api")  # D29 Better Stack (Sentry SDK)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with build() as c:
            app.include_router(crm_router(c.databases, c.crm_deps), prefix="/v1")
            app.include_router(webhook_router(c.webhook_deps))
            app.include_router(unsubscribe_router(c.unsubscribe_deps))  # §8.1 step 10
            cms_secret = c.app.cms_webhook_secret.get_secret_value() if c.app.cms_webhook_secret else None
            app.include_router(cms_router(c.databases, cms_secret))  # §7 / D22 publish webhook
            app.include_router(website_router(c.website_deps))  # §6 website chat and voice
            app.include_router(channels_admin_router(c.databases))  # §20.11 numbers and templates
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

    @app.get("/.well-known/jwks.json", tags=["auth"])
    async def jwks(request: Request) -> dict[str, Any]:
        """The public key that verifies staff access tokens (RS256, D26)."""
        tokens = request.app.state.container.crm_deps.tokens
        if tokens is None:
            raise Unavailable("token signing is not configured")
        result: dict[str, Any] = tokens.jwks()
        return result

    return app
