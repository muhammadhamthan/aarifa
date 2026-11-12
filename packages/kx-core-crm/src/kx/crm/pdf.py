"""Quote PDF and Requirement Summary PDF: jinja2 HTML (RTL-capable) -> PDF, plus the buyer messages.

Engines (11 Nov, D37; platform docs Recommendation "Quote PDFs in Arabic"): headless Chromium on the
worker first, WeasyPrint as the backup. Every switch is logged and counted (`Kairox/VendorSwitch`,
field `pdf`).

The PDF bytes and the message body are hashed together into the send grant (see services.grants),
so what the MD approved is exactly what leaves the platform. Numbers are formatted from Decimal via
kx.core.money.display only: no float ever touches a printed price.
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import cached_property
from importlib.resources import files
from typing import Any, Final, Protocol, runtime_checkable

from jinja2 import Environment, PackageLoader, StrictUndefined
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from kx.core.logging import get_logger
from kx.core.metrics import emit_metric
from kx.core.money import display

log = get_logger(__name__)

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
        "delivery_time": "Delivery time",
        "place": "Delivery place",
        "payment": "Payment terms",
        "fx": "Exchange rate",
        "contact": "Your contact",
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
        "delivery_time": "مدة التسليم",
        "place": "مكان التسليم",
        "payment": "شروط الدفع",
        "fx": "سعر الصرف",
        "contact": "جهة الاتصال",
    },
}

# §11.4: short and natural, the PDF attached, inviting a reply, signed with the salesperson's name (D11).
MESSAGES: Final[dict[str, tuple[str, str]]] = {
    "en": (
        "Quotation {number} from {seller}",
        "Hi {name},\n\nHere is your {what} {number} for {summary}: {currency} {total}, valid until "
        "{valid_until}. Advance payment is required to start production.{extra}\n\n— {signer}",
    ),
    "ar": (
        "عرض السعر {number} من {seller}",
        "مرحباً {name}،\n\nإليك {what} رقم {number} لـ {summary}: {currency} {total}، صالح حتى {valid_until}. "
        "يلزم دفع مقدم لبدء الإنتاج.{extra}\n\n— {signer}",
    ),
}
WHAT: Final[dict[str, dict[str, str]]] = {
    "en": {"final": "final quotation", "revision": "updated quotation", "other": "quotation"},
    "ar": {"final": "عرض السعر النهائي", "revision": "عرض السعر المحدّث", "other": "عرض السعر"},
}
# D9 fallback when the AI wrote no cover line before approval: approved, natural, varied per quote.
OPEN_TO_DISCUSS: Final[dict[str, tuple[str, ...]]] = {
    "en": (
        "Better pricing is possible for larger quantities or repeat orders — just reply and we'll work it "
        "out together.",
        "If you're planning a bigger volume or regular orders, reply here and we can look at the price "
        "together.",
        "Happy to talk it through: if the quantity grows or you order regularly, let me know and I'll see "
        "what I can do.",
    ),
    "ar": (
        "يمكن تقديم سعر أفضل للكميات الأكبر أو الطلبات المتكررة — فقط رد على هذه الرسالة وسنتفق معاً.",
        "إذا كنت تخطط لكمية أكبر أو طلبات منتظمة، رد هنا وسننظر في السعر معاً.",
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
class SalesContact:
    """§11.2 "Contact: assigned salesperson's name, WhatsApp, email"."""

    name: str
    phone: str | None = None
    email: str | None = None


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
    kind: str = "initial"
    # §11.2 delivery and terms (D10: product default, salesperson sets or confirms)
    delivery_time_text: str | None = None
    delivery_place: str | None = None
    payment_terms_text: str | None = None
    fx_rate: Decimal | None = None  # AED per 1 unit of `currency`, printed when converted (§10)
    fx_rate_date: date | None = None
    salesperson: SalesContact | None = None
    # message only, never printed on the PDF (D9, §13.3)
    cover_note: str | None = None
    change_summary: str | None = None

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


def fmt_rate(rate: Decimal) -> str:
    """FX rate as printed: 3.6725 (no trailing zeros, at least 2 places)."""
    text = f"{rate.normalize():f}"
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(2, '0')}"


def fmt_qty(quantity: Decimal) -> str:
    return f"{quantity:,.0f}" if quantity == quantity.to_integral_value() else f"{quantity:,}"


def _summary(doc: QuoteDocument) -> str:
    if len(doc.lines) != 1:
        return f"{len(doc.lines)} items"
    line = doc.lines[0]
    return f"{fmt_qty(line.quantity)} {line.unit} {line.description.split(' — ')[0]}"


def open_to_discuss(doc: QuoteDocument) -> str:
    """D9: the AI's cover line, else one of the approved variants, picked per quote so it varies."""
    if doc.cover_note:
        return doc.cover_note
    variants = OPEN_TO_DISCUSS.get(doc.lang, OPEN_TO_DISCUSS["en"])
    return variants[sum(map(ord, f"{doc.number}/{doc.version_no}")) % len(variants)]


def quote_message(doc: QuoteDocument) -> tuple[str, str]:
    """(subject, body) of the message that carries the PDF, in the buyer's language (§11.4).

    The "open to discuss" line is in the message only, not in the PDF (D9); a revised quote says what
    changed (§13.3); the final quote has no discuss line (the price is agreed, §14)."""
    subject, body = MESSAGES.get(doc.lang, MESSAGES["en"])
    what = WHAT.get(doc.lang, WHAT["en"])
    extra = ""
    if doc.change_summary:
        extra += f"\n\n{doc.change_summary}"
    if doc.kind != "final":
        extra += f"\n\n{open_to_discuss(doc)}"
    seller = doc.letterhead.name
    values = {
        "number": doc.number,
        "seller": seller,
        "name": doc.buyer_name or "Customer",
        "what": what.get(doc.kind, what["other"]),
        "summary": _summary(doc),
        "currency": doc.currency,
        "total": fmt_money(doc.total, doc.currency),
        "valid_until": doc.valid_until.isoformat(),
        "extra": extra,
        "signer": f"{doc.salesperson.name}, {seller}" if doc.salesperson else seller,
    }
    return subject.format(**values), body.format(**values)


class PdfEngine(Protocol):
    name: str

    async def render(self, html: str) -> bytes: ...


class ChromiumEngine:
    """Headless Chromium `--print-to-pdf` (best Arabic shaping). Runs as a subprocess in /tmp, the only
    writable path on the read-only ECS task; `--no-sandbox` because the task runs as a non-root user
    without user namespaces, `--disable-dev-shm-usage` because Fargate's /dev/shm is small."""

    name = "chromium"

    def __init__(self, path: str, timeout_s: float = 30.0) -> None:
        self._path = path
        self._timeout = timeout_s

    async def render(self, html: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="kx-pdf-") as tmp:
            src, out = pathlib.Path(tmp) / "doc.html", pathlib.Path(tmp) / "doc.pdf"
            src.write_text(html, encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                self._path,
                "--headless",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={out}",
                src.as_uri(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await asyncio.wait_for(proc.communicate(), self._timeout)
            if proc.returncode != 0 or not out.exists():
                raise RuntimeError(f"chromium exited {proc.returncode}: {err.decode(errors='replace')[:300]}")
            return out.read_bytes()


class WeasyPrintEngine:
    name = "weasyprint"

    async def render(self, html: str) -> bytes:
        def run() -> bytes:
            from weasyprint import HTML  # type: ignore[import-untyped]  # noqa: PLC0415 - heavy (Pango)

            data: bytes = HTML(string=html).write_pdf()
            return data

        return await asyncio.to_thread(run)


async def render_pdf(html: str, engines: Sequence[PdfEngine], company: str = "") -> bytes:
    """First engine that works wins (D37: Chromium -> WeasyPrint). A switch is logged and counted."""
    last: Exception | None = None
    for i, engine in enumerate(engines):
        try:
            data = await engine.render(html)
        except Exception as exc:
            last = exc
            log.warning("pdf.engine_failed", engine=engine.name, error=str(exc)[:300])
            continue
        if i > 0:
            emit_metric("VendorSwitch", 1, company=company or "unknown", field="pdf", vendor=engine.name)
        return data
    raise RuntimeError("no PDF engine could render the document") from last


class QuoteRenderer:
    """Renders QuoteDocument / SummaryDocument -> HTML -> PDF through the engine chain (D37)."""

    def __init__(self, engines: Sequence[PdfEngine] | None = None, company: str = "") -> None:
        self.engines: tuple[PdfEngine, ...] = tuple(engines) if engines else (WeasyPrintEngine(),)
        self._company = company

    @cached_property
    def _env(self) -> Environment:
        env = Environment(
            loader=PackageLoader("kx.crm", "templates"),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters.update(money=fmt_money, unit_price=fmt_unit_price, qty=fmt_qty, rate=fmt_rate)
        return env

    @cached_property
    def _css(self) -> str:
        return files("kx.crm").joinpath("templates/quote.css").read_text(encoding="utf-8")

    def html(self, doc: QuoteDocument) -> str:
        return self._env.get_template("quote.html.j2").render(doc=doc, t=LABELS[doc.lang], css=self._css)

    def summary_html(self, doc: SummaryDocument) -> str:
        return self._env.get_template("requirement_summary.html.j2").render(
            doc=doc, t=SUMMARY_LABELS[doc.lang], css=self._css
        )

    async def pdf(self, doc: QuoteDocument) -> bytes:
        return await render_pdf(self.html(doc), self.engines, self._company)

    async def summary_pdf(self, doc: SummaryDocument) -> bytes:
        return await render_pdf(self.summary_html(doc), self.engines, self._company)


# ---------------------------------------------------------------- §6.5 / §8.2 Requirement Summary (no prices)
SUMMARY_LABELS: Final[dict[str, dict[str, str]]] = {
    "en": {
        "title": "Requirement Summary",
        "to": "For",
        "date": "Date",
        "ref": "Reference",
        "item": "Product",
        "qty": "Quantity",
        "spec": "Specification",
        "delivery": "Delivery",
        "needed_by": "Needed by",
        "next": "Next steps",
        "next_text": "Our sales team is preparing your quotation. You'll receive it on WhatsApp shortly.",
        "no_prices": "This summary lists what you asked for. Prices follow in your quotation.",
    },
    "ar": {
        "title": "ملخص الطلب",
        "to": "إلى",
        "date": "التاريخ",
        "ref": "المرجع",
        "item": "المنتج",
        "qty": "الكمية",
        "spec": "المواصفات",
        "delivery": "التسليم",
        "needed_by": "مطلوب بحلول",
        "next": "الخطوات التالية",
        "next_text": "يقوم فريق المبيعات لدينا بإعداد عرض السعر. ستصلك على واتساب قريباً.",
        "no_prices": "يعرض هذا الملخص ما طلبته. ستصلك الأسعار في عرض السعر.",
    },
}
# §8.2 step 2, worded as service: "Hi Ahmed, here's the summary of what we discussed — 50,000 aluminium
# containers, 750 ml with lid, delivery to Riyadh. Our team is preparing your quotation."
SUMMARY_CAPTION: Final[dict[str, str]] = {
    "en": "Hi {name}, here's the summary of what we discussed — {summary}. "
    "Our team is preparing your quotation.",
    "ar": "مرحباً {name}، إليك ملخص ما ناقشناه — {summary}. يقوم فريقنا بإعداد عرض السعر.",
}
_DELIVERY_TO: Final = {"en": "delivery to {place}", "ar": "التسليم إلى {place}"}


@dataclass(frozen=True, slots=True)
class SummaryLine:
    description: str
    code: str | None
    quantity: Decimal | None
    unit: str
    spec: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SummaryDocument:
    """What the buyer asked for, never a price (§6.5 "It never includes prices")."""

    reference: str
    issued_on: date
    language: str
    buyer_name: str
    buyer_company: str | None
    lines: tuple[SummaryLine, ...]
    delivery_country: str | None
    delivery_city: str | None
    needed_by: date | None
    letterhead: Letterhead

    @property
    def lang(self) -> str:
        return self.language if self.language in SUMMARY_LABELS else "en"

    @property
    def direction(self) -> str:
        return "rtl" if self.lang in RTL_LANGUAGES else "ltr"


def summary_phrase(doc: SummaryDocument) -> str:
    """ "50,000 Aluminium container 750 ml, delivery to Riyadh" (no prices)."""
    parts = [
        " ".join(x for x in (fmt_qty(ln.quantity) if ln.quantity else None, ln.description) if x)
        for ln in doc.lines
    ]
    place = doc.delivery_city or doc.delivery_country
    if place:
        parts.append(_DELIVERY_TO[doc.lang].format(place=place))
    return ", ".join(p for p in parts if p)


def summary_caption(doc: SummaryDocument) -> str:
    """The WhatsApp / email words that carry the summary (never a code, never "verify", §8.2 step 5)."""
    parts = [
        " ".join(x for x in (fmt_qty(ln.quantity) if ln.quantity else None, ln.description) if x)
        for ln in doc.lines
    ]
    place = doc.delivery_city or doc.delivery_country
    if place:
        parts.append(_DELIVERY_TO[doc.lang].format(place=place))
    return SUMMARY_CAPTION[doc.lang].format(
        name=doc.buyer_name or "", summary=", ".join(p for p in parts if p)
    )


def summary_key(company_code: str, reference: str, pdf_sha256: str) -> str:
    return f"{company_code}/summaries/{reference}-{pdf_sha256[:12]}.pdf"


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
