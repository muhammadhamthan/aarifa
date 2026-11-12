"""§20 screens 6, 7, 9 and the FX overrides of 11: contacts and companies, the product catalogue (the
CRM-owned fields), orders, FX rates.

* §3 visibility: a salesperson sees the contacts and orders of their own deals (and those they cover).
* §7 / D22: the CMS owns a product's description (name, specification, photos); the CRM owns prices, MOQ for
  quoting, the default delivery time and whether it may be quoted. Only those CRM fields are editable here.
* §10 / D38: "the manager can override" the daily FX rate; the latest rate per currency wins.
* Phone and email are not edited here: they are the buyer's identity (merge rules, phone status, opt-out
  list) and change through the deal (`ContactPatch`, §8.2).
"""

# No `from __future__ import annotations` here: FastAPI resolves the closure-local annotations.

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_human_mfa, require_role
from kx.core.errors import Forbidden, NotFound
from kx.crm.api.context import MANAGERS, STAFF, ApiContext
from kx.crm.api.deals import visible_to
from kx.crm.api.pagination import Page, keyset, page_of
from kx.crm.models import Company, Contact, Deal, FxRate, Order, Product
from kx.crm.services.journal import Actor, record

Staff = Annotated[Principal, Depends(require_role(*STAFF))]
Manager = Annotated[Principal, Depends(require_role(*MANAGERS))]
HumanManager = Annotated[Principal, Depends(require_human_mfa(Role.MANAGER))]
Limit = Annotated[int, Query(ge=1, le=100)]


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ContactRow(_Row):
    id: uuid.UUID
    full_name: str | None
    designation: str | None
    company_id: uuid.UUID | None
    phone_e164: str | None
    whatsapp_e164: str | None
    email: str | None
    preferred_channel: str | None
    language: str
    timezone: str
    phone_status: str  # §8.2: unknown | not_reached | reachable | likely | verified
    source: str
    created_at: datetime


class ContactEditIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=200)
    company_id: uuid.UUID | None = None
    preferred_channel: Literal["whatsapp", "email", "phone"] | None = None
    language: str | None = Field(default=None, max_length=16)
    timezone: str | None = Field(default=None, max_length=64)


class CompanyRow(_Row):
    id: uuid.UUID
    name: str
    country_code: str | None
    city: str | None
    customer_type: str | None
    vat_trn: str | None
    website: str | None
    notes: str | None
    created_at: datetime


class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    city: str | None = Field(default=None, max_length=200)
    customer_type: str | None = Field(default=None, max_length=64)
    vat_trn: str | None = Field(default=None, max_length=64)
    website: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=4000)


class ProductRow(_Row):
    id: uuid.UUID
    sku: str  # the product code shared with the CMS
    name: str
    category_code: str
    unit: str
    moq: int | None
    carton_qty: int | None
    is_active: bool
    is_quotable: bool
    custom_printing: bool
    default_lead_days_min: int | None
    default_lead_days_max: int | None
    spec: dict[str, Any]
    cms_product_id: str | None
    cms_published: bool
    cms_synced_at: datetime | None


class ProductEditIn(BaseModel):
    """CRM-owned fields only (D22): the CMS owns the description."""

    moq: int | None = Field(default=None, ge=1)
    carton_qty: int | None = Field(default=None, ge=1)
    unit: str | None = Field(default=None, max_length=16)
    is_active: bool | None = None
    is_quotable: bool | None = None
    default_lead_days_min: int | None = Field(default=None, ge=0, le=365)
    default_lead_days_max: int | None = Field(default=None, ge=0, le=365)


class OrderRow(_Row):
    id: uuid.UUID
    number: str
    deal_id: uuid.UUID
    status: str
    production_started_on: date | None
    ready_on: date | None
    shipped_on: date | None
    delivered_on: date | None
    tracking_ref: str | None
    created_at: datetime


class FxRow(_Row):
    currency: str
    rate_to_aed: Decimal
    as_of: date
    source: str


class FxIn(BaseModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    rate_to_aed: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    as_of: date


def _own_contacts[S: Select[Any]](stmt: S, p: Principal) -> S:
    if p.role is not Role.SALES:
        return stmt
    deals = visible_to(select(Deal.primary_contact_id), p)
    return stmt.where(Contact.id.in_(deals))


def router(ctx: ApiContext) -> APIRouter:  # noqa: PLR0915 - one small handler per screen action
    r = APIRouter(tags=["records"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806

    # ---------------------------------------------------------------- contacts (§20.6)
    @r.get("/contacts", response_model=Page[ContactRow])
    async def contacts(
        s: Session, p: Staff, q: str | None = None, cursor: str | None = None, limit: Limit = 50
    ) -> Page[ContactRow]:
        """Search by name, email or phone digits. A salesperson sees the contacts of their own deals."""
        stmt = _own_contacts(select(Contact).where(Contact.anonymised_at.is_(None)), p)
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(Contact.full_name.ilike(like), Contact.email.ilike(like), Contact.phone_e164.like(like))
            )
        stmt = keyset(
            stmt, cursor, limit, Contact.created_at, Contact.id, parse=(datetime.fromisoformat, uuid.UUID)
        )
        rows, nxt = page_of(list((await s.execute(stmt)).scalars()), limit, lambda c: (c.created_at, c.id))
        return Page(items=[ContactRow.model_validate(c) for c in rows], next_cursor=nxt)

    async def _contact(s: AsyncSession, p: Principal, contact_id: uuid.UUID) -> Contact:
        found = (await s.execute(_own_contacts(select(Contact).where(Contact.id == contact_id), p))).scalar()
        if found is None:
            raise NotFound("contact not found")
        return found

    @r.get("/contacts/{contact_id}", response_model=ContactRow)
    async def contact(contact_id: uuid.UUID, s: Session, p: Staff) -> ContactRow:
        """§20.6 one contact (a salesperson only for contacts of their own deals)."""
        return ContactRow.model_validate(await _contact(s, p, contact_id))

    @r.patch("/contacts/{contact_id}", response_model=ContactRow)
    async def edit_contact(contact_id: uuid.UUID, body: ContactEditIn, s: Session, p: Staff) -> ContactRow:
        """Edit name, designation, company, preferred channel, language, time zone (phone / email change
        through the deal)."""
        c = await _contact(s, p, contact_id)
        changes = body.model_dump(exclude_unset=True)
        before = {k: getattr(c, k) for k in changes}
        for k, v in changes.items():
            setattr(c, k, v)
        await s.flush()
        await record(s, Actor.user(p), "contact.updated", "contact", c.id, before=before, after=changes)
        return ContactRow.model_validate(c)

    # ---------------------------------------------------------------- companies (§20.6)
    @r.get("/companies", response_model=Page[CompanyRow])
    async def companies(
        s: Session, _: Staff, q: str | None = None, cursor: str | None = None, limit: Limit = 50
    ) -> Page[CompanyRow]:
        """§20.6 companies: search by name, newest first."""
        stmt = select(Company).where(Company.anonymised_at.is_(None))
        if q:
            stmt = stmt.where(Company.name.ilike(f"%{q.strip()}%"))
        stmt = keyset(
            stmt, cursor, limit, Company.created_at, Company.id, parse=(datetime.fromisoformat, uuid.UUID)
        )
        rows, nxt = page_of(list((await s.execute(stmt)).scalars()), limit, lambda c: (c.created_at, c.id))
        return Page(items=[CompanyRow.model_validate(c) for c in rows], next_cursor=nxt)

    @r.post("/companies", response_model=CompanyRow, status_code=201)
    async def create_company(body: CompanyIn, s: Session, p: Staff) -> CompanyRow:
        """Create a company record."""
        company = Company(**body.model_dump())
        s.add(company)
        await s.flush()
        await record(s, Actor.user(p), "company.created", "company", company.id, after=body.model_dump())
        return CompanyRow.model_validate(company)

    @r.patch("/companies/{company_id}", response_model=CompanyRow)
    async def edit_company(company_id: uuid.UUID, body: CompanyIn, s: Session, p: Staff) -> CompanyRow:
        """Edit a company record (logged)."""
        company = await s.get(Company, company_id)
        if company is None:
            raise NotFound("company not found")
        changes = body.model_dump(exclude_unset=True)
        before = {k: getattr(company, k) for k in changes}
        for k, v in changes.items():
            setattr(company, k, v)
        await s.flush()
        await record(s, Actor.user(p), "company.updated", "company", company.id, before=before, after=changes)
        return CompanyRow.model_validate(company)

    # ---------------------------------------------------------------- product catalogue (§20.7, D22)
    @r.get("/products", response_model=list[ProductRow])
    async def products(
        s: Session, _: Staff, category: str | None = None, quotable: bool | None = None
    ) -> list[ProductRow]:
        """§20.7 product catalogue with the CRM-owned fields (MOQ, lead time, quotable) and CMS sync state."""
        stmt = select(Product).order_by(Product.category_code, Product.sku)
        if category:
            stmt = stmt.where(Product.category_code == category)
        if quotable is not None:
            stmt = stmt.where(Product.is_quotable.is_(quotable))
        return [ProductRow.model_validate(x) for x in (await s.execute(stmt)).scalars()]

    @r.get("/products/{product_id}", response_model=ProductRow)
    async def product(product_id: uuid.UUID, s: Session, _: Staff) -> ProductRow:
        """One product with its specification (from the CMS) and CRM-owned fields."""
        found = await s.get(Product, product_id)
        if found is None:
            raise NotFound("product not found")
        return ProductRow.model_validate(found)

    @r.patch("/products/{product_id}", response_model=ProductRow)
    async def edit_product(
        product_id: uuid.UUID, body: ProductEditIn, s: Session, p: HumanManager
    ) -> ProductRow:
        """Manager (MFA): MOQ for quoting, default delivery time, quotable/active (D22: CRM-owned fields)."""
        found = await s.get(Product, product_id, with_for_update=True)
        if found is None:
            raise NotFound("product not found")
        changes = body.model_dump(exclude_unset=True)
        before = {k: getattr(found, k) for k in changes}
        for k, v in changes.items():
            setattr(found, k, v)
        await s.flush()
        await record(s, Actor.user(p), "product.updated", "product", found.id, before=before, after=changes)
        return ProductRow.model_validate(found)

    # ---------------------------------------------------------------- orders (§20.9)
    @r.get("/orders", response_model=Page[OrderRow])
    async def orders(
        s: Session,
        p: Staff,
        status: str | None = None,
        cursor: str | None = None,
        limit: Limit = 50,
    ) -> Page[OrderRow]:
        """§20.9 orders (a salesperson: their own deals), newest first."""
        stmt = select(Order).join(Deal, Deal.id == Order.deal_id)
        stmt = visible_to(stmt, p)
        if status:
            stmt = stmt.where(Order.status == status)
        stmt = keyset(
            stmt, cursor, limit, Order.created_at, Order.id, parse=(datetime.fromisoformat, uuid.UUID)
        )
        rows, nxt = page_of(list((await s.execute(stmt)).scalars()), limit, lambda o: (o.created_at, o.id))
        return Page(items=[OrderRow.model_validate(o) for o in rows], next_cursor=nxt)

    # ---------------------------------------------------------------- FX (§10, §20.11, D38)
    @r.get("/fx-rates", response_model=list[FxRow])
    async def fx_rates(s: Session, _: Manager) -> list[FxRow]:
        """The latest rate per currency (the one quotes use)."""
        stmt = select(FxRate).distinct(FxRate.currency).order_by(FxRate.currency, FxRate.as_of.desc())
        return [FxRow.model_validate(x) for x in (await s.execute(stmt)).scalars()]

    @r.post("/fx-rates", response_model=FxRow, status_code=201)
    async def fx_override(body: FxIn, s: Session, p: HumanManager) -> FxRow:
        """D38 "the manager can override": a manual rate for a day (it replaces that day's rate)."""
        if body.currency == "AED":
            raise Forbidden("AED is the base currency")
        row = await s.get(FxRate, (body.currency, body.as_of))
        before = {"rate_to_aed": row.rate_to_aed, "source": row.source} if row else None
        if row is None:
            row = FxRate(
                currency=body.currency, as_of=body.as_of, rate_to_aed=body.rate_to_aed, source="manual"
            )
            s.add(row)
        else:
            row.rate_to_aed, row.source = body.rate_to_aed, "manual"
        await s.flush()
        await record(
            s,
            Actor.user(p),
            "fx.override",
            "fx_rate",
            f"{body.currency}:{body.as_of}",
            before=before,
            after={"rate_to_aed": body.rate_to_aed, "source": "manual"},
        )
        return FxRow.model_validate(row)

    return r
