"""Authentication (11 Nov, D26: in-house RS256 tokens; Cognito removed) and role checks.

Tokens are issued by the CRM's staff login (`kx.crm.services.auth`) and signed with the company's RSA key;
every API process verifies them with the public key (or, for another issuer, a JWKS URL). `mfa` is true only
after the authenticator-app (TOTP) step: an email-code login lets a person in but is not MFA for approvals,
price edits or settings (NIST SP 800-63B-4; OPEN_QUESTIONS #49).

Approvals and settings changes require a *human* principal with MFA; service tokens are refused.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
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


class TokenIssuer:
    """Signs staff access tokens (RS256). Claims: sub, user_id, role, mfa, amr, iss, aud, iat, exp, jti."""

    def __init__(
        self, private_key_pem: str, key_id: str, issuer: str, audience: str, ttl_seconds: int
    ) -> None:
        key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("KX_JWT_PRIVATE_KEY must be an RSA private key")
        self._key: rsa.RSAPrivateKey = key
        self.key_id = key_id
        self._issuer, self._audience, self.ttl_seconds = issuer, audience, ttl_seconds

    @classmethod
    def from_settings(cls, settings: CoreSettings) -> TokenIssuer:
        pem = settings.jwt_private_key.get_secret_value() if settings.jwt_private_key else dev_private_key()
        return cls(
            pem,
            settings.jwt_key_id,
            settings.jwt_issuer,
            settings.jwt_audience,
            settings.auth.access_ttl_seconds,
        )

    @property
    def public_key(self) -> rsa.RSAPublicKey:
        return self._key.public_key()

    def issue(self, *, user_id: uuid.UUID, role: str, mfa: bool, amr: tuple[str, ...], now: datetime) -> str:
        claims = {
            "sub": str(user_id),
            "user_id": str(user_id),
            "role": role,
            "mfa": mfa,
            "amr": list(amr),
            "iss": self._issuer,
            "aud": self._audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self.ttl_seconds)).timestamp()),
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.key_id})

    def jwks(self) -> dict[str, Any]:
        """`GET /.well-known/jwks.json`: the public key for anyone verifying our tokens."""
        jwk: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.public_key))
        jwk.update({"kid": self.key_id, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}


_DEV_KEY: list[str] = []


def dev_private_key() -> str:
    """A process-local key for development and tests only (tokens die with the process)."""
    if not _DEV_KEY:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _DEV_KEY.append(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            ).decode()
        )
    return _DEV_KEY[0]


class JwtVerifier:
    """Verifies RS256 tokens: our own public key by default, or a JWKS URL for another issuer."""

    def __init__(self, settings: CoreSettings) -> None:
        self._settings = settings
        self._jwks = (
            jwt.PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=3600) if settings.jwks_url else None
        )
        self._local = None if self._jwks else TokenIssuer.from_settings(settings).public_key

    def verify(self, token: str) -> Principal:
        try:
            key: Any = self._jwks.get_signing_key_from_jwt(token).key if self._jwks else self._local
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._settings.jwt_audience,
                issuer=self._settings.jwt_issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise Unauthorized("invalid or expired token") from exc
        return principal_from_claims(claims)


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    role = Role(claims.get("custom:role") or claims.get("role") or "sales")
    amr = claims.get("amr") or []
    user_id = claims.get("custom:user_id") or claims.get("user_id")
    return Principal(
        subject=str(claims["sub"]),
        user_id=uuid.UUID(user_id) if user_id else None,
        role=role,
        mfa=bool(claims.get("mfa")) or "mfa" in amr,
        is_human=role is not Role.SERVICE,
    )


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
