"""§14 click-to-call (11 Nov plan WP17, D21): the salesperson's phone rings first, then the buyer is dialled
from the company number; the buyer's call stop and the calls kill switch are checked before anything rings."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from automation_fakes import Clock, FakeKill
from sqlalchemy import select

from kx.automation.alerts import CallBridge
from kx.automation.models import CallAttempt
from kx.automation.repo import suppress
from kx.automation.settings import AutomationSettings
from kx.core.errors import RuleViolation
from kx.core.identity import recipient_hash
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db
STAFF, BUYER = "+971501110003", "+971509876543"
SETTINGS = AutomationSettings.model_validate(
    {"company_name": "Arifaa Pack", "twilio": {"from_number": "+97165550000"}}
)


class Caller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def place_call(self, to_e164: str, twiml: str, status_callback_url: str) -> str:
        self.calls.append((to_e164, twiml, status_callback_url))
        return f"CA{uuid.uuid4().hex[:8]}"


def bridge(auto_db: Any, caller: Caller, kill: FakeKill | None = None) -> CallBridge:
    return CallBridge(auto_db, SETTINGS, caller, kill or FakeKill(), Clock())


async def test_rings_the_salesperson_then_dials_the_buyer_from_the_company_number(auto_db: Any) -> None:
    caller, deal = Caller(), uuid.uuid4()
    key = f"click-to-call:{deal}:{uuid.uuid4()}"
    first = await bridge(auto_db, caller).bridge(
        deal_ref=deal, contact_ref=None, staff_e164=STAFF, buyer_e164=BUYER, idempotency_key=key
    )
    again = await bridge(auto_db, caller).bridge(
        deal_ref=deal, contact_ref=None, staff_e164=STAFF, buyer_e164=BUYER, idempotency_key=key
    )
    assert first == again and len(caller.calls) == 1  # a double click rings once
    to, twiml, callback = caller.calls[0]
    assert to == STAFF and f"<Number>{BUYER}</Number>" in twiml
    assert 'callerId="+97165550000"' in twiml and f"attempt_id={first}" in callback
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        row = await s.get(CallAttempt, first)
    assert row is not None and row.purpose == "click_to_call" and row.deal_ref == deal
    assert row.provider_call_id is not None


async def test_a_buyer_who_asked_not_to_be_called_is_never_dialled(auto_db: Any) -> None:
    """§8.1 step 2: the one suppression list is checked before every call."""
    caller, buyer = Caller(), "+971509870001"
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        await suppress(s, "phone", recipient_hash("phone", buyer), "opt_out")
    with pytest.raises(RuleViolation) as err:
        await bridge(auto_db, caller).bridge(
            deal_ref=uuid.uuid4(),
            contact_ref=None,
            staff_e164=STAFF,
            buyer_e164=buyer,
            idempotency_key=f"k-{uuid.uuid4()}",
        )
    assert err.value.extra.get("reason") == "opt_out" and caller.calls == []


async def test_the_calls_kill_switch_stops_click_to_call(auto_db: Any) -> None:
    caller, deal = Caller(), uuid.uuid4()
    with pytest.raises(RuleViolation):
        await bridge(auto_db, caller, FakeKill(off={"phone"})).bridge(
            deal_ref=deal,
            contact_ref=None,
            staff_e164=STAFF,
            buyer_e164=BUYER,
            idempotency_key=f"k-{uuid.uuid4()}",
        )
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        rows = (await s.execute(select(CallAttempt).where(CallAttempt.deal_ref == deal))).scalars().all()
    assert caller.calls == [] and rows == []
