"""D34 daily spend caps per company: AI (USD), voice minutes, WhatsApp templates.

"Alert at 80 %, degrade at 100 %" (Recommendation, Fallback plan): the meter counts in Redis per company per
local day, sends one alert at each threshold (the CloudWatch `SpendCap` metric, namespace `Kairox`, which
pages, plus the optional hook), and answers `left()` so callers degrade: AI -> hand-over, voice -> text
chat, templates -> held for staff. Amounts are stored as integers (micro-dollars, seconds, messages),
never floats.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final, Literal
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from kx.core.metrics import emit_metric

SpendKind = Literal["ai_usd", "voice_minutes", "wa_templates"]
SCALE: Final[dict[SpendKind, int]] = {"ai_usd": 1_000_000, "voice_minutes": 60, "wa_templates": 1}
ALERT_AT: Final = (Decimal("0.8"), Decimal(1))
TTL_SECONDS: Final = 3 * 86_400
Alert = Callable[[SpendKind, Decimal, Decimal], Awaitable[None]]  # kind, used, cap


@dataclass(frozen=True, slots=True)
class SpendCaps:
    ai_usd: Decimal
    voice_minutes: Decimal
    wa_templates: Decimal

    def of(self, kind: SpendKind) -> Decimal:
        cap: Decimal = getattr(self, kind)
        return cap


class SpendMeter:
    def __init__(
        self,
        redis: Redis,
        company_code: str,
        caps: SpendCaps,
        *,
        timezone: str = "Asia/Dubai",
        alert: Alert | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._redis = redis
        self._company = company_code
        self._caps = caps
        self._tz = ZoneInfo(timezone)
        self._alert = alert
        self._now = clock

    def _key(self, kind: SpendKind, suffix: str = "") -> str:
        day = self._now().astimezone(self._tz).strftime("%Y%m%d")
        return f"kx:{self._company}:spend:{kind}:{day}{suffix}"

    async def used(self, kind: SpendKind) -> Decimal:
        raw = await self._redis.get(self._key(kind))
        return Decimal(int(raw or 0)) / SCALE[kind]

    async def left(self, kind: SpendKind) -> bool:
        """False once today's cap is reached: the caller degrades instead of spending more."""
        return await self.used(kind) < self._caps.of(kind)

    async def add(self, kind: SpendKind, amount: Decimal) -> Decimal:
        """Record spend; returns today's total. Crossing 80 % or 100 % alerts once per day."""
        units = int((amount * SCALE[kind]).to_integral_value())
        if units <= 0:
            return await self.used(kind)
        key = self._key(kind)
        total_units = int(await self._redis.incrby(key, units))
        await self._redis.expire(key, TTL_SECONDS)
        total = Decimal(total_units) / SCALE[kind]
        cap = self._caps.of(kind)
        for threshold in ALERT_AT:
            if (
                cap > 0
                and total >= cap * threshold
                and total - Decimal(units) / SCALE[kind] < cap * threshold
            ):
                await self._crossed(kind, threshold, total, cap)
        return total

    async def _crossed(self, kind: SpendKind, threshold: Decimal, total: Decimal, cap: Decimal) -> None:
        if not await self._redis.set(self._key(kind, f":alert{threshold}"), "1", nx=True, ex=TTL_SECONDS):
            return
        emit_metric(
            "SpendCap",
            float(threshold * 100),
            company=self._company,
            unit="Percent",
            kind=kind,
            used=str(total),
            cap=str(cap),
        )
        if self._alert is not None:
            await self._alert(kind, total, cap)
