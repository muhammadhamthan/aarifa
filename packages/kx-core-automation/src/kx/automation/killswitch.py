"""Outbound kill switch: `kx:{company}:kill:{channel}` or `kx:{company}:kill:all` present = nothing leaves.

The CRM (MD/admin setting `ai_outbound_enabled`) writes the keys; automation only reads them, at
send time and again at delivery time so messages queued before the switch was thrown stay put.
"""

from __future__ import annotations

from redis.asyncio import Redis


class KillSwitch:
    def __init__(self, redis: Redis, company_code: str) -> None:
        self._redis = redis
        self._prefix = f"kx:{company_code}:kill"

    def keys(self, channel: str) -> tuple[str, str]:
        return f"{self._prefix}:{channel}", f"{self._prefix}:all"

    async def engaged(self, channel: str) -> bool:
        return int(await self._redis.exists(*self.keys(channel))) > 0
