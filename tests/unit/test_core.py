"""kx.core unit tests (no DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kx.core.auth import Principal, Role, principal_from_claims
from kx.core.errors import NotFound, RuleViolation, install_problem_handlers
from kx.core.events import QUOTE_APPROVED, Event, InMemoryBus, _ident
from kx.core.identity import (
    constant_time_equals,
    hmac_sha256_hex,
    new_id,
    normalise_email,
    normalise_phone,
    recipient_hash,
)
from kx.core.logging import mask_pii
from kx.core.money import d, display, pct, q, to_currency
from kx.core.timewindow import Window

UAE_ALERTS = Window.parse("08:00", "22:00", "Asia/Dubai")


# ---------------------------------------------------------------- money
def test_money_rounding_half_up_and_no_float() -> None:
    assert q(D("0.22780445"), D("0.000001")) == D("0.227804")
    assert q(D("0.00005")) == D("0.0001")
    assert display(D("11390.205"), "aed") == D("11390.21")
    assert pct(D(40)) == D("0.4")
    with pytest.raises(TypeError):
        d(0.1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        to_currency(D(1), D(0))


# ---------------------------------------------------------------- identity
def test_new_id_is_time_ordered_uuid7() -> None:
    a, b = new_id(), new_id()
    assert a.version == 7 and a < b


def test_phone_and_email_normalisation_feed_one_recipient_hash() -> None:
    assert normalise_phone("050 123 4567") == "+971501234567"
    assert normalise_phone("+91 98765 43210") == "+919876543210"
    assert normalise_email("  Ahmed@Example.COM ") == "ahmed@example.com"
    assert recipient_hash("whatsapp", "050 123 4567") == recipient_hash("whatsapp", "+971501234567")
    assert recipient_hash("email", "A@B.com") != recipient_hash("whatsapp", "+971501234567")


def test_hmac_and_constant_time() -> None:
    sig = hmac_sha256_hex(b"secret", b"payload")
    assert constant_time_equals(sig, hmac_sha256_hex(b"secret", b"payload"))
    assert not constant_time_equals(sig, hmac_sha256_hex(b"other", b"payload"))


# ---------------------------------------------------------------- time windows (approval timer pauses overnight)
def test_window_contains_in_local_time() -> None:
    assert UAE_ALERTS.contains(datetime(2026, 11, 9, 5, 0, tzinfo=UTC))  # 09:00 Dubai
    assert not UAE_ALERTS.contains(datetime(2026, 11, 9, 19, 0, tzinfo=UTC))  # 23:00 Dubai


def test_two_hour_timer_pauses_overnight() -> None:
    submitted = datetime(2026, 11, 9, 17, 0, tzinfo=UTC)  # 21:00 Dubai, 1h left today
    due = UAE_ALERTS.add_open_minutes(submitted, 120)
    assert due == datetime(2026, 11, 10, 5, 0, tzinfo=UTC)  # 09:00 Dubai next day


def test_timer_started_at_night_starts_at_8() -> None:
    due = UAE_ALERTS.add_open_minutes(datetime(2026, 11, 9, 22, 30, tzinfo=UTC), 120)  # 02:30 Dubai
    assert due == datetime(2026, 11, 10, 6, 0, tzinfo=UTC)  # 10:00 Dubai


def test_buyer_window_skips_weekend() -> None:
    w = Window.parse("09:00", "18:00", "Asia/Kolkata", frozenset({1, 2, 3, 4, 5}))
    saturday = datetime(2026, 11, 14, 6, 0, tzinfo=UTC)
    assert w.next_open(saturday) == datetime(2026, 11, 16, 3, 30, tzinfo=UTC)  # Mon 09:00 IST


def test_window_rejects_naive_and_overnight() -> None:
    with pytest.raises(ValueError):
        UAE_ALERTS.contains(datetime(2026, 11, 9, 9, 0))
    with pytest.raises(ValueError):
        Window.parse("22:00", "08:00", "Asia/Dubai")


# ---------------------------------------------------------------- events
async def test_in_memory_bus_fan_out() -> None:
    bus, seen = InMemoryBus(), []

    async def handler(e: Event) -> None:
        seen.append(e.aggregate_id)

    bus.subscribe(QUOTE_APPROVED, handler)
    await bus.publish(Event(event_type=QUOTE_APPROVED, aggregate_type="quote", aggregate_id="q1"))
    await bus.publish(Event(event_type="other.v1", aggregate_type="x", aggregate_id="x1"))
    assert seen == ["q1"] and len(bus.published) == 2


def test_outbox_schema_is_whitelisted() -> None:
    assert _ident("crm") == "crm"
    with pytest.raises(ValueError):
        _ident("crm; drop table x")


# ---------------------------------------------------------------- auth
def test_claims_to_principal_and_md_covers_manager() -> None:
    p = principal_from_claims(
        {"sub": "abc", "custom:role": "md", "amr": ["pwd", "mfa"], "custom:user_id": str(uuid.uuid4())}
    )
    assert p.role is Role.MD and p.mfa and p.has(Role.MANAGER)
    sales = Principal(subject="s", user_id=None, role=Role.SALES, mfa=True, is_human=True)
    assert not sales.has(Role.MANAGER)


# ---------------------------------------------------------------- errors + logging
def test_problem_json() -> None:
    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/nf")
    async def nf() -> None:
        raise NotFound("deal not found", deal="D-1")

    @app.get("/rv")
    async def rv() -> None:
        raise RuleViolation("advance payment is mandatory")

    c = TestClient(app)
    r = c.get("/nf")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["detail"] == "deal not found"
    assert c.get("/rv").status_code == 422


def test_pii_is_masked_in_logs() -> None:
    masked = mask_pii("buyer ahmed@example.com +971501234567 asked")
    assert "ahmed@example.com" not in masked and "501234567" not in masked
