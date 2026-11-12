"""WP11 Excel price-list row validation (pure: no database, so it also runs without KX_TEST_PG_DSN)."""

from __future__ import annotations

from decimal import Decimal as D

from kx.crm.services.price_lists import parse_rows

SKUS = {"ALC-750-L", "ALU-750"}


# ---------------------------------------------------------------- Excel row validation (pure)
def test_import_rows_are_validated_before_anything_is_saved() -> None:
    rows = [
        ("ALC-750-L", 5000, 9999, "0.32", "0.29", "AED"),
        ("ALC-750-L", 9000, 49999, "0.28", "0.255", "AED"),  # overlaps row 2
        ("ALC-750-L", 50000, None, 0.25, 0.30, None),  # minimum above list (floats from Excel)
        ("NOPE-1", 1000, None, "0.5", None, "AED"),  # unknown code
        ("ALU-750", "x", None, "0.5", None, "USD"),  # not a number, not AED
        (None, None, None, None, None, None),  # blank row: skipped
    ]
    parsed, errors = parse_rows(rows, SKUS)
    got = {(e.row, e.field) for e in errors}
    assert {(3, "min_qty"), (4, "min_price"), (5, "sku"), (6, "min_qty"), (6, "currency")} <= got
    assert parsed[2].list_price == D("0.25") and parsed[2].min_price == D("0.3")  # exact, not 0.25000000001


def test_valid_rows_have_no_errors() -> None:
    rows = [
        ("ALC-750-L", 5000, 9999, "0.32", "0.29", "AED"),
        ("ALC-750-L", 10000, 49999, "0.28", "0.255", "AED"),
        ("ALC-750-L", 50000, None, "0.25", "0.23", "AED"),
    ]
    parsed, errors = parse_rows(rows, SKUS)
    assert errors == [] and len(parsed) == 3
