"""WP6: what the PDF prints and what the message says (the SRS §10, §11.2, §11.4, §13.3; D9-D11).

Done when: the PDF renders the new fields, and the guard still passes on the generated message.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from kx.core.contracts import AllowedPrice
from kx.core.price_guard import check_prices
from kx.crm.pdf import (
    OPEN_TO_DISCUSS,
    DocLine,
    Letterhead,
    QuoteDocument,
    QuoteRenderer,
    SalesContact,
    quote_message,
    weasyprint_available,
)

COVER = "If you can grow the order or plan repeat runs, reply and we'll look at the price together."
DOC = QuoteDocument(
    number="Q-2026-00012",
    version_no=1,
    issued_on=date(2026, 11, 5),
    valid_until=date(2026, 11, 12),
    currency="USD",
    language="en",
    buyer_name="Ahmed",
    buyer_company="Gulf Catering",
    lines=(
        DocLine(
            1,
            "Aluminium container 750 ml + lid (ALC-750-L) — lid: yes",
            D(50_000),
            "pcs",
            D("0.068074"),
            D("3403.6760"),
        ),
    ),
    subtotal=D("3403.6760"),
    vat_rate=D(0),
    vat_amount=D(0),
    total=D("3403.6760"),
    incoterm="DAP",
    clauses=("Advance payment is required before production starts.",),
    estimated_delivery=None,
    letterhead=Letterhead(name="Arifa Pack"),
    delivery_time_text="10–14 working days after advance payment",
    delivery_place="Riyadh warehouse",
    payment_terms_text="50% advance, balance before dispatch",
    fx_rate=D("3.67250000"),
    fx_rate_date=date(2026, 11, 5),
    salesperson=SalesContact(name="Sara", phone="+971501110003", email="sara@arifapack.ae"),
    cover_note=COVER,
)
ALLOWED = (
    AllowedPrice(amount=D("3403.6760"), currency="USD", label="total"),
    AllowedPrice(amount=D("0.068074"), currency="USD", label="unit"),
)


def test_pdf_html_prints_delivery_terms_fx_and_the_salesperson_but_not_the_cover_line() -> None:
    html = QuoteRenderer().html(DOC)
    for text in (
        "Delivery time",
        "10–14 working days after advance payment",
        "Riyadh warehouse",
        "Payment terms",
        "50% advance, balance before dispatch",
        "1 USD = 3.6725 AED (2026-11-05)",  # §10: the rate and its date are printed
        "Sara",
        "+971501110003",
        "DAP",
    ):
        assert text in html, text
    assert COVER not in html  # D9: message only
    for variant in OPEN_TO_DISCUSS["en"]:
        assert variant not in html


def test_arabic_pdf_is_right_to_left_with_arabic_labels() -> None:
    html = QuoteRenderer().html(replace(DOC, language="ar"))
    assert 'dir="rtl"' in html and "مدة التسليم" in html and "شروط الدفع" in html and "سعر الصرف" in html


@pytest.mark.skipif(not weasyprint_available(), reason="WeasyPrint system libraries not installed")
async def test_pdf_renders() -> None:
    assert (await QuoteRenderer().pdf(DOC)).startswith(b"%PDF")


def test_message_is_signed_by_the_salesperson_and_invites_a_reply() -> None:
    subject, body = quote_message(DOC)
    assert subject == "Quotation Q-2026-00012 from Arifa Pack"
    assert body.startswith("Hi Ahmed,")
    assert "50,000 pcs Aluminium container 750 ml + lid (ALC-750-L)" in body
    assert COVER in body and body.rstrip().endswith("— Sara, Arifa Pack")
    assert check_prices(body, ALLOWED, default_currency="USD").ok  # the guard passes on the generated message


def test_no_cover_line_from_the_ai_falls_back_to_varied_approved_lines() -> None:
    lines = {quote_message(replace(DOC, cover_note=None, number=f"Q-2026-{n:05d}"))[1] for n in range(1, 9)}
    used = {v for v in OPEN_TO_DISCUSS["en"] for body in lines if v in body}
    assert len(used) >= 2  # different wording on different quotes (§11.4 "never like a template")


def test_final_quote_has_no_discuss_line_and_a_revision_says_what_changed() -> None:
    final = quote_message(replace(DOC, kind="final"))[1]
    assert "final quotation" in final and COVER not in final
    revised = quote_message(
        replace(DOC, kind="revision", change_summary="Updated: delivery in 5 working days from stock.")
    )[1]
    assert "updated quotation" in revised and "Updated: delivery in 5 working days from stock." in revised
