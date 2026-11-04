"""Manager/MD administration: price proposals (T12), business settings, feature flags, dashboard."""

# No `from __future__ import annotations` here: FastAPI must evaluate the closure-local `Session`
# alias; PEP 649 (Python 3.14) lazy annotations resolve it, string annotations would not.

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_human_mfa, require_role
from kx.crm.api.context import MANAGERS, ApiContext
from kx.crm.api.pagination import Page, keyset, page_of
from kx.crm.api.schemas import (
    DashboardSummary,
    FlagIn,
    FreshnessOut,
    RateDecisionIn,
    RateOut,
    SettingIn,
    SettingOut,
)
from kx.crm.models import Approval, Deal, MaterialRate, Quote, QuoteVersion, Setting, Task
from kx.crm.rules import OPEN_STAGES
from kx.crm.services import approvals, price_proposals
from kx.crm.services.journal import Actor

Manager = Annotated[Principal, Depends(require_role(*MANAGERS))]
HumanManager = Annotated[Principal, Depends(require_human_mfa(Role.MANAGER))]
SettingsReader = Annotated[Principal, Depends(require_role(Role.ADMIN, Role.MD))]
SettingsWriter = Annotated[Principal, Depends(require_human_mfa(Role.ADMIN, Role.MD))]


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(tags=["admin"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    # ------------------------------------------------------------ price proposals
    @r.get("/price-proposals", response_model=Page[RateOut], tags=["prices"])
    async def proposals(
        s: Session,
        _: Manager,
        status: Literal["proposed", "approved", "rejected", "superseded"] = "proposed",
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Page[RateOut]:
        stmt = keyset(
            select(MaterialRate).where(MaterialRate.status == status),
            cursor,
            limit,
            MaterialRate.proposed_at,
            MaterialRate.id,
            parse=(datetime.fromisoformat, uuid.UUID),
        )
        rows, nxt = page_of(list((await s.execute(stmt)).scalars()), limit, lambda m: (m.proposed_at, m.id))
        return Page(items=[RateOut.model_validate(m) for m in rows], next_cursor=nxt)

    @r.post("/price-proposals/{rate_id}/approve", response_model=RateOut, tags=["prices"])
    async def approve(rate_id: uuid.UUID, body: RateDecisionIn, s: Session, p: HumanManager) -> RateOut:
        rate = await price_proposals.approve_rate(
            s, rate_id, Actor.user(p), deps.now(), rate_override=body.rate_per_unit, note=body.note
        )
        return RateOut.model_validate(rate)

    @r.post("/price-proposals/{rate_id}/reject", response_model=RateOut, tags=["prices"])
    async def reject(rate_id: uuid.UUID, body: RateDecisionIn, s: Session, p: HumanManager) -> RateOut:
        return RateOut.model_validate(
            await price_proposals.reject_rate(s, rate_id, Actor.user(p), deps.now(), body.note)
        )

    @r.get("/materials/freshness", response_model=list[FreshnessOut], tags=["prices"])
    async def freshness(s: Session, _: Manager) -> list[FreshnessOut]:
        return await _freshness(s)

    async def _freshness(s: AsyncSession) -> list[FreshnessOut]:
        rows = await price_proposals.material_freshness(s, await deps.settings.get(s), deps.now())
        return [
            FreshnessOut(
                material_code=f.material_code, days_since_approval=f.days_since_approval, level=f.level
            )
            for f in rows
        ]

    # ------------------------------------------------------------ settings & flags
    @r.get("/settings", response_model=list[SettingOut], tags=["settings"])
    async def list_settings(s: Session, _: SettingsReader) -> list[SettingOut]:
        rows = (await s.execute(select(Setting).order_by(Setting.key))).scalars()
        return [SettingOut.model_validate(x) for x in rows]

    @r.put("/settings/{key}", response_model=SettingOut, tags=["settings"])
    async def put_setting(key: str, body: SettingIn, s: Session, p: SettingsWriter) -> SettingOut:
        await deps.settings.update(s, key, body.value, Actor.user(p))
        await s.flush()
        row = await s.get(Setting, key, populate_existing=True)
        return SettingOut.model_validate(row)

    @r.put("/feature-flags/{key}", status_code=204, tags=["settings"])
    async def put_flag(key: str, body: FlagIn, s: Session, p: SettingsWriter) -> None:
        await deps.settings.set_flag(s, key, body.enabled, Actor.user(p))

    # ------------------------------------------------------------ dashboard
    @r.get("/dashboard/summary", response_model=DashboardSummary, tags=["dashboard"])
    async def summary(s: Session, _: Manager) -> DashboardSummary:
        by_stage = dict(
            (await s.execute(select(Deal.stage, func.count()).group_by(Deal.stage))).tuples().all()
        )
        open_stages = [st.value for st in OPEN_STAGES]
        pipeline = await s.scalar(
            select(func.coalesce(func.sum(QuoteVersion.total_aed), 0))
            .join(Quote, Quote.current_version_id == QuoteVersion.id)
            .join(Deal, Deal.id == Quote.deal_id)
            .where(Deal.stage.in_(open_stages))
        )
        appr = dict(
            (
                await s.execute(
                    select(Approval.status, func.count())
                    .where(Approval.status.in_(approvals.OPEN))
                    .group_by(Approval.status)
                )
            )
            .tuples()
            .all()
        )
        return DashboardSummary(
            deals_by_stage=by_stage,
            open_pipeline_aed=Decimal(pipeline or 0),
            approvals_pending=appr.get("pending", 0),
            approvals_overdue=appr.get("overdue", 0),
            open_tasks=await s.scalar(select(func.count()).where(Task.status == "open")) or 0,
            ai_paused_deals=await s.scalar(
                select(func.count()).where(Deal.stage.in_(open_stages), Deal.ai_enabled.is_(False))
            )
            or 0,
            materials=await _freshness(s),
        )

    return r
