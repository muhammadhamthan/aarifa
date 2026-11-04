"""SPEC for kx.core.price_guard (the judge for competing implementations).

The guard runs on EVERY AI reply (chat, voice transcript, WhatsApp, email). A reply may only state
amounts that appear in `allowed` (approved quote lines/totals, issued offers), compared at the
precision the speaker used (minimum 2 decimals). Percentages must be explicitly allowed.
API:
    extract_prices(text, default_currency=None) -> list[PriceMention]
    check_prices(text, allowed, *, default_currency=None, allowed_percentages=()) -> GuardResult
"""

from __future__ import annotations

import importlib
import os
from decimal import Decimal as D

import pytest

from kx.core.contracts import AllowedPrice

guard = importlib.import_module(os.environ.get("KX_PRICE_GUARD", "kx.core.price_guard"))

AED_UNIT = AllowedPrice(amount=D("0.227804"), currency="AED", label="unit price")
AED_TOTAL = AllowedPrice(amount=D("11390.20"), currency="AED", label="total")


def amounts(text: str, default: str | None = None) -> list[tuple[D, str | None]]:
    return [(m.amount, m.currency) for m in guard.extract_prices(text, default_currency=default)
            if not m.is_percentage]


# ---------------------------------------------------------------- extraction
@pytest.mark.parametrize(("text", "expected"), [
    ("The price is AED 0.227804 per piece", [(D("0.227804"), "AED")]),
    ("AED0.23 each", [(D("0.23"), "AED")]),
    ("0.23 AED each", [(D("0.23"), "AED")]),
    ("I can do 0.20 dirhams", [(D("0.20"), "AED")]),
    ("Dhs 1,250.50", [(D("1250.50"), "AED")]),
    ("US$ 3,100", [(D("3100"), "USD")]),
    ("$1,250.00 total", [(D("1250.00"), "USD")]),
    ("€1.250,50", [(D("1250.50"), "EUR")]),
    ("9,20 €", [(D("9.20"), "EUR")]),
    ("Rs. 8.20 per piece", [(D("8.20"), "INR")]),
    ("₹8.50", [(D("8.50"), "INR")]),
    ("INR 1,25,000", [(D("125000"), "INR")]),            # Indian grouping
    ("£ 12.40", [(D("12.40"), "GBP")]),
    ("SAR 15", [(D("15"), "SAR")]),
    ("价格 ¥1.60", [(D("1.60"), "CNY")]),
    ("RMB 2.10", [(D("2.10"), "CNY")]),
    ("الإجمالي ١١٬٣٩٠٫٢٠ درهم", [(D("11390.20"), "AED")]),   # Arabic-Indic digits
    ("₹८.५० प्रति पीस", [(D("8.50"), "INR")]),               # Devanagari digits
])
def test_extracts_amount_and_currency(text: str, expected: list[tuple[D, str]]) -> None:
    assert amounts(text) == expected


@pytest.mark.parametrize("text", [
    "We need 50,000 pieces of 750 ml containers",
    "Delivery around 2026-11-15, maybe 15/11/2026",
    "Call me on +971 50 123 4567 or 050-1234567",
    "Your quote is Q-2026-00012, version 3",
    "The meeting is at 10:30, container 40HC, 8011 alloy",
])
def test_ignores_non_prices(text: str) -> None:
    assert amounts(text) == []


def test_mention_spans_point_at_the_text() -> None:
    text = "Sure, AED 9.20 per piece."
    [m] = guard.extract_prices(text)
    assert text[m.start:m.end].replace(" ", "") in {"AED9.20", "9.20"}


def test_price_word_without_currency_uses_default() -> None:
    assert amounts("unit price 0.2177 per piece", default="AED") == [(D("0.2177"), "AED")]


def test_price_word_without_currency_and_no_default_has_unknown_currency() -> None:
    assert amounts("the rate is 0.2177") == [(D("0.2177"), None)]


def test_percentages_are_flagged_as_such() -> None:
    ms = guard.extract_prices("We can give a 5% discount, or 7.5 percent for 100k")
    pcts = sorted(m.amount for m in ms if m.is_percentage)
    assert pcts == [D("5"), D("7.5")]


# ---------------------------------------------------------------- checking
def test_exact_allowed_price_passes() -> None:
    assert guard.check_prices("The price is AED 0.227804 per piece", [AED_UNIT]).ok


def test_rounded_to_speaker_precision_passes() -> None:
    assert guard.check_prices("about AED 0.23 per piece", [AED_UNIT]).ok


def test_lower_amount_is_a_violation() -> None:
    r = guard.check_prices("best I can do is AED 0.22 per piece", [AED_UNIT])
    assert not r.ok and r.violations[0].amount == D("0.22")


def test_minimum_two_decimals_precision() -> None:
    assert not guard.check_prices("roughly AED 0.2 per piece", [AED_UNIT]).ok


def test_total_with_quantity_passes_and_quantity_is_ignored() -> None:
    r = guard.check_prices("Total: AED 11,390.20 for 50,000 pieces", [AED_UNIT, AED_TOTAL])
    assert r.ok and len(r.mentions) == 1


def test_arabic_total_passes() -> None:
    assert guard.check_prices("الإجمالي ١١٬٣٩٠٫٢٠ درهم", [AED_TOTAL]).ok


def test_currency_mismatch_is_a_violation() -> None:
    assert not guard.check_prices("USD 0.227804 per piece", [AED_UNIT]).ok


def test_unknown_currency_matches_any_allowed_amount() -> None:
    assert guard.check_prices("the rate is 0.2278", [AED_UNIT]).ok


def test_nothing_allowed_means_any_price_is_a_violation() -> None:
    assert not guard.check_prices("AED 0.227804", []).ok


def test_no_prices_is_ok() -> None:
    r = guard.check_prices("Thanks! We'll send the quote shortly.", [])
    assert r.ok and r.mentions == ()


def test_percentage_needs_permission() -> None:
    assert not guard.check_prices("we can give 5% off", [AED_UNIT]).ok
    assert guard.check_prices("we can give 5% off", [AED_UNIT], allowed_percentages=(D(5),)).ok


def test_multiple_mentions_reports_only_the_bad_one() -> None:
    r = guard.check_prices("Unit AED 0.227804, total AED 11,390.20, or AED 0.19 for cash", [AED_UNIT, AED_TOTAL])
    assert not r.ok and [v.amount for v in r.violations] == [D("0.19")]


# ---------------------------------------------------------------- speed
CORPUS = [
    "Hello Ahmed, the price for 50,000 pcs of the 750 ml container is AED 0.227804 per piece.",
    "الإجمالي ١١٬٣٩٠٫٢٠ درهم شامل الضريبة",
    "We can deliver around 2026-12-03 to Colombo; call +971 50 123 4567.",
    "Unit AED 0.227804, total AED 11,390.20, or AED 0.19 for cash",
] * 250


@pytest.mark.bench
def test_bench_check_1000_messages(benchmark) -> None:  # type: ignore[no-untyped-def]
    allowed = [AED_UNIT, AED_TOTAL]

    def run() -> int:
        return sum(1 for t in CORPUS if not guard.check_prices(t, allowed).ok)

    assert benchmark(run) == 250


# ---------------------------------------------------------------- fail-closed cases (added after the contest)
USD_FREIGHT = AllowedPrice(amount=D("3100"), currency="USD", label="freight per 40HC")
CONTEST_ALLOWED = [AED_UNIT, AED_TOTAL, USD_FREIGHT]


@pytest.mark.parametrize(("text", "ok"), [
    ("AED0.22/pc", False),
    ("price: 0.22 per pc", False),
    ("Our best: AED 0.19!", False),
    ("AED 0.2278.", True),
    ("AED 0.21 per piece", False),
    ("2 x 40HC at USD 3,100 each", True),
    ("2 x 40HC at USD 2,900 each", False),
    ("Unit price:\nAED 0.21", False),
    ("total AED 11,390.20 (incl. VAT)", True),
    ("total AED 11,390.19", False),
    ("سعر القطعة ٠٫٢١ درهم", False),
    ("سعر القطعة ٠٫٢٣ درهم", True),
    ("单价 ¥0.21", False),
    ("We need 120,000 pcs by 2026-12-01", True),
    ("Call +971 4 123 4567 for the price", True),
    ("price 0.22 AED", False),
    ("aed 0.21", False),
    ("Dh0.21", False),
    ("I can offer 10% discount", False),
    ("the rate is 0.23 per piece", True),
    ("AED 0.227804 x 50,000 pcs = AED 11,390.20", True),
    ("AED 0.227804 x 50,000 pcs = AED 10,000.00", False),
    ("Delivery 15/11/2026, 750 ml, 8011 alloy, Q-2026-00012", True),
    ("USD 3100", True),
    ("₹ 8.50/-", False),
    ("Okay, I can do 0.21 each", False),          # bare number + per-piece word
    ("0.21 per piece is my final", False),
    ("I could go to 0.21 if you confirm today", False),  # bare 2-decimal number: fail closed
    ("AED 0.22 or 0.21", False),
    ("AED 0.21-0.23", False),
])
def test_fail_closed_cases(text: str, ok: bool) -> None:
    assert guard.check_prices(text, CONTEST_ALLOWED, default_currency="AED").ok is ok
