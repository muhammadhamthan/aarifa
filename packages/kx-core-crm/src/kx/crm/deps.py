"""Long-lived collaborators every CRM service needs. Build one per process and inject it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

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

    def now(self) -> datetime:
        return self.clock()
