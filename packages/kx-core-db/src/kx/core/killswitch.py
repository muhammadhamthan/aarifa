"""D35 kill-switch ladder (Fallback plan): "stop marketing sends -> stop outbound calls -> text only ->
hold and hand everything to staff". One level per company in Redis, set by an admin from the CRM; each step
includes the ones before it. Readers ask `engaged(scope)`:

* `marketing`: follow-ups and re-order nudges (the proactive sends) stop from level `marketing`;
* `phone`: every outbound call (alert ladder, click-to-call) from level `calls`;
* `voice`: live AI voice on the website from level `text_only` (chat stays);
* anything else (`whatsapp`, `email`, `all`): from level `hold`; nothing leaves and queued messages are
  cancelled at delivery, so staff take over.

The older per-channel keys (`kx:{company}:kill:{channel}` / `:all`) still work.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

from redis.asyncio import Redis

KillLevel = Literal["off", "marketing", "calls", "text_only", "hold"]
LEVELS: Final[tuple[KillLevel, ...]] = get_args(KillLevel)
_FROM: Final[dict[str, KillLevel]] = {"marketing": "marketing", "phone": "calls", "voice": "text_only"}


def engaged_at(level: KillLevel, scope: str) -> bool:
    return LEVELS.index(level) >= LEVELS.index(_FROM.get(scope, "hold"))


class KillSwitchLadder:
    def __init__(self, redis: Redis, company_code: str) -> None:
        self._redis = redis
        self._prefix = f"kx:{company_code}:kill"

    async def level(self) -> KillLevel:
        raw = await self._redis.get(f"{self._prefix}:level")
        value = raw.decode() if isinstance(raw, bytes) else raw
        return value if value in LEVELS else "off"

    async def set_level(self, level: KillLevel) -> None:
        await self._redis.set(f"{self._prefix}:level", level)

    async def engaged(self, scope: str) -> bool:
        if int(await self._redis.exists(f"{self._prefix}:{scope}", f"{self._prefix}:all")) > 0:
            return True
        return engaged_at(await self.level(), scope)
