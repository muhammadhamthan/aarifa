"""Quote PDF: jinja2 HTML (RTL-capable) -> WeasyPrint PDF, plus the buyer message that carries it.

The PDF bytes and the message body are hashed together into the send grant (see services.grants),
so what the MD approved is exactly what leaves the platform. Numbers are formatted from Decimal via
kx.core.money.display only: no float ever touches a printed price.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import cached_property
from importlib.resources import files
from typing import Any, Final, Protocol, runtime_checkable

from jinja2 import Environment, PackageLoader, StrictUndefined
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from kx.core.money import display

RTL_LANGUAGES: Final = frozenset({"ar", "fa", "ur", "he"})

LABELS: Final[dict[str, dict[str, str]]] = {
    "en": {
        "title": "Quotation",
        "to": "To",
        "date": "Date",
        "valid": "Valid until",
        "no": "#",
        "item": "Description",
        "qty": "Quantity",
        "unit_price": "Unit price",
        "amount": "Amount",
        "subtotal": "Subtotal",
        "vat": "VAT",
        "total": "Total",
        "terms": "Terms",
        "incoterm": "Incoterm",
        "delivery": "Estimated delivery",
        "delivery_note": "estimated, confirmed by our team",
        "version": "Version",
    },
    "ar": {
        "title": "عرض سعر",
        "to": "إلى",
        "date": "التاريخ",
        "valid": "صالح حتى",
        "no": "#",
        "item": "الوصف",
        "qty": "الكمية",
        "unit_price": "سعر الوحدة",
        "amount": "المبلغ",
        "subtotal": "المجموع الفرعي",
        "vat": "ضريبة القيمة المضافة",
        "total": "الإجمالي",
        "terms": "الشروط",
        "incoterm": "شروط التسليم",
        "delivery": "موعد التسليم التقديري",
        "delivery_note": "تقديري، يؤكده فريقنا",
        "version": "الإصدار",
    },
}

MESSAGES: Final[dict[str, tuple[str, str]]] = {
    "en": (
        "Quotation {number} from {seller}",
        "Dear {name},\n\nPlease find attached our quotation {number} for {currency} {total}, "
        "valid until {valid_until}. Advance payment is required to start production.\n\n"
        "Best regards,\n{seller}",
    ),
    "ar": (
        "عرض السعر {number} من {seller}",
        "عزيزي {name}،\n\nمرفق عرض السعر {number} بقيمة {currency} {total}، صالح حتى {valid_until}. "
        "يلزم دفع مقدم لبدء الإنتاج.\n\nمع أطيب التحيات،\n{seller}",
    ),
}


@dataclass(frozen=True, slots=True)
class Letterhead:
    """Seller identity printed on quotes; the client app fills it from company.yaml."""

    name: str = "Kairox"
    address: str = ""
    trn: str = ""
    phone: str = ""
    email: str = ""


@dataclass(frozen=True, slots=True)
class DocLine:
    line_no: int
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True, slots=True)
class QuoteDocument:
    """Everything printed on a quote. Built from frozen DB rows; contains no cost data by construction."""

    number: str
    version_no: int
    issued_on: date
    valid_until: date
    currency: str
    language: str
    buyer_name: str
    buyer_company: str | None
    lines: tuple[DocLine, ...]
    subtotal: Decimal
    vat_rate: Decimal
    vat_amount: Decimal
    total: Decimal
    incoterm: str
    clauses: tuple[str, ...]
    estimated_delivery: date | None
    letterhead: Letterhead = field(default_factory=Letterhead)

    @property
    def lang(self) -> str:
        return self.language if self.language in LABELS else "en"

    @property
    def direction(self) -> str:
        return "rtl" if self.language.split("-")[0] in RTL_LANGUAGES else "ltr"


def fmt_money(amount: Decimal, currency: str) -> str:
    return f"{display(amount, currency):,}"


def fmt_unit_price(amount: Decimal) -> str:
    """Per-piece prices keep up to 6 places (as approved) but at least 2: 0.227804, 1.50."""
    text = f"{amount.normalize():f}"
    whole, _, frac = text.partition(".")
    return f"{int(whole):,}.{frac.ljust(2, '0')[:6]}"


def fmt_qty(quantity: Decimal) -> str:
    return f"{quantity:,.0f}" if quantity == quantity.to_integral_value() else f"{quantity:,}"


def quote_message(doc: QuoteDocument) -> tuple[str, str]:
    """(subject, body) of the message that carries the PDF, in the buyer's language."""
    subject, body = MESSAGES.get(doc.lang, MESSAGES["en"])
    values = {
        "number": doc.number,
        "seller": doc.letterhead.name,
        "name": doc.buyer_name or "Customer",
        "currency": doc.currency,
        "total": fmt_money(doc.total, doc.currency),
        "valid_until": doc.valid_until.isoformat(),
    }
    return subject.format(**values), body.format(**values)


class QuoteRenderer:
    """Renders QuoteDocument -> HTML -> PDF. WeasyPrint is CPU-bound, so it runs in a worker thread."""

    @cached_property
    def _env(self) -> Environment:
        env = Environment(
            loader=PackageLoader("kx.crm", "templates"),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters.update(money=fmt_money, unit_price=fmt_unit_price, qty=fmt_qty)
        return env

    @cached_property
    def _css(self) -> str:
        return files("kx.crm").joinpath("templates/quote.css").read_text(encoding="utf-8")

    def html(self, doc: QuoteDocument) -> str:
        return self._env.get_template("quote.html.j2").render(doc=doc, t=LABELS[doc.lang], css=self._css)

    def pdf_sync(self, doc: QuoteDocument) -> bytes:
        from weasyprint import HTML  # type: ignore[import-untyped]  # noqa: PLC0415 - heavy (Pango), render only

        data: bytes = HTML(string=self.html(doc)).write_pdf()
        return data

    async def pdf(self, doc: QuoteDocument) -> bytes:
        return await asyncio.to_thread(self.pdf_sync, doc)


def weasyprint_available() -> bool:
    """True when WeasyPrint and its system libraries (Pango, HarfBuzz) load."""
    try:
        from weasyprint import HTML  # noqa: PLC0415 - probe must not fail at import time

        HTML(string="<p>ok</p>").write_pdf()
    except Exception:
        return False
    return True


@runtime_checkable
class PdfUploader(Protocol):
    """Stores a rendered PDF under `key`. The app injects S3PdfUploader; tests inject a fake."""

    async def upload(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> None: ...


class S3PdfUploader:
    """aioboto3 upload with KMS encryption and jittered retries for transient S3 errors."""

    def __init__(
        self, session: Any, bucket: str, region: str, kms_key_id: str | None = None, attempts: int = 4
    ) -> None:
        self._session = session  # aioboto3.Session (untyped library)
        self._bucket = bucket
        self._region = region
        self._kms_key_id = kms_key_id
        self._attempts = attempts

    async def upload(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> None:
        extra: dict[str, str] = {"ServerSideEncryption": "aws:kms"}
        if self._kms_key_id:
            extra["SSEKMSKeyId"] = self._kms_key_id
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential_jitter(initial=0.5, max=8),
            retry=retry_if_exception_type((OSError, TimeoutError)),
            reraise=True,
        ):
            with attempt:
                async with self._session.client("s3", region_name=self._region) as s3:
                    await s3.put_object(
                        Bucket=self._bucket, Key=key, Body=data, ContentType=content_type, **extra
                    )


def pdf_key(company_code: str, quote_number: str, version_no: int, pdf_sha256: str) -> str:
    """Content-addressed key: a re-render never overwrites the PDF a grant was issued for."""
    return f"{company_code}/quotes/{quote_number}/v{version_no}-{pdf_sha256[:16]}.pdf"
