"""§7 / D22 CMS -> CRM product mirror by product code (11 Nov plan WP18): descriptive fields only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import Seed

from kx.core.event_payloads import ProductPublished
from kx.core.settings import LogicalDb
from kx.crm.models import Product, ProductCategory
from kx.crm.services import catalogue

pytestmark = pytest.mark.db


async def mirror(databases: Any, p: ProductPublished) -> Product:
    async with databases.transaction(LogicalDb.CRM) as s:
        product = await catalogue.mirror_product(s, p, datetime.now(UTC))
        await s.refresh(product)
        return product


async def test_publish_updates_description_but_never_prices_moq_or_quotability(
    databases: Any, seed: Seed
) -> None:
    code = f"CMS-{uuid.uuid4().hex[:6]}".upper()
    first = await mirror(databases, ProductPublished(code=code, cms_id="cms-42", name="Tray 1"))
    async with databases.transaction(LogicalDb.CRM) as s:  # the manager prices it and sets CRM-owned fields
        row = await s.get(Product, first.id)
        assert row is not None
        row.moq, row.is_quotable, row.default_lead_days_max = 20_000, True, 14
    p = ProductPublished(
        code=code.lower(),  # the code matches whatever its case
        cms_id="cms-42",
        name="Tray 1 (smooth wall)",
        spec={"capacity_ml": 750},
        custom_printing=True,
    )
    product = await mirror(databases, p)
    assert product.id == first.id and product.name == "Tray 1 (smooth wall)"
    assert (
        product.spec == {"capacity_ml": 750} and product.cms_published and product.cms_product_id == "cms-42"
    )
    assert (product.moq, product.is_quotable, product.default_lead_days_max) == (20_000, True, 14)

    product = await mirror(databases, p.model_copy(update={"published": False}))
    assert not product.cms_published and product.is_quotable  # kept: open quotes refer to it


async def test_a_new_code_is_filed_but_not_quotable_until_the_manager_prices_it(databases: Any) -> None:
    code = f"NEW-{uuid.uuid4().hex[:6]}"
    category = f"CAT_{uuid.uuid4().hex[:6]}".upper()
    product = await mirror(
        databases,
        ProductPublished(
            code=code, cms_id="cms-7", name="Round tray", category_code=category, category_name="Trays"
        ),
    )
    assert product.sku == code.upper() and product.is_quotable is False and product.cms_published
    async with databases.transaction(LogicalDb.CRM) as s:
        cat = await s.get(ProductCategory, category)
    assert cat is not None and cat.name == "Trays"
