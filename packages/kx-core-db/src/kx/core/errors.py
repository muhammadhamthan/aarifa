"""Domain errors and RFC 9457 problem+json responses."""

from __future__ import annotations

from typing import ClassVar

import orjson
from fastapi import FastAPI, Request, Response


class DomainError(Exception):
    status: ClassVar[int] = 400
    code: ClassVar[str] = "domain_error"
    title: ClassVar[str] = "Request cannot be completed"

    def __init__(self, detail: str, **extra: object) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class NotFound(DomainError):
    status, code, title = 404, "not_found", "Not found"


class Conflict(DomainError):
    status, code, title = 409, "conflict", "Conflict with current state"


class Forbidden(DomainError):
    status, code, title = 403, "forbidden", "Not allowed"


class Unauthorized(DomainError):
    status, code, title = 401, "unauthorized", "Authentication required"


class TooManyRequests(DomainError):
    """Rate limit or lock-out (staff login after repeated failures; public website endpoints)."""

    status, code, title = 429, "too_many_requests", "Too many requests"


class Unavailable(DomainError):
    """A dependency the request needs is switched off or down (e.g. email codes without a mailer)."""

    status, code, title = 503, "unavailable", "Temporarily unavailable"


class RuleViolation(DomainError):
    """A business rule refused the action (e.g. prices stale, grant invalid, stage move not allowed)."""

    status, code, title = 422, "rule_violation", "Business rule violated"


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> Response:
        body = {
            "type": f"https://errors.kairox.ai/{exc.code}",
            "title": exc.title,
            "status": exc.status,
            "detail": exc.detail,
            "instance": str(request.url.path),
            **exc.extra,
        }
        return Response(
            orjson.dumps(body, default=str), status_code=exc.status, media_type="application/problem+json"
        )
