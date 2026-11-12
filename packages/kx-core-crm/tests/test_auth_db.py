"""In-house staff login over HTTP (11 Nov plan WP21, D26): password -> TOTP or email code -> RS256 tokens,
refresh rotation and reuse detection, lock-out, break-glass alert, admin user management.

Real JWT verification: no `current_principal` override, so the tokens the login issues are the ones the
routes check.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import Seed
from fastapi import FastAPI
from sqlalchemy import select

from kx.core import events as ev
from kx.core.auth import TokenIssuer
from kx.core.errors import install_problem_handlers
from kx.core.passwords import SeedBox, totp_now
from kx.core.settings import AuthSettings, LogicalDb, get_settings
from kx.crm.api import build_router
from kx.crm.deps import CrmDeps
from kx.crm.models import AuditEvent, OutboxEvent, StaffCredential
from kx.crm.services import auth as svc
from kx.crm.services.journal import Actor

pytestmark = pytest.mark.db


@dataclass
class FakeMailer:
    sent: list[tuple[str, str, str]] = field(default_factory=list)

    async def send_code(self, to_email: str, subject: str, body: str) -> None:
        self.sent.append((to_email, subject, body))

    def last_code(self) -> str:
        return self.sent[-1][2].split("code is ", 1)[1][:6]


@dataclass
class Clock:
    # starts in the past, so tokens issued after a TOTP step forward never carry a future `iat`
    at: datetime = field(default_factory=lambda: datetime.now(UTC) - timedelta(minutes=5))

    def __call__(self) -> datetime:
        return self.at


@dataclass
class Env:
    client: httpx.AsyncClient
    deps: CrmDeps
    mailer: FakeMailer
    clock: Clock
    databases: Any


@pytest.fixture
async def env(databases: Any, seed: Seed, deps: CrmDeps) -> AsyncIterator[Env]:
    mailer, clock = FakeMailer(), Clock()
    auth_deps = replace(
        deps,
        tokens=TokenIssuer.from_settings(get_settings()),
        seed_box=SeedBox(SeedBox.new_key()),
        mailer=mailer,
        auth=AuthSettings(cookie_secure=False),
        clock=clock,
    )
    app = FastAPI()
    app.include_router(build_router(databases, auth_deps), prefix="/v1")
    install_problem_handlers(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield Env(client, auth_deps, mailer, clock, databases)


async def new_user(env: Env, role: str = "admin", *, break_glass: bool = False) -> tuple[str, str, uuid.UUID]:
    email = f"staff-{uuid.uuid4().hex[:10]}@test.ae"
    async with env.databases.transaction(LogicalDb.CRM) as s:
        user, password = await svc.create_user(
            s,
            env.deps,
            email=email,
            full_name="Test Staff",
            role=role,
            break_glass=break_glass,
            actor=Actor.system("test"),
        )
    return email, password, user.id


async def email_login(env: Env, email: str, password: str) -> dict[str, Any]:
    challenge = (await env.client.post("/v1/auth/login", json={"email": email, "password": password})).json()
    assert (await env.client.post("/v1/auth/mfa/email/send", json=challenge)).status_code == 204
    body = {"challenge_token": challenge["challenge_token"], "code": env.mailer.last_code()}
    r = await env.client.post("/v1/auth/mfa/email/verify", json=body)
    assert r.status_code == 200, r.text
    result: dict[str, Any] = r.json()
    return result


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def enrol_totp(env: Env, tokens: dict[str, Any]) -> str:
    enrol = (await env.client.post("/v1/auth/totp/enrol", headers=bearer(tokens))).json()
    assert enrol["otpauth_uri"].startswith("otpauth://totp/")
    code = totp_now(enrol["secret"], env.clock())
    r = await env.client.post("/v1/auth/totp/confirm", json={"code": code}, headers=bearer(tokens))
    assert r.status_code == 204
    return str(enrol["secret"])


async def totp_login(env: Env, email: str, password: str, secret: str) -> dict[str, Any]:
    challenge = (await env.client.post("/v1/auth/login", json={"email": email, "password": password})).json()
    assert challenge["methods"] == ["totp", "email"] and challenge["totp_enrolled"]
    env.clock.at += timedelta(seconds=30)  # next TOTP step (the enrolment code cannot be replayed)
    body = {"challenge_token": challenge["challenge_token"], "code": totp_now(secret, env.clock())}
    r = await env.client.post("/v1/auth/mfa/totp", json=body)
    assert r.status_code == 200, r.text
    result: dict[str, Any] = r.json()
    return result


async def test_email_code_then_totp_gives_mfa(env: Env) -> None:
    email, password, user_id = await new_user(env)

    # first sign-in: only the email code is on offer; the session has no MFA and must change the password
    first = (await env.client.post("/v1/auth/login", json={"email": email, "password": password})).json()
    assert first["methods"] == ["email"] and not first["totp_enrolled"]
    tokens = await email_login(env, email, password)
    assert tokens["mfa"] is False and tokens["amr"] == ["pwd", "email"]
    assert tokens["must_change_password"] is True and tokens["user"]["id"] == str(user_id)
    me = await env.client.get("/v1/auth/me", headers=bearer(tokens))
    assert me.status_code == 200 and me.json()["email"] == email

    # an email-code session cannot approve or manage users (require_human_mfa)
    r = await env.client.get("/v1/users", headers=bearer(tokens))
    assert r.status_code == 200  # reading the team is fine
    r = await env.client.post(
        "/v1/users",
        json={"email": "x@test.ae", "full_name": "X", "role": "sales"},
        headers=bearer(tokens),
    )
    assert r.status_code == 403

    new_password = "a-much-longer-passphrase-2026"
    r = await env.client.post(
        "/v1/auth/password",
        json={"current_password": password, "new_password": new_password},
        headers=bearer(tokens),
    )
    assert r.status_code == 204

    secret = await enrol_totp(env, tokens)
    mfa = await totp_login(env, email, new_password, secret)
    assert mfa["mfa"] is True and mfa["amr"] == ["pwd", "otp"] and mfa["must_change_password"] is False

    # an MFA admin creates a salesperson; the one-time password is returned once
    r = await env.client.post(
        "/v1/users",
        json={"email": f"new-{uuid.uuid4().hex[:8]}@test.ae", "full_name": "New Sales", "role": "sales"},
        headers=bearer(mfa),
    )
    assert r.status_code == 201, r.text
    assert len(r.json()["temporary_password"]) >= 12 and r.json()["user"]["role"] == "sales"


async def test_wrong_password_locks_after_five(env: Env) -> None:
    email, password, user_id = await new_user(env)
    for _ in range(5):
        r = await env.client.post("/v1/auth/login", json={"email": email, "password": "wrong-password"})
        assert r.status_code == 401
    r = await env.client.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 429  # locked even with the right password

    env.clock.at += timedelta(minutes=16)
    r = await env.client.post("/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200
    async with env.databases.transaction(LogicalDb.CRM) as s:
        actions = (
            (await s.execute(select(AuditEvent.action).where(AuditEvent.entity_id == str(user_id))))
            .scalars()
            .all()
        )
    assert "auth.locked" in actions and actions.count("auth.login_failed") == 4

    # an unknown address answers exactly like a wrong password
    r = await env.client.post("/v1/auth/login", json={"email": "nobody@test.ae", "password": password})
    assert r.status_code == 401 and r.json()["detail"] == svc.GENERIC


async def test_refresh_rotates_and_reuse_ends_every_session(env: Env) -> None:
    email, password, _ = await new_user(env, "sales")
    tokens = await email_login(env, email, password)
    old = env.client.cookies.get("kx_refresh")
    assert old

    rotated = await env.client.post("/v1/auth/refresh")
    assert rotated.status_code == 200
    new = env.client.cookies.get("kx_refresh")
    assert new and new != old

    # the stolen old token comes back: refused, and the new one stops working too
    env.client.cookies.clear()
    r = await env.client.post("/v1/auth/refresh", json={"refresh_token": old})
    assert r.status_code == 401
    r = await env.client.post("/v1/auth/refresh", json={"refresh_token": new})
    assert r.status_code == 401
    assert tokens["access_token"]  # access tokens simply expire (15 minutes)


async def test_codes_are_bounded_and_single_use(env: Env) -> None:
    email, password, _ = await new_user(env, "sales")
    challenge = (await env.client.post("/v1/auth/login", json={"email": email, "password": password})).json()
    await env.client.post("/v1/auth/mfa/email/send", json=challenge)
    code = env.mailer.last_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        body = {"challenge_token": challenge["challenge_token"], "code": wrong}
        assert (await env.client.post("/v1/auth/mfa/email/verify", json=body)).status_code in {401, 429}
    body = {"challenge_token": challenge["challenge_token"], "code": code}
    r = await env.client.post("/v1/auth/mfa/email/verify", json=body)
    assert r.status_code in {401, 429}  # the challenge is spent (or the account locked): sign in again

    # an expired challenge is refused
    env.clock.at += timedelta(minutes=20)
    challenge = (await env.client.post("/v1/auth/login", json={"email": email, "password": password})).json()
    await env.client.post("/v1/auth/mfa/email/send", json=challenge)
    env.clock.at += timedelta(minutes=11)
    body = {"challenge_token": challenge["challenge_token"], "code": env.mailer.last_code()}
    assert (await env.client.post("/v1/auth/mfa/email/verify", json=body)).status_code == 401


async def test_break_glass_sign_in_alerts_md_and_manager(env: Env, seed: Seed) -> None:
    email, password, user_id = await new_user(env, break_glass=True)
    await email_login(env, email, password)
    async with env.databases.transaction(LogicalDb.CRM) as s:
        notices = (
            (
                await s.execute(
                    select(OutboxEvent.payload).where(
                        OutboxEvent.aggregate_id == str(user_id), OutboxEvent.event_type == ev.STAFF_NOTICE
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(notices) == 1
    notified = {n["user_ref"] for n in notices[0]["notify"]}
    assert notified == {str(seed.md), str(seed.manager)}
    assert "Break-glass" in notices[0]["text"]


async def test_reset_password_by_email_code(env: Env) -> None:
    email, _, user_id = await new_user(env, "sales")
    r = await env.client.post("/v1/auth/password/reset/start", json={"email": email})
    assert r.status_code == 202
    code = env.mailer.last_code()
    r = await env.client.post("/v1/auth/password/reset/start", json={"email": "nobody@test.ae"})
    assert r.status_code == 202 and len(env.mailer.sent) == 1  # no hint about unknown addresses

    new_password = "reset-passphrase-for-sales"
    body = {"email": email, "code": code, "new_password": new_password}
    assert (await env.client.post("/v1/auth/password/reset/finish", json=body)).status_code == 204
    tokens = await email_login(env, email, new_password)
    assert tokens["must_change_password"] is False
    async with env.databases.transaction(LogicalDb.CRM) as s:
        cred = await s.get(StaffCredential, user_id)
        assert cred is not None and cred.password_hash.startswith("$argon2id$")
