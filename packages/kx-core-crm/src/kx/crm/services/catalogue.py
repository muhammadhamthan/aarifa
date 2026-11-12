"""§7 / D22: the CMS product record mirrored into the CRM by product code (`products.sku`).

"Which system is the master copy: CMS for descriptive content; CRM for prices, MOQ and the default delivery
time." So a publish writes only the descriptive fields (name, category, specification, custom-printing flag,
the CMS id and the published flag) and never touches price lists, tiers, MOQ, lead days or `is_quotable`.

A code the CRM has not seen yet becomes a product that is **not quotable** until the manager adds its price
list and switches it on (D8: the AI never prices a product without approved prices). An unknown category is
created with the CMS's name, so the product can be filed; its margin rules stay empty until the manager sets
them. Unpublish keeps the product (open deals and quotes refer to it) and only clears the published flag.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.event_payloads import ProductPublished
from kx.crm.models import Product, ProductCategory
from kx.crm.services.journal import Actor, record

UNFILED = "UNCATEGORISED"
_ACTOR = Actor.system("cms_sync")


async def _category(session: AsyncSession, code: str | None, name: str | None) -> str:
    code = (code or UNFILED).strip().upper()
    if await session.get(ProductCategory, code) is None:
        session.add(ProductCategory(code=code, name=(name or code).strip()))
        await session.flush()
        await record(session, _ACTOR, "catalogue.category_created", "product_category", code)
    return code


async def mirror_product(session: AsyncSession, p: ProductPublished, now: datetime) -> Product:
    sku = p.code.strip().upper()
    product = (
        await session.execute(select(Product).where(func.upper(Product.sku) == sku).with_for_update())
    ).scalar_one_or_none()
    before = None
    if product is None:
        product = Product(
            sku=sku,
            name=p.name,
            category_code=await _category(session, p.category_code, p.category_name),
            is_quotable=False,  # D8: no approved price yet
            spec={},
        )
        session.add(product)
    else:
        before = {
            "name": product.name,
            "category_code": product.category_code,
            "cms_published": product.cms_published,
        }
        product.name = p.name
        if p.category_code:
            product.category_code = await _category(session, p.category_code, p.category_name)
    product.spec = dict(p.spec)
    if p.custom_printing is not None:
        product.custom_printing = p.custom_printing
    product.cms_product_id, product.cms_published, product.cms_synced_at = p.cms_id, p.published, now
    await session.flush()
    await record(
        session,
        _ACTOR,
        "catalogue.product_published" if p.published else "catalogue.product_unpublished",
        "product",
        product.id,
        before=before,
        after={"name": product.name, "category_code": product.category_code, "cms_published": p.published},
    )
    return product
