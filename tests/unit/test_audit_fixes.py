"""12 Nov audit fixes with no database: D27 model tiers, the X-Forwarded-For spoof guard, D30 VendorOrder,
and the metric names the CloudWatch alarms (infra/modules/observability) page on."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

from kx.agents.registry import AgentRegistry, ModelTier
from kx.agents.website import _ip
from kx.core.contracts import AiRole

ROOT = Path(__file__).resolve().parents[2]

# D27: Sonnet (SMART) on turns that may discuss money; Haiku (FAST) on the rest.
FAST_ROLES = {
    AiRole.ORCHESTRATOR,
    AiRole.CONCIERGE,
    AiRole.PRODUCT,
    AiRole.MANUFACTURING,
    AiRole.LEAD_QUALIFICATION,
    AiRole.CONTACT_CAPTURE,
    AiRole.FOLLOW_UP,
}
SMART_ROLES = {
    AiRole.QUOTATION,
    AiRole.COMMERCIAL_APPROVAL,
    AiRole.COMMUNICATION,
    AiRole.DEAL_CLOSER,
    AiRole.REPEAT_SALES,
}


@pytest.mark.parametrize("role", sorted(FAST_ROLES | SMART_ROLES, key=lambda r: r.value))
def test_model_tier_matches_d27(role: AiRole) -> None:
    tier = AgentRegistry().get(role).tier
    assert tier == (ModelTier.FAST if role in FAST_ROLES else ModelTier.SMART)


def test_every_price_role_runs_on_the_smart_tier() -> None:
    """A role that may quote prices must never be on the cheap model (D27): money turns get Sonnet."""
    reg = AgentRegistry()
    for role in AiRole:
        spec = reg.get(role)
        if (
            spec.may_quote_prices and role is not AiRole.FOLLOW_UP
        ):  # follow-up only re-engages, never quotes live
            assert spec.tier == ModelTier.SMART, role


def _request(xff: str | None, client: str | None = "10.0.0.9") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    scope: dict[str, Any] = {
        "type": "http",
        "headers": headers,
        "client": (client, 12345) if client else None,
    }
    return Request(scope)


def test_x_forwarded_for_uses_the_last_entry_not_the_spoofable_first() -> None:
    # the ALB appends the real peer last; earlier entries are whatever the client sent
    assert _ip(_request("1.1.1.1, 2.2.2.2, 203.0.113.7")) == "203.0.113.7"
    assert _ip(_request("evil-spoof")) == "evil-spoof"  # one hop: the ALB's own append
    assert _ip(_request(None)) == "10.0.0.9"  # no header: the socket peer
    assert _ip(_request("", client=None)) == "unknown"


class _FakeSSM:
    def __init__(self, value: str | Exception) -> None:
        self.value = value
        self.calls = 0

    async def get_parameter(self, Name: str) -> dict[str, Any]:  # noqa: N803 - boto3 param name
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return {"Parameter": {"Value": self.value}}


async def test_vendor_order_reads_parameter_store_and_caches() -> None:
    from arifaa_pack.adapters import VendorOrder

    ssm = _FakeSSM("ses, postmark")
    vo = VendorOrder(ssm, "/kx/vendors", {"email": ("ses", "postmark")})
    assert await vo.order("email") == ("ses", "postmark")
    assert await vo.order("email") == ("ses", "postmark")
    assert ssm.calls == 1  # 60 s cache: one read


async def test_vendor_order_falls_back_to_defaults() -> None:
    from arifaa_pack.adapters import VendorOrder

    defaults = {"email": ("ses", "postmark"), "calls": ("twilio",)}
    # no prefix configured: the documented default, no SSM call
    vo = VendorOrder(_FakeSSM("ignored"), None, defaults)
    assert await vo.order("calls") == ("twilio",)
    # unreadable parameter: the default order, not an empty list
    vo = VendorOrder(_FakeSSM(RuntimeError("no such parameter")), "/kx/vendors", defaults)
    assert await vo.order("email") == ("ses", "postmark")


def _infra_metric_names() -> set[str]:
    tf = (ROOT / "infra/modules/observability/main.tf").read_text(encoding="utf-8")
    return set(re.findall(r'metric_name\s*=\s*"([^"]+)"', tf))


def test_emitted_metric_names_match_the_cloudwatch_alarms() -> None:
    """The app emits bare metric names (EMF namespace Kairox); the alarms must name the same strings."""
    names = _infra_metric_names()
    assert "VendorSwitch" in names  # kx.core.fallback / kx.agents.service on every switch (incl. hand-over)
    assert "SpendCap" in names  # kx.core.spend at 80 % / 100 % (D34)
    for src in (
        "packages/kx-core-db/src/kx/core/fallback.py",
        "packages/kx-agent-framework/src/kx/agents/service.py",
    ):
        assert '"VendorSwitch"' in (ROOT / src).read_text(encoding="utf-8")
    assert '"SpendCap"' in (ROOT / "packages/kx-core-db/src/kx/core/spend.py").read_text(encoding="utf-8")
