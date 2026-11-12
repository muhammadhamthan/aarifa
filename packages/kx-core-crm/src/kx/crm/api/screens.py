"""§20 screens: my inbox, price list (tiers, Excel import/export, change history), stock table. Thin:
services do the work."""

# No `from __future__ import annotations` here: FastAPI must evaluate the closure-local `Session`
# alias; PEP 649 (Python 3.14) lazy annotations resolve it, string annotations would not.

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_human_mfa, require_role
from kx.crm.api.context import STAFF, ApiContext
from kx.crm.models import AuditEvent, StockItem
from kx.crm.services import inbox, orders, price_lists
from kx.crm.services.dashboards import PRICE_LIST_ACTIONS
from kx.crm.services.journal import Actor

Staff = Annotated[Principal, Depends(require_role(*STAFF))]
# §3 "Edit price list: manager and admin" (a person with MFA: prices are money)
PriceEditor = Annotated[Principal, Depends(require_human_mfa(Role.MANAGER, Role.ADMIN))]
StockEditor = Annotated[Principal, Depends(require_role(Role.MANAGER, Role.ADMIN))]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InboxItemOut(_Out):
    kind: str
    ref_id: str
    deal_id: uuid.UUID
    deal_number: str
    title: str
    due_at: datetime | None
    created_at: datetime
    overdue: bool


class PriceTierOut(_Out):
    id: uuid.UUID
    product_id: uuid.UUID
    min_qty: Decimal
    max_qty: Decimal | None
    list_price: Decimal
    min_price: Decimal | None
    currency: str
    valid_from: datetime


class PriceTierIn(BaseModel):
    min_qty: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    max_qty: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    list_price: Decimal = Field(gt=0, max_digits=14, decimal_places=6)
    min_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=6)
    valid_from: datetime | None = None


class PriceTierPatch(BaseModel):
    min_qty: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    max_qty: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    list_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=6)
    min_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=6)


class RowErrorOut(BaseModel):
    row: int
    field: str
    error: str


class ImportOut(BaseModel):
    saved: int
    errors: list[RowErrorOut]


class ChangeOut(_Out):
    """One logged change (§3 "every change is logged"): who, when, what, old and new value."""

    at: datetime
    actor_type: str
    actor_id: str
    action: str
    entity_type: str
    entity_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class StockOut(_Out):
    id: uuid.UUID
    item_type: str
    product_id: uuid.UUID | None
    material_code: str | None
    quantity: Decimal
    reserved_quantity: Decimal
    unit: str


class StockIn(BaseModel):
    product_id: uuid.UUID | None = None
    material_code: str | None = None
    quantity: Decimal = Field(ge=0, max_digits=16, decimal_places=3)
    unit: str = "pcs"


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter()
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    @r.get("/inbox", response_model=list[InboxItemOut], tags=["inbox"])
    async def my_inbox(
        s: Session, p: Staff, limit: Annotated[int, Query(ge=1, le=500)] = 200
    ) -> list[InboxItemOut]:
        """§20.2: approvals, change requests, below-minimum alerts, buyer messages on handed-over deals."""
        return [InboxItemOut.model_validate(i) for i in await inbox.inbox_for(s, p, deps.now(), limit)]

    # ------------------------------------------------------------ price list (§10, §20.7)
    @r.get("/products/{product_id}/price-tiers", response_model=list[PriceTierOut], tags=["price list"])
    async def tiers(product_id: uuid.UUID, s: Session, _: Staff) -> list[PriceTierOut]:
        """§20.7 price list: the quantity tiers of one product (list price and minimum per tier)."""
        return [PriceTierOut.model_validate(t) for t in await price_lists.tiers_for(s, product_id)]

    @r.post(
        "/products/{product_id}/price-tiers",
        response_model=PriceTierOut,
        status_code=201,
        tags=["price list"],
    )
    async def add_tier(product_id: uuid.UUID, body: PriceTierIn, s: Session, p: PriceEditor) -> PriceTierOut:
        """Manager (MFA): add a quantity tier (every change is logged, §3)."""
        tier = await price_lists.add_tier(
            s,
            product_id,
            min_qty=body.min_qty,
            max_qty=body.max_qty,
            list_price=body.list_price,
            min_price=body.min_price,
            valid_from=body.valid_from or deps.now(),
            actor=Actor.user(p),
        )
        return PriceTierOut.model_validate(tier)

    @r.patch("/price-tiers/{tier_id}", response_model=PriceTierOut, tags=["price list"])
    async def edit_tier(tier_id: uuid.UUID, body: PriceTierPatch, s: Session, p: PriceEditor) -> PriceTierOut:
        """Manager (MFA): change a tier's prices or quantities (logged with old and new value)."""
        changes: dict[str, Any] = body.model_dump(exclude_unset=True)
        return PriceTierOut.model_validate(await price_lists.update_tier(s, tier_id, changes, Actor.user(p)))

    @r.delete("/price-tiers/{tier_id}", status_code=204, tags=["price list"])
    async def remove_tier(tier_id: uuid.UUID, s: Session, p: PriceEditor) -> None:
        """Manager (MFA): delete a tier (logged)."""
        await price_lists.delete_tier(s, tier_id, Actor.user(p))

    @r.post("/price-list/import", response_model=ImportOut, tags=["price list"])
    async def import_price_list(request: Request, s: Session, p: PriceEditor) -> ImportOut:
        """Body: the .xlsx file (columns sku, min_qty, max_qty, list_price, min_price, currency). All rows are
        validated first; any error saves nothing and returns the row errors (§10)."""
        result = await price_lists.import_xlsx(
            s, await request.body(), valid_from=deps.now(), actor=Actor.user(p)
        )
        return ImportOut(
            saved=result.saved,
            errors=[RowErrorOut(row=e.row, field=e.field, error=e.error) for e in result.errors],
        )

    @r.get("/price-list/export", tags=["price list"])
    async def export_price_list(s: Session, _: Staff) -> Response:
        """Download the price list as Excel (the same layout the import accepts)."""
        data = await price_lists.export_xlsx(s, deps.now())
        return Response(
            data, media_type=XLSX, headers={"Content-Disposition": 'attachment; filename="price-list.xlsx"'}
        )

    @r.get("/products/{product_id}/history", response_model=list[ChangeOut], tags=["price list"])
    async def product_history(
        product_id: uuid.UUID, s: Session, _: Staff, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[ChangeOut]:
        """§20.7 change history of one product: its CRM-owned fields and its price tiers, newest first."""
        pid = str(product_id)
        own = (AuditEvent.entity_type == "product") & (AuditEvent.entity_id == pid)
        tier = (AuditEvent.entity_type == "price_tier") & or_(
            AuditEvent.after["product_id"].astext == pid, AuditEvent.before["product_id"].astext == pid
        )
        stmt = (
            select(AuditEvent)
            .where(or_(own, tier))
            .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
            .limit(limit)
        )
        return [ChangeOut.model_validate(e) for e in (await s.execute(stmt)).scalars()]

    @r.get("/price-list/history", response_model=list[ChangeOut], tags=["price list"])
    async def price_list_history(
        s: Session, _: Staff, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[ChangeOut]:
        """§20.7 change history of the whole price list (tier edits and Excel imports), newest first."""
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.action.in_(PRICE_LIST_ACTIONS))
            .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
            .limit(limit)
        )
        return [ChangeOut.model_validate(e) for e in (await s.execute(stmt)).scalars()]

    # ------------------------------------------------------------ stock table (§15.5, §20.8)
    @r.get("/stock", response_model=list[StockOut], tags=["stock"])
    async def stock(s: Session, _: Staff) -> list[StockOut]:
        """§20.8 stock table: quantity on hand per product or material."""
        rows = (await s.execute(select(StockItem).order_by(StockItem.item_type, StockItem.id))).scalars()
        return [StockOut.model_validate(x) for x in rows]

    @r.put("/stock", response_model=StockOut, tags=["stock"])
    async def set_stock(body: StockIn, s: Session, p: StockEditor) -> StockOut:
        """Manager or admin: set the quantity on hand of a product or material (logged)."""
        item = await orders.set_stock(
            s,
            product_id=body.product_id,
            material_code=body.material_code,
            quantity=body.quantity,
            unit=body.unit,
            actor=Actor.user(p),
        )
        await s.refresh(item)
        return StockOut.model_validate(item)

    return r
