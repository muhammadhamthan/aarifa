"""Value types for the pricing engine (pure, no I/O). Implementations live in kx.crm.pricing.engine."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CostInputs:
    """Cost recipe for 1,000 pieces, all in the base currency (AED)."""

    material_rate_per_kg: Decimal
    kg_per_1000: Decimal
    yield_pct: Decimal              # 92 means 8% scrap
    production_per_1000: Decimal
    packaging_per_1000: Decimal
    freight_per_1000: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class FreightInputs:
    container_cost: Decimal         # per 40HC, base currency
    surcharge: Decimal              # war-risk / peak, per container
    buffer_pct: Decimal             # lane risk buffer, e.g. 10
    pcs_per_40hc: int | None        # pieces of this product that fit in one 40HC


@dataclass(frozen=True, slots=True)
class MarginRule:
    rule_id: uuid.UUID
    category_code: str
    customer_type: str | None
    min_qty: Decimal | None
    max_qty: Decimal | None
    markup_min_pct: Decimal
    markup_max_pct: Decimal
    max_concessions: int
    priority: int = 100


@dataclass(frozen=True, slots=True)
class MarginBand:
    markup_min_pct: Decimal
    markup_max_pct: Decimal
    max_concessions: int
    rule_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class QuoteLineInput:
    description: str
    quantity: Decimal
    cost: CostInputs
    unit: str = "pcs"
    product_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class PricedLine:
    description: str
    quantity: Decimal
    unit: str
    unit_cost: Decimal              # per piece, 6 dp (MD-only)
    markup_pct: Decimal
    unit_price: Decimal             # per piece, 6 dp
    line_total: Decimal             # 4 dp
    product_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class QuoteTotals:
    subtotal: Decimal
    vat_amount: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class NegotiationState:
    # per piece, UNROUNDED: unit_cost_per_1000(...) / 1000. Never PricedLine.unit_cost (6 dp) or the
    # ladder top drifts from the quoted price (0.227805 vs 0.227804).
    unit_cost: Decimal
    band: MarginBand
    concessions_used: int
    current_offer_unit_price: Decimal
    base_quantity: Decimal
    currency: str = "AED"


@dataclass(frozen=True, slots=True)
class DeliveryInputs:
    today: date
    quantity: Decimal
    pieces_per_day: int
    finished_stock: Decimal
    backlog_days: int               # working days of orders ahead
    material_shortfall: bool
    supplier_lead_days: int         # calendar days
    packing_days: int               # working days
    transit_days: int               # calendar days
    buffer_days: int                # calendar days (lane risk)
    weekend: frozenset[int] = field(default_factory=lambda: frozenset({7}))  # ISO weekdays off


@dataclass(frozen=True, slots=True)
class DeliveryEstimate:
    production_days: int
    material_wait_days: int
    ready_date: date
    delivery_date: date
