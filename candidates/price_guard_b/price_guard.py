"""Price guard, candidate B: a hand-written single-pass lexer (no regex).

Design
------
1. One C-level pass maps the text to a *class string* of the same length: digits of every supported
   script -> ``d``, letters/marks -> ``a``, CJK -> ``h``, horizontal space -> `` ``, line breaks ->
   ``\\n``, clause punctuation -> ``|``, currency symbols -> ``$``, percent signs -> ``%``, dashes ->
   ``-``; the decimal/thousands separators stay ``.``/``,`` (Arabic U+066B/U+066C included).
   ASCII text goes through ``bytes.translate`` (a 256-byte table), other text through
   ``str.translate`` with a 64K-entry string table.
2. The lexer walks the class string left to right, jumping from NUMBER token to NUMBER token with
   ``str.find`` (text without a digit returns at once). Only a NUMBER can produce a mention, so its
   neighbouring WORD / SYMBOL / PERCENT / SPACE tokens are lexed on demand, using ``lstrip``/``rstrip``
   on the class string to find token ends without a per-character Python loop.
3. A small state machine over (left token, NUMBER, right token) decides: compound (date, time, code,
   phone, range); percentage; prefix/suffix currency; quantity unit or glued letters -> not a price;
   otherwise a bare number is a price only after a price keyword in the same clause.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Final

from kx.core.contracts import AllowedPrice, GuardResult, PriceMention

__all__ = ["check_prices", "extract_prices"]

# ------------------------------------------------------------------ character classes
_DIGIT_BLOCKS: Final = (0x30, 0x660, 0x6F0, 0x966, 0xFF10)  # ASCII, Arabic-Indic, Ext. Arabic, Devanagari, FW
_LINE_BREAKS: Final = "\n\r\x0b\x0c\x85  "
_CLAUSE_PUNCT: Final = "!?;。！？；।॥؟؛"
_CUR_SYMBOLS: Final = "$€£¥₹＄￥"
_PERCENT_SIGNS: Final = "%٪％"
_DASHES: Final = "-‐‑‒–—−"


def _class_of(cp: int) -> str:
    ch = chr(cp)
    if 0x3040 <= cp <= 0x30FF or 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF:
        return "h"
    if ch.isalpha() or unicodedata.category(ch)[0] == "M":
        return "a"
    if ch in _LINE_BREAKS:
        return "\n"
    if ch.isspace():
        return " "
    if ch in _CLAUSE_PUNCT:
        return "|"
    if ch in _CUR_SYMBOLS:
        return "$"
    if ch in _PERCENT_SIGNS:
        return "%"
    if ch in _DASHES:
        return "-"
    if ch in "٫":
        return "."
    if ch in "٬":
        return ","
    return ch if cp < 128 else "~"


def _build_tables() -> tuple[bytes, str, dict[int, str]]:
    classes = [_class_of(cp) for cp in range(0x10000)]
    norm: dict[int, str] = {0x066B: ".", 0x066C: ","}
    for base in _DIGIT_BLOCKS:
        for i in range(10):
            classes[base + i] = "d"
            if base != 0x30:
                norm[base + i] = chr(0x30 + i)
    ascii_table = bytes(ord(classes[b]) if b < 128 else b for b in range(256))
    return ascii_table, "".join(classes), norm


_CLS_ASCII, _CLS_STR, _NORM = _build_tables()

# ------------------------------------------------------------------ vocabularies
_SYM_CUR: Final = {"$": "USD", "＄": "USD", "€": "EUR", "£": "GBP", "¥": "CNY", "￥": "CNY", "₹": "INR"}
_WORD_CUR: Final = {
    "aed": "AED",
    "dhs": "AED",
    "dh": "AED",
    "dirham": "AED",
    "dirhams": "AED",
    "درهم": "AED",
    "دراهم": "AED",
    "usd": "USD",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "rs": "INR",
    "inr": "INR",
    "rupee": "INR",
    "rupees": "INR",
    "cny": "CNY",
    "rmb": "CNY",
    "sar": "SAR",
    "ريال": "SAR",
    "gbp": "GBP",
}
_DOT_ABBR: Final = frozenset({"rs", "dhs", "dh"})  # may be written with a trailing '.'
_AED_AR: Final = "د.إ"
_UNITS: Final = (
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
)
# Magnitude words: a quantity unit after a bare number, a multiplier after an explicit currency.
_MULT: Final = {
    "k": 1000,
    "thousand": 1000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "crore": 10_000_000,
    "crores": 10_000_000,
    "mn": 1_000_000,
    "million": 1_000_000,
}
_PCT_WORDS: Final = ("percent", "pct", "प्रतिशत")
_KW_LATIN: Final = frozenset(
    {"price", "prices", "rate", "rates", "cost", "costs", "total", "subtotal", "amount", "amounts"}
)
_KW_CONTAINS: Final = ("سعر", "الإجمالي", "المبلغ", "价格", "单价", "总价", "價格", "單價", "總價")
_KW_PREFIX: Final = ("कीमत", "मूल्य", "दाम", "விலை")
_TOKEN_SEPARATORS: Final = str.maketrans({c: " " for c in ":,()[]{}\"'*=/<>~_|·"})
_KW_WINDOW: Final = 36  # chars looked back for a price keyword
_KW_TOKENS: Final = 6  # the keyword plus ~5 tokens between it and the number

# right-context kinds
_R_NONE: Final = 0
_R_PCT: Final = 1
_R_CUR: Final = 2
_R_UNIT: Final = 3
_R_MULT: Final = 4
_R_GLUE: Final = 5
_R_PER: Final = 6  # "per" -> maybe "per cent"
_R_US: Final = 7  # "US" -> maybe "US$"
_GLUE: Final = "!"  # left-context sentinel: letters glued to the number that are not a currency
_SIMPLE_RIGHT: Final = frozenset({_R_UNIT, _R_MULT, _R_PCT})  # complete after a spaced word


def _variants(word: str) -> set[str]:
    return {word, word.upper(), word.capitalize()}


def _build_right_words() -> dict[str, tuple[int, str | int | None]]:
    table: dict[str, tuple[int, str | int | None]] = {}
    for w in _UNITS:
        for v in _variants(w):
            table[v] = (_R_UNIT, None)
    for w, f in _MULT.items():
        for v in _variants(w):
            table[v] = (_R_MULT, f)
    for w in _PCT_WORDS:
        for v in _variants(w):
            table[v] = (_R_PCT, None)
    for w, c in _WORD_CUR.items():
        for v in _variants(w):
            table[v] = (_R_CUR, c)
    for v in _variants("per"):
        table[v] = (_R_PER, None)
    for v in _variants("us"):
        table[v] = (_R_US, None)
    return table


_RIGHT_WORDS: Final = _build_right_words()
_LEFT_WORDS: Final = {v: c for w, c in _WORD_CUR.items() for v in _variants(w)}

_PAD: Final = "|" * 40
_CTX: Final = Context(prec=34)
_EXP: Final = tuple(Decimal(1).scaleb(-p) for p in range(40))

# hit = (amount, currency, start, end, is_percentage); the written precision is the amount's exponent
type _Hit = tuple[Decimal, str | None, int, int, bool]


# ------------------------------------------------------------------ result objects
# PriceMention/GuardResult are frozen pydantic models; validating ~1-2 per reply costs more than the
# whole scan, and the scanner only ever produces well-typed values. So they are built straight into
# their slots. The layout is checked once at import; any contract change falls back to the validating
# constructors.
_PM_FIELDS: Final = ("amount", "currency", "start", "end", "raw", "is_percentage")
_GR_FIELDS: Final = ("ok", "mentions", "violations")
_PM_SET: Final = set(_PM_FIELDS)  # copied per instance: model_copy() updates the copy in place
_GR_SET: Final = set(_GR_FIELDS)
_new = object.__new__
_setattr = object.__setattr__


def _slow_mention(h: _Hit, raw: str) -> PriceMention:
    return PriceMention(amount=h[0], currency=h[1], start=h[2], end=h[3], raw=raw, is_percentage=h[4])


def _slow_result(
    ok: bool, mentions: tuple[PriceMention, ...], violations: tuple[PriceMention, ...]
) -> GuardResult:
    return GuardResult(ok=ok, mentions=mentions, violations=violations)


def _fast_mention(h: _Hit, raw: str) -> PriceMention:
    m: PriceMention = _new(PriceMention)
    _setattr(
        m,
        "__dict__",
        {"amount": h[0], "currency": h[1], "start": h[2], "end": h[3], "raw": raw, "is_percentage": h[4]},
    )
    _setattr(m, "__pydantic_fields_set__", _PM_SET.copy())
    _setattr(m, "__pydantic_extra__", None)
    _setattr(m, "__pydantic_private__", None)
    return m


def _fast_result(
    ok: bool, mentions: tuple[PriceMention, ...], violations: tuple[PriceMention, ...]
) -> GuardResult:
    r: GuardResult = _new(GuardResult)
    _setattr(r, "__dict__", {"ok": ok, "mentions": mentions, "violations": violations})
    _setattr(r, "__pydantic_fields_set__", _GR_SET.copy())
    _setattr(r, "__pydantic_extra__", None)
    _setattr(r, "__pydantic_private__", None)
    return r


def _fast_layout_ok() -> bool:
    try:
        hit: _Hit = (Decimal(1), "AED", 0, 1, False)
        probe = _slow_mention(hit, "1")
        same = (
            tuple(PriceMention.model_fields) == _PM_FIELDS
            and tuple(GuardResult.model_fields) == _GR_FIELDS
            and _fast_mention(hit, "1") == probe
            and _fast_result(True, (probe,), ()) == _slow_result(True, (probe,), ())
        )
    except TypeError, AttributeError:  # pragma: no cover - pydantic internals changed
        return False
    return same


_mk_mention, _mk_result = (
    (_fast_mention, _fast_result) if _fast_layout_ok() else (_slow_mention, _slow_result)
)
_OK_EMPTY: Final = _slow_result(True, (), ())


# ------------------------------------------------------------------ number formats
def _grouped(g: list[str], indian: bool) -> bool:
    if not 1 <= len(g[0]) <= 3:
        return False
    if all(len(x) == 3 for x in g[1:]):
        return True
    if not indian or len(g) < 3 or len(g[0]) > 2 or len(g[-1]) != 3:
        return False
    return all(len(x) == 2 for x in g[1:-1])


def _parse(r: str) -> tuple[str, int] | None:
    """ASCII digits with '.'/',' -> (plain decimal string, decimals written); None if not a number."""
    dot = r.rfind(".")
    com = r.rfind(",")
    if com < 0:
        if dot < 0:
            return r, 0
        if r.find(".") == dot:  # single dot: decimal
            return r, len(r) - dot - 1
        g = r.split(".")
        return ("".join(g), 0) if _grouped(g, False) else None
    if dot >= 0:
        last, dsep, tsep = (dot, ".", ",") if dot > com else (com, ",", ".")
        ip, fp = r[:last], r[last + 1 :]
        if dsep in ip:
            return None
        g = ip.split(tsep)
        if not _grouped(g, tsep == ","):
            return None
        return "".join(g) + "." + fp, len(fp)
    g = r.split(",")
    if len(g) == 2 and len(g[1]) <= 2:
        return g[0] + "." + g[1], len(g[1])
    return ("".join(g), 0) if _grouped(g, True) else None


def _lenient(r: str) -> tuple[str, int]:
    """Best effort for an odd format next to an explicit currency: last separator is the decimal."""
    last = max(r.rfind("."), r.rfind(","))
    ip = r[:last].replace(".", "").replace(",", "")
    fp = r[last + 1 :]
    return ip + "." + fp, len(fp)


def _number(text: str, s: int, e: int, strict: bool) -> tuple[str, int] | None:
    raw = text[s:e]
    if not raw.isascii():
        raw = raw.translate(_NORM)
    if raw[0] == ".":
        raw = "0" + raw
    p = _parse(raw)
    if p is None and not strict:
        p = _lenient(raw)
    return p


# ------------------------------------------------------------------ lexer helpers
def _num_end(cls: str, j: int) -> int:
    """End of the NUMBER token starting at digit j: digits, plus '.'/',' only when a digit follows.

    `cls` carries the trailing `_PAD` sentinels, so each 32-char slice is full length.
    """
    while True:
        k = j + 32 - len(cls[j : j + 32].lstrip("d.,"))
        if k - j < 32:
            break
        j = k
    while cls[k - 1] != "d":
        k -= 1
    return k


def _skip_spaces(cls: str, i: int) -> int:
    chunk = cls[i : i + 16]
    return i + len(chunk) - len(chunk.lstrip(" "))


def _word_start(cls: str, i: int) -> int:
    chunk = cls[i - 24 if i > 24 else 0 : i]
    return i - (len(chunk) - len(chunk.rstrip("a")))


def _digit_follows(cls: str, i: int, n: int) -> bool:
    i = _skip_spaces(cls, i)
    return i < n and cls[i] == "d"


def _left(text: str, cls: str, s: int, c: str) -> tuple[str | None, int, bool]:
    """Prefix currency token before the number: (currency | None | _GLUE, token start, newline gap).

    `c` is cls[s - 1]; the caller only calls this for c in " \\na$.h".
    """
    i = s
    glued = True
    nl = False
    if c == " " or c == "\n":  # noqa: PLR1714 - two 1-char compares beat building a set
        chunk = cls[i - 12 if i > 12 else 0 : i]
        gap = len(chunk) - len(chunk.rstrip(" \n"))
        if gap > 1 or c == "\n":
            nls = cls.count("\n", i - gap, i)
            if nls > 1:
                return None, s, False
            nl = nls == 1
        i -= gap
        if i == 0:
            return None, s, False
        glued = False
        c = cls[i - 1]
    if c == "a":
        ws = _word_start(cls, i)
        word = text[ws:i]
        cur = _LEFT_WORDS.get(word)
        if cur is None and not word.islower():
            cur = _LEFT_WORDS.get(word.lower())
        if cur is not None:
            return cur, ws, nl
        if text.startswith(_AED_AR, i - 3):
            return "AED", i - 3, nl
        return (_GLUE if glued else None), s, nl
    if c == "$":
        sym = text[i - 1]
        if i >= 2 and cls[i - 2] == "a":
            ws = _word_start(cls, i - 1)
            word = text[ws : i - 1].upper()
            return ("USD" if word == "US" else word + sym), ws, nl
        return _SYM_CUR[sym], i - 1, nl
    if c == ".":
        if i >= 2 and cls[i - 2] == "a":
            ws = _word_start(cls, i - 1)
            lw = text[ws : i - 1].lower()
            if lw in _DOT_ABBR:
                return _WORD_CUR[lw], ws, nl
            if glued:
                return _GLUE, s, nl
        return None, s, nl
    if c == "h" and text.endswith("人民币", 0, i):
        return "CNY", i - 3, nl
    return None, s, nl


def _right(text: str, cls: str, e: int, n: int, c: str) -> tuple[int, int, str | int | None]:
    """Token after the number: (kind, end of that token, currency or multiplier).

    `c` is cls[e]; the caller only calls this for c in " a%$h".
    """
    i = e
    glued = True
    if c == " ":
        i += 1
        if i < n and cls[i] == " ":
            i = _skip_spaces(cls, i)
        if i >= n:
            return _R_NONE, e, None
        glued = False
        c = cls[i]
    if c == "a":
        chunk = cls[i : i + 24]
        we = i + len(chunk) - len(chunk.lstrip("a"))
        word = text[i:we]
        info = _RIGHT_WORDS.get(word)
        if info is None and not word.islower():
            info = _RIGHT_WORDS.get(word.lower())
        if info is None:
            if text.startswith(_AED_AR, i):
                we = i + 3
                return (_R_NONE, e, None) if _digit_follows(cls, we, n) else (_R_CUR, we, "AED")
            return (_R_GLUE if glued else _R_NONE), e, None
        kind = info[0]
        if kind == _R_CUR:
            if we < n and text[we] == "." and word.lower() in _DOT_ABBR:
                we += 1
            return (_R_NONE, e, None) if _digit_follows(cls, we, n) else (_R_CUR, we, info[1])
        if kind == _R_PER:
            j = _skip_spaces(cls, we)
            if j > we and text[j : j + 4].lower() == "cent" and (j + 4 >= n or cls[j + 4] != "a"):
                return _R_PCT, j + 4, None
            return _R_NONE, e, None
        if kind == _R_US:
            if we < n and text[we] == "$" and not _digit_follows(cls, we + 1, n):
                return _R_CUR, we + 1, "USD"
            return (_R_GLUE if glued else _R_NONE), e, None
        return kind, we, info[1]
    if c == "%":
        return _R_PCT, i + 1, None
    if c == "$":
        if _digit_follows(cls, i + 1, n):
            return _R_NONE, e, None
        return _R_CUR, i + 1, _SYM_CUR[text[i]]
    if c == "h":
        if text[i] == "元":
            return _R_CUR, i + 1, "CNY"
        if text.startswith("人民币", i):
            return _R_CUR, i + 3, "CNY"
    return _R_NONE, e, None


def _keyword_before(text: str, cls: str, s: int) -> bool:
    """A price keyword within ~36 chars / 6 tokens before s, in the same clause."""
    lo = s - _KW_WINDOW if s > _KW_WINDOW else 0
    b = max(cls.rfind("|", lo, s), cls.rfind("\n", lo, s))
    if b >= 0:
        lo = b + 1
    d = text.rfind(". ", lo, s)
    while d >= 0:  # ". " followed by a capital ends a sentence
        k = _skip_spaces(cls, d + 1)
        if k < s and text[k].isupper():
            lo = k
            break
        d = text.rfind(". ", lo, d)
    if lo > 0 and cls[lo - 1] == "a":  # don't start inside a word
        chunk = cls[lo : lo + 24]
        lo += len(chunk) - len(chunk.lstrip("a"))
    if lo >= s:
        return False
    for tok in text[lo:s].translate(_TOKEN_SEPARATORS).split()[-_KW_TOKENS:]:
        t = tok.strip(".-!?").lower()
        if t in _KW_LATIN:
            return True
        if not t.isascii():
            for kw in _KW_CONTAINS:
                if kw in t:
                    return True
            if t.startswith(_KW_PREFIX):
                return True
    return False


# ------------------------------------------------------------------ state machine (rare paths)
def _skip_phone(cls: str, j: int, n: int) -> int:
    """Consume the digit groups of a '+' phone number (separated by spaces, dashes, brackets)."""
    while j < n:
        k = j
        while k < n and k - j < 3 and cls[k] in " -()":
            k += 1
        if k == j or k >= n or cls[k] != "d":
            return j
        j = _num_end(cls, k)
    return j


def _compound(text: str, cls: str, n: int, s: int, e: int, *, default: str | None, hits: list[_Hit]) -> int:
    """NUMBER joined to more numbers by '-', '/' or ':' (dates, times, codes, phones, ranges).

    Three or more parts (2026-11-15, 15/11/2026) are never prices. Two parts are reported as two
    amounts when an explicit currency is attached ("AED 0.22-0.25", "AED 0.22/0.25"), and as a
    dash range after a price keyword when either part has decimals ("price 0.22-0.25").
    """
    parts = [(s, e)]
    joiners = []
    j = e
    while j + 1 < n and cls[j] in "-/:" and cls[j + 1] == "d":
        joiners.append(cls[j])
        ne = _num_end(cls, j + 1)
        parts.append((j + 1, ne))
        j = ne
    if len(parts) != 2:
        return j
    dash = joiners[0] == "-"
    lc = cls[s - 1] if s else " "
    if lc in "-/:_#@+" or (lc == "." and s >= 2 and cls[s - 2] in "dah"):
        return j
    (s1, e1), (s2, e2) = parts
    n1 = _number(text, s1, e1, True)
    n2 = _number(text, s2, e2, True)
    if n1 is None or n2 is None:
        return j
    pcur, pstart = None, s
    if lc in " \na$.h":
        pcur, pstart, _ = _left(text, cls, s, lc)
        if pcur == _GLUE:
            return j
    rk, rend, rval = (
        (_R_NONE, j, None) if j >= n or cls[j] not in " a%$h" else _right(text, cls, j, n, cls[j])
    )
    if rk == _R_PCT:
        if not dash:
            return j
        hits.append((Decimal(n1[0]), None, s1, e1, True))
        hits.append((Decimal(n2[0]), None, s2, rend, True))
        return rend
    if rk != _R_NONE and rk != _R_CUR and pcur is None:  # noqa: PLR1714
        return j
    cur: str | None
    end = j
    if pcur is not None:
        cur = pcur
    elif rk == _R_CUR and isinstance(rval, str):
        cur, end = rval, rend
    elif dash and (n1[1] or n2[1]) and _keyword_before(text, cls, s):
        cur, pstart = default, s
    else:
        return j
    hits.append((Decimal(n1[0]), cur, pstart, e1, False))
    hits.append((Decimal(n2[0]), cur, s2, end, False))
    return end


def _odd_left(text: str, cls: str, n: int, s: int, *, e: int, hits: list[_Hit]) -> int:
    """Number right after '-', '/', ':', '_', '#', '@' or '+': code, reference or phone number.

    Returns where to resume, or -1 when the number is an ordinary one after all.
    """
    lc = cls[s - 1]
    before = cls[s - 2] if s >= 2 else " "
    if lc == "+":
        if before in "da":
            return -1
        rk, rend, _ = (_R_NONE, e, None) if e >= n or cls[e] not in " a%" else _right(text, cls, e, n, cls[e])
        if rk == _R_PCT:  # "+5%"
            num = _number(text, s, e, False)
            assert num is not None
            hits.append((Decimal(num[0]), None, s, rend, True))
            return rend
        return _skip_phone(cls, e, n)  # +971 50 123 4567
    return e if before in "dah" else -1  # Q-2026, A/123, #5 ... vs "- 5", " /5"


# ------------------------------------------------------------------ the scanner
def _scan(text: str, default_currency: str | None) -> list[_Hit]:  # noqa: PLR0915 - hot loop, inlined
    ascii_text = text.isascii()
    # Trailing sentinels: every look-ahead slice is full length, and cls[-1] (s == 0) reads as '|'.
    cls = (text.encode().translate(_CLS_ASCII).decode() if ascii_text else text.translate(_CLS_STR)) + _PAD
    s = cls.find("d")
    hits: list[_Hit] = []
    if s < 0:
        return hits
    default = default_currency.upper() if default_currency else None
    n = len(text)
    while s >= 0:
        # ---- NUMBER token [s, e): digits, '.' and ',' that are followed by a digit
        e = s + 32 - len(cls[s : s + 32].lstrip("d.,"))
        if e - s == 32:
            e = _num_end(cls, s)
        while cls[e - 1] != "d":
            e -= 1
        rc = cls[e]
        if rc in "-/:" and cls[e + 1] == "d":
            s = cls.find("d", _compound(text, cls, n, s, e, default=default, hits=hits))
            continue
        lc = cls[s - 1]
        if lc in "-/:_#@+":
            resume = _odd_left(text, cls, n, s, e=e, hits=hits)
            if resume >= 0:
                s = cls.find("d", resume)
                continue
        elif lc == "." and text[s - 1] == "." and (s == 1 or cls[s - 2] in " \n$|"):
            s -= 1  # ".50"
            lc = cls[s - 1]
        # ---- right neighbour (fast path: one space then a plain word)
        rk, rend, rval = _R_NONE, e, None
        if rc == " " and cls[e + 1] == "a":
            i = e + 1
            we = i + 24 - len(cls[i : i + 24].lstrip("a"))
            word = text[i:we]
            info = _RIGHT_WORDS.get(word)
            if info is None and not word.islower():
                info = _RIGHT_WORDS.get(word.lower())
            if info is not None:
                if info[0] in _SIMPLE_RIGHT:
                    rk, rend, rval = info[0], we, info[1]
                elif info[0] == _R_PER:  # "per piece" vs "per cent"
                    if text[we : we + 5].lower() == " cent" and cls[we + 5] != "a":
                        rk, rend = _R_PCT, we + 5
                else:
                    rk, rend, rval = _right(text, cls, e, n, rc)
            elif text[i] == "د":
                rk, rend, rval = _right(text, cls, e, n, rc)
        elif rc in " a%$h":
            rk, rend, rval = _right(text, cls, e, n, rc)
        if rk == _R_PCT:
            num = _number(text, s, e, False)
            assert num is not None
            hits.append((Decimal(num[0]), None, s, rend, True))
            s = cls.find("d", rend)
            continue
        # ---- left neighbour (fast path: a plain word then one space)
        pcur, pstart, pnl = None, s, False
        if lc == " " and cls[s - 2] == "a":
            i = s - 1
            chunk = cls[i - 24 if i > 24 else 0 : i]
            ws = i - (len(chunk) - len(chunk.rstrip("a")))
            word = text[ws:i]
            pcur = _LEFT_WORDS.get(word)
            if pcur is None and not word.islower():
                pcur = _LEFT_WORDS.get(word.lower())
            if pcur is not None:
                pstart = ws
            elif text[i - 1] == "إ":
                pcur, pstart, pnl = _left(text, cls, s, lc)
        elif lc in " \na$.h":
            pcur, pstart, pnl = _left(text, cls, s, lc)
            if pcur == _GLUE:
                s = cls.find("d", e)  # v2, HC40 ...
                continue
        # ---- decide
        cur: str | None
        start, end, mult, strict = s, e, 1, False
        if pcur is not None and not (pnl and rk in (_R_UNIT, _R_MULT)):
            cur, start = pcur, pstart
            if rk == _R_MULT and isinstance(rval, int):
                mult, end = rval, rend
        elif rk == _R_CUR and isinstance(rval, str):
            cur, end = rval, rend
        elif rk != _R_NONE or not _keyword_before(text, cls, s):
            s = cls.find("d", e)  # unit, glued letters, or no context
            continue
        else:
            cur, strict = default, True
        # ---- amount
        raw = text[s:e]
        if ascii_text and "," not in raw and raw.count(".") < 2:
            amount = Decimal(raw)  # plain "15", "0.227804", ".50"
        else:
            num = _number(text, s, e, strict)
            if num is None:
                s = cls.find("d", e)
                continue
            amount = Decimal(num[0])
        if mult != 1:
            amount *= mult
        hits.append((amount, cur, start, end, False))
        s = cls.find("d", end)
    return hits


# ------------------------------------------------------------------ public API
def extract_prices(text: str, default_currency: str | None = None) -> list[PriceMention]:
    """Every money amount and percentage in `text`, in text order."""
    return [_mk_mention(h, text[h[2] : h[3]]) for h in _scan(text, default_currency)]


def check_prices(
    text: str,
    allowed: Sequence[AllowedPrice],
    *,
    default_currency: str | None = None,
    allowed_percentages: Sequence[Decimal] = (),
) -> GuardResult:
    """ok iff every mention matches an allowed price (at the speaker's precision) or percentage."""
    hits = _scan(text, default_currency)
    if not hits:
        return _OK_EMPTY
    mentions: list[PriceMention] = []
    violations: list[PriceMention] = []
    for h in hits:
        amount, cur, start, end, pct = h
        m = _mk_mention(h, text[start:end])
        mentions.append(m)
        if pct:
            if amount not in allowed_percentages:
                violations.append(m)
            continue
        # Exact numeric equality implies a match at any precision >= the one written, so only a
        # mismatch needs the rounding comparison.
        for a in allowed:
            if a.amount == amount and (cur is None or a.currency == cur or a.currency.upper() == cur):
                break
        else:
            if not _rounded_match(amount, cur, allowed):
                violations.append(m)
    return _mk_result(not violations, tuple(mentions), tuple(violations))


def _rounded_match(amount: Decimal, cur: str | None, allowed: Sequence[AllowedPrice]) -> bool:
    """quantize(allowed, max(2, decimals written), ROUND_HALF_UP) == amount, currency-compatible."""
    exponent = amount.as_tuple().exponent
    written = -exponent if isinstance(exponent, int) else 0
    p = written if written > 2 else 2
    exp = _EXP[p] if p < 40 else Decimal(1).scaleb(-p)
    for a in allowed:
        if (cur is None or a.currency.upper() == cur) and a.amount.quantize(
            exp, ROUND_HALF_UP, _CTX
        ) == amount:
            return True
    return False
