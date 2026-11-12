"""Long-lived collaborators every CRM service needs. Build one per process and inject it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kx.core.auth import TokenIssuer
from kx.core.contracts import ClickToCall, MessageSender, StaffMailer
from kx.core.killswitch import KillSwitchLadder
from kx.core.passwords import SeedBox
from kx.core.settings import AuthSettings
from kx.crm.pdf import Letterhead, PdfUploader, QuoteRenderer
from kx.crm.services.settings import SettingsService


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CrmDeps:
    """`clock` is injectable so timers (approval window, staleness, retention) are testable."""

    uploader: PdfUploader
    settings: SettingsService = field(default_factory=SettingsService)
    renderer: QuoteRenderer = field(default_factory=QuoteRenderer)
    letterhead: Letterhead = field(default_factory=Letterhead)
    company_code: str = "pack"
    base_currency: str = "AED"
    clock: Callable[[], datetime] = utcnow
    # ---- staff login (D26)
    tokens: TokenIssuer | None = None
    mailer: StaffMailer | None = None
    seed_box: SeedBox | None = None
    auth: AuthSettings = field(default_factory=AuthSettings)
    # ---- §14 the salesperson writes to the buyer from the CRM inbox (through OutboundService)
    sender: MessageSender | None = None
    caller: ClickToCall | None = None  # §14 click-to-call through the company number
    kill_switch: KillSwitchLadder | None = None  # D35 ladder, set from the admin screen

    def now(self) -> datetime:
        return self.clock()
