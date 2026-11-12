"""§7A.5 / §20.11 "Users and roles" (admin) and each person's alert channels; with the in-house login (D26):
create with a one-time password, reset a lost authenticator, unlock after failed attempts.
"""

# No `from __future__ import annotations` here: FastAPI resolves the closure-local annotations.

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_human_mfa, require_role
from kx.core.errors import NotFound
from kx.crm.api.auth import StaffUserOut
from kx.crm.api.context import ApiContext
from kx.crm.models import StaffCredential, User
from kx.crm.services import auth as auth_svc
from kx.crm.services.journal import Actor, record

# §3 "Admin / IT: users, roles, settings"; the manager and MD see the team (team board, reassign)
Reader = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.MANAGER))]
Admin = Annotated[Principal, Depends(require_human_mfa(Role.ADMIN))]
StaffRole = Literal["md", "manager", "admin", "sales"]
AlertChannel = Literal["whatsapp", "email", "call", "in_app"]


class UserCreateIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    full_name: str = Field(min_length=1, max_length=200)
    role: StaffRole
    phone_e164: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{6,14}$")
    break_glass: bool = False


class UserCreatedOut(BaseModel):
    user: StaffUserOut
    temporary_password: str  # shown once; the person changes it at first sign-in, then enrols TOTP


class UserPatchIn(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: StaffRole | None = None
    phone_e164: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{6,14}$")
    is_active: bool | None = None
    languages: list[str] | None = None  # §9 assignment rules: languages, countries, product categories
    countries: list[str] | None = None
    category_codes: list[str] | None = None
    alert_channels: list[AlertChannel] | None = None  # §7A.5 / §16 each person's alert channels


class UserAdminOut(StaffUserOut):
    backup_user_id: uuid.UUID | None
    totp_enrolled: bool
    locked: bool


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(prefix="/users", tags=["users"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    async def out(s: AsyncSession, user: User) -> UserAdminOut:
        cred = await s.get(StaffCredential, user.id)
        locked = bool(cred and cred.locked_until and cred.locked_until > deps.now())
        return UserAdminOut.model_validate(
            {
                **StaffUserOut.model_validate(user).model_dump(),
                "backup_user_id": user.backup_user_id,
                "totp_enrolled": bool(cred and cred.totp_enabled_at),
                "locked": locked,
            }
        )

    @r.get("", response_model=list[UserAdminOut])
    async def list_users(s: Session, _: Reader) -> list[UserAdminOut]:
        """§20.11 users and roles: every staff member with MFA enrolment and lock state."""
        users = (await s.execute(select(User).order_by(User.role, User.full_name))).scalars()
        return [await out(s, u) for u in users]

    @r.post("", response_model=UserCreatedOut, status_code=201)
    async def create(body: UserCreateIn, s: Session, p: Admin) -> UserCreatedOut:
        """Admin (MFA): a new staff user; the one-time password in the answer is shown once."""
        user, password = await auth_svc.create_user(
            s,
            deps,
            email=body.email,
            full_name=body.full_name,
            role=body.role,
            phone_e164=body.phone_e164,
            break_glass=body.break_glass,
            actor=Actor.user(p),
        )
        return UserCreatedOut(user=StaffUserOut.model_validate(user), temporary_password=password)

    @r.patch("/{user_id}", response_model=UserAdminOut)
    async def patch(user_id: uuid.UUID, body: UserPatchIn, s: Session, p: Admin) -> UserAdminOut:
        """Admin (MFA): role, active flag, phone, languages / countries / categories for assignment (§9),
        alert channels."""
        user = await s.get(User, user_id, with_for_update=True)
        if user is None:
            raise NotFound("user not found")
        changes = body.model_dump(exclude_unset=True)
        before = {k: getattr(user, k) for k in changes}
        for key, value in changes.items():
            setattr(user, key, value)
        await s.flush()
        await record(s, Actor.user(p), "user.updated", "user", user.id, before=before, after=changes)
        return await out(s, user)

    @r.post("/{user_id}/reset-mfa", response_model=UserAdminOut)
    async def reset_mfa(user_id: uuid.UUID, s: Session, p: Admin) -> UserAdminOut:
        """A lost authenticator: sessions end; the person signs in with an email code and enrols again."""
        await auth_svc.reset_mfa(s, deps, user_id, Actor.user(p))
        user = await s.get(User, user_id)
        assert user is not None
        return await out(s, user)

    @r.post("/{user_id}/unlock", response_model=UserAdminOut)
    async def unlock(user_id: uuid.UUID, s: Session, p: Admin) -> UserAdminOut:
        """Admin (MFA): lift a lock-out after too many wrong passwords or codes."""
        await auth_svc.unlock(s, deps, user_id, Actor.user(p))
        user = await s.get(User, user_id)
        assert user is not None
        return await out(s, user)

    return r
