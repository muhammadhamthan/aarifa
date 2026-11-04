"""Staff CRM HTTP API (thin routers over kx.crm.services).

Wire in the app:
    app.include_router(build_router(databases, deps), prefix="/v1")
    install_problem_handlers(app)          # kx.core.errors -> RFC 9457 problem+json
"""

from __future__ import annotations

from fastapi import APIRouter

from kx.core.db import Databases
from kx.crm.api import admin, deals, quotes
from kx.crm.api.context import ApiContext
from kx.crm.deps import CrmDeps


def build_router(databases: Databases, deps: CrmDeps) -> APIRouter:
    ctx = ApiContext(databases, deps)
    root = APIRouter()
    for module in (deals, quotes, admin):
        root.include_router(module.router(ctx))
    return root


__all__ = ["ApiContext", "build_router"]
