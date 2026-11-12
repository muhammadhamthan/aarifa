"""WP14 (§8.1 step 6, D18): the AI reads a stop word inside a sentence. It may add a stop, never remove one."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from kx.agents.models import ModelRouter
from kx.agents.service import AgentService, InboundTurn, StopReading
from kx.agents.testing import FakeCrmGateway, ScriptedChatModel, make_deal
from kx.agents.voice import voice_stop
from kx.core.contracts import DealStage

pytestmark = pytest.mark.db


def turn(deal_id: uuid.UUID, text: str, thread: str | None = None) -> InboundTurn:
    return InboundTurn(
        channel="whatsapp",
        text=text,
        deal_id=deal_id,
        thread_key=thread or f"whatsapp:{uuid.uuid7()}",
        trigger_event="message.received.v1",
        trigger_event_id=uuid.uuid7(),
        stop_word=True,
    )


def service(databases: Any, gateway: FakeCrmGateway, *script: Any) -> AgentService:
    return AgentService(
        databases=databases,
        gateway=gateway,
        models=ModelRouter.static(ScriptedChatModel(script=list(script))),
    )


async def test_a_meant_opt_out_is_recorded_and_nothing_is_replied(databases: Any) -> None:
    deal = make_deal(DealStage.NEGOTIATION)
    gw = FakeCrmGateway(deal)
    svc = service(databases, gw, StopReading(decision="opt_out", reason="found cheaper"))
    sent: list[str] = []

    async def deliver(body: str, language: str) -> None:
        sent.append(body)

    result = await svc.handle_turn(
        turn(deal.deal_id, "please stop sending me offers, found cheaper"), deliver
    )
    assert result.status == "silent" and sent == []  # automation sends the one confirmation
    assert (
        "record_opt_out",
        {"scope": "all", "words": "please stop sending me offers, found cheaper", "reason": "found cheaper"},
    ) in gw.calls


async def test_not_an_opt_out_continues_as_a_normal_turn(databases: Any) -> None:
    deal = make_deal(DealStage.NEGOTIATION)
    gw = FakeCrmGateway(deal)
    svc = service(databases, gw, StopReading(decision="not_opt_out"), "Sure, take your time.")
    result = await svc.handle_turn(turn(deal.deal_id, "stop, let me check with my boss"))
    assert result.reply == "Sure, take your time." and not [c for c in gw.calls if "opt" in c[0]]


async def test_unclear_asks_once_when_live_and_hands_over_when_not(databases: Any) -> None:
    deal = make_deal(DealStage.NEGOTIATION)
    gw = FakeCrmGateway(deal)
    thread = f"whatsapp:{uuid.uuid7()}"
    svc = service(
        databases,
        gw,
        "Here is our catalogue.",
        StopReading(decision="unclear"),
        StopReading(decision="unclear"),
    )
    plain = turn(deal.deal_id, "send me the catalogue", thread)
    first = await svc.handle_turn(dataclasses.replace(plain, stop_word=False))  # the buyer is chatting
    assert first.reply == "Here is our catalogue."
    live = await svc.handle_turn(turn(deal.deal_id, "ok stop it there", thread))
    assert live.reply == "Just to check, should we stop messaging you?"  # the SRS's own words
    assert gw.calls[-1] == ("flag_unclear_stop", {"words": "ok stop it there", "live": True})
    cold = await svc.handle_turn(turn(deal.deal_id, "stop it there"))  # a fresh thread: nobody is chatting
    assert cold.status == "silent" and gw.calls[-1][1]["live"] is False


async def test_a_failed_reading_is_treated_as_unclear_never_as_carry_on(databases: Any) -> None:
    deal = make_deal(DealStage.NEGOTIATION)
    gw = FakeCrmGateway(deal)
    svc = service(databases, gw)  # no scripted answer: the model call fails
    result = await svc.handle_turn(turn(deal.deal_id, "stop it"))
    assert result.status == "silent" and gw.calls[-1][0] == "flag_unclear_stop"


async def test_voice_stop_means_wait(databases: Any) -> None:
    """§8.1 step 10: "stop" -> listen; "stop calling me" -> calls stopped + goodbye; unclear -> ask."""
    deal = make_deal(DealStage.NEW)
    gw = FakeCrmGateway(deal)
    svc = service(databases, gw)
    assert await voice_stop(svc, deal.deal_id, "stop", "en") == ""
    assert await voice_stop(svc, deal.deal_id, "what sizes do you have?", "en") is None
    assert "end the call" in (await voice_stop(svc, deal.deal_id, "hmm stop that one", "en") or "")
    assert await voice_stop(svc, deal.deal_id, "stop calling me", "en")
    assert gw.calls[-1][1]["scope"] == "phone"
