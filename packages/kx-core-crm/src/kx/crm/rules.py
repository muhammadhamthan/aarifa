"""Pure business rules (no I/O). Services call these; unit tests pin them down.

Kept separate from the services so a rule reads as one table or one function a reviewer can check
against the signed requirements, without wading through SQL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal, get_args
from zoneinfo import ZoneInfo

from kx.core.contracts import CounterVerdict, CounterVerdictKind, DealStage
from kx.core.errors import RuleViolation

S = DealStage

# Allowed stage moves. LOST is reachable from every open stage; WON and LOST are terminal (a returning
# buyer gets a new deal). Backward moves exist only where the business really goes back: a revised
# quote (NEGOTIATION -> QUOTATION) or a buyer reopening talks before paying (READY_TO_CLOSE -> NEGOTIATION).
TRANSITIONS: Final[Mapping[DealStage, frozenset[DealStage]]] = {
    S.NEW: frozenset({S.QUALIFIED, S.LOST}),
    S.QUALIFIED: frozenset({S.QUOTATION, S.LOST}),
    S.QUOTATION: frozenset({S.NEGOTIATION, S.READY_TO_CLOSE, S.LOST}),
    S.NEGOTIATION: frozenset({S.QUOTATION, S.READY_TO_CLOSE, S.LOST}),
    S.READY_TO_CLOSE: frozenset({S.NEGOTIATION, S.PAYMENT, S.LOST}),
    S.PAYMENT: frozenset({S.WON, S.LOST}),
    S.WON: frozenset(),
    S.LOST: frozenset(),
}
OPEN_STAGES: Final = frozenset(s for s, nxt in TRANSITIONS.items() if nxt)
QUOTABLE_STAGES: Final = frozenset({S.QUALIFIED, S.QUOTATION, S.NEGOTIATION})

# §15.4: price, delivery time, specification not available, chose a competitor, no response, budget,
# duplicate, other (the first five keep their 4 Nov codes)
LostReason = Literal[
    "price_too_high",
    "delivery_not_suitable",
    "bought_competitor",
    "product_unavailable",
    "no_response_postponed",
    "budget",
    "duplicate",
    "other",
]
LOST_REASONS: Final[frozenset[str]] = frozenset(get_args(LostReason))


def check_transition(
    current: DealStage, target: DealStage, *, lost_reason: str | None = None, has_payment: bool = False
) -> None:
    """Raise RuleViolation unless `current -> target` is allowed with the given facts."""
    if target not in TRANSITIONS[current]:
        raise RuleViolation(f"deal cannot move from {current} to {target}", current=current, target=target)
    if target is S.LOST and lost_reason not in LOST_REASONS:
        raise RuleViolation("LOST needs a lost_reason", allowed=sorted(LOST_REASONS))
    if target is S.WON and not has_payment:
        raise RuleViolation("WON needs a confirmed advance payment")


# ---------------------------------------------------------------- qualification
@dataclass(frozen=True, slots=True)
class QualificationFacts:
    has_product: bool
    has_quantity: bool
    has_delivery_country: bool
    has_contact_name: bool
    has_phone_or_email: bool


QUALIFICATION_FIELDS: Final = (
    ("product", "has_product"),
    ("quantity", "has_quantity"),
    ("delivery_country", "has_delivery_country"),
    ("contact_name", "has_contact_name"),
    ("contact_phone_or_email", "has_phone_or_email"),
)


def missing_fields(facts: QualificationFacts) -> tuple[str, ...]:
    """What the AI still has to ask for before the deal is QUALIFIED (in asking order)."""
    return tuple(name for name, attr in QUALIFICATION_FIELDS if not getattr(facts, attr))


def lost_proposal_due(sent_at: datetime, cadence_days: tuple[int, ...], lost_after_days: int) -> datetime:
    """§16: "No reply 14 days after day 10: the system proposes LOST (no response)" (D15). The last
    follow-up is the last cadence day after the quote was sent; the proposal is a task, never a move."""
    return sent_at + timedelta(days=max(cadence_days, default=0) + lost_after_days)


def next_working_day(at: datetime, weekend: frozenset[int], tz: str) -> datetime:
    """§16: a follow-up that falls on the buyer country's weekend moves to the next working day, same time
    (scenario 9: day 5 is a Saturday in Riyadh -> Sunday)."""
    if not weekend or len(weekend) >= 7:
        return at
    zone = ZoneInfo(tz)
    local = at.astimezone(zone)
    while local.isoweekday() in weekend:
        local = (local + timedelta(days=1)).replace(tzinfo=zone)
    return local.astimezone(at.tzinfo)


# ---------------------------------------------------------------- price staleness (T12)
Staleness = Literal["ok", "warn", "block"]


def staleness(last_approved_at: datetime | None, now: datetime, warn_days: int, block_days: int) -> Staleness:
    """Material prices not reviewed for `warn_days` warn the manager; after `block_days` no new quote
    may be built (enforced at quote time, not by a job flag, so a dead job can never unblock quotes)."""
    if last_approved_at is None:
        return "block"
    age = now - last_approved_at
    if age.total_seconds() >= block_days * 86_400:
        return "block"
    if age.total_seconds() >= warn_days * 86_400:
        return "warn"
    return "ok"


def days_since(at: datetime, now: datetime) -> int:
    return max((now - at).days, 0)


# ---------------------------------------------------------------- negotiation round mapping
RoundVerdict = Literal["within_range", "concession", "below_range", "exception", "accepted"]


def round_verdict(v: CounterVerdict) -> RoundVerdict:
    """Map an engine verdict to negotiation_rounds.verdict. A concession is any verdict that used one."""
    match v.kind:
        case CounterVerdictKind.NEEDS_MANAGER:
            return "below_range" if v.reason == "below_range" else "exception"
        case CounterVerdictKind.REQUOTE | CounterVerdictKind.CHANGE_REQUEST:
            return "exception"  # a human handles it: revised quote or change request (§12.2 rule 5, D12)
        case CounterVerdictKind.ACCEPT:
            return "accepted"
        case CounterVerdictKind.COUNTER if v.traded_for is not None:
            return "concession"
        case _:
            return "within_range"


PauseReason = Literal[
    "human_takeover",
    "below_range",
    "exception",
    "md_takeover",
    "opt_out",
    "handover",  # D13 / §14.6: final quote approved; the AI stops replying on the deal
    "concessions_exhausted",  # §12.2 rule 4: all steps used and the buyer still wants lower
]
PAUSE_REASONS: Final[frozenset[str]] = frozenset(get_args(PauseReason))


def pause_reason(reason: str) -> PauseReason:
    """Free-text pause reasons from agents map onto the DB's closed set ('exception' otherwise)."""
    return reason if reason in PAUSE_REASONS else "exception"  # type: ignore[return-value]
