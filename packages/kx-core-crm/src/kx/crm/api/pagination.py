"""Keyset pagination: opaque cursors over (sort key, id), stable under concurrent inserts and O(limit)."""

from __future__ import annotations

import base64
from typing import Any

import orjson
from pydantic import BaseModel
from sqlalchemy import Select, tuple_
from sqlalchemy.orm import InstrumentedAttribute

from kx.core.errors import RuleViolation

MAX_LIMIT = 100


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


def encode_cursor(*values: Any) -> str:
    return base64.urlsafe_b64encode(orjson.dumps(values, default=str)).decode().rstrip("=")


def decode_cursor(cursor: str, arity: int) -> list[Any]:
    try:
        values = orjson.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, orjson.JSONDecodeError) as exc:
        raise RuleViolation("invalid cursor") from exc
    if not isinstance(values, list) or len(values) != arity:
        raise RuleViolation("invalid cursor")
    return values


def keyset[R: Any](
    stmt: Select[R],
    cursor: str | None,
    limit: int,
    *keys: InstrumentedAttribute[Any],
    parse: tuple[Any, ...] = (),
) -> Select[R]:
    """Newest-first page after `cursor` over `keys` (all descending). `parse` converts cursor values."""
    stmt = stmt.order_by(*(k.desc() for k in keys)).limit(min(max(limit, 1), MAX_LIMIT) + 1)
    if cursor:
        raw = decode_cursor(cursor, len(keys))
        values = [p(v) if p else v for p, v in zip(parse or (None,) * len(keys), raw, strict=True)]
        stmt = stmt.where(tuple_(*keys) < tuple_(*values))
    return stmt


def page_of[T](rows: list[T], limit: int, cursor_of: Any) -> tuple[list[T], str | None]:
    """Split the limit+1 probe row off and build the next cursor from the last returned row."""
    limit = min(max(limit, 1), MAX_LIMIT)
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, encode_cursor(*cursor_of(rows[-1])) if more and rows else None
