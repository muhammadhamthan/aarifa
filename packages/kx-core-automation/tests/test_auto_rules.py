"""Pure business rules: follow-up window + weekly cap, content hash, template rendering, price parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kx.automation.contact_hours import WEEK, cap_conflict, next_followup_slot
from kx.automation.outbound import content_sha256, render_template
from kx.automation.scraping import ScrapeError, extract, parse_price
from kx.automation.settings import AutomationSettings, PriceSourceConfig
from kx.core.errors import RuleViolation
from kx.core.identity import sha256_hex

DUBAI = ZoneInfo("Asia/Dubai")
WINDOW = AutomationSettings().followup_window("Asia/Dubai")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=DUBAI).astimezone(UTC)


def test_followup_waits_for_buyer_morning() -> None:
    assert next_followup_slot(at(3, 20), [], WINDOW, 2) == at(4, 9)
    assert next_followup_slot(at(3, 11), [], WINDOW, 2) == at(3, 11)
    india = AutomationSettings().followup_window("Asia/Kolkata")
    assert next_followup_slot(at(3, 7), [], india, 2).astimezone(ZoneInfo("Asia/Kolkata")).hour == 9


def test_third_followup_in_a_week_moves_out() -> None:
    sent = [at(2, 10), at(5, 10)]  # day 2 and day 5
    assert cap_conflict(at(6, 10), sent, 2) == at(9, 10)
    assert next_followup_slot(at(6, 10), sent, WINDOW, 2) == at(9, 10)
    assert next_followup_slot(at(12, 10), sent, WINDOW, 2) == at(12, 10)  # day 10 fits


def test_cap_counts_future_scheduled_ones() -> None:
    assert next_followup_slot(at(3, 10), [at(5, 10), at(6, 10)], WINDOW, 2) == at(
        12, 10
    )  # 7 days after day 5


@settings(max_examples=150, deadline=None)
@given(
    due_offset=st.integers(0, 60 * 24 * 20),
    offsets=st.lists(st.integers(-60 * 24 * 10, 60 * 24 * 20), max_size=6),
    max_n=st.integers(1, 3),
)
def test_slot_respects_window_and_cap(due_offset: int, offsets: list[int], max_n: int) -> None:
    base = at(1, 0)
    due = base + timedelta(minutes=due_offset)
    existing = [base + timedelta(minutes=o) for o in offsets]
    slot = next_followup_slot(due, existing, WINDOW, max_n)
    assert slot >= due and WINDOW.contains(slot)
    # brute force: no 7-day span containing the slot holds more than max_n messages including it
    for start in sorted({slot - WEEK + timedelta(microseconds=1), *(e for e in existing if e <= slot)}):
        if start > slot or slot - start >= WEEK:
            continue
        inside = [e for e in existing if start <= e < start + WEEK]
        assert len(inside) + 1 <= max_n


def test_content_hash_canonical_form() -> None:
    assert content_sha256("Quote Q-1", None) == sha256_hex(b"Quote Q-1")
    pdf = b"%PDF quote"
    assert content_sha256("Quote Q-1", pdf) == sha256_hex(b"Quote Q-1\n" + sha256_hex(pdf).encode())


def test_render_template_positional_and_named() -> None:
    assert render_template("Quote {{1}} total {{ 2 }}", [], ("Q-1", "AED 5")) == "Quote Q-1 total AED 5"
    assert render_template("Hi {{name}}", ["name"], ("Ahmed",)) == "Hi Ahmed"
    with pytest.raises(RuleViolation):
        render_template("Quote {{1}} {{2}}", [], ("Q-1",))


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("USD 2,345.50 / t", D("2345.50")),
        ("1 234,5", D("1234")),
        ("Price: 1980", D("1980")),
        (" ٢٣٤٥.٥", D("2345.5")),
        ("2 610.00 USD", D("2610.00")),
    ],
)
def test_parse_price(text: str, value: D) -> None:
    assert parse_price(text) == value


def test_parse_price_rejects_missing_or_zero() -> None:
    with pytest.raises(ScrapeError):
        parse_price("n/a")
    with pytest.raises(ScrapeError):
        parse_price("0.00")


SOURCE = PriceSourceConfig(
    code="lme_al",
    source_ref="0190f000-0000-7000-8000-000000000001",  # type: ignore[arg-type]
    url="https://prices.example.com/al",
    material_code="AL_INGOT",  # type: ignore[arg-type]
    price_selector="td.cash",
    currency="usd",
    unit="tonne",
    date_selector="span.asof",
    terms_ok=True,
)


def test_extract_with_source_date() -> None:
    html = "<table><tr><td class=cash>2,610.50</td></tr></table><span class=asof>2026-11-02 close</span>"
    assert extract(html, SOURCE, at(3, 9).date()) == (D("2610.50"), at(2, 9).date(), "2,610.50")
    with pytest.raises(ScrapeError):
        extract("<p>moved</p>", SOURCE, at(3, 9).date())
