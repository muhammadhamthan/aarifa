"""Pricing engine (candidate A): pure ``decimal.Decimal`` maths with a fixed module-level context.

All functions are pure. Every rounding to money places is ROUND_HALF_UP through ``_CTX`` (prec 34),
and every inexact division runs in ``_CTX`` too. Additions and multiplications use the plain operators:
they are exact for money-sized operands, and the operators are about twice as fast as Context methods.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal
from functools import lru_cache
from typing import Final

from kx.core.contracts import CounterVerdict, CounterVerdictKind
from kx.crm.pricing.types import (
    CostInputs,
    DeliveryEstimate,
    DeliveryInputs,
    FreightInputs,
    MarginBand,
    MarginRule,
    NegotiationState,
    PricedLine,
    QuoteLineInput,
    QuoteTotals,
)

__all__ = [
    "concession_ladder",
    "estimate_delivery",
    "evaluate_counter",
    "freight_per_1000",
    "price_line",
    "quote_totals",
    "resolve_band",
    "unit_cost_per_1000",
]

_CTX: Final = Context(prec=34, rounding=ROUND_HALF_UP)
_Q4: Final = Decimal("0.0001")
_Q6: Final = Decimal("0.000001")
_ZERO: Final = Decimal(0)
_TEN: Final = Decimal(10)
_HUNDRED: Final = Decimal(100)
# Exact powers of ten: multiplying by these is cheaper than Decimal.scaleb.
_E2: Final = Decimal("1E-2")
_E3: Final = Decimal("1E-3")
_E5: Final = Decimal("1E-5")

_div = _CTX.divide
_quant = _CTX.quantize

_ACCEPT: Final = CounterVerdictKind.ACCEPT
_COUNTER: Final = CounterVerdictKind.COUNTER
_MANAGER: Final = CounterVerdictKind.NEEDS_MANAGER
# The model's compiled validator, fed a dict: the fastest validated way to build a CounterVerdict.
_validate_verdict: Callable[[dict[str, object]], CounterVerdict] = (
    CounterVerdict.__pydantic_validator__.validate_python
)

_ISO_DAYS: Final = frozenset(range(1, 8))
_ONE_DAY: Final = timedelta(days=1)


# ---------------------------------------------------------------- cost & price
def unit_cost_per_1000(c: CostInputs) -> Decimal:
    """Cost of 1,000 pieces: material grossed up for yield + production + packaging + freight (4 dp)."""
    if c.yield_pct <= _ZERO:
        raise ValueError("yield_pct must be positive")
    # kg / (yield/100) * rate == kg * rate * 100 / yield, with a single inexact step.
    material = _div(c.kg_per_1000 * c.material_rate_per_kg * _HUNDRED, c.yield_pct)
    return _quant(material + c.production_per_1000 + c.packaging_per_1000 + c.freight_per_1000, _Q4)


def freight_per_1000(f: FreightInputs) -> Decimal:
    """Container freight spread over the pieces that fit in a 40HC, per 1,000 pieces (4 dp)."""
    pcs = f.pcs_per_40hc
    if pcs is None or pcs <= 0:
        raise ValueError("pcs_per_40hc must be a positive integer")
    # (cost + surcharge) * (1 + buffer/100) / pcs * 1000 == (cost + surcharge) * (100 + buffer) * 10 / pcs
    gross = (f.container_cost + f.surcharge) * (_HUNDRED + f.buffer_pct) * _TEN
    return _quant(_div(gross, pcs), _Q4)


def price_line(line: QuoteLineInput, band: MarginBand, markup_pct: Decimal | None = None) -> PricedLine:
    """Price one quote line at ``markup_pct`` on cost (default: top of the band)."""
    m = band.markup_max_pct if markup_pct is None else markup_pct
    if not band.markup_min_pct <= m <= band.markup_max_pct:
        raise ValueError("markup_pct is outside the margin band")
    c = line.cost  # unit_cost_per_1000 inlined: this is the hot path
    if c.yield_pct <= _ZERO:
        raise ValueError("yield_pct must be positive")
    material = _div(c.kg_per_1000 * c.material_rate_per_kg * _HUNDRED, c.yield_pct)
    cost1000 = _quant(material + c.production_per_1000 + c.packaging_per_1000 + c.freight_per_1000, _Q4)
    # cost1000 * (1 + m/100) / 1000 == cost1000 * (100 + m) * 10^-5 (exact before rounding)
    unit_price = _quant(cost1000 * (_HUNDRED + m) * _E5, _Q6)
    return PricedLine(
        description=line.description,
        quantity=line.quantity,
        unit=line.unit,
        unit_cost=_quant(cost1000 * _E3, _Q6),
        markup_pct=m,
        unit_price=unit_price,
        line_total=_quant(unit_price * line.quantity, _Q4),
        product_id=line.product_id,
    )


def quote_totals(line_totals: Iterable[Decimal], vat_rate_pct: Decimal) -> QuoteTotals:
    """Subtotal, VAT and grand total, each at 4 dp."""
    subtotal = _quant(sum(line_totals, _ZERO), _Q4)
    vat = _quant(subtotal * vat_rate_pct * _E2, _Q4)
    return QuoteTotals(subtotal=subtotal, vat_amount=vat, total=subtotal + vat)


# ---------------------------------------------------------------- margin band resolution
def resolve_band(
    rules: Iterable[MarginRule], category_code: str, customer_type: str | None, quantity: Decimal
) -> MarginBand:
    """Pick the matching rule with the lowest priority number; ties go to the most specific rule."""
    best: MarginRule | None = None
    best_key = (0, 0)
    for r in rules:
        if r.category_code != category_code:
            continue
        if r.customer_type is not None and r.customer_type != customer_type:
            continue
        if r.min_qty is not None and quantity < r.min_qty:
            continue
        if r.max_qty is not None and quantity > r.max_qty:
            continue
        spec = (r.customer_type is not None) + (r.min_qty is not None) + (r.max_qty is not None)
        key = (r.priority, -spec)
        if best is None or key < best_key:
            best, best_key = r, key
    if best is None:
        raise LookupError(f"no margin rule for category {category_code!r}")
    return MarginBand(
        markup_min_pct=best.markup_min_pct,
        markup_max_pct=best.markup_max_pct,
        max_concessions=best.max_concessions,
        rule_id=best.rule_id,
    )


# ---------------------------------------------------------------- negotiation
@lru_cache(maxsize=4096)
def concession_ladder(unit_cost: Decimal, band: MarginBand) -> tuple[Decimal, ...]:
    """Per-piece prices from the top of the band down to the floor in ``max_concessions`` equal steps."""
    n = band.max_concessions
    top, bottom = band.markup_max_pct, band.markup_min_pct
    if n <= 0:
        return (_quant(_div(unit_cost * (_HUNDRED + top), _HUNDRED), _Q6),)
    # price_i = unit_cost * (1 + (top - i*(top-bottom)/n)/100)
    #         = unit_cost * (100n + top*n - i*(top-bottom)) / (100n)   -> one inexact division per step
    span = top - bottom
    head = (_HUNDRED + top) * n
    denom = _HUNDRED * n
    return tuple(_quant(_div(unit_cost * (head - span * i), denom), _Q6) for i in range(n + 1))


def evaluate_counter(
    state: NegotiationState, ask_unit_price: Decimal, ask_quantity: Decimal | None
) -> CounterVerdict:
    """Decide how to answer a buyer's counter-offer. Never reveals cost or floor in ``reason``."""
    band = state.band
    maxc = band.max_concessions
    used = state.concessions_used
    current = state.current_offer_unit_price
    left = max(maxc - used, 0)

    if ask_unit_price >= current:
        return _validate_verdict(
            {
                "kind": _ACCEPT,
                "offer_unit_price": current,
                "currency": state.currency,
                "concessions_left": left,
            }
        )

    ladder = concession_ladder(state.unit_cost, band)
    if ask_unit_price < ladder[-1]:
        return _validate_verdict(
            {
                "kind": _MANAGER,
                "offer_unit_price": None,
                "currency": state.currency,
                "concessions_left": left,
                "reason": "below_range",
            }
        )
    if used >= maxc:
        return _validate_verdict(
            {
                "kind": _MANAGER,
                "offer_unit_price": None,
                "currency": state.currency,
                "concessions_left": 0,
                "reason": "concessions_exhausted",
            }
        )

    qty_up = ask_quantity is not None and ask_quantity > state.base_quantity
    if used >= 1 and not qty_up:
        return _validate_verdict(
            {
                "kind": _COUNTER,
                "offer_unit_price": current,
                "currency": state.currency,
                "concessions_left": left,
                "reason": "ask_for_quantity",
            }
        )

    step = ladder[used + 1]
    if ask_unit_price >= step:
        kind, offer = _ACCEPT, ask_unit_price
    else:
        kind, offer = _COUNTER, step
    return _validate_verdict(
        {
            "kind": kind,
            "offer_unit_price": offer,
            "currency": state.currency,
            "concessions_left": left - 1,
            "traded_for": "quantity_increase" if qty_up else "goodwill",
        }
    )


# ---------------------------------------------------------------- delivery estimate
def _add_working_days(start: date, days: int, weekend: frozenset[int], per_week: int) -> date:
    """``start`` plus ``days`` working days (days whose ISO weekday is not in ``weekend``)."""
    if days <= 0:
        return start
    weeks, rest = divmod(days, per_week)
    if rest == 0:  # land on the last working day of the final week, not on the same weekday
        weeks, rest = weeks - 1, per_week
    cur = start + timedelta(days=7 * weeks)
    while rest:
        cur += _ONE_DAY
        if cur.isoweekday() not in weekend:
            rest -= 1
    return cur


def estimate_delivery(i: DeliveryInputs) -> DeliveryEstimate:
    """Ready and delivery dates from stock, material wait, backlog, production and packing."""
    if i.pieces_per_day <= 0:
        raise ValueError("pieces_per_day must be positive")
    weekend = i.weekend
    per_week = 7 - len(weekend & _ISO_DAYS)
    if per_week <= 0:
        raise ValueError("weekend leaves no working days")

    need = i.quantity - i.finished_stock
    if need <= 0:
        production_days = material_wait = 0
        ready = _add_working_days(i.today, i.packing_days, weekend, per_week)
    else:
        whole, part = divmod(need, i.pieces_per_day)
        production_days = int(whole) + (1 if part else 0)
        material_wait = i.supplier_lead_days if i.material_shortfall else 0
        start = i.today + timedelta(days=material_wait)
        ready = _add_working_days(start, i.backlog_days + production_days + i.packing_days, weekend, per_week)
    delivery = ready + timedelta(days=i.transit_days + i.buffer_days)
    return DeliveryEstimate(
        production_days=production_days,
        material_wait_days=material_wait,
        ready_date=ready,
        delivery_date=delivery,
    )
