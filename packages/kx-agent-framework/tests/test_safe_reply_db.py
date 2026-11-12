"""D27 last resort (11 Nov plan WP22): every model fails -> "Let me check with our sales team." and the deal
goes to the salesperson; after 3 failures in 60 s the breaker skips Claude for 5 minutes (no waiting)."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from pydantic import PrivateAttr

from kx.agents.models import ModelRouter
from kx.agents.service import AgentService, InboundTurn
from kx.agents.testing import FakeCrmGateway, make_deal
from kx.core.contracts import DealStage

pytestmark = pytest.mark.db


class DownModel(BaseChatModel):
    _calls: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "down"

    @property
    def calls(self) -> int:
        return self._calls

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._calls += 1
        raise httpx.ConnectError("api.anthropic.com unreachable")

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def turn(deal_id: uuid.UUID) -> InboundTurn:
    return InboundTurn(
        channel="whatsapp",
        text="what sizes do you have for aluminium containers?",
        deal_id=deal_id,
        thread_key=f"whatsapp:{uuid.uuid7()}",
        trigger_event="message.received.v1",
        trigger_event_id=uuid.uuid7(),
    )


async def test_spend_cap_hands_over_before_spending_anything(databases: Any) -> None:
    """D34: at 100 % of the daily AI budget the turn degrades to hand-over, and no model is called."""
    deal = make_deal(DealStage.QUALIFIED)
    gw, model = FakeCrmGateway(deal), DownModel()

    async def no_budget() -> bool:
        return False

    svc = AgentService(
        databases=databases,
        gateway=gw,
        models=ModelRouter.static(model),
        deal_summaries=False,
        budget_left=no_budget,
    )
    sent: list[str] = []

    async def deliver(body: str, language: str) -> None:
        sent.append(body)

    out = await svc.handle_turn(turn(deal.deal_id), deliver)
    assert out.status == "handoff" and out.reply == "Let me check with our sales team."
    assert model.calls == 0  # the cap short-circuits before any token is spent
    assert ("pause_ai", "exception") in gw.calls and sent == [out.reply]


async def test_every_model_down_gives_the_safe_reply_and_hands_over(databases: Any) -> None:
    deal = make_deal(DealStage.QUALIFIED)
    gw, model = FakeCrmGateway(deal), DownModel()
    svc = AgentService(
        databases=databases, gateway=gw, models=ModelRouter.static(model), deal_summaries=False
    )
    sent: list[str] = []

    async def deliver(body: str, language: str) -> None:
        sent.append(body)

    first = await svc.handle_turn(turn(deal.deal_id), deliver)
    assert first.reply == "Let me check with our sales team." and first.status == "handoff"
    assert ("pause_ai", "exception") in gw.calls and sent == [first.reply]

    for _ in range(2):  # the CRM keeps a paused deal paused; reset so these turns reach the models again
        gw.deal = deal
        await svc.handle_turn(turn(deal.deal_id), deliver)
    before = model.calls
    gw.deal = deal
    fourth = await svc.handle_turn(turn(deal.deal_id), deliver)
    assert svc.llm_breaker.state == "open" and model.calls == before  # no call while Claude cools down
    assert fourth.reply == "Let me check with our sales team."
