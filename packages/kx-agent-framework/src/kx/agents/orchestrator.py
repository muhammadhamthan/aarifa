"""Deterministic router (the Orchestrator role). No LLM decides which graph runs: routing is a pure
function of the deal state and a few multilingual signals, so the price-bearing graphs can only be
reached in the stages where the business allows them, and every decision is unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

from kx.agents.registry import GraphName
from kx.core.contracts import AiRole, DealContext, DealStage
from kx.core.price_guard import extract_prices

_PRICE_ASK: Final = re.compile(
    r"\b(?:price|prices|pricing|cost|rate|quote|quotation|how much|discount|cheaper|lowest|best offer)\b"
    r"|سعر|الأسعار|كم|عرض سعر|خصم|价格|多少钱|报价|优惠|कीमत|दाम|रेट|कोटेशन|விலை|தள்ளுபடி|මිල|ගණන",
    re.IGNORECASE,
)
_COUNTER: Final = re.compile(
    r"\b(?:can you do|would you accept|i can pay|my budget|our target|counter|deal at|agree|accept|"
    r"too (?:high|expensive)|confirmed?|go ahead|proceed|purchase order|po)\b"  # §14.1 agreement wording
    r"|أقبل|موافق|غالي|可以接受|太贵|मंज़ूर|महंगा|ஒப்புக்கொள்|அதிகம்",
    re.IGNORECASE,
)
# §13 / D12: what only the salesperson decides. After a quote these go to the deal graph, which raises the
# change request deterministically; a payment topic goes there in every open stage (D13).
PAYMENT_TOPIC: Final = re.compile(
    r"\b(payments?|advance|deposit|credit|letter of credit|l/?c|bank|transfer|instal+ments?|net ?\d+|cash|"
    r"cheque|invoice due)\b|الدفع|دفعة|مقدم|تحويل|ائتمان",
    re.IGNORECASE,
)
_CHANGE_TOPIC: Final = re.compile(
    r"\b(?:deliver(?:y|ed)? (?:in|by|within|before|faster|earlier|sooner)|faster|earlier|sooner|urgent|"
    r"express|split (?:the )?deliver\w*|incoterm|cif|fob|ddp|dap|samples?|print\w*|logo|brand\w*|colou?r|"
    r"thickness|another (?:size|product|lid)|different (?:size|product|lid)|certificat\w*|complain\w*)\b"
    r"|تسليم خلال|أسرع|عينة|طباعة|شعار|لون",
    re.IGNORECASE,
)
_MANUFACTURING: Final = re.compile(
    r"\b(?:factory|machine|machines|capacity|production|lead time|certificat\w*|iso|moq|minimum order)\b"
    r"|مصنع|طاقة إنتاج|工厂|产能|फैक्ट्री|உற்பத்தி",
    re.IGNORECASE,
)
_PRODUCT: Final = re.compile(
    r"\b(?:bottle|bottles|jar|jars|cap|caps|closure|preform|pet|hdpe|pp|carton|pouch|material|sample|spec)\b"
    r"|زجاجة|غطاء|瓶|盖|बोतल|பாட்டில்",
    re.IGNORECASE,
)

_CONTACT_FIELDS: Final = frozenset({"contact_name", "contact_phone_or_email"})
_AFTER_QUOTE: Final = frozenset(
    {DealStage.QUOTATION, DealStage.NEGOTIATION, DealStage.READY_TO_CLOSE, DealStage.PAYMENT}
)


@dataclass(frozen=True, slots=True)
class Route:
    action: Literal["reply", "silent", "acknowledge"]
    graph: GraphName | None
    role: AiRole
    reason: str


def asks_price(text: str) -> bool:
    return _PRICE_ASK.search(text) is not None


def is_counter(text: str, currency: str) -> bool:
    """A buyer naming an amount, or using counter-offer language, is negotiating."""
    return bool(extract_prices(text, default_currency=currency)) or _COUNTER.search(text) is not None


def _topic_role(text: str) -> AiRole | None:
    if _MANUFACTURING.search(text):
        return AiRole.MANUFACTURING
    if _PRODUCT.search(text):
        return AiRole.PRODUCT
    return None


def _qualification_role(deal: DealContext) -> AiRole:
    missing = set(deal.missing_fields)
    if missing - _CONTACT_FIELDS:
        return AiRole.LEAD_QUALIFICATION
    return AiRole.CONTACT_CAPTURE if missing else AiRole.LEAD_QUALIFICATION


def route(
    text: str, deal: DealContext, *, first_turn: bool = False, disabled: frozenset[AiRole] = frozenset()
) -> Route:
    conv = GraphName.CONVERSATION
    if not deal.ai_enabled:
        # D13 / §14.6: after the final quote the salesperson owns the conversation; outside working hours
        # the buyer gets the fixed acknowledgement only, never an AI reply
        if deal.ai_paused_reason == "handover" and not deal.team_available:
            return Route("acknowledge", None, AiRole.ORCHESTRATOR, "handover_after_hours")
        return Route("silent", None, AiRole.ORCHESTRATOR, "ai_disabled")
    if deal.stage is DealStage.LOST:
        return Route("reply", conv, AiRole.CONCIERGE, "lost_reengage")
    if deal.stage is DealStage.WON:
        if AiRole.REPEAT_SALES in disabled:  # v2 agent off (D14): the salesperson answers won customers
            return Route("silent", None, AiRole.ORCHESTRATOR, "won_salesperson")
        return Route("reply", conv, AiRole.REPEAT_SALES, "won_customer")
    if PAYMENT_TOPIC.search(text):  # §13 "Payment": always the salesperson, never the AI (D13)
        return Route("reply", GraphName.DEAL, AiRole.DEAL_CLOSER, "payment_to_salesperson")
    price = asks_price(text)
    if deal.stage in _AFTER_QUOTE:
        if (price or is_counter(text, deal.currency)) and deal.allowed_prices:
            return Route("reply", GraphName.DEAL, AiRole.DEAL_CLOSER, "price_talk_on_issued_quote")
        if _CHANGE_TOPIC.search(text):
            return Route("reply", GraphName.DEAL, AiRole.DEAL_CLOSER, "change_request")
        if deal.stage is DealStage.QUOTATION:
            return Route("reply", conv, AiRole.QUOTATION, "quote_pending")
        return Route(
            "reply",
            conv,
            _topic_role(text)
            or (AiRole.COMMUNICATION if deal.stage is DealStage.NEGOTIATION else AiRole.DEAL_CLOSER),
            "post_quote",
        )
    if deal.stage is DealStage.QUALIFIED and price:
        return Route("reply", GraphName.QUOTATION, AiRole.QUOTATION, "ready_for_quote")
    if first_turn and not price:
        return Route("reply", conv, AiRole.CONCIERGE, "first_turn")
    topic = _topic_role(text)
    if topic is not None and not price:
        return Route("reply", conv, topic, "topic")
    if deal.stage is DealStage.QUALIFIED:
        return Route("reply", conv, AiRole.PRODUCT, "qualified_question")
    return Route("reply", conv, _qualification_role(deal), "qualify")
