"""Staff credentials (11 Nov, D26: in-house login replaces Cognito). Pure functions, no I/O.

* Passwords: Argon2id in PHC form (`$argon2id$v=19$m=19456,t=2,p=1$...`), the OWASP Password Storage
  parameters, via the `cryptography` package already in the lock.
* Second step: TOTP (RFC 6238: HMAC-SHA1, 30-second steps, 6 digits), the authenticator app the platform docs
  recommend ("NIST SP 800-63B-4 says email SHALL NOT be used for out-of-band authentication"). The used
  step is remembered so a code can never be replayed.
* Email codes: 6 random digits, stored only as a hash, for the fallback login and for password recovery.
* TOTP seeds are encrypted at rest (Fernet, key `KX_AUTH__ENCRYPTION_KEY` from Secrets Manager).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import datetime
from typing import Final
from urllib.parse import quote

from cryptography.exceptions import InvalidKey
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

ARGON2_MEMORY_KIB: Final = 19_456  # OWASP: m=19 MiB, t=2, p=1
ARGON2_ITERATIONS: Final = 2
ARGON2_LANES: Final = 1
TOTP_STEP_SECONDS: Final = 30
TOTP_DIGITS: Final = 6
MIN_PASSWORD_LENGTH: Final = 12


def hash_password(password: str) -> str:
    kdf = Argon2id(
        salt=secrets.token_bytes(16),
        length=32,
        iterations=ARGON2_ITERATIONS,
        lanes=ARGON2_LANES,
        memory_cost=ARGON2_MEMORY_KIB,
    )
    return kdf.derive_phc_encoded(password.encode())


def verify_password(password: str, phc: str) -> bool:
    try:
        Argon2id.verify_phc_encoded(password.encode(), phc)
    except InvalidKey, ValueError:
        return False
    return True


def password_problem(password: str) -> str | None:
    """None when acceptable. Length over composition rules (NIST 800-63B)."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"use at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > 256:
        return "use at most 256 characters"
    if password.strip() != password or len(set(password)) < 4:
        return "choose a less predictable password"
    return None


def temporary_password() -> str:
    """A one-time password for a new user (must be changed at first login)."""
    return secrets.token_urlsafe(18)


# ---------------------------------------------------------------- TOTP (RFC 6238)
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)


def totp_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def totp_step(at: datetime) -> int:
    return int(at.timestamp()) // TOTP_STEP_SECONDS


def totp_now(secret: str, at: datetime) -> str:
    return totp_at(secret, totp_step(at))


def verify_totp(
    secret: str, code: str, at: datetime, *, last_step: int | None = None, window: int = 1
) -> int | None:
    """The matched step (store it as `last_step`), or None. One step of clock drift either way; a step at or
    before `last_step` is a replay and is refused."""
    code = code.strip().replace(" ", "")
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    now = totp_step(at)
    for step in range(now - window, now + window + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp_at(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )


# ---------------------------------------------------------------- one-time codes and tokens
def new_numeric_code(digits: int = 6) -> str:
    return str(secrets.randbelow(10**digits)).zfill(digits)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------- TOTP seeds at rest
class SeedBox:
    """Encrypts TOTP seeds with a Fernet key (url-safe base64 of 32 bytes)."""

    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def seal(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode()).decode()

    def open(self, sealed: str) -> str:
        try:
            return self._fernet.decrypt(sealed.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("TOTP seed cannot be decrypted (wrong KX_AUTH__ENCRYPTION_KEY?)") from exc

    @staticmethod
    def new_key() -> str:
        return Fernet.generate_key().decode()
