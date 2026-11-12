"""Token bucket Lua script and kill switch against a throwaway local redis-server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from kx.automation.killswitch import KillSwitch
from kx.automation.ratelimit import Bucket, TokenBucketLimiter


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


async def test_bucket_allows_capacity_then_refuses_with_wait(redis: Redis) -> None:
    limiter = TokenBucketLimiter(redis, "pack")
    bucket = Bucket(capacity=3, refill_per_minute=60)  # one token per second
    results = [await limiter.take("recipient", "h1", bucket) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert 0 < results[-1].retry_after_seconds <= 1
    assert (await limiter.take("recipient", "h2", bucket)).allowed  # buckets are per key
    await asyncio.sleep(1.1)
    assert (await limiter.take("recipient", "h1", bucket)).allowed


async def test_bucket_is_atomic_under_concurrency(redis: Redis) -> None:
    limiter = TokenBucketLimiter(redis, "pack")
    bucket = Bucket(capacity=5, refill_per_minute=0.001)
    results = await asyncio.gather(*(limiter.take("channel", "whatsapp", bucket) for _ in range(20)))
    assert sum(r.allowed for r in results) == 5


async def test_kill_switch_channel_and_all(redis: Redis) -> None:
    kill = KillSwitch(redis, "pack")
    assert not await kill.engaged("whatsapp")
    await redis.set("kx:pack:kill:whatsapp", "1")
    assert await kill.engaged("whatsapp") and not await kill.engaged("email")
    await redis.set("kx:pack:kill:all", "1")
    assert await kill.engaged("email")


def test_bucket_validation() -> None:
    with pytest.raises(ValueError):
        Bucket(0, 1)


async def test_kill_switch_ladder_levels_are_cumulative(redis: Redis) -> None:
    """D35: marketing -> calls -> text only -> hold; each step includes the ones before it."""
    kill = KillSwitch(redis, "ladder")
    expected = {
        "off": set(),
        "marketing": {"marketing"},
        "calls": {"marketing", "phone"},
        "text_only": {"marketing", "phone", "voice"},
        "hold": {"marketing", "phone", "voice", "whatsapp", "email"},
    }
    for level, stopped in expected.items():
        await kill.set_level(level)  # type: ignore[arg-type]
        assert await kill.level() == level
        on = {s for s in ("marketing", "phone", "voice", "whatsapp", "email") if await kill.engaged(s)}
        assert on == stopped, level
