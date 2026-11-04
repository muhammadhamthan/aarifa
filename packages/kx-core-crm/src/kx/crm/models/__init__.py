"""Typed ORM for the 45 tables of the `crm` database (mirrors db/ddl/01_crm.sql exactly)."""

from kx.crm.models.base import Base
from kx.crm.models.catalogue import (
    FreightLane,
    FxRate,
    LaneRiskHistory,
    MarginRuleRow,
    Material,
    MaterialRate,
    PriceSource,
    Product,
    ProductCategory,
    ProductCosting,
    SupplierLeadTime,
)
from kx.crm.models.customers import Company, Consent, Contact
from kx.crm.models.deals import Deal, DealRequirement, StageHistory, Task, TimelineEvent
from kx.crm.models.fulfilment import (
    FactoryVisit,
    Order,
    OrderStatusHistory,
    Payment,
    ReorderOpportunity,
    StockItem,
    StockMovement,
    VisitSlot,
)
from kx.crm.models.people import FeatureFlag, Setting, SettingHistory, User
from kx.crm.models.plumbing import AuditEvent, IdempotencyKey, InboxEvent, OutboxEvent, RptDailyMetric
from kx.crm.models.quotes import (
    Approval,
    ApprovalEvent,
    DeliveryEstimateRow,
    NegotiationRound,
    Quote,
    QuoteLine,
    QuoteLineCost,
    QuoteSendGrant,
    QuoteVersion,
)

metadata = Base.metadata

__all__ = [
    "Approval",
    "ApprovalEvent",
    "AuditEvent",
    "Base",
    "Company",
    "Consent",
    "Contact",
    "Deal",
    "DealRequirement",
    "DeliveryEstimateRow",
    "FactoryVisit",
    "FeatureFlag",
    "FreightLane",
    "FxRate",
    "IdempotencyKey",
    "InboxEvent",
    "LaneRiskHistory",
    "MarginRuleRow",
    "Material",
    "MaterialRate",
    "NegotiationRound",
    "Order",
    "OrderStatusHistory",
    "OutboxEvent",
    "Payment",
    "PriceSource",
    "Product",
    "ProductCategory",
    "ProductCosting",
    "Quote",
    "QuoteLine",
    "QuoteLineCost",
    "QuoteSendGrant",
    "QuoteVersion",
    "ReorderOpportunity",
    "RptDailyMetric",
    "Setting",
    "SettingHistory",
    "StageHistory",
    "StockItem",
    "StockMovement",
    "SupplierLeadTime",
    "Task",
    "TimelineEvent",
    "User",
    "VisitSlot",
    "metadata",
]
