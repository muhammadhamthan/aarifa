"""Staff login + MFA routes (11 Nov, D26). Public: login, second step, refresh, recovery. Authenticated: me,
logout, TOTP enrolment, password change.

The refresh token travels only in an HttpOnly, Secure, SameSite=Lax cookie scoped to `/v1/auth` (the CRM
screens on Vercel and the API share the company domain); the access token is returned in the body and sent as
`Authorization: Bearer`. Each route opens its own transaction so a refused attempt still counts toward the
lock-out before the 401/429 answer.
"""

# No `from __future__ import annotations` here: FastAPI resolves the closure-local annotations.

import uuid
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, current_principal
from kx.core.errors import TooManyRequests, Unauthorized, Unavailable
from kx.core.settings import LogicalDb
from kx.crm.api.context import ApiContext
from kx.crm.models import User
from kx.crm.services import auth as svc

COOKIE = "kx_refresh"
COOKIE_PATH = "/v1/auth"
Signed = Annotated[Principal, Depends(current_principal)]


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class ChallengeOut(BaseModel):
    challenge_token: str
    methods: list[Literal["totp", "email"]]  # what the buyer of this screen may use next, TOTP first
    totp_enrolled: bool
    expires_at: datetime


class ChallengeIn(BaseModel):
    challenge_token: str = Field(min_length=10, max_length=200)


class CodeIn(ChallengeIn):
    code: str = Field(min_length=6, max_length=10)


class StaffUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    phone_e164: str | None
    is_active: bool
    is_away: bool
    languages: list[str]
    countries: list[str]
    category_codes: list[str]
    alert_channels: list[str]


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 - the OAuth token type name, not a secret
    expires_in: int
    mfa: bool  # true only after the authenticator-app step: approvals need it
    amr: list[str]
    must_change_password: bool
    totp_enrolled: bool
    user: StaffUserOut


class RefreshIn(BaseModel):
    refresh_token: str | None = None  # non-browser clients; browsers use the HttpOnly cookie


class TotpEnrolOut(BaseModel):
    secret: str  # show once, for manual entry
    otpauth_uri: str  # render as a QR code


class TotpConfirmIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class ResetStartIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class ResetFinishIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=6, max_length=10)
    new_password: str = Field(min_length=12, max_length=256)


def _client(request: Request) -> tuple[str | None, str | None]:
    return (request.client.host if request.client else None), request.headers.get("user-agent")


def _refuse(r: svc.Refused) -> Exception:
    return TooManyRequests(r.detail) if r.status == 429 else Unauthorized(r.detail)


def router(ctx: ApiContext) -> APIRouter:  # noqa: PLR0915 - one small handler per auth step
    r = APIRouter(prefix="/auth", tags=["auth"])
    deps = ctx.deps
    dbs = ctx.databases

    def tx() -> AbstractAsyncContextManager[AsyncSession]:
        return dbs.transaction(LogicalDb.CRM)

    def token_out(t: svc.Tokens, response: Response) -> TokenOut:
        response.set_cookie(
            COOKIE,
            t.refresh_token,
            max_age=deps.auth.refresh_ttl_seconds,
            path=COOKIE_PATH,
            domain=deps.auth.cookie_domain,
            secure=deps.auth.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return TokenOut(
            access_token=t.access_token,
            expires_in=t.expires_in,
            mfa=t.mfa,
            amr=list(t.amr),
            must_change_password=t.must_change_password,
            totp_enrolled=t.totp_enrolled,
            user=StaffUserOut.model_validate(t.user),
        )

    @r.post("/login", response_model=ChallengeOut)
    async def login(body: LoginIn) -> ChallengeOut:
        """Step 1: email + password. Answers the second steps on offer (TOTP first; email code fallback)."""
        async with tx() as s:
            result = await svc.login(s, deps, body.email, body.password)
        if isinstance(result, svc.Refused):
            raise _refuse(result)
        return ChallengeOut(
            challenge_token=result.token,
            methods=list(result.methods),
            totp_enrolled=result.totp_enrolled,
            expires_at=result.expires_at,
        )

    @r.post("/mfa/totp", response_model=TokenOut)
    async def mfa_totp(body: CodeIn, request: Request, response: Response) -> TokenOut:
        """Step 2 with the authenticator app: `mfa=true` (needed for approvals, price edits, settings)."""
        ip, ua = _client(request)
        async with tx() as s:
            result = await svc.verify_second_step(
                s, deps, body.challenge_token, body.code, "totp", ip=ip, user_agent=ua
            )
        if isinstance(result, svc.Refused):
            raise _refuse(result)
        return token_out(result, response)

    @r.post("/mfa/email/send", status_code=204)
    async def mfa_email_send(body: ChallengeIn) -> None:
        """Fallback second step: a 6-digit code to the staff member's work address (valid 10 minutes)."""
        if deps.mailer is None:
            raise Unavailable("email codes are not available")
        async with tx() as s:
            result = await svc.send_email_code(s, deps, body.challenge_token)
        if isinstance(result, svc.Refused):
            raise _refuse(result)
        address, code = result
        await deps.mailer.send_code(
            address, "Your sign-in code", f"Your sign-in code is {code}. It expires in 10 minutes."
        )

    @r.post("/mfa/email/verify", response_model=TokenOut)
    async def mfa_email_verify(body: CodeIn, request: Request, response: Response) -> TokenOut:
        """Step 2 with the email code: signed in with `mfa=false` (no approvals until TOTP is used)."""
        ip, ua = _client(request)
        async with tx() as s:
            result = await svc.verify_second_step(
                s, deps, body.challenge_token, body.code, "email", ip=ip, user_agent=ua
            )
        if isinstance(result, svc.Refused):
            raise _refuse(result)
        return token_out(result, response)

    @r.post("/refresh", response_model=TokenOut)
    async def refresh(
        request: Request,
        response: Response,
        body: RefreshIn | None = None,
        kx_refresh: Annotated[str | None, Cookie()] = None,
    ) -> TokenOut:
        """A new access token. The refresh token rotates; a reused old one ends all the user's sessions."""
        token = (body.refresh_token if body else None) or kx_refresh
        if not token:
            raise Unauthorized("no session")
        ip, ua = _client(request)
        async with tx() as s:
            result = await svc.refresh(s, deps, token, ip=ip, user_agent=ua)
        if isinstance(result, svc.Refused):
            response.delete_cookie(COOKIE, path=COOKIE_PATH, domain=deps.auth.cookie_domain)
            raise _refuse(result)
        return token_out(result, response)

    @r.post("/logout", status_code=204)
    async def logout(response: Response, kx_refresh: Annotated[str | None, Cookie()] = None) -> None:
        """End this browser's session: the refresh token is revoked and its cookie cleared."""
        if kx_refresh:
            async with tx() as s:
                await svc.logout(s, deps, kx_refresh)
        response.delete_cookie(COOKIE, path=COOKIE_PATH, domain=deps.auth.cookie_domain)

    @r.get("/me", response_model=StaffUserOut)
    async def me(p: Signed) -> StaffUserOut:
        """The signed-in staff member (name, role, languages, alert channels) for the screen header."""
        async with tx() as s:
            user = await s.get(User, p.user_id) if p.user_id else None
            if user is None:
                raise Unauthorized("unknown user")
            return StaffUserOut.model_validate(user)

    @r.post("/totp/enrol", response_model=TotpEnrolOut)
    async def totp_enrol(p: Signed) -> TotpEnrolOut:
        """A new authenticator seed for the signed-in user (active only after /totp/confirm)."""
        async with tx() as s:
            secret, uri = await svc.begin_totp(s, deps, _uid(p))
        return TotpEnrolOut(secret=secret, otpauth_uri=uri)

    @r.post("/totp/confirm", status_code=204)
    async def totp_confirm(body: TotpConfirmIn, p: Signed) -> None:
        """The first code from the app switches TOTP on. Sign in again to get an MFA session."""
        async with tx() as s:
            ok = await svc.confirm_totp(s, deps, _uid(p), body.code)
        if not ok:
            raise Unauthorized("the code is not correct")

    @r.post("/password", status_code=204)
    async def password_change(body: PasswordChangeIn, p: Signed) -> None:
        """Change the password (ends every other session)."""
        async with tx() as s:
            result = await svc.change_password(s, deps, _uid(p), body.current_password, body.new_password)
        if result is not None:
            raise _refuse(result)

    @r.post("/password/reset/start", status_code=202)
    async def reset_start(body: ResetStartIn) -> None:
        """Recovery: a code to the work address. Always 202 (nobody learns which addresses exist)."""
        async with tx() as s:
            found = await svc.start_reset(s, deps, body.email)
        if found is not None and deps.mailer is not None:
            await deps.mailer.send_code(
                found[0],
                "Reset your password",
                f"Your password reset code is {found[1]}. It expires in 10 minutes.",
            )

    @r.post("/password/reset/finish", status_code=204)
    async def reset_finish(body: ResetFinishIn) -> None:
        """Recovery step 2: the emailed code + a new password (min 12 characters); every session ends."""
        async with tx() as s:
            result = await svc.finish_reset(s, deps, body.email, body.code, body.new_password)
        if result is not None:
            raise _refuse(result)

    return r


def _uid(p: Principal) -> uuid.UUID:
    if p.user_id is None:
        raise Unauthorized("token has no CRM user id")
    return p.user_id


__all__ = ["COOKIE", "StaffUserOut", "router"]
