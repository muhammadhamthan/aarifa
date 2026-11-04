"""SPEC for kx.crm.pricing.engine (the judge for competing implementations).

Rules (Business Flow Final v1, sections 5 and 7):
* cost/1000 = kg_per_1000 / (yield/100) * material_rate + production + packaging + freight   -> 4 dp
* price per piece = cost/1000 * (1 + markup/100) / 1000                                       -> 6 dp
* line total = unit_price * quantity                                                            -> 4 dp
* markup is ON COST; AI opens at the TOP of the MD's range; ladder of max_concessions equal steps
* concession 1 may be goodwill; further concessions only for a bigger quantity; never below range
"""

from __future__ import annotations

import importlib
import os
import uuid
from datetime import date
from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kx.core.contracts import CounterVerdictKind
from kx.crm.pricing.types import (
    CostInputs,
    DeliveryInputs,
    FreightInputs,
    MarginBand,
    MarginRule,
    NegotiationState,
    QuoteLineInput,
)

engine = importlib.import_module(os.environ.get("KX_PRICING_ENGINE", "kx.crm.pricing.engine"))

COST = CostInputs(
    material_rate_per_kg=D("12.00"),
    kg_per_1000=D("8.5"),
    yield_pct=D("92"),
    production_per_1000=D("35"),
    packaging_per_1000=D("6"),
)
BAND = MarginBand(markup_min_pct=D(40), markup_max_pct=D(50), max_concessions=3)
UNIT_COST = D("0.1518696")
LADDER = (D("0.227804"), D("0.222742"), D("0.217680"), D("0.212617"))


def state(used: int, offer: D, base_qty: D = D(50000)) -> NegotiationState:
    return NegotiationState(
        unit_cost=UNIT_COST,
        band=BAND,
        concessions_used=used,
        current_offer_unit_price=offer,
        base_quantity=base_qty,
    )


# ---------------------------------------------------------------- cost & price
def test_unit_cost_per_1000() -> None:
    assert engine.unit_cost_per_1000(COST) == D("151.8696")


def test_unit_cost_includes_freight() -> None:
    c = CostInputs(D("12.00"), D("8.5"), D("92"), D("35"), D("6"), freight_per_1000=D("25"))
    assert engine.unit_cost_per_1000(c) == D("176.8696")


def test_freight_per_1000() -> None:
    f = FreightInputs(container_cost=D(4000), surcharge=D(1000), buffer_pct=D(10), pcs_per_40hc=220_000)
    assert engine.freight_per_1000(f) == D("25.0000")


def test_freight_requires_container_fill() -> None:
    with pytest.raises(ValueError):
        engine.freight_per_1000(FreightInputs(D(4000), D(0), D(0), None))


def test_price_line_opens_at_top_of_range() -> None:
    line = engine.price_line(QuoteLineInput("Al container 750 ml", D(50000), COST), BAND)
    assert line.markup_pct == D(50)
    assert line.unit_price == D("0.227804")
    assert line.line_total == D("11390.2000")
    assert line.unit_cost == D("0.151870")


def test_price_line_explicit_markup_must_be_inside_band() -> None:
    line = engine.price_line(QuoteLineInput("x", D(1000), COST), BAND, markup_pct=D(40))
    assert line.unit_price == D("0.212617")
    with pytest.raises(ValueError):
        engine.price_line(QuoteLineInput("x", D(1000), COST), BAND, markup_pct=D(39))


def test_quote_totals_with_vat() -> None:
    lines = [engine.price_line(QuoteLineInput("a", D(50000), COST), BAND)]
    totals = engine.quote_totals([lines[0].line_total, D("500.0000")], vat_rate_pct=D(5))
    assert (totals.subtotal, totals.vat_amount, totals.total) == (
        D("11890.2000"),
        D("594.5100"),
        D("12484.7100"),
    )


def test_quote_totals_no_vat_outside_uae() -> None:
    totals = engine.quote_totals([D("100.0000")], vat_rate_pct=D(0))
    assert totals.total == D("100.0000") and totals.vat_amount == D("0.0000")


# ---------------------------------------------------------------- margin band resolution
R_ANY = MarginRule(uuid.uuid4(), "ALU", None, None, None, D(40), D(50), 3, 100)
R_DIST = MarginRule(uuid.uuid4(), "ALU", "distributor", None, None, D(35), D(45), 3, 100)
R_BULK = MarginRule(uuid.uuid4(), "ALU", None, D(100000), None, D(30), D(40), 2, 50)


def test_band_most_specific_wins_on_equal_priority() -> None:
    band = engine.resolve_band([R_ANY, R_DIST, R_BULK], "ALU", "distributor", D(50000))
    assert (band.markup_min_pct, band.markup_max_pct, band.rule_id) == (D(35), D(45), R_DIST.rule_id)


def test_band_lower_priority_number_wins() -> None:
    band = engine.resolve_band([R_ANY, R_DIST, R_BULK], "ALU", "distributor", D(150000))
    assert band.rule_id == R_BULK.rule_id and band.max_concessions == 2


def test_band_generic_rule_for_other_customer_types() -> None:
    assert engine.resolve_band([R_ANY, R_DIST], "ALU", "caterer", D(1000)).rule_id == R_ANY.rule_id


def test_band_missing_category_raises() -> None:
    with pytest.raises(LookupError):
        engine.resolve_band([R_ANY], "PLASTIC", None, D(1000))


# ---------------------------------------------------------------- negotiation ladder & verdicts
def test_concession_ladder() -> None:
    assert engine.concession_ladder(UNIT_COST, BAND) == LADDER


def test_ask_at_or_above_offer_is_accepted_at_offer() -> None:
    v = engine.evaluate_counter(state(0, LADDER[0]), D("0.23"), D(50000))
    assert v.kind is CounterVerdictKind.ACCEPT and v.offer_unit_price == LADDER[0] and v.concessions_left == 3


def test_ask_below_floor_needs_manager() -> None:
    v = engine.evaluate_counter(state(0, LADDER[0]), D("0.20"), D(50000))
    assert (
        v.kind is CounterVerdictKind.NEEDS_MANAGER
        and v.offer_unit_price is None
        and v.reason == "below_range"
    )


def test_first_concession_is_goodwill() -> None:
    v = engine.evaluate_counter(state(0, LADDER[0]), D("0.215"), D(50000))
    assert v.kind is CounterVerdictKind.COUNTER and v.offer_unit_price == LADDER[1]
    assert v.concessions_left == 2 and v.traded_for == "goodwill"


def test_second_concession_needs_more_quantity() -> None:
    v = engine.evaluate_counter(state(1, LADDER[1]), D("0.215"), D(50000))
    assert v.kind is CounterVerdictKind.COUNTER and v.offer_unit_price == LADDER[1]
    assert v.concessions_left == 2 and v.traded_for is None and v.reason == "ask_for_quantity"


def test_second_concession_with_more_quantity() -> None:
    v = engine.evaluate_counter(state(1, LADDER[1]), D("0.215"), D(60000))
    assert v.kind is CounterVerdictKind.COUNTER and v.offer_unit_price == LADDER[2]
    assert v.concessions_left == 1 and v.traded_for == "quantity_increase"


def test_ask_between_steps_is_accepted_at_ask() -> None:
    v = engine.evaluate_counter(state(2, LADDER[2]), D("0.2175"), D(70000))
    assert v.kind is CounterVerdictKind.ACCEPT and v.offer_unit_price == D("0.2175")
    assert v.concessions_left == 0 and v.traded_for == "quantity_increase"


def test_concessions_exhausted_needs_manager() -> None:
    # 3rd concession was accepted between steps at 0.2175, so the offer is still above the floor
    v = engine.evaluate_counter(state(3, D("0.2175")), D("0.215"), D(90000))
    assert v.kind is CounterVerdictKind.NEEDS_MANAGER and v.reason == "concessions_exhausted"
    assert v.offer_unit_price is None and v.concessions_left == 0


def test_below_floor_wins_over_exhausted() -> None:
    v = engine.evaluate_counter(state(3, LADDER[3]), D("0.21"), D(90000))
    assert v.kind is CounterVerdictKind.NEEDS_MANAGER and v.reason == "below_range"


def test_verdict_never_exposes_floor_or_cost() -> None:
    v = engine.evaluate_counter(state(0, LADDER[0]), D("0.20"), D(50000))
    dumped = v.model_dump_json()
    assert "0.212617" not in dumped and "0.1518" not in dumped


@settings(max_examples=300, deadline=None)
@given(
    ask=st.decimals(min_value=D("0.05"), max_value=D("0.40"), places=6),
    used=st.integers(min_value=0, max_value=3),
    qty=st.integers(min_value=10_000, max_value=500_000),
)
def test_property_offer_never_below_floor_nor_above_current(ask: D, used: int, qty: int) -> None:
    current = LADDER[used]
    v = engine.evaluate_counter(state(used, current), ask, D(qty))
    if v.offer_unit_price is not None:
        assert LADDER[-1] <= v.offer_unit_price <= current
        assert v.offer_unit_price >= min(ask, current) or v.kind is CounterVerdictKind.COUNTER
    assert 0 <= v.concessions_left <= BAND.max_concessions


# ---------------------------------------------------------------- delivery estimate
MON = date(2026, 11, 9)  # Monday


def d_inputs(**kw: object) -> DeliveryInputs:
    base: dict[str, object] = dict(
        today=MON,
        quantity=D(50000),
        pieces_per_day=20000,
        finished_stock=D(0),
        backlog_days=4,
        material_shortfall=False,
        supplier_lead_days=10,
        packing_days=1,
        transit_days=12,
        buffer_days=3,
    )
    base.update(kw)
    return DeliveryInputs(**base)  # type: ignore[arg-type]


def test_delivery_skips_sundays() -> None:
    e = engine.estimate_delivery(d_inputs())
    assert (e.production_days, e.material_wait_days) == (3, 0)
    assert e.ready_date == date(2026, 11, 18) and e.delivery_date == date(2026, 12, 3)


def test_delivery_waits_for_material() -> None:
    e = engine.estimate_delivery(d_inputs(material_shortfall=True))
    assert e.material_wait_days == 10
    assert e.ready_date == date(2026, 11, 28) and e.delivery_date == date(2026, 12, 13)


def test_delivery_from_stock_skips_backlog() -> None:
    e = engine.estimate_delivery(d_inputs(finished_stock=D(60000), material_shortfall=True))
    assert (e.production_days, e.material_wait_days) == (0, 0)
    assert e.ready_date == date(2026, 11, 10) and e.delivery_date == date(2026, 11, 25)


# ---------------------------------------------------------------- speed (pytest --benchmark-enable)
@pytest.mark.bench
def test_bench_evaluate_counter(benchmark) -> None:  # type: ignore[no-untyped-def]
    s = state(1, LADDER[1])
    benchmark(engine.evaluate_counter, s, D("0.215"), D(60000))


@pytest.mark.bench
def test_bench_price_line(benchmark) -> None:  # type: ignore[no-untyped-def]
    li = QuoteLineInput("x", D(50000), COST)
    benchmark(engine.price_line, li, BAND)
