"""kx.crm test fixtures.

The root tests/conftest.py (pg_databases / databases / DB-skip hook) is not an ancestor of this
directory, so pytest would not discover it here. It is loaded by path and its fixtures re-exported,
which works both for `python -m pytest packages/kx-core-crm/tests` and for a whole-repo run.
"""

from __future__ import annotations

import importlib.util
import pathlib
import random
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from types import ModuleType

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]


def _root_conftest() -> ModuleType:
    name = "_kx_root_conftest"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / "tests" / "conftest.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


_root = _root_conftest()
pg_databases = _root.pg_databases
databases = _root.databases
pytest_collection_modifyitems = _root.pytest_collection_modifyitems


class FakeUploader:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> None:
        self.objects[key] = data


def new_phone() -> str:
    return f"+97150{random.randrange(1_000_000, 9_999_999)}"


@dataclass(frozen=True)
class Seed:
    md: uuid.UUID
    manager: uuid.UUID
    product_sku: str
    material: str


MD_ID = uuid.UUID("00000000-0000-7000-8000-0000000000a1")
MGR_ID = uuid.UUID("00000000-0000-7000-8000-0000000000a2")
PRODUCT_ID = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
# §23 salespersons: Sara (Arabic + English, GCC) and Ravi
SALES_ID = uuid.UUID("00000000-0000-7000-8000-0000000000a3")
SALES2_ID = uuid.UUID("00000000-0000-7000-8000-0000000000a4")
SKU, MATERIAL, CATEGORY = "ALU-750", "AL_COIL_8011", "ALU_CONTAINER"


async def ensure_seed(dbs: object, now: datetime | None = None) -> Seed:
    """Idempotent catalogue + staff seed (spec numbers from tests/spec/test_spec_pricing.py)."""
    from sqlalchemy import select

    from kx.core.settings import LogicalDb
    from kx.crm import models as m

    now = now or datetime.now(UTC)
    async with dbs.transaction(LogicalDb.CRM) as s:  # type: ignore[attr-defined]
        if await s.get(m.User, MD_ID) is None:
            s.add_all(
                [
                    m.User(
                        id=MD_ID,
                        email="md@test.ae",
                        full_name="The MD",
                        role="md",
                        phone_e164="+971501110001",
                    ),
                    m.User(
                        id=MGR_ID,
                        email="mgr@test.ae",
                        full_name="The Manager",
                        role="manager",
                        phone_e164="+971501110002",
                    ),
                    m.User(
                        id=SALES_ID,
                        email="sara@test.ae",
                        full_name="Sara",
                        role="sales",
                        phone_e164="+971501110003",
                        languages=["en", "ar"],
                        countries=["AE", "SA"],
                    ),
                    m.User(
                        id=SALES2_ID,
                        email="ravi@test.ae",
                        full_name="Ravi",
                        role="sales",
                        phone_e164="+971501110004",
                        languages=["en", "hi", "ta"],
                    ),
                    m.ProductCategory(code=CATEGORY, name="Aluminium containers"),
                    m.Material(code=MATERIAL, name="Aluminium coil 8011"),
                ]
            )
            await s.flush()
            s.add(
                m.Product(id=PRODUCT_ID, sku=SKU, name="Aluminium container 750 ml", category_code=CATEGORY)
            )
            await s.flush()
            s.add_all(
                [
                    m.ProductCosting(
                        product_id=PRODUCT_ID,
                        material_code=MATERIAL,
                        kg_per_1000=D("8.5"),
                        yield_pct=D(92),
                        production_cost_per_1000=D(35),
                        packaging_cost_per_1000=D(6),
                        pcs_per_40hc=400_000,
                        pieces_per_day=100_000,
                        packing_days=1,
                        effective_from=now - timedelta(days=30),
                    ),
                    m.MarginRuleRow(
                        category_code=CATEGORY, markup_min_pct=D(40), markup_max_pct=D(50), max_concessions=3
                    ),
                    m.FxRate(currency="USD", rate_to_aed=D("3.6725"), as_of=date(2026, 1, 1), source="test"),
                    m.FreightLane(
                        code="JEA-CMB-SEA",
                        origin_port="Jebel Ali",
                        dest_country="LK",
                        dest_port="Colombo",
                        mode="sea",
                        container_cost=D(2500),
                        surcharge=D(300),
                        currency="USD",
                        transit_days=12,
                        risk_score=2,
                        buffer_pct=D(10),
                        buffer_days=3,
                    ),
                ]
            )
        current = (
            await s.execute(
                select(m.MaterialRate).where(
                    m.MaterialRate.material_code == MATERIAL, m.MaterialRate.status == "approved"
                )
            )
        ).scalar_one_or_none()
        if current is None or current.decided_at is None or now - current.decided_at > timedelta(days=1):
            if current is not None:
                current.status = "superseded"
                await s.flush()
            s.add(
                m.MaterialRate(
                    material_code=MATERIAL,
                    rate_per_unit=D("12.00"),
                    source="manual",
                    status="approved",
                    decided_by=MGR_ID,
                    decided_at=now,
                    effective_at=now,
                )
            )
    return Seed(md=MD_ID, manager=MGR_ID, product_sku=SKU, material=MATERIAL)


@pytest.fixture
async def seed(databases: object) -> Seed:
    return await ensure_seed(databases)


# ---------------------------------------------------------------- v1 price list (the SRS §23)
LIST_SKU, LIST_PRODUCT_ID = "ALC-750-L", uuid.UUID("00000000-0000-7000-8000-0000000000b2")
LIST_CATEGORY = "ALUMINIUM"
TIERS_23 = ((5_000, 9_999, "0.32", "0.29"), (10_000, 49_999, "0.28", "0.255"), (50_000, None, "0.25", "0.23"))
PRICE_LIST_FROM = datetime(2026, 1, 1, tzinfo=UTC)


async def ensure_price_list(dbs: object) -> str:
    """§23 price list: ALC-750-L, three tiers, 10-14 working days, MOQ 5,000; category minimum 8 %."""
    from sqlalchemy import select

    from kx.core.settings import LogicalDb
    from kx.crm import models as m

    await ensure_seed(dbs)
    async with dbs.transaction(LogicalDb.CRM) as s:  # type: ignore[attr-defined]
        if await s.get(m.Product, LIST_PRODUCT_ID) is not None:
            return LIST_SKU
        if (
            await s.execute(select(m.ProductCategory).where(m.ProductCategory.code == LIST_CATEGORY))
        ).first() is None:
            s.add(m.ProductCategory(code=LIST_CATEGORY, name="Aluminium", min_discount_pct=D(8)))
            await s.flush()
        s.add(
            m.Product(
                id=LIST_PRODUCT_ID,
                sku=LIST_SKU,
                name="Aluminium container 750 ml + lid",
                category_code=LIST_CATEGORY,
                moq=5_000,
                default_lead_days_min=10,
                default_lead_days_max=14,
                custom_printing=True,
            )
        )
        await s.flush()
        s.add_all(
            m.ProductPriceTier(
                product_id=LIST_PRODUCT_ID,
                min_qty=D(lo),
                max_qty=None if hi is None else D(hi),
                list_price=D(lp),
                min_price=D(mp),
                valid_from=PRICE_LIST_FROM,
                created_by=MGR_ID,
            )
            for lo, hi, lp, mp in TIERS_23
        )
    return LIST_SKU


@pytest.fixture
async def price_list(databases: object) -> str:
    return await ensure_price_list(databases)


async def assign(dbs: object, deal_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
    """Set the deal owner directly (tests that are not about assignment)."""
    from sqlalchemy import update

    from kx.core.settings import LogicalDb
    from kx.crm import models as m

    async with dbs.transaction(LogicalDb.CRM) as s:  # type: ignore[attr-defined]
        await s.execute(update(m.Deal).where(m.Deal.id == deal_id).values(owner_user_id=user_id))


async def set_setting(dbs: object, key: str, value: object) -> None:
    import orjson
    from sqlalchemy import text

    from kx.core.settings import LogicalDb

    async with dbs.transaction(LogicalDb.CRM) as s:  # type: ignore[attr-defined]
        await s.execute(
            text("UPDATE crm.settings SET value = CAST(:v AS jsonb) WHERE key = :k"),
            {"k": key, "v": orjson.dumps(value).decode()},
        )


@pytest.fixture
async def cost_mode(databases: object) -> AsyncIterator[None]:
    """v2 cost-based pricing (D14 keeps the code): the 4 Nov flow tests run with pricing.mode = 'cost'."""
    await set_setting(databases, "pricing.mode", "cost")
    yield
    await set_setting(databases, "pricing.mode", "price_list")


@pytest.fixture
def uploader() -> FakeUploader:
    return FakeUploader()


@pytest.fixture
def deps(uploader: FakeUploader) -> object:
    from kx.crm.deps import CrmDeps
    from kx.crm.pdf import Letterhead
    from kx.crm.services.settings import SettingsService

    return CrmDeps(
        uploader=uploader,
        settings=SettingsService(ttl_seconds=0),
        letterhead=Letterhead(name="Arifaa Pack (test)", address="Sharjah, UAE"),
    )
