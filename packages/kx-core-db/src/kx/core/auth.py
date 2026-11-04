"""Authentication (Cognito / any OIDC JWT) and role checks.

Approvals and settings changes require a *human* principal with MFA; service tokens are refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header

from kx.core.errors import Forbidden, Unauthorized
from kx.core.settings import CoreSettings, get_settings


class Role(StrEnum):
    MD = "md"
    MANAGER = "manager"
    ADMIN = "admin"
    SALES = "sales"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    user_id: uuid.UUID | None
    role: Role
    mfa: bool
    is_human: bool

    def has(self, *roles: Role) -> bool:
        return self.role in roles or (self.role is Role.MD and Role.MANAGER in roles)


class JwtVerifier:
    """Verifies RS256 tokens against the issuer's JWKS (cached by PyJWKClient)."""

    def __init__(self, settings: CoreSettings) -> None:
        self._settings = settings
        self._jwks = jwt.PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> Principal:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(
                token, key, algorithms=["RS256"], audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer, options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise Unauthorized("invalid or expired token") from exc
        return principal_from_claims(claims)


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    role = Role(claims.get("custom:role") or claims.get("role") or "sales")
    amr = claims.get("amr") or []
    user_id = claims.get("custom:user_id") or claims.get("user_id")
    return Principal(subject=str(claims["sub"]), user_id=uuid.UUID(user_id) if user_id else None, role=role,
                     mfa=bool(claims.get("mfa")) or "mfa" in amr, is_human=role is not Role.SERVICE)


_verifier: JwtVerifier | None = None


def _get_verifier(settings: Annotated[CoreSettings, Depends(get_settings)]) -> JwtVerifier:
    global _verifier  # noqa: PLW0603 - process-wide JWKS cache
    if _verifier is None:
        _verifier = JwtVerifier(settings)
    return _verifier


async def current_principal(
    authorization: Annotated[str | None, Header()] = None,
    verifier: JwtVerifier = Depends(_get_verifier),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("missing bearer token")
    return verifier.verify(authorization.split(" ", 1)[1])


def require_role(*roles: Role) -> Callable[..., Awaitable[Principal]]:
    async def _dep(p: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not p.has(*roles):
            raise Forbidden(f"requires role {' or '.join(r.value for r in roles)}")
        return p

    return _dep


def require_human_mfa(*roles: Role) -> Callable[..., Awaitable[Principal]]:
    """For approvals and settings: a person, signed in with MFA, holding one of `roles`."""

    async def _dep(p: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if not p.is_human:
            raise Forbidden("service tokens cannot perform this action")
        if not p.mfa:
            raise Forbidden("multi-factor authentication required")
        if roles and not p.has(*roles):
            raise Forbidden(f"requires role {' or '.join(r.value for r in roles)}")
        return p

    return _dep
