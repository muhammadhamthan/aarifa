"""kx.core.signed (website session tokens) and kx.core.spend (D34 daily caps, 80 % / 100 % alerts once)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kx.core.signed import sign, verify
from kx.core.spend import SpendCaps, SpendMeter

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)


def test_signed_tokens_refuse_tampering_and_expiry() -> None:
    token = sign("s3cret", {"sid": "abc"}, expires_at=NOW + timedelta(hours=2))
    assert verify("s3cret", token, now=NOW) == {
        "sid": "abc",
        "exp": int((NOW + timedelta(hours=2)).timestamp()),
    }
    assert verify("other", token, now=NOW) is None
    body, _, mac = token.partition(".")
    assert verify("s3cret", f"{body}x.{mac}", now=NOW) is None
    assert verify("s3cret", token, now=NOW + timedelta(hours=3)) is None
    assert verify("s3cret", "garbage", now=NOW) is None


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, int | str] = {}

    async def get(self, key: str) -> int | str | None:
        return self.data.get(key)

    async def incrby(self, key: str, n: int) -> int:
        self.data[key] = int(self.data.get(key, 0)) + n
        return int(self.data[key])

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True


async def test_spend_cap_alerts_once_at_80_and_100_percent_then_degrades() -> None:
    alerts: list[tuple[str, Decimal, Decimal]] = []

    async def alert(kind: str, used: Decimal, cap: Decimal) -> None:
        alerts.append((kind, used, cap))

    meter = SpendMeter(
        FakeRedis(),  # type: ignore[arg-type]
        "pack",
        SpendCaps(ai_usd=Decimal(10), voice_minutes=Decimal(60), wa_templates=Decimal(100)),
        alert=alert,
        clock=lambda: NOW,
    )
    await meter.add("ai_usd", Decimal("7.999999"))
    assert alerts == [] and await meter.left("ai_usd")
    await meter.add("ai_usd", Decimal("0.5"))  # crosses 80 %
    await meter.add("ai_usd", Decimal("0.1"))  # still between: no second alert
    assert [a[0] for a in alerts] == ["ai_usd"]
    await meter.add("ai_usd", Decimal(2))  # crosses 100 %
    assert len(alerts) == 2 and not await meter.left("ai_usd")
    assert await meter.used("ai_usd") == Decimal("10.599999")
    assert await meter.left("voice_minutes")  # each kind is counted on its own
