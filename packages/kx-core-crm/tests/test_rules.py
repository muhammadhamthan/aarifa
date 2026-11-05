"""Unit tests for pure CRM rules: stage machine, qualification, staleness, verdict mapping, content
hash, settings parsing (against the DDL seed values), PDF formatting and rendering."""

from __future__ import annotations

import pathlib
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from itertools import pairwise

import orjson
import pytest
from hypothesis import given
from hypothesis import strategies as st

from kx.core.contracts import CounterVerdict, CounterVerdictKind, DealStage
from kx.core.errors import RuleViolation
from kx.core.identity import priced_content_sha256 as content_sha256
from kx.core.identity import sha256_hex
from kx.crm.pdf import (
    DocLine,
    Letterhead,
    QuoteDocument,
    QuoteRenderer,
    fmt_unit_price,
    quote_message,
    weasyprint_available,
)
from kx.crm.rules import (
    OPEN_STAGES,
    TRANSITIONS,
    QualificationFacts,
    check_transition,
    missing_fields,
    pause_reason,
    round_verdict,
    staleness,
)
from kx.crm.services.settings import parse_settings

S = DealStage
NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)
DDL = pathlib.Path(__file__).resolve().parents[3] / "db" / "ddl" / "01_crm.sql"


# ---------------------------------------------------------------- stage machine
def test_happy_path_is_allowed() -> None:
    path = [S.NEW, S.QUALIFIED, S.QUOTATION, S.NEGOTIATION, S.READY_TO_CLOSE, S.PAYMENT]
    for a, b in pairwise(path):
        check_transition(a, b)
    check_transition(S.PAYMENT, S.WON, has_payment=True)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (S.NEW, S.QUOTATION),
        (S.QUALIFIED, S.WON),
        (S.WON, S.LOST),
        (S.LOST, S.NEW),
        (S.NEGOTIATION, S.PAYMENT),
        (S.NEW, S.NEW),
    ],
)
def test_illegal_moves_refused(a: DealStage, b: DealStage) -> None:
    with pytest.raises(RuleViolation):
        check_transition(a, b, lost_reason="price_too_high", has_payment=True)


@given(st.sampled_from(sorted(OPEN_STAGES)))
def test_any_open_stage_can_be_lost_only_with_reason(stage: DealStage) -> None:
    with pytest.raises(RuleViolation):
        check_transition(stage, S.LOST)
    with pytest.raises(RuleViolation):
        check_transition(stage, S.LOST, lost_reason="cheaper elsewhere")  # not in the closed set
    check_transition(stage, S.LOST, lost_reason="bought_competitor")


def test_won_needs_payment_and_terminal_stages() -> None:
    with pytest.raises(RuleViolation, match="advance payment"):
        check_transition(S.PAYMENT, S.WON)
    assert TRANSITIONS[S.WON] == TRANSITIONS[S.LOST] == frozenset()


# ---------------------------------------------------------------- qualification
def test_missing_fields_in_asking_order() -> None:
    facts = QualificationFacts(
        has_product=True,
        has_quantity=False,
        has_delivery_country=False,
        has_contact_name=True,
        has_phone_or_email=False,
    )
    assert missing_fields(facts) == ("quantity", "delivery_country", "contact_phone_or_email")
    assert missing_fields(QualificationFacts(True, True, True, True, True)) == ()


# ---------------------------------------------------------------- staleness (T12)
@pytest.mark.parametrize(
    ("age", "level"),
    [
        (timedelta(days=2, hours=23), "ok"),
        (timedelta(days=3), "warn"),
        (timedelta(days=6, hours=23), "warn"),
        (timedelta(days=7), "block"),
        (timedelta(days=40), "block"),
    ],
)
def test_staleness_levels(age: timedelta, level: str) -> None:
    assert staleness(NOW - age, NOW, 3, 7) == level


def test_never_approved_blocks() -> None:
    assert staleness(None, NOW, 3, 7) == "block"


# ---------------------------------------------------------------- verdict mapping
def _v(kind: CounterVerdictKind, traded: str | None = None, reason: str = "") -> CounterVerdict:
    return CounterVerdict(
        kind=kind,
        offer_unit_price=D("0.2"),
        currency="AED",
        concessions_left=1,
        traded_for=traded,
        reason=reason,
    )


def test_round_verdicts() -> None:
    assert round_verdict(_v(CounterVerdictKind.COUNTER, "goodwill")) == "concession"
    assert round_verdict(_v(CounterVerdictKind.COUNTER, reason="ask_for_quantity")) == "within_range"
    assert round_verdict(_v(CounterVerdictKind.ACCEPT)) == "accepted"
    assert round_verdict(_v(CounterVerdictKind.NEEDS_MANAGER, reason="below_range")) == "below_range"
    assert round_verdict(_v(CounterVerdictKind.NEEDS_MANAGER, reason="concessions_exhausted")) == "exception"
    assert pause_reason("below_range") == "below_range"
    assert pause_reason("buyer wants a human") == "exception"


# ---------------------------------------------------------------- content hash
def test_content_sha_binds_body_and_pdf() -> None:
    pdf_sha = sha256_hex(b"%PDF-1.7 ...")
    assert content_sha256("Dear Ahmed", pdf_sha) == sha256_hex(b"Dear Ahmed\n" + pdf_sha.encode())
    assert content_sha256("Dear Ahmed", pdf_sha) != content_sha256("Dear Ahmed ", pdf_sha)
    assert content_sha256("مرحبا", pdf_sha) == sha256_hex("مرحبا".encode() + b"\n" + pdf_sha.encode())


# ---------------------------------------------------------------- settings parsing
def _ddl_settings() -> dict[str, object]:
    rows = re.findall(r"\('([a-z_.]+)',\s+'(.*?)',\s+'", DDL.read_text(encoding="utf-8"))
    return {k: orjson.loads(v) for k, v in rows}


def test_ddl_seed_settings_parse() -> None:
    raw = _ddl_settings()
    assert len(raw) == 20
    s = parse_settings(raw, {"ai_enabled": True})
    assert s.approval_window_minutes == 120 and s.md_value_limit_aed is None
    assert s.followup_cadence_days == (2, 5, 10) and s.grant_ttl_hours == 48 and s.quote_validity_days == 7
    assert (s.stale_warn_days, s.stale_block_days) == (3, 7)
    assert s.alert_window().tz == "Asia/Dubai" and s.flag("ai_enabled") and not s.flag("missing")
    assert s.buyer_window("Asia/Colombo").start.hour == 9


def test_settings_reject_inconsistent_values() -> None:
    raw = _ddl_settings()
    for bad in (
        {"pricing.stale_block_days": 2},
        {"followup.cadence_days": [5, 2]},
        {"quote.validity_days": 0},
        {"approval.alert_hours": {"start": "22:00", "end": "08:00"}},
    ):
        with pytest.raises(RuleViolation):
            parse_settings({**raw, **bad})
    assert parse_settings({**raw, "approval.md_value_limit_aed": 50000}).md_value_limit_aed == D(50000)
    assert parse_settings({**raw, "unknown.key": 1}).approval_window_minutes == 120


# ---------------------------------------------------------------- PDF
def _doc(language: str = "en") -> QuoteDocument:
    return QuoteDocument(
        number="Q-2026-00001",
        version_no=1,
        issued_on=date(2026, 11, 4),
        valid_until=date(2026, 11, 11),
        currency="AED",
        language=language,
        buyer_name="Ahmed",
        buyer_company="Gulf Catering",
        lines=(DocLine(1, "Aluminium container 750 ml", D(50000), "pcs", D("0.227804"), D("11390.2000")),),
        subtotal=D("11390.2000"),
        vat_rate=D(5),
        vat_amount=D("569.5100"),
        total=D("11959.7100"),
        incoterm="EXW",
        clauses=("Advance payment is required.",),
        estimated_delivery=None,
        letterhead=Letterhead(name="Test Seller"),
    )


def test_formatting_and_message() -> None:
    assert fmt_unit_price(D("0.227804")) == "0.227804"
    assert fmt_unit_price(D("1.500000")) == "1.50"
    assert fmt_unit_price(D("1234.5")) == "1,234.50"
    subject, body = quote_message(_doc())
    assert "Q-2026-00001" in subject and "AED 11,959.71" in body and "2026-11-11" in body


def test_html_direction_per_language() -> None:
    r = QuoteRenderer()
    assert 'dir="rtl"' in r.html(_doc("ar")) and "عرض سعر" in r.html(_doc("ar"))
    html = r.html(_doc("en"))
    assert 'dir="ltr"' in html and "0.227804" in html and "11,959.71" in html


@pytest.mark.skipif(not weasyprint_available(), reason="WeasyPrint system libraries (Pango) not installed")
@pytest.mark.parametrize("language", ["en", "ar"])
async def test_pdf_renders(language: str) -> None:
    pdf = await QuoteRenderer().pdf(_doc(language))
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000


def test_uuid_ids_are_v7() -> None:
    from kx.core.identity import new_id

    assert new_id().version == 7 and isinstance(new_id(), uuid.UUID)
