"""§7 / D22: the CMS (Payload) tells the platform what was published or unpublished.

`POST /webhooks/cms`, signed by the CMS with the shared secret (`KX_CMS_WEBHOOK_HMAC` in the CMS container,
`KX_APP_CMS_WEBHOOK_SECRET` here):

    X-Kx-Timestamp: <unix seconds>
    X-Kx-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + raw body)>

A request older than 5 minutes, or with a bad signature, is refused (401) and writes nothing. Each `event_id`
is handled once. What it raises, in the automation outbox (same transaction as the receipt):

* `kb.content_published.v1` per locale for content the AI may talk about ("the AI's knowledge is re-indexed
  automatically, so the AI only ever talks about published content"). On unpublish the indexer finds no
  published document and removes it from the knowledge base.
* `catalogue.product_published.v1` for products: the CRM mirrors the record by product code. "The CMS holds
  marketing content; the CRM holds prices, ranges, MOQ for quoting and lead times. Prices never appear in the
  CMS": any price-like key in the specification is dropped here, before anything is stored.
"""

from __future__ import annotations

import time
import uuid
from typing import Final, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kx.automation.outbox import emit
from kx.automation.repo import first_receipt
from kx.core.db import Databases
from kx.core.errors import RuleViolation, Unauthorized
from kx.core.event_payloads import KbContentPublished, ProductPublished
from kx.core.identity import constant_time_equals, hmac_sha256_hex
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)
MAX_SKEW_SECONDS: Final = 300
# Collections whose published text the AI may use (§7 content types; media, logos and settings are not text).
KB_COLLECTIONS: Final = frozenset(
    {"pages", "products", "product-categories", "faqs", "certificates", "tour-hotspots", "testimonials"}
)
_PRICE_WORDS: Final = ("price", "cost", "margin", "discount", "tier", "aed", "usd")


class CmsProduct(BaseModel):
    """The descriptive product record (§7 "Products" fields). No price field exists on purpose."""

    model_config = ConfigDict(extra="ignore")

    code: str = Field(min_length=1, max_length=64)  # = crm.products.sku, the shared product code
    name: str = Field(min_length=1, max_length=300)
    category_code: str | None = Field(default=None, max_length=64)
    category_name: str | None = Field(default=None, max_length=200)
    spec: dict[str, object] = Field(default_factory=dict)  # material, capacity, dimensions, colours ...
    custom_printing: bool | None = None


class CmsEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: uuid.UUID  # unique per publish action: the CMS retries with the same id
    operation: Literal["publish", "unpublish"]
    collection: str = Field(min_length=1, max_length=64)
    doc_id: str = Field(min_length=1, max_length=128)
    locales: list[str] = Field(default_factory=lambda: ["en"], min_length=1, max_length=10)
    url: str | None = Field(default=None, max_length=500)
    product: CmsProduct | None = None  # required for collection "products"


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac_sha256_hex(secret.encode(), timestamp.encode() + b"." + body)


def verify(secret: str | None, timestamp: str | None, sig: str | None, body: bytes, now: float) -> None:
    if not secret:
        raise Unauthorized("CMS webhook secret is not configured")
    if not timestamp or not sig or not timestamp.isdigit():
        raise Unauthorized("missing CMS signature")
    if abs(now - int(timestamp)) > MAX_SKEW_SECONDS:
        raise Unauthorized("CMS webhook too old")
    if not constant_time_equals(signature(secret, timestamp, body), sig):
        raise Unauthorized("bad CMS signature")


def descriptive_spec(spec: dict[str, object]) -> dict[str, object]:
    """Drop anything that looks like a price (D22: prices never come from, or go into, the CMS)."""
    return {k: v for k, v in spec.items() if not any(w in k.lower() for w in _PRICE_WORDS)}


async def record(databases: Databases, event: CmsEvent) -> bool:
    """One transaction: receipt + events. False when this `event_id` was already handled."""
    if event.collection == "products" and event.product is None:
        raise RuleViolation("a product event needs the product record", reason="cms_payload")
    async with databases.transaction(LogicalDb.AUTOMATION) as s:
        if not await first_receipt(s, "cms", str(event.event_id)):
            return False
        if event.collection in KB_COLLECTIONS:
            for locale in dict.fromkeys(event.locales):
                await emit(
                    s,
                    KbContentPublished(
                        collection=event.collection, doc_id=event.doc_id, locale=locale, url=event.url
                    ),
                )
        if event.product is not None:
            p = event.product
            await emit(
                s,
                ProductPublished(
                    code=p.code.strip(),
                    cms_id=event.doc_id,
                    name=p.name.strip(),
                    category_code=p.category_code,
                    category_name=p.category_name,
                    spec=descriptive_spec(p.spec),
                    published=event.operation == "publish",
                    custom_printing=p.custom_printing,
                ),
            )
    return True


def build_router(databases: Databases, secret: str | None) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"], include_in_schema=False)

    @router.post("/cms")
    async def cms_published(request: Request) -> Response:
        body = await request.body()
        verify(
            secret,
            request.headers.get("x-kx-timestamp"),
            request.headers.get("x-kx-signature"),
            body,
            time.time(),
        )
        try:
            event = CmsEvent.model_validate_json(body)
        except ValidationError as exc:
            raise RuleViolation("CMS payload not understood", reason="cms_payload") from exc
        fresh = await record(databases, event)
        log.info("cms.webhook", collection=event.collection, doc_id=event.doc_id, fresh=fresh)
        return Response(status_code=200)

    return router
