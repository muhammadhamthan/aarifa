"""SPEC for v1 price-list pricing (the SRS §10, §11.1, §12, §23; V1_CHANGE_PLAN D6, D7, WP2).

Expected values are the worked examples of the business flow, verbatim:
* §11.1   50,000 pcs at AED 0.25 = 12,500.00; VAT 5% = 625.00; total 13,125.00; export in USD at 3.6725
          = 3,403.68
* §12.1   ladder 0.2500 / 0.2433 / 0.2367 / 0.2300 (3 equal steps, 4 dp); line totals 12,500 / 12,165 /
          11,835 / 11,500
* §12.2   rules 1-5 on every buyer counter-offer
* §23     scenario 2 (70,000 x 0.2367 = 16,569.00; 2 steps; 5.3 % average discount; quantity +40 %) and
          scenario 3 (all steps used, buyer insists lower -> the AI pauses)
"""

from __future__ import annotations

import itertools
import uuid
from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kx.core.contracts import CounterVerdictKind
from kx.core.money import display
from kx.crm.pricing.engine import evaluate_ladder_counter, ladder_between, quote_totals
from kx.crm.pricing.price_list import (
    PriceTier,
    TierPrice,
    convert_tier,
    discount_from_list_pct,
    list_line,
    min_price_for,
    resolve_tier,
)
from kx.crm.pricing.types import LadderState

T1, T2, T3 = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)
# §23 price list: ALC-750-L
TIERS = (
    PriceTier(T1, D(5_000), D(9_999), D("0.32"), D("0.29")),
    PriceTier(T2, D(10_000), D(49_999), D("0.28"), D("0.255")),
    PriceTier(T3, D(50_000), None, D("0.25"), D("0.23")),
)
LADDER = (D("0.2500"), D("0.2433"), D("0.2367"), D("0.2300"))


def state(used: int, offer: D, qty: D = D(50_000), low: int = 0, tier: uuid.UUID | None = T3) -> LadderState:
    return LadderState(
        ladder=LADDER,
        steps_used=used,
        current_offer=offer,
        base_quantity=qty,
        low_ask_count=low,
        tier_id=tier,
    )


# ---------------------------------------------------------------- §10 tiers and minimum
@pytest.mark.parametrize(
    ("qty", "tier", "list_price"),
    [
        (5_000, T1, "0.32"),
        (9_999, T1, "0.32"),
        (10_000, T2, "0.28"),
        (49_999, T2, "0.28"),
        (50_000, T3, "0.25"),
        (1_000_000, T3, "0.25"),
    ],
)
def test_resolve_tier_picks_the_tier_containing_the_quantity(
    qty: int, tier: uuid.UUID, list_price: str
) -> None:
    got = resolve_tier(TIERS, D(qty))
    assert (got.tier_id, got.list_price) == (tier, D(list_price))


@pytest.mark.parametrize("qty", ["4999", "9999.5", "0"])
def test_quantity_outside_the_tiers_raises_lookup_error(qty: str) -> None:
    """Below the MOQ or between tiers: the caller turns this into a change request (D12)."""
    with pytest.raises(LookupError):
        resolve_tier(TIERS, D(qty))


def test_min_price_per_product_wins_over_the_category_percentage() -> None:
    tier = resolve_tier(TIERS, D(50_000))
    assert min_price_for(tier, D(8)) == D("0.23")
    no_floor = TierPrice(list_price=D("0.25"), min_price=None, tier_id=T3)
    assert min_price_for(no_floor, D(8)) == D("0.23")  # "Aluminium: 8 % below list" (§10)
    assert min_price_for(no_floor, D("7.5")) == D("0.23125")


def test_no_minimum_anywhere_means_no_room_to_negotiate() -> None:
    """Not written down: with neither a tier minimum nor a category % the floor is the list price, so the AI
    can never discount (safe default, docs/OPEN_QUESTIONS.md)."""
    assert min_price_for(TierPrice(D("0.25"), None, None), None) == D("0.25")


def test_category_minimum_is_rounded_up_never_below_the_true_floor() -> None:
    assert min_price_for(TierPrice(D("0.333333"), None, None), D(10)) == D("0.300000")
    assert min_price_for(TierPrice(D("0.123457"), None, None), D(10)) == D("0.111112")  # 0.1111113 -> up


# ---------------------------------------------------------------- §11.1 the quote
def test_example_dubai_with_vat() -> None:
    line = list_line(resolve_tier(TIERS, D(50_000)), D(50_000))
    assert (line.unit_price, line.line_total) == (D("0.25"), D("12500.0000"))
    totals = quote_totals([line.line_total], D(5))
    assert (
        display(totals.subtotal, "AED"),
        display(totals.vat_amount, "AED"),
        display(totals.total, "AED"),
    ) == (
        D("12500.00"),
        D("625.00"),
        D("13125.00"),
    )


def test_example_riyadh_export_in_usd_at_the_printed_rate() -> None:
    line = list_line(resolve_tier(TIERS, D(50_000)), D(50_000), fx_to_aed=D("3.6725"))
    totals = quote_totals([line.line_total], D(0))
    assert display(totals.total, "USD") == D("3403.68")
    assert line.unit_price == D("0.068074")  # 0.25 / 3.6725 at 6 dp
    assert line.line_total_aed == D("12500.0000")  # the AED quote at the printed rate


def test_negotiation_range_converts_at_the_same_rate_and_the_floor_rounds_up() -> None:
    usd = convert_tier(resolve_tier(TIERS, D(50_000)), D("3.6725"))
    assert usd.list_price == D("0.068074")
    assert usd.min_price == D("0.062628")  # 0.23 / 3.6725 = 0.0626276... rounded UP
    assert usd.min_price * D("3.6725") >= D("0.23")


# ---------------------------------------------------------------- §12.1 the ladder
def test_ladder_three_equal_steps_at_four_decimals() -> None:
    assert ladder_between(D("0.25"), D("0.23"), 3) == LADDER
    assert [display(p * 50_000, "AED") for p in LADDER] == [
        D("12500.00"),
        D("12165.00"),
        D("11835.00"),
        D("11500.00"),
    ]
    assert (D("0.25") - D("0.23")) / 3 == pytest.approx(D("0.006667"), abs=D("0.000001"))


def test_ladder_edge_cases() -> None:
    assert ladder_between(D("0.25"), D("0.25"), 3) == (D("0.25"),)  # no room
    assert ladder_between(D("0.25"), D("0.23"), 0) == (D("0.25"),)
    assert ladder_between(D("0.32"), D("0.29"), 3) == (D("0.32"), D("0.3100"), D("0.3000"), D("0.2900"))
    assert ladder_between(D("0.2301"), D("0.23"), 3) == (D("0.2301"), D("0.2300"))  # duplicates collapse
    with pytest.raises(ValueError, match="minimum"):
        ladder_between(D("0.23"), D("0.25"), 3)


# ---------------------------------------------------------------- §12.2 rules 1-5
def test_rule1_ask_at_or_above_the_offer_is_accepted_at_the_offer() -> None:
    v = evaluate_ladder_counter(state(1, D("0.2433")), D("0.26"), None)
    assert (v.kind, v.offer_unit_price, v.concessions_left) == (CounterVerdictKind.ACCEPT, D("0.2433"), 2)


def test_rule2_ask_between_next_step_and_offer_is_accepted_and_uses_one_step() -> None:
    v = evaluate_ladder_counter(state(0, D("0.25")), D("0.245"), D(50_000))
    assert (v.kind, v.offer_unit_price, v.traded_for, v.concessions_left) == (
        CounterVerdictKind.ACCEPT,
        D("0.245"),
        "goodwill",
        2,
    )


def test_rule3_ask_below_next_step_gets_the_next_step() -> None:
    v = evaluate_ladder_counter(state(0, D("0.25")), D("0.24"), D(50_000))
    assert (v.kind, v.offer_unit_price, v.traded_for) == (CounterVerdictKind.COUNTER, D("0.2433"), "goodwill")


def test_rule3_steps_two_and_three_need_a_bigger_quantity() -> None:
    v = evaluate_ladder_counter(state(1, D("0.2433")), D("0.235"), D(50_000))
    assert (v.kind, v.offer_unit_price, v.reason, v.traded_for) == (
        CounterVerdictKind.COUNTER,
        D("0.2433"),
        "ask_for_quantity",
        None,
    )
    v = evaluate_ladder_counter(state(1, D("0.2433")), D("0.24"), D(50_000))  # rule 2 needs quantity too
    assert (v.kind, v.offer_unit_price, v.reason) == (
        CounterVerdictKind.COUNTER,
        D("0.2433"),
        "ask_for_quantity",
    )
    v = evaluate_ladder_counter(state(1, D("0.2433")), D("0.235"), D(70_000))
    assert (v.kind, v.offer_unit_price, v.traded_for) == (
        CounterVerdictKind.COUNTER,
        D("0.2367"),
        "quantity_increase",
    )


def test_rule4_first_ask_below_minimum_gets_the_next_step_and_a_quantity_question() -> None:
    v = evaluate_ladder_counter(state(0, D("0.25")), D("0.21"), D(50_000))
    assert (v.kind, v.offer_unit_price, v.traded_for, v.reason) == (
        CounterVerdictKind.COUNTER,
        D("0.2433"),
        "goodwill",
        "ask_for_quantity",
    )


def test_rule4_insisted_ask_below_minimum_needs_a_human() -> None:
    v = evaluate_ladder_counter(state(1, D("0.2433"), low=1), D("0.21"), D(50_000))
    assert (v.kind, v.offer_unit_price, v.reason) == (CounterVerdictKind.NEEDS_MANAGER, None, "below_range")


def test_rule4_all_steps_used_and_still_lower_needs_a_human() -> None:
    """Scenario 3: offer AED 0.23 (all 3 steps used), buyer: '0.20 or I go elsewhere'."""
    v = evaluate_ladder_counter(state(3, D("0.23"), qty=D(100_000)), D("0.20"), D(100_000))
    assert (v.kind, v.offer_unit_price, v.reason, v.concessions_left) == (
        CounterVerdictKind.NEEDS_MANAGER,
        None,
        "concessions_exhausted",
        0,
    )


def test_rule5_quantity_into_another_tier_requotes_for_approval() -> None:
    s = state(0, D("0.28"), qty=D(20_000), tier=T2)
    v = evaluate_ladder_counter(s, D("0.24"), D(50_000), tiers=TIERS)
    assert (v.kind, v.offer_unit_price, v.requires_approval, v.reason) == (
        CounterVerdictKind.REQUOTE,
        D("0.25"),
        True,
        "tier_change",
    )


def test_quantity_outside_the_tiers_becomes_a_change_request() -> None:
    v = evaluate_ladder_counter(state(0, D("0.25")), D("0.24"), D(3_000), tiers=TIERS)
    assert (v.kind, v.offer_unit_price, v.reason) == (
        CounterVerdictKind.CHANGE_REQUEST,
        None,
        "quantity_outside_tiers",
    )


def test_same_tier_quantity_change_stays_in_the_negotiation() -> None:
    v = evaluate_ladder_counter(state(1, D("0.2433")), D("0.23"), D(70_000), tiers=TIERS)
    assert (v.kind, v.offer_unit_price) == (CounterVerdictKind.COUNTER, D("0.2367"))


# ---------------------------------------------------------------- §23 scenario 2, end to end on the engine
def test_scenario_2_riyadh_distributor() -> None:
    s = state(0, D("0.25"))
    v1 = evaluate_ladder_counter(s, D("0.21"), None, tiers=TIERS)  # "Too high, 0.21?" (below min, 1st time)
    assert (v1.kind, v1.offer_unit_price, v1.reason) == (
        CounterVerdictKind.COUNTER,
        D("0.2433"),
        "ask_for_quantity",
    )
    s = state(1, D("0.2433"), low=1)
    v2 = evaluate_ladder_counter(s, D("0.23"), D(70_000), tiers=TIERS)  # "70,000 at 0.23"
    assert (v2.kind, v2.offer_unit_price, v2.traded_for) == (
        CounterVerdictKind.COUNTER,
        D("0.2367"),
        "quantity_increase",
    )
    s = state(2, D("0.2367"), qty=D(70_000), low=1)
    v3 = evaluate_ladder_counter(s, D("0.2367"), D(70_000), tiers=TIERS)  # "Deal at 0.2367"
    assert (v3.kind, v3.offer_unit_price) == (CounterVerdictKind.ACCEPT, D("0.2367"))
    final = list_line(TierPrice(D("0.2367"), None, T3), D(70_000))
    assert display(final.line_total, "AED") == D("16569.00")
    assert discount_from_list_pct(D("0.25"), D("0.2367")).quantize(D("0.1")) == D("5.3")
    assert (D(70_000) - D(50_000)) / D(50_000) * 100 == 40


# ---------------------------------------------------------------- properties
ASK = st.decimals(min_value=D("0.10"), max_value=D("0.40"), places=4)


@settings(max_examples=500, deadline=None)
@given(
    ask=ASK,
    used=st.integers(min_value=0, max_value=3),
    low=st.integers(min_value=0, max_value=3),
    qty=st.integers(min_value=50_000, max_value=500_000),
)
def test_no_offer_is_ever_below_the_minimum_or_more_than_one_step(
    ask: D, used: int, low: int, qty: int
) -> None:
    current = LADDER[used]
    v = evaluate_ladder_counter(state(used, current, low=low), ask, D(qty), tiers=TIERS)
    if v.offer_unit_price is not None:
        assert v.offer_unit_price >= LADDER[-1]  # never below the minimum
        assert v.offer_unit_price <= current  # never raises the price
        assert v.offer_unit_price >= LADDER[min(used + 1, len(LADDER) - 1)]  # at most one step at a time
    assert 0 <= v.concessions_left <= len(LADDER) - 1


@settings(max_examples=500, deadline=None)
@given(
    top=st.decimals(min_value=D("0.01"), max_value=D("100"), places=6),
    cut=st.decimals(min_value=D(0), max_value=D("0.9"), places=3),
    steps=st.integers(min_value=0, max_value=6),
)
def test_ladder_properties(top: D, cut: D, steps: int) -> None:
    floor = (top * (1 - cut)).quantize(D("0.000001"))
    if floor <= 0:
        return
    ladder = ladder_between(top, floor, steps)
    assert ladder[0] == top
    assert all(p >= floor for p in ladder)  # never below the minimum, even after rounding
    assert all(a > b for a, b in itertools.pairwise(ladder))  # strictly decreasing
    assert len(ladder) <= steps + 1


# ---------------------------------------------------------------- speed
@pytest.mark.bench
def test_bench_ladder_between(benchmark) -> None:  # type: ignore[no-untyped-def]
    benchmark(ladder_between, D("0.25"), D("0.23"), 3)


@pytest.mark.bench
def test_bench_evaluate_ladder_counter(benchmark) -> None:  # type: ignore[no-untyped-def]
    benchmark(evaluate_ladder_counter, state(1, D("0.2433")), D("0.23"), D(70_000), tiers=TIERS)


@pytest.mark.bench
def test_bench_resolve_tier(benchmark) -> None:  # type: ignore[no-untyped-def]
    benchmark(resolve_tier, TIERS, D(70_000))
