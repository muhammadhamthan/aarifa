"""Small HMAC-signed tokens for public, short-lived capabilities (website chat sessions).

`<base64url(json claims)>.<hex HMAC-SHA256>` with an `exp` claim (unix seconds). Not a JWT on purpose: the
claims are ours only, never read by another party, and a tampered or expired token is simply refused.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from kx.core.identity import constant_time_equals, hmac_sha256_hex


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, claims: dict[str, Any], *, expires_at: datetime) -> str:
    body = _b64(json.dumps({**claims, "exp": int(expires_at.timestamp())}, separators=(",", ":")).encode())
    return f"{body}.{hmac_sha256_hex(secret.encode(), body.encode())}"


def verify(secret: str, token: str, *, now: datetime) -> dict[str, Any] | None:
    """The claims, or None when the token is malformed, tampered with or expired."""
    body, _, mac = token.partition(".")
    if not body or not mac or not constant_time_equals(hmac_sha256_hex(secret.encode(), body.encode()), mac):
        return None
    try:
        claims = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(claims, dict) or int(claims.get("exp", 0)) <= int(now.timestamp()):
        return None
    return claims
