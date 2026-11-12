"""WP4 (agents): anything that is not price becomes a change request (the SRS §13, D12).

Done when: "Can you deliver in 5 days?" creates a change request, with no promise in the reply.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from langchain_core.messages import HumanMessage

from kx.agents.context import AgentContext
from kx.agents.graphs.conversation import build_conversation_agent, final_reply
from kx.agents.graphs.deal import BuyerAsk, build_deal_graph
from kx.agents.models import ModelSet
from kx.agents.registry import AgentRegistry, DefaultPromptStore
from kx.agents.testing import FakeCrmGateway, ScriptedChatModel, make_deal, tool_call
from kx.core.contracts import AiRole, ChangeField, CounterVerdict, CounterVerdictKind, DealContext, DealStage
from kx.core.price_guard import check_prices

REG = AgentRegistry()
PROMPTS = DefaultPromptStore()
# promises the AI must never make on a change it cannot decide (§13.1 "acknowledges without promising")
PROMISE = re.compile(
    r"\b(yes,? we can|we can deliver|no problem|guarantee|confirmed|will deliver|free)\b", re.I
)


def ctx_for(deal: DealContext, gw: FakeCrmGateway, role: AiRole = AiRole.DEAL_CLOSER) -> AgentContext:
    return AgentContext(
        deal=deal, spec=REG.get(role), channel="whatsapp", language="en", gateway=gw, prompts=PROMPTS
    )


def sara_deal() -> DealContext:
    deal = make_deal(DealStage.NEGOTIATION, allowed=[("0.25", "unit")], unit_price="0.25")
    return deal.model_copy(update={"salesperson_name": "Sara"})


async def test_scenario_4_faster_delivery_becomes_a_change_request_with_no_promise() -> None:
    deal = sara_deal()
    gw = FakeCrmGateway(deal)
    model = ScriptedChatModel(
        script=[
            BuyerAsk(
                intent="change", change_type="delivery", change_field="delivery_time", requested="5 days"
            )
        ]
    )
    text = "Can you deliver in 5 days?"
    out = await build_deal_graph(ModelSet(model)).ainvoke({"text": text}, context=ctx_for(deal, gw))
    ((name, (kind, words, fields)),) = gw.calls
    assert (name, kind, words) == ("raise_change_request", "delivery", text)
    assert fields == (ChangeField(field="delivery_time", requested="5 days"),)
    assert out["reply"] == (
        "Good question — I've passed this to Sara from our sales team. "
        "You'll get an updated quotation within 4 working hours."
    )
    assert PROMISE.search(out["reply"]) is None
    assert check_prices(out["reply"], ()).ok  # no amount at all
    assert len(model.calls) == 1  # the reply is the fixed phrase, not LLM-written


@pytest.mark.parametrize(
    "text",
    [
        "What payment terms do you offer?",
        "Can we pay 30% advance and the rest by bank transfer?",
        "هل يمكن الدفع لاحقاً؟",
    ],
)
async def test_payment_always_goes_to_the_salesperson(text: str) -> None:
    deal = sara_deal()
    gw = FakeCrmGateway(deal)
    model = ScriptedChatModel(script=[BuyerAsk(intent="question")])  # even if the model misses it
    out = await build_deal_graph(ModelSet(model)).ainvoke({"text": text}, context=ctx_for(deal, gw))
    assert gw.calls[0][0] == "raise_change_request" and gw.calls[0][1][0] == "payment"
    assert "evaluate_counter" not in [c[0] for c in gw.calls]
    assert "within 4 working hours" in out["reply"] or "4" in out["reply"]


async def test_quantity_outside_the_tiers_raises_a_quantity_change_request() -> None:
    deal = sara_deal()
    verdict = CounterVerdict(
        kind=CounterVerdictKind.CHANGE_REQUEST,
        offer_unit_price=None,
        currency="AED",
        concessions_left=3,
        reason="quantity_outside_tiers",
    )
    gw = FakeCrmGateway(deal, [verdict])
    model = ScriptedChatModel(
        script=[BuyerAsk(intent="counter", ask_unit_price=Decimal("0.25"), ask_quantity=Decimal(3000))]
    )
    out = await build_deal_graph(ModelSet(model)).ainvoke(
        {"text": "Only 3,000 pieces at AED 0.25"}, context=ctx_for(deal, gw)
    )
    assert [c[0] for c in gw.calls] == ["evaluate_counter", "raise_change_request"]
    assert gw.calls[1][1][0] == "quantity" and gw.calls[1][1][2][0].requested == "3,000 pcs"
    assert "updated quotation within 4 working hours" in out["reply"]


async def test_tier_change_promises_a_revised_quote_without_a_price() -> None:
    deal = sara_deal()
    verdict = CounterVerdict(
        kind=CounterVerdictKind.REQUOTE,
        offer_unit_price=None,
        currency="AED",
        concessions_left=3,
        reason="tier_change",
        requires_approval=True,
    )
    gw = FakeCrmGateway(deal, [verdict])
    model = ScriptedChatModel(
        script=[BuyerAsk(intent="counter", ask_unit_price=Decimal("0.24"), ask_quantity=Decimal(50000))]
    )
    out = await build_deal_graph(ModelSet(model)).ainvoke(
        {"text": "What about 50,000 at AED 0.24?"}, context=ctx_for(deal, gw)
    )
    assert "revised quotation" in out["reply"] and "Sara from our sales team" in out["reply"]
    assert check_prices(out["reply"], ()).ok


async def test_no_salesperson_yet_names_the_team() -> None:
    deal = make_deal(DealStage.NEGOTIATION, allowed=[("0.25", "unit")], unit_price="0.25")
    gw = FakeCrmGateway(deal)
    model = ScriptedChatModel(
        script=[BuyerAsk(intent="change", change_type="samples", requested="2 samples")]
    )
    out = await build_deal_graph(ModelSet(model)).ainvoke(
        {"text": "Can I get samples?"}, context=ctx_for(deal, gw)
    )
    assert out["reply"].startswith("Good question — I've passed this to our sales team.")


async def test_conversation_agents_have_the_raise_change_request_tool() -> None:
    deal = sara_deal().model_copy(update={"stage": DealStage.QUOTATION})
    gw = FakeCrmGateway(deal)
    ctx = ctx_for(deal, gw, AiRole.COMMUNICATION)
    model = ScriptedChatModel(
        script=[
            tool_call(
                "raise_change_request",
                {
                    "type": "printing",
                    "buyer_words": "Can you print our logo on the lid?",
                    "field": "printing",
                    "requested": "logo on the lid",
                },
            ),
            "Good question — I've passed this to Sara from our sales team.",
        ]
    )
    agent = build_conversation_agent(ModelSet(model))
    out = await agent.ainvoke({"messages": [HumanMessage("Can you print our logo on the lid?")]}, context=ctx)
    assert gw.calls[0][0] == "raise_change_request" and gw.calls[0][1][0] == "printing"
    assert [t.status for t in ctx.log.tool_calls] == ["ok"]
    assert final_reply(out["messages"]) is not None
