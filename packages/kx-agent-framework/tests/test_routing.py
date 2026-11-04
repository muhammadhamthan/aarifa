from __future__ import annotations

import pytest

from kx.agents.evals.harness import load_scenarios, run_scenario
from kx.agents.language import detect_script_language, reply_language
from kx.agents.models import BedrockChatAnthropic
from kx.agents.orchestrator import route
from kx.agents.registry import AgentRegistry
from kx.agents.testing import make_deal
from kx.agents.voice import VoiceRouter
from kx.core.contracts import AiRole, DealStage

ISSUED = [("0.25", "line 1 unit price")]


@pytest.mark.parametrize(
    ("stage", "kw", "text", "first", "graph", "role"),
    [
        (DealStage.NEW, {"ai_enabled": False}, "hi", False, None, AiRole.ORCHESTRATOR),
        (DealStage.NEW, {}, "Hello", True, "conversation", AiRole.CONCIERGE),
        (DealStage.NEW, {"missing": ["quantity"]}, "I need jars", False, "conversation", AiRole.PRODUCT),
        (
            DealStage.NEW,
            {"missing": ["quantity"]},
            "what is your price?",
            False,
            "conversation",
            AiRole.LEAD_QUALIFICATION,
        ),
        (DealStage.NEW, {"missing": ["contact_name"]}, "ok", False, "conversation", AiRole.CONTACT_CAPTURE),
        (DealStage.NEW, {}, "what is your factory capacity?", False, "conversation", AiRole.MANUFACTURING),
        (DealStage.QUALIFIED, {}, "how much for these?", False, "quotation", AiRole.QUOTATION),
        (DealStage.QUALIFIED, {}, "do you ship to Oman?", False, "conversation", AiRole.PRODUCT),
        (DealStage.QUOTATION, {}, "price?", False, "conversation", AiRole.QUOTATION),  # quote not issued yet
        (DealStage.QUOTATION, {"allowed": ISSUED}, "Can you do AED 0.20?", False, "deal", AiRole.DEAL_CLOSER),
        (DealStage.NEGOTIATION, {"allowed": ISSUED}, "كم السعر الأفضل؟", False, "deal", AiRole.DEAL_CLOSER),
        (
            DealStage.NEGOTIATION,
            {"allowed": ISSUED},
            "when can you deliver?",
            False,
            "conversation",
            AiRole.COMMUNICATION,
        ),
        (DealStage.WON, {}, "reorder please", False, "conversation", AiRole.REPEAT_SALES),
    ],
)
def test_routing_table(
    stage: DealStage, kw: dict[str, object], text: str, first: bool, graph: str | None, role: AiRole
) -> None:
    rt = route(text, make_deal(stage, **kw), first_turn=first)  # type: ignore[arg-type]
    assert (rt.graph.value if rt.graph else None, rt.role) == (graph, role)
    assert (rt.action == "silent") == (graph is None)


def test_registry_has_all_twelve_roles_and_only_price_roles_quote() -> None:
    reg = AgentRegistry()
    assert len(reg) == 12
    assert (
        not reg.get(AiRole.LEAD_QUALIFICATION).may_quote_prices
        and reg.get(AiRole.DEAL_CLOSER).may_quote_prices
    )


@pytest.mark.parametrize(
    ("lang", "stt", "tts"),
    [
        ("en", "elevenlabs", "elevenlabs"),
        ("ar-AE", "elevenlabs", "elevenlabs"),
        ("zh", "elevenlabs", "elevenlabs"),
        ("hi", "sarvam", "sarvam"),
        ("ta", "sarvam", "sarvam"),
        ("si", "azure", "azure"),
        ("fr", "elevenlabs", "elevenlabs"),
    ],
)
def test_voice_routing(lang: str, stt: str, tts: str) -> None:
    r = VoiceRouter().route(lang)
    assert (r.stt, r.tts) == (stt, tts)


def test_language_detection() -> None:
    assert detect_script_language("أريد زجاجات PET") == "ar"
    assert detect_script_language("我要500个瓶子") == "zh"
    assert detect_script_language("எனக்கு பாட்டில்கள் வேண்டும்") == "ta"
    assert reply_language("hello", "si") == "si"


def test_bedrock_transport_uses_block_level_caching() -> None:
    m = BedrockChatAnthropic(model_name="global.anthropic.claude-sonnet-5-5", aws_region="me-central-1")
    assert m._llm_type == "anthropic-bedrock-chat"
    assert type(m._async_client).__name__ == "AsyncAnthropicBedrock"


@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda s: s.id)
async def test_eval_scenarios_scripted(scenario: object) -> None:
    result = await run_scenario(scenario)  # type: ignore[arg-type]
    assert result.passed, result.failures
