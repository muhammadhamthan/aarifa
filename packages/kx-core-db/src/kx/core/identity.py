"""Identifiers, hashing and contact normalisation shared by CRM and automation.

The CRM issues send grants with `recipient_hash(...)`; automation consumes them with the same
function. Keeping both in one place is what makes the grant check meaningful.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Literal

import phonenumbers

Channel = Literal["whatsapp", "email", "phone", "sms"]


def new_id() -> uuid.UUID:
    """Time-ordered UUIDv7 (index-friendly primary keys)."""
    return uuid.uuid7()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def priced_content_sha256(body: str, pdf_sha256_hex: str | None = None) -> str:
    """Canonical hash a send grant is bound to. CRM computes it when issuing the grant, automation
    recomputes it from what it is about to send; any edit to the body or the PDF breaks the match.
    Form: sha256(body) without a PDF, else sha256(body + "\n" + sha256_hex(pdf))."""
    if pdf_sha256_hex is None:
        return sha256_hex(body.encode())
    return sha256_hex(body.encode() + b"\n" + pdf_sha256_hex.encode())


def normalise_phone(raw: str, default_region: str = "AE") -> str:
    """Return E.164 ('+971501234567') or raise ValueError for an invalid number."""
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"invalid phone number: {raw!r}") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"invalid phone number: {raw!r}")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalise_email(raw: str) -> str:
    email = raw.strip().lower()
    local, sep, domain = email.partition("@")
    if not sep or not local or "." not in domain:
        raise ValueError(f"invalid email: {raw!r}")
    return email


def normalise_address(channel: Channel, address: str) -> str:
    return normalise_email(address) if channel == "email" else normalise_phone(address)


def recipient_hash(channel: Channel, address: str) -> str:
    """Stable hash of a normalised recipient; whatsapp/phone/sms share the phone namespace."""
    namespace = "email" if channel == "email" else "phone"
    return sha256_hex(f"{namespace}:{normalise_address(channel, address)}".encode())


def hmac_sha256_hex(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
