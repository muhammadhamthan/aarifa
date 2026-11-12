"""v1 price-list pricing (pure, no I/O). Business rules: the SRS §10 and §11.1, D6.

In v1 the manager enters prices: per product and quantity tier a **list price** (the AI always quotes it
first) and a **minimum** (the AI never goes below it). A category may set a minimum % below list instead;
a per-tier minimum overrides it. Prices are entered in AED; quotes in another currency convert at the
day's FX rate, and the negotiation range converts at the same rate (§10).

Rounding: unit prices 6 dp (numeric(14,6)), money 4 dp (numeric(14,4)), ROUND_HALF_UP, except a
*minimum*, which is always rounded UP so a rounded floor can never sit below the true one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Context, Decimal
from typing import Final

__all__ = [
    "ListLine",
    "PriceTier",
    "TierPrice",
    "convert_tier",
    "discount_from_list_pct",
    "list_line",
    "min_price_for",
    "resolve_tier",
]

_CTX: Final = Context(prec=34, rounding=ROUND_HALF_UP)
_UP: Final = Context(prec=34, rounding=ROUND_CEILING)
_Q4: Final = Decimal("0.0001")
_Q6: Final = Decimal("0.000001")
_HUNDRED: Final = Decimal(100)
_ONE: Final = Decimal(1)


@dataclass(frozen=True, slots=True)
class PriceTier:
    """One row of crm.product_price_tiers (AED). `max_qty` None = no upper limit ("50,000+")."""

    tier_id: uuid.UUID | None
    min_qty: Decimal
    max_qty: Decimal | None
    list_price: Decimal
    min_price: Decimal | None = None  # None: the category minimum % applies


@dataclass(frozen=True, slots=True)
class TierPrice:
    """The prices of the tier that contains a quantity; `min_price` None until `min_price_for` resolves it."""

    list_price: Decimal
    min_price: Decimal | None
    tier_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ListLine:
    """A quote line priced from the list (§11.1 steps 2-3), in the quote currency and in AED."""

    quantity: Decimal
    unit_price: Decimal  # 6 dp, quote currency
    line_total: Decimal  # 4 dp, quote currency
    unit_price_aed: Decimal
    line_total_aed: Decimal


def resolve_tier(tiers: Iterable[PriceTier], qty: Decimal) -> TierPrice:
    """§11.1 step 1: the tier that contains `qty` (bounds inclusive).

    Raises LookupError when no tier fits: below the lowest tier (MOQ) or in a gap between tiers. That is
    not the AI's call: the caller raises a change request for the salesperson (§13 "Quantity", D12)."""
    for t in tiers:
        if t.min_qty <= qty and (t.max_qty is None or qty <= t.max_qty):
            return TierPrice(list_price=t.list_price, min_price=t.min_price, tier_id=t.tier_id)
    raise LookupError(f"no price tier for quantity {qty}")


def min_price_for(tier: TierPrice, category_min_pct: Decimal | None) -> Decimal:
    """D6: the tier's own minimum wins; else list less the category %; else the list price itself.

    The last case is not written down in the business flow: with no minimum anywhere there is no range to
    negotiate in, so the floor is the list price and the AI cannot discount (docs/OPEN_QUESTIONS.md)."""
    if tier.min_price is not None:
        return min(tier.min_price, tier.list_price)
    if category_min_pct is None:
        return tier.list_price
    floor = tier.list_price * (_HUNDRED - category_min_pct) / _HUNDRED
    return _UP.quantize(floor, _Q6)


def convert_tier(tier: TierPrice, fx_to_aed: Decimal) -> TierPrice:
    """AED tier -> quote currency at `fx_to_aed` (AED per 1 unit). §10: "the negotiation range converts at
    the same rate". The list price rounds half-up (it is what the quote prints); the minimum rounds up."""
    if fx_to_aed <= 0:
        raise ValueError("fx_to_aed must be positive")
    if fx_to_aed == _ONE:
        return tier
    return TierPrice(
        list_price=_CTX.quantize(_CTX.divide(tier.list_price, fx_to_aed), _Q6),
        min_price=None
        if tier.min_price is None
        else _UP.quantize(_UP.divide(tier.min_price, fx_to_aed), _Q6),
        tier_id=tier.tier_id,
    )


def list_line(tier: TierPrice, qty: Decimal, fx_to_aed: Decimal = _ONE) -> ListLine:
    """§11.1: unit price = the tier's list price; line total = unit price x quantity.

    In another currency the line total is the AED line total at the printed rate (12,500.00 AED / 3.6725 =
    USD 3,403.68), so the converted quote equals the AED quote and the printed rate reproduces it."""
    unit_aed = tier.list_price
    total_aed = _CTX.quantize(unit_aed * qty, _Q4)
    if fx_to_aed == _ONE:
        return ListLine(qty, unit_aed, total_aed, unit_aed, total_aed)
    return ListLine(
        quantity=qty,
        unit_price=_CTX.quantize(_CTX.divide(unit_aed, fx_to_aed), _Q6),
        line_total=_CTX.quantize(_CTX.divide(total_aed, fx_to_aed), _Q4),
        unit_price_aed=unit_aed,
        line_total_aed=total_aed,
    )


def discount_from_list_pct(list_price: Decimal, agreed: Decimal) -> Decimal:
    """Discount from list in % (§21 MD dashboard "average discount from list"; §23 scenario 2: 5.3 %)."""
    if list_price <= 0:
        raise ValueError("list_price must be positive")
    return _CTX.divide((list_price - agreed) * _HUNDRED, list_price)
