"""§8.2 "Confirming the buyer's phone number silently and naturally" (D19).

No codes, no "verify your identity", no test call: the Requirement Summary is the useful thing that proves the
number, and WhatsApp's own signals are read silently.

| Signal                                   | Status       |
| WhatsApp shows Delivered                 | reachable    |
| WhatsApp shows Read (read receipts on)   | likely       |
| The buyer replies from that number        | verified     |
| Delivery fails (not on WhatsApp/invalid)  | not_reached  |

"Read receipts can be switched off, so no Read tick is not a failure": the status only moves forward, and a
failure never downgrades a number that already proved itself. Quotes go only to Reachable or better.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.event_payloads import StaffNotice
from kx.crm.models import Contact, Deal, Task
from kx.crm.services import staff
from kx.crm.services.journal import Actor, Emit, Timeline, record

PhoneStatus = Literal["unknown", "not_reached", "reachable", "likely", "verified"]
Signal = Literal["delivered", "read", "reply", "failed"]
REACHABLE: Final[frozenset[str]] = frozenset({"reachable", "likely", "verified"})
_RANK: Final[dict[str, int]] = {"unknown": 0, "not_reached": 1, "reachable": 2, "likely": 3, "verified": 4}
_TARGET: Final[dict[Signal, PhoneStatus]] = {
    "delivered": "reachable",
    "read": "likely",
    "reply": "verified",
    "failed": "not_reached",
}


def next_status(current: str, signal: Signal) -> PhoneStatus | None:
    """The new status for this signal, or None when it changes nothing (forward only)."""
    target = _TARGET[signal]
    if signal == "failed":
        return "not_reached" if current == "unknown" else None
    return target if _RANK[target] > _RANK.get(current, 0) else None


def reachable(contact: Contact) -> bool:
    return contact.phone_status in REACHABLE


async def apply_signal(
    session: AsyncSession, contact: Contact, signal: Signal, at: datetime, *, profile_name: str | None = None
) -> bool:
    """Move the contact's phone status forward; store the WhatsApp profile name (shown on the deal card)."""
    changed = False
    if profile_name and profile_name != contact.whatsapp_profile_name:
        contact.whatsapp_profile_name, changed = profile_name[:200], True
    new = next_status(contact.phone_status, signal)
    if new is not None:
        before = contact.phone_status
        contact.phone_status, contact.phone_status_at = new, at
        if new == "verified" and contact.phone_verified_at is None:
            contact.phone_verified_at = at
        await session.flush()
        await record(
            session,
            Actor.system("phone_status"),
            "contact.phone_status",
            "contact",
            contact.id,
            before={"phone_status": before},
            after={"phone_status": new, "signal": signal},
        )
        return True
    if changed:
        await session.flush()
    return changed


async def tell_salesperson(session: AsyncSession, deal: Deal, why: str) -> None:
    """§8.2 step 4 "Otherwise the salesperson is told": a task on the deal and a notice (alert hours)."""
    owner = await staff.approver_for(session, deal)
    session.add(
        Task(deal_id=deal.id, kind="phone_unreached", assigned_user_id=owner.id, payload={"why": why})
    )
    await session.flush()
    await record(
        session,
        Actor.system("phone_status"),
        "deal.phone_unreached",
        "deal",
        deal.id,
        timeline=Timeline("phone", f"Buyer not reached: {why}", deal.id, deal.primary_contact_id),
        emit=Emit(
            ev.STAFF_NOTICE,
            "deal",
            deal.id,
            StaffNotice(
                deal_ref=deal.id,
                notify=(staff.staff_contact(owner),),
                text=f"Deal {deal.number}: buyer not reached ({why}). Please check the number or call.",
                key=f"unreached:{deal.id}:{why[:40]}",
            ),
        ),
    )
