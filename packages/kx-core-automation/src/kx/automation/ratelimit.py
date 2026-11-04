"""Atomic Redis token bucket. Per recipient it stops a looping agent from flooding a buyer; per channel
it keeps us under provider throughput limits. The script uses Redis server TIME so API and worker
tasks with skewed clocks share one consistent bucket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from redis.asyncio import Redis

_BUCKET_LUA: Final = """
local capacity = tonumber(ARGV[1])
local per_ms   = tonumber(ARGV[2])
local cost     = tonumber(ARGV[3])
local t        = redis.call('TIME')
local now      = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local state    = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens   = tonumber(state[1]) or capacity
local ts       = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - ts) * per_ms)
local allowed = 0
local wait_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  wait_ms = math.ceil((cost - tokens) / per_ms)
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / per_ms) + 1000)
return {allowed, wait_ms}
"""


@dataclass(frozen=True, slots=True)
class Bucket:
    capacity: int
    refill_per_minute: float

    def __post_init__(self) -> None:
        if self.capacity < 1 or self.refill_per_minute <= 0:
            raise ValueError("bucket needs capacity >= 1 and a positive refill rate")

    @property
    def per_ms(self) -> float:
        return self.refill_per_minute / 60_000


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    retry_after_seconds: float


class TokenBucketLimiter:
    def __init__(self, redis: Redis, company_code: str) -> None:
        self._script = redis.register_script(_BUCKET_LUA)
        self._prefix = f"kx:{company_code}:rl"

    async def take(self, scope: str, key: str, bucket: Bucket, cost: int = 1) -> Decision:
        """Take `cost` tokens from bucket `scope:key`; never blocks, tells the caller how long to wait."""
        allowed, wait_ms = await self._script(
            keys=[f"{self._prefix}:{scope}:{key}"], args=[bucket.capacity, repr(bucket.per_ms), cost]
        )
        return Decision(bool(int(allowed)), int(wait_ms) / 1000)
