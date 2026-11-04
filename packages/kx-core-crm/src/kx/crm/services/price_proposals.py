"""Raw-material price proposals (T12): the daily scrape proposes, a manager (MFA) approves.

An approved rate supersedes the previous one in the same transaction, so there is always at most one
approved rate per material, and quotes only ever price from approved rates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.errors import Conflict, NotFound
from kx.core.event_payloads import PriceProposed, PriceStale
from kx.core.identity import new_id
from kx.crm.models import Material, MaterialRate
from kx.crm.rules import Staleness, days_since, staleness
from kx.crm.services.journal import Actor, Emit, record
from kx.crm.services.pricing import current_rate
from kx.crm.services.settings import BusinessSettings

CHANGE_PLACES = Decimal("0.001")


def change_pct(new: Decimal, current: Decimal | None) -> Decimal | None:
    return None if not current else ((new - current) / current * 100).quantize(CHANGE_PLACES)


async def propose_rate(session: AsyncSession, p: PriceProposed, actor: Actor) -> MaterialRate | None:
    """Store a scraped price as 'proposed'. Older pending proposals for the material are superseded so
    the manager reviews only the newest observation. Unknown materials are ignored (returns None)."""
    if await session.get(Material, p.material_code) is None:
        return None
    current = await current_rate(session, p.material_code)
    await session.execute(
        update(MaterialRate)
        .where(MaterialRate.material_code == p.material_code, MaterialRate.status == "proposed")
        .values(status="superseded")
    )
    rate = MaterialRate(
        id=new_id(),
        material_code=p.material_code,
        rate_per_unit=p.price,
        currency=p.currency,
        source="scrape",
        source_ref=str(p.price_source_ref),
        source_url=p.source_url,
        observation_ref=p.observation_ref,
        status="proposed",
        change_pct=change_pct(p.price, current.rate_per_unit if current else None),
    )
    session.add(rate)
    await session.flush()
    await record(
        session,
        actor,
        "price.proposed",
        "material_rate",
        rate.id,
        after={
            "material": p.material_code,
            "rate": p.price,
            "currency": p.currency,
            "change_pct": rate.change_pct,
        },
    )
    return rate


async def _pending(session: AsyncSession, rate_id: uuid.UUID) -> MaterialRate:
    rate = await session.get(MaterialRate, rate_id, with_for_update=True)
    if rate is None:
        raise NotFound(f"price proposal {rate_id} not found")
    if rate.status != "proposed":
        raise Conflict(f"price proposal is already {rate.status}")
    return rate


async def approve_rate(
    session: AsyncSession,
    rate_id: uuid.UUID,
    actor: Actor,
    now: datetime,
    *,
    rate_override: Decimal | None = None,
    note: str | None = None,
) -> MaterialRate:
    """Manager approves (optionally editing the price). The previous approved rate is superseded."""
    rate = await _pending(session, rate_id)
    previous = await current_rate(session, rate.material_code)
    if previous is not None:
        previous.status = "superseded"
        await session.flush()  # keep "one approved per material" true at every statement
    if rate_override is not None and rate_override != rate.rate_per_unit:
        rate.note = f"edited from {rate.rate_per_unit}" + (f"; {note}" if note else "")
        rate.rate_per_unit = rate_override
        rate.change_pct = change_pct(rate_override, previous.rate_per_unit if previous else None)
    elif note:
        rate.note = note
    rate.status, rate.decided_by, rate.decided_at, rate.effective_at = "approved", actor.user_id, now, now
    await session.flush()
    await record(
        session,
        actor,
        "price.approved",
        "material_rate",
        rate.id,
        before={"rate": previous.rate_per_unit if previous else None},
        after={"rate": rate.rate_per_unit, "material": rate.material_code},
    )
    return rate


async def reject_rate(
    session: AsyncSession, rate_id: uuid.UUID, actor: Actor, now: datetime, note: str | None = None
) -> MaterialRate:
    rate = await _pending(session, rate_id)
    rate.status, rate.decided_by, rate.decided_at, rate.note = "rejected", actor.user_id, now, note
    await session.flush()
    await record(session, actor, "price.rejected", "material_rate", rate.id, after={"note": note})
    return rate


@dataclass(frozen=True, slots=True)
class MaterialFreshness:
    material_code: str
    days_since_approval: int | None
    level: Staleness


async def material_freshness(
    session: AsyncSession, s: BusinessSettings, now: datetime
) -> list[MaterialFreshness]:
    """Freshness of every material's approved rate (dashboard + daily staleness job)."""
    codes = (await session.execute(select(Material.code).order_by(Material.code))).scalars().all()
    out: list[MaterialFreshness] = []
    for code in codes:
        rate = await current_rate(session, code)
        at = rate.decided_at if rate else None
        out.append(
            MaterialFreshness(
                code,
                days_since(at, now) if at else None,
                staleness(at, now, s.stale_warn_days, s.stale_block_days),
            )
        )
    return out


async def emit_stale(session: AsyncSession, s: BusinessSettings, now: datetime, actor: Actor) -> int:
    """PRICE_STALE for each material at warn/block level (automation alerts the manager)."""
    count = 0
    for f in await material_freshness(session, s, now):
        if f.level == "ok":
            continue
        days = f.days_since_approval if f.days_since_approval is not None else s.stale_block_days
        await record(
            session,
            actor,
            "price.stale",
            "material",
            f.material_code,
            after={"level": f.level, "days": days},
            emit=Emit(
                ev.PRICE_STALE,
                "material",
                f.material_code,
                PriceStale(material_code=f.material_code, days_since_approval=days, level=f.level),
            ),
        )
        count += 1
    return count


__all__ = [
    "MaterialFreshness",
    "approve_rate",
    "change_pct",
    "emit_stale",
    "material_freshness",
    "propose_rate",
    "reject_rate",
]
