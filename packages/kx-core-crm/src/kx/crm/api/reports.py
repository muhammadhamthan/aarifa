"""§21 dashboards per role and §20.12 exports.

Periods default to the current calendar month in the company's time zone. Definitions the SRS does not spell
out (OPEN_QUESTIONS #63):
* response time = an approval's submit -> decision, per approver;
* "quotes sent" = quote versions that reached status `sent` (created in the period);
* win rate = WON / (WON + LOST) among deals closed in the period;
* average discount from list = 1 - final quote total / first quote total, over deals won in the period
  (v1 keeps no per-line list price on quotes);
* opt-outs = deals marked Opted out in the period, with the reasons the buyers gave themselves (§8.1 step 4).
"""

# No `from __future__ import annotations` here: FastAPI resolves the closure-local annotations.

import csv
import io
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Response
from openpyxl import Workbook  # type: ignore[import-untyped]  # types-openpyxl not in uv.lock
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from kx.core.auth import Principal, Role, require_role
from kx.core.errors import Forbidden
from kx.crm.api.context import MANAGERS, STAFF, ApiContext
from kx.crm.api.deals import visible_to
from kx.crm.models import (
    Company,
    Contact,
    Deal,
    DealRequirement,
    Order,
    Product,
    Quote,
    QuoteVersion,
    User,
)
from kx.crm.rules import OPEN_STAGES
from kx.crm.services.dashboards import (
    ManagerBoard,
    SalespersonBoard,
    counts,
    manager_board,
    salesperson_board,
)

Staff = Annotated[Principal, Depends(require_role(*STAFF))]
Manager = Annotated[Principal, Depends(require_role(*MANAGERS))]
Md = Annotated[Principal, Depends(require_role(Role.MD))]
OPEN = [st.value for st in OPEN_STAGES]


class MdBoard(BaseModel):
    period_from: date
    period_to: date
    pipeline_aed_by_stage: dict[str, Decimal]
    win_rate: float | None
    avg_discount_from_list: float | None
    deals_by_country: dict[str, int]
    deals_by_category: dict[str, int]
    deals_by_channel: dict[str, int]
    top_buyers: list[dict[str, Any]]  # company, won value in AED
    takeover_queue: list[uuid.UUID]  # open deals the MD has taken over


def _period(tz: str, start: date | None, end: date | None) -> tuple[date, date, datetime, datetime]:
    today = datetime.now(ZoneInfo(tz)).date()
    start = start or today.replace(day=1)
    end = end or today
    zone = ZoneInfo(tz)
    return (
        start,
        end,
        datetime.combine(start, time.min, zone).astimezone(UTC),
        datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


_EXPORTS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "deals": (
        Deal,
        (
            "number",
            "stage",
            "source",
            "currency",
            "owner_user_id",
            "opted_out_at",
            "won_at",
            "lost_at",
            "lost_reason",
            "created_at",
        ),
    ),
    "contacts": (
        Contact,
        (
            "full_name",
            "designation",
            "email",
            "phone_e164",
            "preferred_channel",
            "language",
            "phone_status",
            "created_at",
        ),
    ),
    "companies": (
        Company,
        ("name", "country_code", "city", "customer_type", "vat_trn", "website", "created_at"),
    ),
    "orders": (
        Order,
        (
            "number",
            "status",
            "production_started_on",
            "ready_on",
            "shipped_on",
            "delivered_on",
            "tracking_ref",
            "created_at",
        ),
    ),
    "products": (
        Product,
        ("sku", "name", "category_code", "unit", "moq", "is_active", "is_quotable", "cms_published"),
    ),
}


def router(ctx: ApiContext) -> APIRouter:  # noqa: PLR0915 - one handler per dashboard
    r = APIRouter(tags=["dashboards"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    async def tz(s: AsyncSession) -> str:
        return (await deps.settings.get(s)).alert_hours.tz or "Asia/Dubai"  # the company's day

    @r.get("/dashboards/salesperson", response_model=SalespersonBoard)
    async def salesperson(
        s: Session,
        p: Staff,
        user_id: uuid.UUID | None = None,
        period_from: date | None = None,
        period_to: date | None = None,
    ) -> SalespersonBoard:
        """§21 salesperson: my open deals by stage, items waiting for me, response times, sent and won."""
        target = user_id or p.user_id
        if p.role is Role.SALES and target != p.user_id:
            raise Forbidden("a salesperson sees their own dashboard")
        user = await s.get(User, target) if target else None
        if user is None:
            raise Forbidden("no CRM user")
        _, _, since, until = _period(await tz(s), period_from, period_to)
        return await salesperson_board(s, user, since, until)

    @r.get("/dashboards/manager", response_model=ManagerBoard)
    async def manager(
        s: Session, _: Manager, period_from: date | None = None, period_to: date | None = None
    ) -> ManagerBoard:
        """§21 manager: everyone's board, below-minimum requests, change requests by type, SLA breaches,
        price-list changes, plus opt-outs and the buyers' own reasons (§8.1)."""
        start, end, since, until = _period(await tz(s), period_from, period_to)
        return await manager_board(s, start, end, since, until)

    @r.get("/dashboards/md", response_model=MdBoard)
    async def md(
        s: Session, _: Md, period_from: date | None = None, period_to: date | None = None
    ) -> MdBoard:
        """§21 MD: pipeline by stage, win rate, average discount from list, deals by country / category /
        channel, top buyers, takeover queue."""
        start, end, since, until = _period(await tz(s), period_from, period_to)
        current = (
            select(Deal.stage, func.coalesce(func.sum(QuoteVersion.total_aed), 0))
            .join(Quote, Quote.deal_id == Deal.id)
            .join(QuoteVersion, QuoteVersion.id == Quote.current_version_id)
            .where(Deal.stage.in_(OPEN))
            .group_by(Deal.stage)
        )
        pipeline = {str(k): Decimal(v) for k, v in (await s.execute(current)).tuples().all()}
        won = await s.scalar(select(func.count()).where(Deal.won_at >= since, Deal.won_at < until)) or 0
        lost = await s.scalar(select(func.count()).where(Deal.lost_at >= since, Deal.lost_at < until)) or 0
        created = (Deal.created_at >= since, Deal.created_at < until)
        v1 = aliased(QuoteVersion)
        first = (
            select(v1.total_aed)
            .where(v1.quote_id == Quote.id)
            .order_by(v1.version_no)
            .limit(1)
            .correlate(Quote)
            .scalar_subquery()
        )
        discount = await s.scalar(
            select(func.avg(1 - QuoteVersion.total_aed / func.nullif(first, 0)))
            .select_from(Deal)
            .join(Quote, Quote.deal_id == Deal.id)
            .join(QuoteVersion, QuoteVersion.id == Quote.current_version_id)
            .where(Deal.won_at >= since, Deal.won_at < until)
        )
        top = (
            (
                await s.execute(
                    select(Company.name, func.sum(QuoteVersion.total_aed))
                    .join(Deal, Deal.company_id == Company.id)
                    .join(Quote, Quote.deal_id == Deal.id)
                    .join(QuoteVersion, QuoteVersion.id == Quote.current_version_id)
                    .where(Deal.won_at >= since, Deal.won_at < until)
                    .group_by(Company.name)
                    .order_by(func.sum(QuoteVersion.total_aed).desc())
                    .limit(10)
                )
            )
            .tuples()
            .all()
        )
        return MdBoard(
            period_from=start,
            period_to=end,
            pipeline_aed_by_stage=pipeline,
            win_rate=won / (won + lost) if won + lost else None,
            avg_discount_from_list=float(discount) if discount is not None else None,
            deals_by_country=await counts(
                s,
                select(DealRequirement.delivery_country, func.count(func.distinct(Deal.id)))
                .join(Deal, Deal.id == DealRequirement.deal_id)
                .where(*created)
                .group_by(DealRequirement.delivery_country),
            ),
            deals_by_category=await counts(
                s,
                select(Product.category_code, func.count(func.distinct(Deal.id)))
                .join(DealRequirement, DealRequirement.product_id == Product.id)
                .join(Deal, Deal.id == DealRequirement.deal_id)
                .where(*created)
                .group_by(Product.category_code),
            ),
            deals_by_channel=await counts(
                s, select(Deal.source, func.count()).where(*created).group_by(Deal.source)
            ),
            top_buyers=[{"company": n, "won_aed": Decimal(v or 0)} for n, v in top],
            takeover_queue=list(
                (
                    await s.execute(
                        select(Deal.id).where(Deal.taken_over_by.is_not(None), Deal.stage.in_(OPEN))
                    )
                ).scalars()
            ),
        )

    @r.get("/exports/{entity}", tags=["exports"])
    async def export(
        entity: Literal["deals", "contacts", "companies", "orders", "products"],
        s: Session,
        p: Staff,
        format: Literal["csv", "xlsx"] = "csv",
    ) -> Response:
        """§20.12: a list as CSV or XLSX (a salesperson exports only their own deals and orders)."""
        model, columns = _EXPORTS[entity]
        stmt: Select[*tuple[Any, ...]] = select(*(getattr(model, c) for c in columns))
        if entity in ("contacts", "companies"):
            stmt = stmt.where(model.anonymised_at.is_(None))  # §22 retention: anonymised records stay out
        if p.role is Role.SALES:  # §3: a salesperson exports only what they may see
            if entity == "orders":
                stmt = visible_to(stmt.join(Deal, Deal.id == Order.deal_id), p)
            elif entity == "deals":
                stmt = visible_to(stmt, p)
            elif entity == "contacts":
                stmt = stmt.where(Contact.id.in_(visible_to(select(Deal.primary_contact_id), p)))
            elif entity == "companies":
                stmt = stmt.where(Company.id.in_(visible_to(select(Deal.company_id), p)))
        rows = (await s.execute(stmt.limit(50_000))).tuples().all()
        name = f"{entity}-{datetime.now(UTC):%Y%m%d}"
        if format == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(columns)
            writer.writerows(["" if v is None else str(v) for v in row] for row in rows)
            return Response(
                buf.getvalue(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
            )
        wb = Workbook(write_only=True)
        ws = wb.create_sheet(entity)
        ws.append(list(columns))
        for row in rows:
            ws.append(
                [
                    v.replace(tzinfo=None)
                    if isinstance(v, datetime)
                    else (str(v) if isinstance(v, uuid.UUID) else v)
                    for v in row
                ]
            )
        out = io.BytesIO()
        wb.save(out)
        return Response(
            out.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'},
        )

    return r
