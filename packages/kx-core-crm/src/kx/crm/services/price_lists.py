"""Price-list maintenance (docs/BUSINESS_FLOW_V1.md §3, §10, §20; D6).

* Only the manager and admin edit the price list; every change is logged with who, when, old and new value.
* A quote keeps the prices from the moment it was approved (its own snapshot); a change affects new quotes.
* Excel import: one row per product per tier. Every row is validated before anything is saved (minimum <=
  list, tiers do not overlap, product codes exist); any error -> nothing is saved and the row errors come
  back. A valid file becomes the products' next price-list version (one `valid_from` for the whole file).
"""

from __future__ import annotations

import io
import itertools
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]  # types-openpyxl not in uv.lock
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.errors import Conflict, NotFound, RuleViolation
from kx.crm.models import Product, ProductPriceTier, QuoteLineTier
from kx.crm.services.journal import Actor, record
from kx.crm.services.pricing import current_tiers

COLUMNS: Final = ("sku", "min_qty", "max_qty", "list_price", "min_price", "currency")


@dataclass(frozen=True, slots=True)
class TierRow:
    row: int
    sku: str
    min_qty: Decimal
    max_qty: Decimal | None
    list_price: Decimal
    min_price: Decimal | None
    currency: str = "AED"


@dataclass(frozen=True, slots=True)
class RowError:
    row: int
    field: str
    error: str


def _dec(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, float):  # spreadsheets store numbers as floats: go through their text form
        value = repr(value)
    try:
        return Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def parse_rows(rows: Iterable[Sequence[Any]], known_skus: set[str]) -> tuple[list[TierRow], list[RowError]]:
    """Validate every row (pure). Row numbers are the spreadsheet's (header = row 1)."""
    parsed: list[TierRow] = []
    errors: list[RowError] = []
    for n, raw in enumerate(rows, start=2):
        cells = list(raw) + [None] * (len(COLUMNS) - len(raw))
        if all(c in (None, "") for c in cells[: len(COLUMNS)]):
            continue
        sku = str(cells[0] or "").strip()
        values: dict[str, Decimal | None] = {}
        for idx, name in enumerate(COLUMNS[1:5], start=1):
            try:
                values[name] = _dec(cells[idx])
            except ValueError as exc:
                errors.append(RowError(n, name, str(exc)))
        if sku not in known_skus:
            errors.append(RowError(n, "sku", f"unknown product code {sku!r}"))
        lo, hi, lp, mp = (values.get(k) for k in ("min_qty", "max_qty", "list_price", "min_price"))
        if lo is None or lo <= 0:
            errors.append(RowError(n, "min_qty", "min_qty must be a positive number"))
        if lp is None or lp <= 0:
            errors.append(RowError(n, "list_price", "list_price must be a positive number"))
        if hi is not None and lo is not None and hi < lo:
            errors.append(RowError(n, "max_qty", "max_qty is below min_qty"))
        if mp is not None and lp is not None and mp > lp:
            errors.append(RowError(n, "min_price", "the minimum is above the list price"))
        if mp is not None and mp <= 0:
            errors.append(RowError(n, "min_price", "min_price must be positive"))
        currency = str(cells[5] or "AED").strip().upper()
        if currency != "AED":
            errors.append(RowError(n, "currency", "prices are entered in AED (§10)"))
        if lo is not None and lp is not None and lo > 0 and lp > 0:
            parsed.append(TierRow(n, sku, lo, hi, lp, mp, currency))
    errors += overlap_errors(parsed)
    return parsed, errors


def overlap_errors(rows: Sequence[TierRow]) -> list[RowError]:
    """§10: a product's tiers never overlap (bounds inclusive; no max = no upper limit)."""
    out: list[RowError] = []
    by_sku: dict[str, list[TierRow]] = defaultdict(list)
    for r in rows:
        by_sku[r.sku].append(r)
    for tiers in by_sku.values():
        tiers.sort(key=lambda t: t.min_qty)
        for a, b in itertools.pairwise(tiers):
            if a.max_qty is None or b.min_qty <= a.max_qty:
                out.append(RowError(b.row, "min_qty", f"overlaps the tier on row {a.row}"))
    return out


# ---------------------------------------------------------------- database
def _snapshot(t: ProductPriceTier) -> dict[str, Any]:
    return {
        "product_id": t.product_id,
        "min_qty": t.min_qty,
        "max_qty": t.max_qty,
        "list_price": t.list_price,
        "min_price": t.min_price,
        "valid_from": t.valid_from,
    }


async def tiers_for(session: AsyncSession, product_id: uuid.UUID) -> list[ProductPriceTier]:
    stmt = (
        select(ProductPriceTier)
        .where(ProductPriceTier.product_id == product_id)
        .order_by(ProductPriceTier.valid_from.desc(), ProductPriceTier.min_qty)
    )
    return list((await session.execute(stmt)).scalars())


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:  # the exclusion / CHECK constraints are the last word
        raise RuleViolation(
            "tiers of a product may not overlap and the minimum may not exceed the list price"
        ) from exc


async def add_tier(
    session: AsyncSession,
    product_id: uuid.UUID,
    *,
    min_qty: Decimal,
    max_qty: Decimal | None,
    list_price: Decimal,
    min_price: Decimal | None,
    valid_from: datetime,
    actor: Actor,
) -> ProductPriceTier:
    if await session.get(Product, product_id) is None:
        raise NotFound(f"product {product_id} not found")
    if min_price is not None and min_price > list_price:
        raise RuleViolation("the minimum may not exceed the list price")
    tier = ProductPriceTier(
        product_id=product_id,
        min_qty=min_qty,
        max_qty=max_qty,
        list_price=list_price,
        min_price=min_price,
        valid_from=valid_from,
        created_by=actor.user_id,
    )
    session.add(tier)
    await _flush(session)
    await record(session, actor, "price_tier.created", "price_tier", tier.id, after=_snapshot(tier))
    return tier


async def update_tier(
    session: AsyncSession, tier_id: uuid.UUID, changes: dict[str, Any], actor: Actor
) -> ProductPriceTier:
    """Edit in place with the old and new value in the audit log (§3). Open quotes keep their snapshot."""
    tier = await session.get(ProductPriceTier, tier_id, with_for_update=True)
    if tier is None:
        raise NotFound(f"price tier {tier_id} not found")
    before = _snapshot(tier)
    for key in ("min_qty", "max_qty", "list_price", "min_price"):
        if key in changes:
            setattr(tier, key, changes[key])
    if tier.min_price is not None and tier.min_price > tier.list_price:
        raise RuleViolation("the minimum may not exceed the list price")
    tier.updated_by = actor.user_id
    await _flush(session)
    await record(
        session, actor, "price_tier.updated", "price_tier", tier.id, before=before, after=_snapshot(tier)
    )
    return tier


async def delete_tier(session: AsyncSession, tier_id: uuid.UUID, actor: Actor) -> None:
    tier = await session.get(ProductPriceTier, tier_id, with_for_update=True)
    if tier is None:
        raise NotFound(f"price tier {tier_id} not found")
    if await session.scalar(select(exists().where(QuoteLineTier.price_tier_id == tier.id))):
        raise Conflict("this tier priced quotes already: add a new price-list version instead")
    before = _snapshot(tier)
    await session.delete(tier)
    await session.flush()
    await record(session, actor, "price_tier.deleted", "price_tier", tier_id, before=before)


@dataclass(frozen=True, slots=True)
class ImportResult:
    saved: int
    errors: tuple[RowError, ...]


async def import_xlsx(
    session: AsyncSession, data: bytes, *, valid_from: datetime, actor: Actor
) -> ImportResult:
    """§10 Excel import: validate every row, then save all of them as one new price-list version."""
    try:
        sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
    except Exception as exc:
        raise RuleViolation("the file is not a readable .xlsx workbook") from exc
    assert sheet is not None
    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    products = {p.sku: p.id for p in (await session.execute(select(Product))).scalars()}
    parsed, errors = parse_rows(rows, set(products))
    if errors:
        return ImportResult(0, tuple(errors))
    for r in parsed:
        session.add(
            ProductPriceTier(
                product_id=products[r.sku],
                min_qty=r.min_qty,
                max_qty=r.max_qty,
                list_price=r.list_price,
                min_price=r.min_price,
                currency=r.currency,
                valid_from=valid_from,
                created_by=actor.user_id,
            )
        )
    await _flush(session)
    await record(
        session,
        actor,
        "price_list.imported",
        "price_list",
        valid_from.isoformat(),
        after={"rows": len(parsed), "products": sorted({r.sku for r in parsed}), "valid_from": valid_from},
    )
    return ImportResult(len(parsed), ())


def _qty(q: Decimal) -> int | str:
    return int(q) if q == q.to_integral_value() else str(q)


async def export_xlsx(session: AsyncSession, now: datetime) -> bytes:
    """The current price list (latest version per product not after `now`), same columns as the import."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "price list"
    ws.append(list(COLUMNS))
    for product in (await session.execute(select(Product).order_by(Product.sku))).scalars():
        for t in await current_tiers(session, product.id, now):
            ws.append(  # quantities as whole numbers, prices as exact text: never a float (money rule)
                [
                    product.sku,
                    _qty(t.min_qty),
                    None if t.max_qty is None else _qty(t.max_qty),
                    str(t.list_price),
                    None if t.min_price is None else str(t.min_price),
                    "AED",
                ]
            )
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
