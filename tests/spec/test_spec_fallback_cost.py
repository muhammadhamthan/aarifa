"""D30 circuit breaker / fallback chain and the D34 per-model cost table (official prices, 11 Nov)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kx.agents.runs import call_cost
from kx.core.fallback import AllVendorsFailed, CircuitBreaker, FallbackChain


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_breaker_opens_after_three_failures_in_a_minute_then_one_trial_decides() -> None:
    clock = Clock()
    b = CircuitBreaker(clock=clock)
    b.failure()
    clock.t += 61  # the first failure is out of the 60 s window
    b.failure()
    b.failure()
    assert b.state == "closed" and b.allow()
    b.failure()
    assert b.state == "open" and not b.allow()
    clock.t += 300
    assert b.allow() and not b.allow()  # exactly one trial call
    b.failure()
    assert b.state == "open"  # the trial failed: five more minutes
    clock.t += 300
    assert b.allow()
    b.success()
    assert b.state == "closed" and b.allow()


async def test_chain_switches_vendor_and_reports_the_last_resort() -> None:
    switches: list[tuple[str, str, str]] = []

    async def alert(field: str, a: str, b: str) -> None:
        switches.append((field, a, b))

    chain: FallbackChain[str] = FallbackChain("email", "pack", alert=alert)

    async def send(vendor: str) -> str:
        if vendor == "ses":
            raise ConnectionError("down")
        return f"sent by {vendor}"

    assert await chain.run(["ses", "postmark"], send) == ("postmark", "sent by postmark")
    assert switches == [("email", "ses", "postmark")]

    async def nothing_works(vendor: str) -> str:
        raise TimeoutError(vendor)

    with pytest.raises(AllVendorsFailed) as err:
        await chain.run(["ses", "postmark"], nothing_works)
    assert set(err.value.errors) == {"ses", "postmark"} and switches[-1] == ("email", "ses", "last_resort")


def test_costs_follow_the_official_price_table() -> None:
    # Sonnet 5.5: in 2, 5-min write 2.50, read 0.10, out 10 USD per 1M
    sonnet = call_cost("claude-sonnet-5-5", uncached=10_000, cache_write=2_000, cache_read=50_000, output=800)
    assert sonnet == Decimal("0.038")  # 20,000 + 5,000 + 5,000 + 8,000 per 1M
    # Haiku 5.5 up to 100K prompt tokens: 0.10 / 0.125 / 0.01 / 0.50; above: 0.50 / 0.625 / 0.05 / 2.50
    assert call_cost(
        "claude-haiku-5-5", uncached=1_000_000, cache_write=0, cache_read=0, output=0
    ) == Decimal("0.50")
    small = call_cost("claude-haiku-5-5", uncached=20_000, cache_write=0, cache_read=0, output=1_000)
    assert small == Decimal("0.0025")
    assert call_cost("some-other-model", uncached=1, cache_write=0, cache_read=0, output=1) is None
