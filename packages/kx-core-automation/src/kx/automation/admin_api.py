"""§20.11 settings that live in automation: "WhatsApp numbers per region" and "message templates" (admin).

* Channel accounts: which company number serves which countries (§11.4 / D11 regional numbers). Secrets are
  never shown or set here: `secret_ref` names the Secrets Manager entry an operator filled.
* Templates: WhatsApp templates must be approved by Meta before they can open a chat (§14 practical notes,
  §17), so a new or edited WhatsApp template is `submitted` until the admin records Meta's approval; email
  templates are usable at once. Every change is logged.
"""

# No `from __future__ import annotations` here: FastAPI resolves the closure-local annotations.

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kx.automation.models import ChannelAccount, MessageTemplate
from kx.core.auth import Principal, Role, require_human_mfa, require_role
from kx.core.db import Databases
from kx.core.errors import NotFound
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)
Reader = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.MANAGER))]
Admin = Annotated[Principal, Depends(require_human_mfa(Role.ADMIN))]


class ChannelAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: str
    provider: str
    display_name: str
    address: str  # the company number / address buyers see
    purpose: str  # customer | internal_alerts
    status: str
    region: str | None
    countries: list[str]  # ISO-2 countries this number serves (§11.4)
    is_default: bool
    created_at: datetime


class ChannelAccountPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    region: str | None = Field(default=None, max_length=64)
    countries: list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] | None = None
    is_default: bool | None = None
    status: Literal["active", "disabled"] | None = None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: str
    name: str
    language: str
    category: str
    provider_status: (
        str  # draft | submitted | approved | rejected | paused (DDL; Meta's decision for WhatsApp)
    )
    subject: str | None
    body: str  # placeholders {{1}}, {{2}} ... in Meta's style
    variables: list[str]
    updated_at: datetime


class TemplateIn(BaseModel):
    channel: Literal["whatsapp", "email"]
    name: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    language: str = Field(min_length=2, max_length=16)
    category: Literal["utility", "marketing", "authentication", "internal"] = "utility"
    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1, max_length=4000)
    variables: list[str] = Field(default_factory=list, max_length=20)


class TemplateStatusIn(BaseModel):
    provider_status: Literal["draft", "submitted", "approved", "rejected", "paused"]


def build_router(databases: Databases) -> APIRouter:
    r = APIRouter(prefix="/v1/admin", tags=["admin: channels"])

    @r.get("/channel-accounts", response_model=list[ChannelAccountOut])
    async def accounts(_: Reader) -> list[ChannelAccountOut]:
        """§20.11 company WhatsApp numbers and email addresses, with the countries each number serves."""
        async with databases.transaction(LogicalDb.AUTOMATION) as s:
            rows = (
                await s.execute(
                    select(ChannelAccount).order_by(ChannelAccount.channel, ChannelAccount.region)
                )
            ).scalars()
            return [ChannelAccountOut.model_validate(a) for a in rows]

    @r.patch("/channel-accounts/{account_id}", response_model=ChannelAccountOut)
    async def edit_account(account_id: uuid.UUID, body: ChannelAccountPatch, p: Admin) -> ChannelAccountOut:
        """Regional routing for WhatsApp numbers: which countries a number serves, the default number."""
        async with databases.transaction(LogicalDb.AUTOMATION) as s:
            account = await s.get(ChannelAccount, account_id, with_for_update=True)
            if account is None:
                raise NotFound("channel account not found")
            changes = body.model_dump(exclude_unset=True)
            for k, v in changes.items():
                setattr(account, k, v)
            await s.flush()
            log.info("admin.channel_account", account=str(account_id), by=p.subject, changes=changes)
            return ChannelAccountOut.model_validate(account)

    @r.get("/message-templates", response_model=list[TemplateOut])
    async def templates(_: Reader) -> list[TemplateOut]:
        """§20.11 message templates and Meta's approval state (only approved WhatsApp ones can be sent)."""
        async with databases.transaction(LogicalDb.AUTOMATION) as s:
            rows = (
                await s.execute(
                    select(MessageTemplate).order_by(MessageTemplate.name, MessageTemplate.language)
                )
            ).scalars()
            return [TemplateOut.model_validate(t) for t in rows]

    @r.put("/message-templates", response_model=TemplateOut)
    async def upsert_template(body: TemplateIn, p: Admin) -> TemplateOut:
        """Add or change a template. A WhatsApp template waits for Meta's approval (`submitted`)."""
        status = "submitted" if body.channel == "whatsapp" else "approved"
        values = {**body.model_dump(), "provider_status": status}
        async with databases.transaction(LogicalDb.AUTOMATION) as s:
            stmt = (
                pg_insert(MessageTemplate)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["channel", "name", "language"],
                    set_={k: v for k, v in values.items() if k not in ("channel", "name", "language")},
                )
                .returning(MessageTemplate)
            )
            row = (await s.execute(stmt)).scalar_one()
            log.info("admin.template", name=body.name, language=body.language, by=p.subject, status=status)
            return TemplateOut.model_validate(row)

    @r.post("/message-templates/{template_id}/status", response_model=TemplateOut)
    async def template_status(template_id: uuid.UUID, body: TemplateStatusIn, p: Admin) -> TemplateOut:
        """Record Meta's decision on a WhatsApp template (only `approved` templates can be sent)."""
        async with databases.transaction(LogicalDb.AUTOMATION) as s:
            row = await s.get(MessageTemplate, template_id, with_for_update=True)
            if row is None:
                raise NotFound("template not found")
            row.provider_status = body.provider_status
            await s.flush()
            log.info(
                "admin.template_status", template=str(template_id), by=p.subject, status=body.provider_status
            )
            return TemplateOut.model_validate(row)

    return r
