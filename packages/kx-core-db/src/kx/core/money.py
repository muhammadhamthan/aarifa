"""Money and percentages with Decimal only. Never use float for prices."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Final

CENT: Final = Decimal("0.01")
MONEY_PLACES: Final = Decimal("0.0001")      # numeric(14,4) in the database
UNIT_PRICE_PLACES: Final = Decimal("0.000001")  # numeric(14,6) for per-piece prices
HUNDRED: Final = Decimal(100)
THOUSAND: Final = Decimal(1000)

# Currencies with 3 minor units are rare here; quote display uses 2 places.
DISPLAY_PLACES: Final[dict[str, Decimal]] = {"AED": CENT, "USD": CENT, "EUR": CENT, "INR": CENT,
                                             "LKR": CENT, "CNY": CENT, "GBP": CENT, "SAR": CENT}


def d(value: Decimal | int | str) -> Decimal:
    """Strict Decimal constructor: refuses float to keep money exact."""
    if isinstance(value, float):  # pragma: no cover - guarded at type level too
        raise TypeError("float is not allowed for money; pass str or Decimal")
    return value if isinstance(value, Decimal) else Decimal(value)


def q(value: Decimal, places: Decimal = MONEY_PLACES) -> Decimal:
    """Round half-up to the given exponent (default 4 places, matches numeric(14,4))."""
    with localcontext() as ctx:
        ctx.prec = 34
        return value.quantize(places, rounding=ROUND_HALF_UP)


def pct(value: Decimal) -> Decimal:
    """Percent -> fraction (40 -> 0.40)."""
    return value / HUNDRED


def display(amount: Decimal, currency: str) -> Decimal:
    return q(amount, DISPLAY_PLACES.get(currency.upper(), CENT))


def to_currency(amount_aed: Decimal, rate_to_aed: Decimal) -> Decimal:
    """Convert from AED using a rate expressed as 'AED per 1 unit of the currency'."""
    if rate_to_aed <= 0:
        raise ValueError("rate_to_aed must be positive")
    return amount_aed / rate_to_aed
