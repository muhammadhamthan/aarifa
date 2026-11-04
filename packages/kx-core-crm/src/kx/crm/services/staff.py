"""Who to alert: the deal's manager and the MD, as StaffContact payloads for automation."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.errors import RuleViolation
from kx.core.event_payloads import StaffContact
from kx.crm.models import Deal, User

APPROVER_ROLES = ("manager", "md")


def staff_contact(user: User) -> StaffContact:
    whatsapp = user.phone_e164 if "whatsapp" in user.alert_channels else None
    return StaffContact(
        user_ref=user.id, name=user.full_name, phone=user.phone_e164, email=user.email, whatsapp=whatsapp
    )


async def first_active(session: AsyncSession, role: str) -> User | None:
    stmt = select(User).where(User.role == role, User.is_active).order_by(User.created_at, User.id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def md_user(session: AsyncSession) -> User:
    if (md := await first_active(session, "md")) is None:
        raise RuleViolation("no active MD user is configured")
    return md


async def manager_for(session: AsyncSession, deal: Deal) -> User:
    """The deal owner if they are a manager/MD, else the first active manager (MD as last resort)."""
    if deal.owner_user_id is not None:
        owner = await session.get(User, deal.owner_user_id)
        if owner is not None and owner.is_active and owner.role in APPROVER_ROLES:
            return owner
    return await first_active(session, "manager") or await md_user(session)
