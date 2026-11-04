"""Price guard (candidate A): find every money amount in an AI reply and reject unapproved ones.

Design
------
* Native digits (Arabic-Indic, Extended Arabic-Indic, Devanagari, full-width) and the Arabic
  decimal/thousands separators are mapped to ASCII with one ``str.translate`` (only when the text
  contains any). Every mapping is one code point -> one code point, so spans found in the
  translated text are valid in the original text.
* One master ``re`` pattern scans the text. It starts with a single character class (``[\\d+]``),
  which lets ``re`` skip digit-free stretches inside its C search loop. It captures an
  international phone number (ignored), or a whole number token (possessive: never a partial
  number), plus either a joiner (``-``, ``/``, ``:`` followed by a digit) or the next word/symbol.
* Joiners trigger an anchored match of the date / time / phone-run patterns; the word after a
  number is classified with one dict lookup (percent / unit / multiplier / currency).
* The prefix currency is the last whitespace-separated token before the number, looked up in a dict
  (Arabic / CJK / Devanagari forms fall back to an end-anchored regex).
* A bare number is a price only after a price keyword in the same clause (~30 chars), before a
  per-piece marker (``/pc``, ``/-``), or right after a priced amount joined by ``-``/``to``/``or``.
* Checking compares each mention to the allowed amounts quantised (ROUND_HALF_UP) to the precision
  the speaker used (minimum 2 places); quantised sets are built lazily per (call, precision).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Final

from kx.core.contracts import AllowedPrice, GuardResult, PriceMention

__all__ = ["check_prices", "extract_prices"]

# ------------------------------------------------------------------ digit normalisation
_TRANSLATE: Final[dict[int, int]] = {
    **{0x0660 + i: 0x30 + i for i in range(10)},  # Arabic-Indic
    **{0x06F0 + i: 0x30 + i for i in range(10)},  # Extended Arabic-Indic (Persian / Urdu)
    **{0x0966 + i: 0x30 + i for i in range(10)},  # Devanagari
    **{0xFF10 + i: 0x30 + i for i in range(10)},  # full-width
    0x066B: 0x2E,  # ARABIC DECIMAL SEPARATOR -> '.'
    0x066C: 0x2C,  # ARABIC THOUSANDS SEPARATOR -> ','
}
_NATIVE: Final = re.compile("[٠-٩٫٬۰-۹०-९０-９]")

# ------------------------------------------------------------------ vocabulary
_CURRENCY: Final[dict[str, str]] = {}
for _code, _tokens in {
    "AED": ("aed", "dhs", "dh", "dirham", "dirhams", "درهم", "دراهم", "د.إ"),
    "USD": ("usd", "us$", "$", "dollar", "dollars", "美元"),
    "EUR": ("eur", "euro", "euros", "€", "欧元"),
    "INR": ("inr", "rs", "rs.", "rupee", "rupees", "₹", "रु", "रु.", "रुपये", "रुपए"),
    "CNY": ("cny", "rmb", "yuan", "¥", "元", "人民币"),
    "SAR": ("sar", "riyal", "riyals", "rial", "rials", "ريال", "ر.س"),
    "GBP": ("gbp", "£", "英镑"),
}.items():
    for _tok in _tokens:
        _CURRENCY[_tok] = _code
_SYMBOL_CURRENCY: Final = {k: v for k, v in _CURRENCY.items() if len(k) == 1}

# Non-ASCII currency forms (as suffix: matched at the word start; as prefix: at the window end).
_NA_FORMS: Final = r"人民币|美元|欧元|英镑|元|درهم|دراهم|د\.إ|ريال|ر\.س|रुपये|रुपए|रु\.?"
_NA_SUFFIX: Final = re.compile(_NA_FORMS)
_NA_PREFIX: Final = re.compile(r"(" + _NA_FORMS + r"|[$€₹¥£])\s{0,3}\Z")

_K: Final = Decimal(1000)
_MILLION: Final = Decimal(1000000)
_UNIT_WORDS: Final = (
    "pcs",
    "pc",
    "piece",
    "pieces",
    "unit",
    "units",
    "nos",
    "ml",
    "l",
    "ltr",
    "ltrs",
    "litre",
    "litres",
    "liter",
    "liters",
    "kg",
    "kgs",
    "g",
    "gm",
    "gms",
    "gsm",
    "mm",
    "cm",
    "m",
    "micron",
    "microns",
    "mic",
    "k",
    "ton",
    "tons",
    "tonne",
    "tonnes",
    "mt",
    "hc",
    "ft",
    "ctn",
    "ctns",
    "carton",
    "cartons",
    "box",
    "boxes",
    "container",
    "containers",
    "pallet",
    "pallets",
    "roll",
    "rolls",
    "bag",
    "bags",
    "bottle",
    "bottles",
    "set",
    "sets",
    "pack",
    "packs",
    "pair",
    "pairs",
    "sheet",
    "sheets",
    "dozen",
    "dozens",
    "day",
    "days",
    "week",
    "weeks",
    "month",
    "months",
    "year",
    "years",
    "hr",
    "hrs",
    "hour",
    "hours",
    "min",
    "mins",
    "minute",
    "minutes",
)
_MULTIPLIERS: Final[dict[str, Decimal]] = {
    "thousand": _K,
    "lakh": Decimal(100000),
    "lakhs": Decimal(100000),
    "lac": Decimal(100000),
    "lacs": Decimal(100000),
    "mn": _MILLION,
    "million": _MILLION,
    "millions": _MILLION,
    "cr": Decimal(10000000),
    "crore": Decimal(10000000),
    "crores": Decimal(10000000),
    "bn": Decimal(1000000000),
    "billion": Decimal(1000000000),
    "billions": Decimal(1000000000),
}
# Units that act as multipliers when a currency is attached ("AED 100k", "USD 2.5m").
_UNIT_AS_MULT: Final[dict[str, Decimal]] = {"k": _K, "m": _MILLION}

# Word after a number -> (kind, payload). kinds: P percent, U unit, X multiplier, C currency
_TAIL: Final[dict[str, tuple[str, Any]]] = {
    **{u: ("U", _UNIT_AS_MULT.get(u)) for u in _UNIT_WORDS},
    **{w: ("X", v) for w, v in _MULTIPLIERS.items()},
    **{w: ("C", c) for w, c in _CURRENCY.items() if w.isascii() and "." not in w},
    **{w: ("C", c) for w, c in _SYMBOL_CURRENCY.items()},
    "%": ("P", None),
    "٪": ("P", None),
    "percent": ("P", None),
    "pct": ("P", None),
    "per": ("?", None),
    "us": ("?", None),
}

_SP: Final = "[ \\t\\u00a0\\u202f]?"
_WS: Final = " \t\n  "
_ASCII_LETTERS: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_LEAD_PUNCT: Final = "([{<\"'*:;,/|-=~"

# Groups: 1 number tail, 2 joiner, 3 next word/symbol
_MASTER: Final = re.compile(
    r"[\d+]"
    r"(?:(?<=\+)\d{1,3}(?:[ \-]?\d{2,4}){2,}(?!\d)"  # +971 50 123 4567 (ignored)
    r"|(?<=\d)(\d*+(?:[.,]\d++)*+)"  # rest of the number token
    r"(?:([-/:])(?=\d)|" + _SP + r"([^\W\d_]+|[%٪$€₹¥£]))?)"
)
_NOT_DIGIT_AFTER: Final = r"(?![.,]?\d)"
# Anchored at a number that is followed by a joiner. Group 1: digit-hyphen run (phone / code).
_SKIP: Final = re.compile(
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    + _NOT_DIGIT_AFTER  # 2026-11-15
    + r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
    + _NOT_DIGIT_AFTER  # 15/11/2026, 15-11-26
    + r"|\d{1,2}:\d{2}(?::\d{2})?"
    + _NOT_DIGIT_AFTER  # 10:30
    + r"|(?=(?:-?\d){7})(\d++(?:-\d++)++)"
    + _NOT_DIGIT_AFTER  # 050-1234567
)
_DOT_DATE: Final = re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}|\d{1,2}\.\d{1,2}\.\d{2,4}")
_PER_CENT: Final = re.compile(r"[ \t]?(?i:cent)(?!\w)")
_AFTER_MULT: Final = re.compile(
    _SP + r"(?:((?i:aed|dhs?|dirhams?|usd|eur|euros?|inr|rs|rupees?|cny|rmb|yuan|sar|riyals?|rials?|"
    r"gbp|dollars?))(?!\w)|((?i:us)\$|[$€₹¥£]|" + _NA_FORMS + r"))"
)

# Price keyword in the same clause, at most ~30 chars before the number.
_KEYWORD: Final = re.compile(
    r"(?:(?<!\w)(?i:prices?|priced|pricing|rates?|costs?|costing|totals?|amounts?|quoted?\s+at|"
    r"charges?|fees?)(?!\w)|سعر|أسعار|اسعار|إجمالي|اجمالي|مبلغ|تكلفة|价格|价钱|单价|总价|报价|金额|"
    r"कीमत|मूल्य|दाम|விலை)"
    r"(?:(?:[^\n;!?.]|\.(?=\d)){0,30}|[ \t]*[:\-–]?[ \t]*\n\s*)\Z"
)
_KEYWORD_WINDOW: Final = 48
_keyword_search = _KEYWORD.search
# Bare number directly followed by a per-piece marker ("0.22/pc", "8.50/-", "0.22 per piece").
_PER_PIECE: Final = re.compile(
    r"[ \t]?(?:/-|/[ \t]?(?i:pcs?|pieces?|units?)(?!\w)|(?i:per[ \t]+(?:pcs?|pieces?|units?))(?!\w))"
)
# Bare number continuing a priced range/alternative: "AED 12.50-15.00", "AED 0.22 or 0.21".
_CONNECTOR: Final = re.compile(r"\s*(?:[-–—~/]|(?i:to|or|and))\s*")


# ------------------------------------------------------------------ fast model construction
# The values we build are already exactly typed, so pydantic re-validation is pure overhead.
# Instances are built the way `model_construct` does it; an import-time self-check falls back to
# the validating constructors if this pydantic version lays models out differently.
_new = object.__new__
_oset = object.__setattr__
_PM_FIELDS: Final = frozenset(PriceMention.model_fields)
_GR_FIELDS: Final = frozenset(GuardResult.model_fields)


def _fast_mention(
    amount: Decimal,
    currency: str | None,
    start: int,
    end: int,
    raw: str,
    is_percentage: bool,
) -> PriceMention:
    m = _new(PriceMention)
    _oset(
        m,
        "__dict__",
        {
            "amount": amount,
            "currency": currency,
            "start": start,
            "end": end,
            "raw": raw,
            "is_percentage": is_percentage,
        },
    )
    _oset(m, "__pydantic_fields_set__", set(_PM_FIELDS))
    _oset(m, "__pydantic_extra__", None)
    _oset(m, "__pydantic_private__", None)
    return m


def _fast_result(
    ok: bool, mentions: tuple[PriceMention, ...], violations: tuple[PriceMention, ...]
) -> GuardResult:
    r = _new(GuardResult)
    _oset(r, "__dict__", {"ok": ok, "mentions": mentions, "violations": violations})
    _oset(r, "__pydantic_fields_set__", set(_GR_FIELDS))
    _oset(r, "__pydantic_extra__", None)
    _oset(r, "__pydantic_private__", None)
    return r


def _slow_mention(
    amount: Decimal,
    currency: str | None,
    start: int,
    end: int,
    raw: str,
    is_percentage: bool,
) -> PriceMention:
    return PriceMention(
        amount=amount, currency=currency, start=start, end=end, raw=raw, is_percentage=is_percentage
    )


def _slow_result(
    ok: bool, mentions: tuple[PriceMention, ...], violations: tuple[PriceMention, ...]
) -> GuardResult:
    return GuardResult(ok=ok, mentions=mentions, violations=violations)


def _fast_path_is_sound() -> bool:
    try:
        args: tuple[Any, ...] = (Decimal("1.50"), "AED", 0, 8, "AED 1.50", False)
        fm, sm = _fast_mention(*args), _slow_mention(*args)
        fr, sr = _fast_result(False, (fm,), (fm,)), _slow_result(False, (sm,), (sm,))
        return (
            fm == sm
            and hash(fm) == hash(sm)
            and fm.model_dump() == sm.model_dump()
            and fr == sr
            and fr.model_dump() == sr.model_dump()
            and repr(fm) == repr(sm)
            and fm.model_fields_set == sm.model_fields_set
        )
    except Exception:  # any surprise means: use the validating constructors
        return False


if _fast_path_is_sound():
    _mention, _result = _fast_mention, _fast_result
else:  # pragma: no cover - depends on the installed pydantic
    _mention, _result = _slow_mention, _slow_result

_OK_EMPTY: Final = _result(True, (), ())


# ------------------------------------------------------------------ number parsing
def _normalise(n: str) -> str:
    """'1,250.50' / '1.250,50' / '1,25,000' / '9,20' -> canonical 'digits[.digits]'."""
    if "," not in n:
        if n.count(".") <= 1:
            return n
        return n.replace(".", "")  # 1.250.000 -> thousands
    if "." not in n:
        head, _, tail = n.partition(",")
        if "," not in tail:  # single comma
            if len(tail) == 3 and head != "0":
                return head + tail  # 3,100 -> thousands
            return head + "." + tail  # 9,20 / 0,227 -> decimal
        return n.replace(",", "")  # 1,250,000 / 1,25,000 (Indian)
    # 1,250.50 -> drop ','   |   1.250,50 -> drop '.', ',' is the decimal separator
    out = n.replace(",", "") if n.rfind(".") > n.rfind(",") else n.replace(".", "").replace(",", ".")
    if out.count(".") > 1:  # malformed ('0.20,0.25'): keep the last
        head, _, tail = out.rpartition(".")  # separator so it still parses; the odd
        out = head.replace(".", "") + "." + tail  # value then fails the check (fail closed)
    return out


def _decimals(amount: Decimal) -> int:
    exp = amount.as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


# ------------------------------------------------------------------ scanning
# A raw hit: (amount, currency, start, end, is_percentage, decimals)
type _Hit = tuple[Decimal, str | None, int, int, bool, int]


def _prefix_currency(t: str, s: int) -> tuple[str, int] | None:
    """Currency written right before the number at `s` (optional whitespace): (code, start)."""
    lo = s - 12 if s > 12 else 0
    seg = t[lo:s].rstrip()
    end = lo + len(seg)
    if not seg or s - end > 3:
        return None  # nothing, or more than 3 blanks in between
    tok = seg.rsplit(None, 1)[-1]
    code = _CURRENCY.get(tok.lower())
    if code is not None:
        return code, end - len(tok)
    word = tok.lstrip(_LEAD_PUNCT).rstrip(".:")
    if word and word.isascii():
        code = _CURRENCY.get(word.lower())
        if code is not None:
            return code, end - len(tok.lstrip(_LEAD_PUNCT))
        last = tok[-1]
        code = _SYMBOL_CURRENCY.get(last)
        if code is None:
            return None
        if tok[-3:-1].lower() == "us" and (len(tok) == 3 or tok[-4] not in _ASCII_LETTERS):
            return code, end - 3
        return code, end - 1
    m = _NA_PREFIX.search(t, lo, s)  # Arabic / CJK / Devanagari forms
    if m is None:
        return None
    return _CURRENCY[m.group(1).lower()], m.start()


def _has_keyword(t: str, s: int) -> bool:
    return _keyword_search(t, s - _KEYWORD_WINDOW if s > _KEYWORD_WINDOW else 0, s) is not None


def _glued_to_code(t: str, s: int) -> bool:
    """Digits glued to an ASCII word ('Q2026', 'ABC-12') are part of a code, not an amount."""
    p = t[s - 1]
    if p in _ASCII_LETTERS:
        return True
    return p == "-" and s > 1 and t[s - 2] in _ASCII_LETTERS


def _scan(text: str, default_currency: str | None) -> list[_Hit]:  # noqa: PLR0915 - one hot loop
    t = text.translate(_TRANSLATE) if not text.isascii() and _NATIVE.search(text) else text
    hits: list[_Hit] = []
    skip_until = 0
    last_end = -1  # end of the previous currency-tagged hit
    last_cur: str | None = None
    for m in _MASTER.finditer(t):
        rest, joiner, word = m.groups()
        if rest is None:
            continue  # international phone number
        s = m.start()
        if s < skip_until:
            continue
        nend = s + 1 + len(rest)
        if s and t[s - 1] in ".," and s > 1 and t[s - 2].isdigit():
            continue  # tail of a malformed number token
        if joiner is not None:
            sk = _SKIP.match(t, s)
            if sk is not None:
                skip_until = sk.end()
                if sk.group(1) is not None:
                    _hyphen_range(t, s, skip_until, default_currency, hits)
                continue
        elif rest.count(".") == 2 and _DOT_DATE.fullmatch(t, s, nend):
            continue  # 15.11.2026

        e = nend
        cur: str | None = None
        mult: Decimal | None = None
        unit = False
        if word is not None:
            info = _TAIL.get(word.lower() if word.isascii() else word)
            wstart = m.end() - len(word)
            if info is None:
                if word.isascii():
                    if wstart == nend and (s == 0 or _prefix_currency(t, s) is None):
                        continue  # 8011a, 1st, 12x: part of a code / ordinal
                else:
                    na = _NA_SUFFIX.match(t, wstart)
                    if na is not None:
                        cur, e = _CURRENCY[na.group()], na.end()
            else:
                kind, payload = info
                if kind == "C":
                    cur, e = payload, m.end()
                elif kind == "U":
                    unit = True
                    mult = payload
                elif kind == "X":
                    mult, e = payload, m.end()
                    am = _AFTER_MULT.match(t, e)
                    if am is not None:
                        cur, e = _CURRENCY[(am.group(1) or am.group(2)).lower()], am.end()
                elif kind == "P":
                    if s == 0 or not _glued_to_code(t, s):
                        hits.append((_to_decimal(t[s:nend])[0], None, s, m.end(), True, 0))
                    continue
                elif word.lower() == "per":
                    pc = _PER_CENT.match(t, m.end())
                    if pc is not None:
                        if s == 0 or not _glued_to_code(t, s):
                            hits.append((_to_decimal(t[s:nend])[0], None, s, pc.end(), True, 0))
                        continue
                elif t.startswith("$", m.end()):  # "5 US$"
                    cur, e = "USD", m.end() + 1

        start = s
        if cur is None and s:
            pre = _prefix_currency(t, s)
            if pre is not None:
                cur, start = pre
            elif _glued_to_code(t, s):
                continue
        if unit:
            if cur is None:
                continue  # quantity / measure: not a price
            if mult is not None:
                e = m.end()
        if cur is None:
            if _has_keyword(t, s) or (mult is None and _PER_PIECE.match(t, e) is not None):
                cur = default_currency
            elif last_end >= 0 and s - last_end <= 8 and _CONNECTOR.fullmatch(t, last_end, s):
                cur = last_cur
            else:
                continue
        else:
            last_end, last_cur = e, cur
        amount, decimals = _to_decimal(t[s:nend])
        if mult is not None:
            amount *= mult
            decimals = _decimals(amount)
        hits.append((amount, cur, start, e, False, decimals))
    return hits


def _to_decimal(n: str) -> tuple[Decimal, int]:
    if "," in n or n.count(".") > 1:
        n = _normalise(n)
    dot = n.find(".")
    return Decimal(n), (len(n) - dot - 1 if dot >= 0 else 0)


def _hyphen_range(t: str, s: int, e: int, default_currency: str | None, hits: list[_Hit]) -> None:
    """A long digit-hyphen run is a phone/code, unless it is a priced range like 'AED 1000-1500'."""
    parts = t[s:e].split("-")
    if len(parts) != 2 or parts[0][0] == "0" or parts[1][0] == "0":
        return
    cur: str | None
    pre = _prefix_currency(t, s) if s else None
    if pre is not None:
        cur, start = pre
    elif s and _glued_to_code(t, s):
        return
    elif s and _has_keyword(t, s):
        cur, start = default_currency, s
    else:
        return
    mid = s + len(parts[0])
    hits.append((Decimal(parts[0]), cur, start, mid, False, 0))
    hits.append((Decimal(parts[1]), cur, mid + 1, e, False, 0))


# ------------------------------------------------------------------ public API
def extract_prices(text: str, default_currency: str | None = None) -> list[PriceMention]:
    """Every money amount and percentage in `text`, in text order."""
    return [_mention(a, c, s, e, text[s:e], p) for a, c, s, e, p, _ in _scan(text, default_currency)]


_EXPONENTS: Final = tuple(Decimal(1).scaleb(-p) for p in range(40))


def _quantise(amount: Decimal, places: int) -> Decimal:
    try:
        return amount.quantize(_EXPONENTS[places], rounding=ROUND_HALF_UP)
    except InvalidOperation:  # absurdly large allowed amount: compare exactly instead
        return amount


def check_prices(
    text: str,
    allowed: Sequence[AllowedPrice],
    *,
    default_currency: str | None = None,
    allowed_percentages: Sequence[Decimal] = (),
) -> GuardResult:
    """ok iff every amount in `text` is an allowed price (at speaker precision, >= 2 places) and
    every percentage is in `allowed_percentages`."""
    hits = _scan(text, default_currency)
    if not hits:
        return _OK_EMPTY

    pairs: list[tuple[Decimal, str]] | None = None
    tables: dict[int, dict[str, set[Decimal]]] = {}
    mentions: list[PriceMention] = []
    violations: list[PriceMention] = []

    for amount, cur, s, e, is_pct, written in hits:
        if is_pct:
            good = amount in allowed_percentages
        else:
            # speaker precision, at least 2 places; beyond 39 places quantising is exact anyway
            places = 2 if written < 2 else (39 if written > 39 else written)
            table = tables.get(places)
            if table is None:
                if pairs is None:
                    pairs = [(a.amount, a.currency.upper()) for a in allowed]
                table = tables[places] = {}
                for a, c in pairs:
                    qa = _quantise(a, places)
                    bucket = table.get(c)
                    if bucket is None:
                        table[c] = {qa}
                    else:
                        bucket.add(qa)
            if cur is None:
                good = any(amount in b for b in table.values())
            else:
                bucket = table.get(cur)
                good = bucket is not None and amount in bucket
        mention = _mention(amount, cur, s, e, text[s:e], is_pct)
        mentions.append(mention)
        if not good:
            violations.append(mention)

    return _result(not violations, tuple(mentions), tuple(violations))
