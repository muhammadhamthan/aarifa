"""Pricing engine, approach B: exact integer rationals inside, Decimal only at the boundary.

Every Decimal input is turned once into an exact ``(numerator, denominator)`` pair with
``Decimal.as_integer_ratio``. All maths is done on Python ints with no intermediate truncation;
rounding happens only at the documented points, with an explicit integer ROUND_HALF_UP
(ties away from zero, as ``decimal.ROUND_HALF_UP``). Results are scaled ints (10^-4 or 10^-6 units)
that are turned back into Decimals with exactly 4 or 6 decimal places.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from decimal import MAX_PREC, Context, Decimal
from functools import lru_cache
from math import lcm
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

# Private context: int * 1E-n is exact whatever the caller's global decimal context says.
_MUL: Final = Context(prec=MAX_PREC).multiply
_E4: Final = Decimal("1E-4")
_E6: Final = Decimal("1E-6")

_ACCEPT: Final = CounterVerdictKind.ACCEPT
_COUNTER: Final = CounterVerdictKind.COUNTER
_MANAGER: Final = CounterVerdictKind.NEEDS_MANAGER


# ---------------------------------------------------------------- integer helpers
def _rhu(n: int, d: int) -> int:
    """Exact n/d rounded half-up (ties away from zero) to an int."""
    if d < 0:
        n, d = -n, -d
    if n >= 0:
        return (n + n + d) // (d + d)
    return -((d - n - n) // (d + d))


def _d4(n: int) -> Decimal:
    """Scaled int (10^-4 units) -> Decimal with exactly 4 places."""
    return _MUL(Decimal(n), _E4)


def _d6(n: int) -> Decimal:
    """Scaled int (10^-6 units) -> Decimal with exactly 6 places."""
    return _MUL(Decimal(n), _E6)


def _cost4(c: CostInputs) -> int:
    """cost/1000 in 10^-4 units: kg/(yield/100)*rate + production + packaging + freight, half-up."""
    kn, kd = c.kg_per_1000.as_integer_ratio()
    yn, yd = c.yield_pct.as_integer_ratio()
    rn, rd = c.material_rate_per_kg.as_integer_ratio()
    pn, pd = c.production_per_1000.as_integer_ratio()
    an, ad = c.packaging_per_1000.as_integer_ratio()
    # rest = production + packaging (+ freight), as one exact fraction
    sn = pn * ad + an * pd
    sd = pd * ad
    f = c.freight_per_1000
    if f:
        fn, fd = f.as_integer_ratio()
        sn = sn * fd + fn * sd
        sd *= fd
    # material term = kg * 100 * yd * rate / (kd * yn * rd)
    td = kd * yn * rd
    return _rhu((kn * 100 * yd * rn * sd + sn * td) * 10_000, td * sd)


# ---------------------------------------------------------------- cost & price
def unit_cost_per_1000(c: CostInputs) -> Decimal:
    """Cost of 1,000 pieces, 4 dp half-up."""
    return _d4(_cost4(c))


def freight_per_1000(f: FreightInputs) -> Decimal:
    """Freight per 1,000 pieces from one 40HC: (container + surcharge) * (1 + buffer%) / pcs * 1000."""
    pcs = f.pcs_per_40hc
    if pcs is None or pcs <= 0:
        raise ValueError("pcs_per_40hc must be a positive number of pieces")
    cn, cd = f.container_cost.as_integer_ratio()
    sn, sd = f.surcharge.as_integer_ratio()
    bn, bd = f.buffer_pct.as_integer_ratio()
    # * 1000 (per 1,000) * 10^4 (4 dp) / 100 (percent) = * 100_000
    return _d4(_rhu((cn * sd + sn * cd) * (100 * bd + bn) * 100_000, cd * sd * bd * pcs))


def resolve_band(rules: Iterable[MarginRule], category_code: str, customer_type: str | None,
                 quantity: Decimal) -> MarginBand:
    """Pick the matching rule with the lowest priority number, then the most specific one."""
    best: MarginRule | None = None
    best_prio = 0
    best_spec = 0
    for r in rules:
        if r.category_code != category_code:
            continue
        ct = r.customer_type
        if ct is not None and ct != customer_type:
            continue
        lo = r.min_qty
        if lo is not None and quantity < lo:
            continue
        hi = r.max_qty
        if hi is not None and quantity > hi:
            continue
        spec = (ct is not None) + (lo is not None) + (hi is not None)
        prio = r.priority
        if best is None or prio < best_prio or (prio == best_prio and spec > best_spec):
            best, best_prio, best_spec = r, prio, spec
    if best is None:
        raise LookupError(f"no margin rule for category {category_code!r}")
    return MarginBand(best.markup_min_pct, best.markup_max_pct, best.max_concessions, best.rule_id)


def price_line(line: QuoteLineInput, band: MarginBand, markup_pct: Decimal | None = None) -> PricedLine:
    """Price one quote line; markup defaults to the top of the band and must lie inside it."""
    m = band.markup_max_pct if markup_pct is None else markup_pct
    if not band.markup_min_pct <= m <= band.markup_max_pct:
        raise ValueError("markup_pct is outside the margin band")
    c4 = _cost4(line.cost)
    mn, md = m.as_integer_ratio()
    # c4/10^4 * (1 + m/100) / 1000, in 10^-6 units
    up6 = _rhu(c4 * (100 * md + mn), md * 1000)
    qty = line.quantity
    qn, qd = qty.as_integer_ratio()
    return PricedLine(line.description, qty, line.unit, _d6(_rhu(c4, 10)), m, _d6(up6),
                      _d4(_rhu(up6 * qn, 100 * qd)), line.product_id)


def quote_totals(line_totals: Iterable[Decimal], vat_rate_pct: Decimal) -> QuoteTotals:
    """Subtotal (4 dp), VAT on the rounded subtotal (4 dp) and their sum."""
    num, den = 0, 1
    for x in line_totals:
        n, d = x.as_integer_ratio()
        if d == den:
            num += n
        elif den % d == 0:
            num += n * (den // d)
        else:
            common = lcm(den, d)
            num = num * (common // den) + n * (common // d)
            den = common
    s4 = _rhu(num * 10_000, den)
    rn, rd = vat_rate_pct.as_integer_ratio()
    v4 = _rhu(s4 * rn, rd * 100)
    return QuoteTotals(_d4(s4), _d4(v4), _d4(s4 + v4))


# ---------------------------------------------------------------- negotiation
@lru_cache(maxsize=8192)
def _ladder(unit_cost: Decimal, lo: Decimal, hi: Decimal, n: int) -> tuple[Decimal, ...]:
    un, ud = unit_cost.as_integer_ratio()
    an, ad = hi.as_integer_ratio()
    if n <= 0:
        # q6(uc * (1 + hi/100)) = un * (100ad + an) * 10^6 / (ud * 100 * ad)
        return (_d6(_rhu(un * (100 * ad + an) * 10_000, ud * ad)),)
    bn, bd = lo.as_integer_ratio()
    # markup_i = (top - i*step) / den, exactly
    den = ad * bd * n
    top = an * bd * n
    step = an * bd - bn * ad
    base = 100 * den + top
    pd = ud * den
    return tuple(_d6(_rhu(un * (base - i * step) * 10_000, pd)) for i in range(n + 1))


def concession_ladder(unit_cost: Decimal, band: MarginBand) -> tuple[Decimal, ...]:
    """Per-piece prices from the top of the band down to the floor in max_concessions equal steps."""
    return _ladder(unit_cost, band.markup_min_pct, band.markup_max_pct, band.max_concessions)


def evaluate_counter(state: NegotiationState, ask_unit_price: Decimal,
                     ask_quantity: Decimal | None) -> CounterVerdict:
    """Decide how to answer a buyer's counter-offer. Never reveals cost or floor."""
    band = state.band
    maxc = band.max_concessions
    ladder = _ladder(state.unit_cost, band.markup_min_pct, band.markup_max_pct, maxc)
    used = state.concessions_used
    current = state.current_offer_unit_price
    left = maxc - used if maxc > used else 0
    if ask_unit_price >= current:
        return CounterVerdict(kind=_ACCEPT, offer_unit_price=current, currency=state.currency,
                              concessions_left=left, traded_for=None, reason="")
    if ask_unit_price < ladder[-1]:
        return CounterVerdict(kind=_MANAGER, offer_unit_price=None, currency=state.currency,
                              concessions_left=left, traded_for=None, reason="below_range")
    if used >= maxc:
        return CounterVerdict(kind=_MANAGER, offer_unit_price=None, currency=state.currency,
                              concessions_left=0, traded_for=None, reason="concessions_exhausted")
    qty_up = ask_quantity is not None and ask_quantity > state.base_quantity
    if used >= 1 and not qty_up:
        return CounterVerdict(kind=_COUNTER, offer_unit_price=current, currency=state.currency,
                              concessions_left=left, traded_for=None, reason="ask_for_quantity")
    nxt = ladder[used + 1]
    hit = ask_unit_price >= nxt
    return CounterVerdict(kind=_ACCEPT if hit else _COUNTER,
                          offer_unit_price=ask_unit_price if hit else nxt, currency=state.currency,
                          concessions_left=left - 1,
                          traded_for="quantity_increase" if qty_up else "goodwill", reason="")


# ---------------------------------------------------------------- delivery
def _add_working_days(start: date, k: int, weekend: frozenset[int]) -> date:
    """start + k working days (days whose ISO weekday is not in weekend), computed arithmetically."""
    if k <= 0:
        return start
    wd = start.isoweekday()
    # calendar offsets (1..7) of the working days in the week after start
    offsets = [o for o in range(1, 8) if (wd + o - 1) % 7 + 1 not in weekend]
    if not offsets:
        raise ValueError("weekend covers every day of the week")
    weeks, rem = divmod(k - 1, len(offsets))
    return start + timedelta(days=7 * weeks + offsets[rem])


def estimate_delivery(i: DeliveryInputs) -> DeliveryEstimate:
    """Ready and delivery dates from stock, backlog, material wait, production and lane times."""
    qn, qd = i.quantity.as_integer_ratio()
    sn, sd = i.finished_stock.as_integer_ratio()
    need_n = qn * sd - sn * qd
    if need_n <= 0:
        ready = _add_working_days(i.today, i.packing_days, i.weekend)
        production = wait = 0
    else:
        ppd = i.pieces_per_day
        if ppd <= 0:
            raise ValueError("pieces_per_day must be positive")
        production = -((-need_n) // (qd * sd * ppd))
        wait = i.supplier_lead_days if i.material_shortfall else 0
        ready = _add_working_days(i.today + timedelta(days=wait),
                                  i.backlog_days + production + i.packing_days, i.weekend)
    return DeliveryEstimate(production_days=production, material_wait_days=wait, ready_date=ready,
                            delivery_date=ready + timedelta(days=i.transit_days + i.buffer_days))
