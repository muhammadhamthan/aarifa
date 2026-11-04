"""What every router factory needs: the CRM session dependency and the service deps."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Role
from kx.core.db import Databases
from kx.core.settings import LogicalDb
from kx.crm.deps import CrmDeps

STAFF = (
    Role.SALES,
    Role.MANAGER,
    Role.ADMIN,
)  # Principal.has() lets the MD through wherever MANAGER is listed
MANAGERS = (Role.MANAGER,)


@dataclass(frozen=True, slots=True)
class ApiContext:
    databases: Databases
    deps: CrmDeps
    session: Callable[[], AsyncIterator[AsyncSession]] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "session", self.databases.dependency(LogicalDb.CRM))
