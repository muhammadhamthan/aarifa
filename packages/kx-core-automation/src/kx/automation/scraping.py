"""Daily raw-material price checks (T12). Results are stored as observations and announced as
PRICE_PROPOSED; the CRM turns them into proposals a manager must approve. Nothing here ever changes
a price the AI can quote.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from zoneinfo import ZoneInfo

import httpx
from selectolax.lexbor import LexborHTMLParser
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kx.automation.channels import ChannelError, HttpRetry
from kx.automation.models import PriceObservation, ScrapeRun, ScrapeStatus
from kx.automation.outbox import emit
from kx.automation.ports import Clock, utc_now
from kx.automation.settings import PriceSourceConfig
from kx.core.db import Databases
from kx.core.event_payloads import PriceProposed
from kx.core.logging import get_logger
from kx.core.money import q
from kx.core.settings import LogicalDb

log = get_logger(__name__)

_NUMBER: Final = re.compile(r"\d{1,3}(?:[,\s\u00a0\u202f]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_SEPARATORS: Final = str.maketrans("", "", ", \u00a0\u202f")
_BLOCKED: Final = frozenset({401, 403, 429, 451})
USER_AGENT: Final = "KairoxPriceCheck/1.0 (+https://kairox.ai/bot)"


class ScrapeError(Exception):
    def __init__(self, detail: str, status: ScrapeStatus = "failed") -> None:
        super().__init__(detail)
        self.status = status


def parse_price(text: str) -> Decimal:
    """First number in `text` as Decimal ('USD 2,345.50 /t' -> 2345.50). Never float."""
    match = _NUMBER.search(text)
    if match is None:
        raise ScrapeError(f"no number in {text[:40]!r}")
    try:
        value = Decimal(match.group(0).translate(_SEPARATORS))
    except InvalidOperation as exc:  # pragma: no cover - the regex only matches digits
        raise ScrapeError("unparseable number") from exc
    if value <= 0:
        raise ScrapeError("price must be positive")
    return value


def extract(html: str, source: PriceSourceConfig, today: date) -> tuple[Decimal, date, str]:
    tree = LexborHTMLParser(html)
    node = tree.css_first(source.price_selector)
    if node is None:
        raise ScrapeError(f"selector {source.price_selector!r} not found")
    raw = node.text(strip=True)
    observed_for = today
    if source.date_selector and (date_node := tree.css_first(source.date_selector)) is not None:
        try:
            observed_for = date.fromisoformat(date_node.text(strip=True)[:10])
        except ValueError as exc:
            raise ScrapeError("source date is not ISO") from exc
    return parse_price(raw), observed_for, raw


class PriceScraper:
    def __init__(
        self,
        *,
        databases: Databases,
        http: httpx.AsyncClient,
        sources: tuple[PriceSourceConfig, ...],
        timezone: str = "Asia/Dubai",
        retry: HttpRetry | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._db = databases
        self._http = http
        self._sources = sources
        self._tz = ZoneInfo(timezone)
        self._retry = retry or HttpRetry()
        self._now = clock

    async def run_all(self) -> dict[str, ScrapeStatus]:
        """Each source independently: one broken site never stops the others."""
        return {source.code: await self.run(source) for source in self._sources}

    async def run(self, source: PriceSourceConfig) -> ScrapeStatus:
        run_id = await self._start(source.code)
        if not source.terms_ok:  # only sites a human checked allow automated reading
            return await self._finish(run_id, "blocked", 0, "terms not confirmed")
        try:
            html = await self._fetch(str(source.url))
            value, observed_for, raw = extract(html, source, self._now().astimezone(self._tz).date())
        except ScrapeError as exc:
            log.warning("scrape.failed", source=source.code, error=str(exc))
            return await self._finish(run_id, exc.status, 0, str(exc))
        items = await self._store(run_id, source, value, observed_for, raw)
        return await self._finish(run_id, "ok", items, None)

    async def _fetch(self, url: str) -> str:
        try:
            response = await self._retry.request(
                self._http, "scrape", "GET", url, headers={"User-Agent": USER_AGENT}, follow_redirects=True
            )
        except ChannelError as exc:
            raise ScrapeError(exc.detail, "blocked" if exc.status in _BLOCKED else "failed") from exc
        return response.text

    async def _start(self, code: str) -> uuid.UUID:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            run = ScrapeRun(source_code=code)
            s.add(run)
            await s.flush()
            return run.id

    async def _store(
        self, run_id: uuid.UUID, source: PriceSourceConfig, value: Decimal, observed_for: date, raw: str
    ) -> int:
        """One observation per source/material/day; a re-run the same day stores and announces nothing."""
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            observation_id = (
                await s.execute(
                    pg_insert(PriceObservation)
                    .values(
                        run_id=run_id,
                        source_code=source.code,
                        material_code=source.material_code,
                        value=q(value),
                        currency=source.currency.upper(),
                        unit=source.unit,
                        observed_for=observed_for,
                        source_url=str(source.url),
                        raw={"text": raw},
                    )
                    .on_conflict_do_nothing(index_elements=["source_code", "material_code", "observed_for"])
                    .returning(PriceObservation.id)
                )
            ).scalar_one_or_none()
            if observation_id is None:
                return 0
            await emit(
                s,
                PriceProposed(
                    observation_ref=observation_id,
                    price_source_ref=source.source_ref,
                    material_code=source.material_code,
                    price=q(value),
                    currency=source.currency.upper(),
                    unit=source.unit,
                    observed_at=datetime.combine(observed_for, datetime.min.time(), self._tz),
                    source_url=str(source.url),
                ),
            )
            return 1

    async def _finish(
        self, run_id: uuid.UUID, status: ScrapeStatus, items: int, error: str | None
    ) -> ScrapeStatus:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            await s.execute(
                update(ScrapeRun)
                .where(ScrapeRun.id == run_id)
                .values(status=status, items=items, error=error, finished_at=self._now())
            )
        return status
