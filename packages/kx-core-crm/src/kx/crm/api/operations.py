"""D35 kill-switch ladder for the admin and the MD: "stop marketing sends -> stop outbound calls -> text
only -> hold and hand everything to staff". Every change is audited."""

# No `from __future__ import annotations` here: FastAPI resolves the closure-local annotations.

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_human_mfa, require_role
from kx.core.errors import Unavailable
from kx.core.killswitch import KillLevel, KillSwitchLadder
from kx.crm.api.context import ApiContext
from kx.crm.services.journal import Actor, record

Reader = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.MANAGER))]
Writer = Annotated[Principal, Depends(require_human_mfa(Role.ADMIN, Role.MD))]


class KillSwitchOut(BaseModel):
    level: KillLevel
    marketing_stopped: bool
    calls_stopped: bool
    voice_stopped: bool  # website voice off; chat stays
    all_held: bool  # nothing leaves; staff handle every buyer


class KillSwitchIn(BaseModel):
    level: KillLevel


def _out(level: KillLevel) -> KillSwitchOut:
    from kx.core.killswitch import engaged_at  # noqa: PLC0415 - tiny helper, keeps the import list short

    return KillSwitchOut(
        level=level,
        marketing_stopped=engaged_at(level, "marketing"),
        calls_stopped=engaged_at(level, "phone"),
        voice_stopped=engaged_at(level, "voice"),
        all_held=engaged_at(level, "all"),
    )


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(prefix="/admin", tags=["admin"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    def ladder() -> KillSwitchLadder:
        if deps.kill_switch is None:
            raise Unavailable("the kill switch is not configured")
        return deps.kill_switch

    @r.get("/kill-switch", response_model=KillSwitchOut)
    async def get_level(_: Reader) -> KillSwitchOut:
        """D35 kill switch: the current level and what it stops."""
        return _out(await ladder().level())

    @r.put("/kill-switch", response_model=KillSwitchOut)
    async def set_level(body: KillSwitchIn, s: Session, p: Writer) -> KillSwitchOut:
        """Admin or MD (MFA): set the kill-switch level (off, marketing, calls, text_only, hold); logged."""
        before = await ladder().level()
        await ladder().set_level(body.level)
        await record(
            s,
            Actor.user(p),
            "admin.kill_switch",
            "company",
            deps.company_code,
            before={"level": before},
            after={"level": body.level},
        )
        return _out(body.level)

    return r
