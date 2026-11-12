"""WP6: the regional WhatsApp number for the buyer's country (the SRS §11.4, §19; D11).

Done when: a regional routing test passes.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from kx.automation.repo import channel_account, upsert_thread
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db


async def _accounts(db: Any) -> dict[str, uuid.UUID]:
    async with db.transaction(LogicalDb.AUTOMATION) as s:
        await s.execute(
            text(
                "INSERT INTO channel_accounts (channel, provider, external_account_id, display_name, address, purpose, "
                "secret_ref, region, countries, is_default) VALUES "
                "('whatsapp','meta_cloud','GCC1','Arifa Pack GCC','+971500000010','customer','arn:g','GCC',"
                "'{AE,SA,OM,KW,QA,BH}', true),"
                "('whatsapp','meta_cloud','SAS1','Arifa Pack South Asia','+94770000010','customer','arn:s',"
                "'South Asia','{IN,LK}', false)"
            )
        )
        rows = (await s.execute(text("SELECT external_account_id, id FROM channel_accounts"))).all()
    return {k: v for k, v in rows}


async def _pick(db: Any, channel: str, **kw: Any) -> uuid.UUID:
    async with db.transaction(LogicalDb.AUTOMATION) as s:
        return await channel_account(s, channel, "customer", **kw)


async def test_whatsapp_number_by_buyer_country_with_default_fallback(auto_db: Any) -> None:
    ids = await _accounts(auto_db)
    assert await _pick(auto_db, "whatsapp", country="SA") == ids["GCC1"]  # Riyadh -> GCC number
    assert await _pick(auto_db, "whatsapp", country="lk") == ids["SAS1"]  # Colombo -> South Asia number
    assert await _pick(auto_db, "whatsapp", country="FR") == ids["GCC1"]  # no region: the default number
    assert await _pick(auto_db, "whatsapp") == ids["GCC1"]


async def test_the_number_the_buyer_writes_to_wins(auto_db: Any) -> None:
    """§19: replies stay on the number the buyer uses (its conversation and 24-hour window)."""
    ids = await _accounts(auto_db)
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        await upsert_thread(s, ids["SAS1"], "+966501234567")
    assert await _pick(auto_db, "whatsapp", address="+966501234567", country="SA") == ids["SAS1"]


async def test_email_always_from_the_company_address_and_staff_alerts_from_the_alerts_number(
    auto_db: Any,
) -> None:
    ids = await _accounts(auto_db)
    assert await _pick(auto_db, "email", country="IN") == ids["sales@arifapack.com"]
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        assert await channel_account(s, "whatsapp", "internal_alerts", country="SA") == ids["PNID2"]
