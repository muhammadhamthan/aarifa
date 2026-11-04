"""Price guard, approach C (hybrid): a tiny regex finds number candidates, O(1) probes classify them.

Stage 1  `_CAND` finds digit runs with internal separators on a copy of the text whose non-ASCII
         digits/separators were mapped to ASCII by a length-preserving `str.translate`, so every index
         is also an index into the original text. Runs joined by - / : come out as ONE compound
         candidate (date, time, code, phone or a-b range), decided in one place.
Stage 2  per candidate, O(1) probes on a lower-cased copy: the char before selects a few currency
         tokens (dict: last char -> tokens) checked with `startswith`; the word after is sliced up to
         the next space and looked up whole (currency / unit / percent table). Bare numbers need a
         price keyword earlier in the same clause, or a range partner ("AED 0.22 - 0.25").
Checking builds lazily, per call, {(currency|None, precision) -> quantized allowed amounts}; exact
equality short-circuits the rounding. Small plans are tuples because Decimal hashing is costly.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel

from kx.core.contracts import AllowedPrice, GuardResult, PriceMention

__all__ = ["check_prices", "extract_prices"]

# ------------------------------------------------------------------------------------------ tables
_TRANSLATE: Final = str.maketrans(
    {
        **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic
        **{chr(0x06F0 + i): str(i) for i in range(10)},  # Extended Arabic-Indic (Persian/Urdu)
        **{chr(0x0966 + i): str(i) for i in range(10)},  # Devanagari
        **{chr(0xFF10 + i): str(i) for i in range(10)},  # full-width (CJK input methods)
        "٬": ",",  # Arabic thousands separator
        "٫": ".",  # Arabic decimal separator
    }
)
_NEEDS_TRANSLATE: Final = re.compile("[٠-٩۰-۹०-९０-９٫٬]")

# Stage 1: a digit and everything number-like glued to it. Kept this simple on purpose: a leading
# character class lets `re` use its fast prefix scan (2x faster than a structured pattern). Trailing
# separators ("0.2278." / "0.227804, total") are punctuation and get trimmed; - / : make it compound.
_CAND: Final = re.compile(r"[0-9][0-9.,:/\-]*")
_SEPARATORS: Final = ".,:/-"
_PHONE: Final = re.compile(r"\+[0-9]{1,4}(?:[ \-]?[0-9]){6,14}")

_SYMBOLS: Final[dict[str, str]] = {"$": "USD", "€": "EUR", "₹": "INR", "¥": "CNY", "£": "GBP"}
_CURRENCY_WORDS: Final[dict[str, str]] = {
    "aed": "AED",
    "dhs": "AED",
    "dh": "AED",
    "dirham": "AED",
    "dirhams": "AED",
    "درهم": "AED",
    "دراهم": "AED",
    "د.إ": "AED",
    "us$": "USD",
    "usd": "USD",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "rupee": "INR",
    "rupees": "INR",
    "cny": "CNY",
    "rmb": "CNY",
    "元": "CNY",
    "人民币": "CNY",
    "sar": "SAR",
    "ريال": "SAR",
    "gbp": "GBP",
}
_GLUED_OK: Final = frozenset({"元", "人民币"})  # CJK is written without spaces: no word boundary
_UNITS: Final = frozenset(
    {
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
        "gp",
        "ft",
        "ctn",
        "ctns",
        "carton",
        "cartons",
        "box",
        "boxes",
        "x",
        "container",
        "containers",
        "pallet",
        "pallets",
        "roll",
        "rolls",
        "sheet",
        "sheets",
        "bag",
        "bags",
        "bottle",
        "bottles",
        "day",
        "days",
        "week",
        "weeks",
        "month",
        "months",
        "year",
        "years",
        "hour",
        "hours",
        "hr",
        "hrs",
        "min",
        "mins",
        "minute",
        "minutes",
    }
)
_PERCENT_WORDS: Final = ("percent", "per cent", "pct")

_NOTHING: Final = 0
_CURRENCY: Final = 1
_PERCENT: Final = 2
_UNIT: Final = 3

_Token = tuple[str, int, str | None, bool]  # (token, kind, currency, needs word boundary)


def _index(tokens: Sequence[_Token], *, by_last: bool) -> dict[str, tuple[_Token, ...]]:
    out: dict[str, list[_Token]] = {}
    for tok in sorted(tokens, key=lambda x: -len(x[0])):  # longest first: "dirhams" before "dh"
        out.setdefault(tok[0][-1] if by_last else tok[0][0], []).append(tok)
    return {k: tuple(v) for k, v in out.items()}


_CUR_TOKENS: Final = [(w, _CURRENCY, c, w not in _GLUED_OK) for w, c in _CURRENCY_WORDS.items()]
# prefix probe: keyed by the character just before the number (after optional spaces)
_BEFORE: Final = _index(_CUR_TOKENS, by_last=True)
_AFTER_TOKENS: Final = [
    *_CUR_TOKENS,
    *((w, _PERCENT, None, True) for w in _PERCENT_WORDS),
    *((w, _UNIT, None, True) for w in _UNITS),
]
# suffix probe: the next word looked up whole; tokens keyed by first char for glued/odd cases
_AFTER_WORDS: Final[dict[str, _Token]] = {tok[0]: tok for tok in _AFTER_TOKENS}
_AFTER: Final = _index(_AFTER_TOKENS, by_last=False)
_TRAILING_PUNCT: Final = ".,;:!?)]}>\"'/-"

_KEYWORD: Final = re.compile(
    r"\b(?:prices?|rates?|costs?|total|subtotal|amount)\b"
    r"|سعر|السعر|الإجمالي|المبلغ|价格|单价|总价|कीमत|मूल्य|दाम|விலை"
)
_CLAUSE_END: Final = re.compile(r"[.!?](?:\s|$)|[;\n。؟！？।]")
_RANGE_GAPS: Final = frozenset({"-", "–", "—", "~", "to", "or", "/"})
_WS: Final = frozenset(" \t\u00a0\u202f")  # incl. no-break + narrow no-break space
_WS_BEFORE: Final = frozenset(" \t\n\r\u00a0\u202f")  # "AED\n0.19" is still AED
_SPECIAL_BEFORE: Final = frozenset("+#-/:")
_KEYWORD_WINDOW: Final = 30
_SMALL_PLAN: Final = 16  # up to this many allowed prices, == scans beat hashing Decimals

_CTX: Final = Context(prec=200, rounding=ROUND_HALF_UP)
_EXPS: Final = tuple(Decimal(1).scaleb(-p) for p in range(64))


# ------------------------------------------------------- fast, validation-free model construction
# Every value below is built by this module with the exact declared field type, so pydantic's
# validation would only re-check what we already know. An import-time self-test compares against
# the validated constructors and falls back to them if pydantic internals ever change.
_new = object.__new__
_set_dict = BaseModel.__dict__["__dict__"].__set__
_set_fields = BaseModel.__dict__["__pydantic_fields_set__"].__set__
_set_extra = BaseModel.__dict__["__pydantic_extra__"].__set__
_set_private = BaseModel.__dict__["__pydantic_private__"].__set__
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
    _set_dict(
        m,
        {
            "amount": amount,
            "currency": currency,
            "start": start,
            "end": end,
            "raw": raw,
            "is_percentage": is_percentage,
        },
    )
    _set_fields(m, _PM_FIELDS)
    _set_extra(m, None)
    _set_private(m, None)
    return m


def _fast_result(
    ok: bool, mentions: tuple[PriceMention, ...], violations: tuple[PriceMention, ...]
) -> GuardResult:
    r = _new(GuardResult)
    _set_dict(r, {"ok": ok, "mentions": mentions, "violations": violations})
    _set_fields(r, _GR_FIELDS)
    _set_extra(r, None)
    _set_private(r, None)
    return r


def _safe_mention(
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


def _safe_result(
    ok: bool, mentions: tuple[PriceMention, ...], violations: tuple[PriceMention, ...]
) -> GuardResult:
    return GuardResult(ok=ok, mentions=mentions, violations=violations)


def _fast_path_is_sound() -> bool:
    try:
        a = _fast_mention(Decimal("1.50"), "AED", 0, 8, "AED 1.50", False)
        b = _safe_mention(Decimal("1.50"), "AED", 0, 8, "AED 1.50", False)
        ra, rb = _fast_result(False, (a,), (a,)), _safe_result(False, (b,), (b,))
        return (
            a == b
            and hash(a) == hash(b)
            and repr(a) == repr(b)
            and a.model_dump() == b.model_dump()
            and a.model_dump_json() == b.model_dump_json()
            and a.model_fields_set == b.model_fields_set
            and ra == rb
            and ra.model_dump() == rb.model_dump()
            and repr(ra) == repr(rb)
        )
    except Exception:
        return False


if _fast_path_is_sound():
    _mention, _result = _fast_mention, _fast_result
else:  # pragma: no cover - only if pydantic internals change
    _mention, _result = _safe_mention, _safe_result

_OK_EMPTY: Final = _result(True, (), ())  # frozen and holds only empty tuples: safe to share


# --------------------------------------------------------------------------------- number parsing
# Integer part with thousands separators: Western 1,234,567 or Indian 12,34,567 (same with '.').
_GROUPED: Final = {
    sep: re.compile(
        rf"[0-9]{{1,3}}(?:{re.escape(sep)}[0-9]{{3}})+"
        rf"|[0-9]{{1,2}}(?:{re.escape(sep)}[0-9]{{2}})+{re.escape(sep)}[0-9]{{3}}"
    ).fullmatch
    for sep in ",."
}


def _parse(num: str) -> tuple[str, int] | None:
    """'1.250,50' -> ('1250.50', 2); None when the separators do not form a valid number."""
    dot, comma = num.find("."), num.find(",")
    if comma < 0:
        if dot < 0:
            return num, 0
        if num.find(".", dot + 1) < 0:  # the common case: 0.227804
            return num, len(num) - dot - 1
    elif dot < 0 and num.find(",", comma + 1) < 0:  # single comma: 3,100 or 9,20
        if len(num) - comma == 4:
            return num.replace(",", ""), 0
        if len(num) - comma <= 3:
            return num.replace(",", "."), len(num) - comma - 1
        return None
    dot, comma = num.rfind("."), num.rfind(",")
    if dot >= 0 and comma >= 0:  # both: the LAST one is the decimal separator
        last = max(dot, comma)
        thou = "," if last == dot else "."
        int_part, frac = num[:last], num[last + 1 :]
        if thou in int_part and _GROUPED[thou](int_part) is None:
            return None
        if num[last] in int_part:
            return None
        return int_part.replace(thou, "") + "." + frac, len(frac)
    if dot < 0 and comma < 0:
        return num, 0
    sep = "," if comma >= 0 else "."
    parts = num.split(sep)
    if len(parts) == 2 and (sep == "." or len(parts[1]) <= 2):  # 0.23 / 9,20
        return parts[0] + "." + parts[1], len(parts[1])
    if _GROUPED[sep](num) is not None:  # 1,250,000 / 1,25,000 / 1.250.000
        return "".join(parts), 0
    return None


# ------------------------------------------------------------------------------- context probes
_Probe = tuple[int, str | None, int, int]  # (kind, currency, span start, span end)


def _probe(t: str, lt: str, n: int, s: int, e: int) -> _Probe | None:
    """Classify the number at t[s:e] from its neighbours. None = not a money amount."""
    # ---- before: symbol or currency word, optional whitespace in between
    i = s
    while i > 0 and s - i < 3 and t[i - 1] in _WS_BEFORE:
        i -= 1
    cur: str | None = None
    start = s
    if i:
        c = lt[i - 1]
        cur = _SYMBOLS.get(c)
        if cur is not None:
            start = i - 3 if c == "$" and lt.startswith("us", i - 3) and i >= 3 else i - 1
        else:
            for word, _kind, tcur, bounded in _BEFORE.get(c, ()):
                j = i - len(word)
                if j >= 0 and lt.startswith(word, j) and not (bounded and j and lt[j - 1].isalpha()):
                    cur, start = tcur, j
                    break
            else:
                if i == s and c.isascii() and c.isalpha():  # glued to a Latin word: v3, Q2026
                    return None
    # ---- after: percent sign/word, symbol, currency word or unit
    k = e
    while k < n and k - e < 2 and t[k] in _WS:
        k += 1
    if k < n:
        c = lt[k]
        if c in "%٪":
            return _PERCENT, None, s, k + 1
        if cur is not None:
            return _CURRENCY, cur, start, e
        scur = _SYMBOLS.get(c)
        if scur is not None:
            return _CURRENCY, scur, s, k + 1
        if c == "×":
            return None
        if not c.isalpha():
            return _NOTHING, None, s, e
        sp = lt.find(" ", k, k + 16)
        word = lt[k : sp if sp >= 0 else k + 16].rstrip(_TRAILING_PUNCT)
        hit = _AFTER_WORDS.get(word)
        if hit is None and (word == "per" or not word.isascii() or not word.isalpha()):
            for tok in _AFTER.get(c, ()):  # "pcs/ctn", "per cent", "元每个", "us$"
                m = k + len(tok[0])
                if lt.startswith(tok[0], k) and not (tok[3] and lt[m : m + 1].isalpha()):
                    hit = tok
                    break
        if hit is not None:
            if hit[1] == _UNIT:
                return None
            return hit[1], hit[2], s, k + len(hit[0])
        if k == e and c.isascii():  # glued to a Latin word: 40HC, 3rd, 2x
            return None
    elif cur is not None:
        return _CURRENCY, cur, start, e
    return _NOTHING, None, s, e


def _has_keyword(lt: str, s: int) -> bool:
    lo = max(s - _KEYWORD_WINDOW, 0)
    for b in _CLAUSE_END.finditer(lt, lo, s):
        lo = b.end()
    return lo < s and _KEYWORD.search(lt, lo, s) is not None


def _compound(t: str, lt: str, n: int, s: int, e: int) -> list[tuple[str, int, _Probe]]:
    """Joined by - / : -> dates, times, phones, codes are rejected; 'a-b' is a range of two."""
    cand = t[s:e]
    if ":" in cand or "/" in cand:
        return []
    parts = cand.split("-")
    if len(parts) != 2:
        return []
    a, b = parts
    if len(a) == 4 and a.isdigit():  # 2026-11
        return []
    if a.isdigit() and b.isdigit() and len(a) + len(b) >= 7:  # 050-1234567
        return []
    pa, pb = _parse(a), _parse(b)
    qa, qb = _probe(t, lt, n, s, s + len(a)), _probe(t, lt, n, e - len(b), e)
    if pa is None or pb is None or qa is None or qb is None:
        return []
    if qa[0] == _NOTHING and qb[0] == _CURRENCY:
        qa = (_CURRENCY, qb[1], qa[2], qa[3])
    elif qb[0] == _NOTHING and qa[0] == _CURRENCY:
        qb = (_CURRENCY, qa[1], qb[2], qb[3])
    elif qb[0] == _PERCENT:  # 5-7%
        qa = (_PERCENT, None, qa[2], qa[3])
    return [(pa[0], pa[1], qa), (pb[0], pb[1], qb)]


# ------------------------------------------------------------------------------------- scanning
_NOT_A_PRICE: Final = "\0"  # sentinel currency: the bare number is not a price


# "0.21 each", "0.21 per piece", "8.50/-": a per-piece phrase right after a bare number makes it a price.
_PRICE_SUFFIX: Final = re.compile(
    r"[ \t]*(?:each\b|apiece\b|a piece\b|per (?:piece|pc|pcs|unit|item|container)\b"
    r"|/(?:-|=|pcs|pc|piece|unit))"
)


def _bare_currency(
    lt: str,
    s: int,
    e: int,
    dec: int,
    prev_end: int,
    prev_cur: str | None,
    default_currency: str | None,
) -> str | None:
    """A number with no currency of its own is a price (fail closed) when it follows a range/alternative
    connector ("AED 0.22 - 0.25", "AED 0.22 or 0.21"), a price keyword earlier in the same clause, a
    per-piece phrase follows it ("0.21 each"), or it is written with 2+ decimals ("I can do 0.21"):
    in a sales reply such a number is almost always a price, and a false alarm only costs a rewrite."""
    if prev_cur is not None and 0 <= s - prev_end <= 5 and lt[prev_end:s].strip() in _RANGE_GAPS:
        return prev_cur
    if dec >= 2 or _has_keyword(lt, s) or _PRICE_SUFFIX.match(lt, e) is not None:
        return default_currency.upper() if default_currency else None
    return _NOT_A_PRICE


def _scan(text: str, default_currency: str | None) -> tuple[list[PriceMention], list[int]]:  # noqa: PLR0915
    """Mentions in text order plus, per mention, decimals written (-1 for a percentage)."""
    mentions: list[PriceMention] = []
    decimals: list[int] = []
    t = text
    if not t.isascii() and _NEEDS_TRANSLATE.search(t) is not None:
        t = t.translate(_TRANSLATE)
    lt = ""
    n = len(t)
    skip_to = 0
    prev_end, prev_cur = -10, None
    pending: tuple[str, int, int, int] | None = None
    for m in _CAND.finditer(t):
        s, e = m.span()
        if s < skip_to:
            continue
        if not lt:
            lt = t.lower()
            if len(lt) != n:  # a few code points lower-case to 2 chars; keep indexes aligned
                lt = "".join(ch if len(ch.lower()) != 1 else ch.lower() for ch in t)
        if s and t[s - 1] in _SPECIAL_BEFORE:
            cb = t[s - 1]
            if cb == "+":
                pm = _PHONE.match(t, s - 1)
                if pm is not None:  # +971 50 123 4567
                    skip_to = pm.end()
                    continue
            elif cb == "#" or (s > 1 and t[s - 2].isalnum()):  # #123, Q-2026-.., A/123, 10:30
                continue
        if s and t[s - 1] == "." and (s == 1 or not t[s - 2].isalnum()):
            s -= 1  # leading decimal point: "AED .19" is 0.19, never a bare 19
        cand = t[s:e]
        if cand[-1] in _SEPARATORS:
            cand = cand.rstrip(_SEPARATORS)
            e = s + len(cand)
        if cand.isdigit():
            probe = _probe(t, lt, n, s, e)
            if probe is None:
                continue
            amount_s, dec = cand, 0
        elif "-" in cand or "/" in cand or ":" in cand:  # rare: dates, times, codes, ranges
            for amount_s, dec, (kind, piece_cur, ps, pe) in _compound(t, lt, n, s, e):
                cur = piece_cur
                if kind == _NOTHING:
                    cur = _bare_currency(lt, ps, pe, dec, prev_end, prev_cur, default_currency)
                    if cur is _NOT_A_PRICE:
                        continue
                mentions.append(_mention(Decimal(amount_s), cur, ps, pe, text[ps:pe], kind == _PERCENT))
                decimals.append(-1 if kind == _PERCENT else dec)
                prev_end, prev_cur = pe, cur
            continue
        else:
            probe = _probe(t, lt, n, s, e)
            if probe is None:
                continue
            parsed = _parse(cand)
            if parsed is None:
                if probe[0] != _CURRENCY:  # 15.11.2026, 1.2.3
                    continue
                # malformed but next to a currency: report it (stripped) so the guard fails closed
                parsed = cand.replace(",", "").replace(".", ""), 0
            amount_s, dec = parsed
        kind, cur, ps, pe = probe
        if kind == _PERCENT:
            mentions.append(_mention(Decimal(amount_s), None, ps, pe, text[ps:pe], True))
            decimals.append(-1)
            prev_end, prev_cur = pe, None
            continue
        if kind == _NOTHING:
            cur = _bare_currency(lt, ps, pe, dec, prev_end, prev_cur, default_currency)
            if cur is _NOT_A_PRICE:
                pending = amount_s, dec, ps, pe  # may be the low end of "0.22 to 0.25 AED"
                continue
        elif pending is not None:
            p_amount, p_dec, p_s, p_e = pending
            if 0 <= ps - p_e <= 5 and lt[p_e:ps].strip() in _RANGE_GAPS:
                mentions.append(_mention(Decimal(p_amount), cur, p_s, p_e, text[p_s:p_e], False))
                decimals.append(p_dec)
            pending = None
        mentions.append(_mention(Decimal(amount_s), cur, ps, pe, text[ps:pe], False))
        decimals.append(dec)
        prev_end, prev_cur = pe, cur
    return mentions, decimals


# ------------------------------------------------------------------------------------------- API
def extract_prices(text: str, default_currency: str | None = None) -> list[PriceMention]:
    """Every money amount and percentage in `text`, in text order."""
    return _scan(text, default_currency)[0]


def check_prices(
    text: str,
    allowed: Sequence[AllowedPrice],
    *,
    default_currency: str | None = None,
    allowed_percentages: Sequence[Decimal] = (),
) -> GuardResult:
    """ok iff every amount equals an allowed price at the speaker's precision (>= 2 dp) and every
    percentage is in `allowed_percentages`."""
    mentions, decimals = _scan(text, default_currency)
    if not mentions:
        return _OK_EMPTY
    # Check plan, built lazily per call: (currency|None, precision) -> quantized allowed amounts.
    # Decimal.__hash__ is expensive (~0.4 us), so small plans are tuples compared with ==; only
    # large allowed lists pay for frozensets.
    plan: dict[tuple[str | None, int], tuple[Decimal, ...] | frozenset[Decimal]] = {}
    small = len(allowed) <= _SMALL_PLAN
    bad: list[PriceMention] = []
    for idx, mention in enumerate(mentions):
        dec = decimals[idx]
        amount = mention.amount
        if dec < 0:
            if amount not in allowed_percentages:
                bad.append(mention)
            continue
        cur = mention.currency
        if small and _exact_hit(allowed, amount, cur):
            continue
        key = (cur, dec if dec > 2 else 2)
        ok_set = plan.get(key)
        if ok_set is None:
            ok_set = plan[key] = _quantized(allowed, cur, key[1], small=small)
        if amount not in ok_set:
            bad.append(mention)
    return _result(not bad, tuple(mentions), tuple(bad))


def _exact_hit(allowed: Sequence[AllowedPrice], amount: Decimal, cur: str | None) -> bool:
    """Equal to an allowed amount as written => also equal after quantizing that amount to the
    speaker's precision (which is >= the decimals written), so no rounding is needed."""
    for a in allowed:
        if a.amount == amount and (cur is None or a.currency == cur or a.currency.upper() == cur):
            return True
    return False


def _quantized(
    allowed: Sequence[AllowedPrice], cur: str | None, p: int, *, small: bool
) -> tuple[Decimal, ...] | frozenset[Decimal]:
    """Allowed amounts of a compatible currency, rounded half-up to p places."""
    exp = _EXPS[p] if p < len(_EXPS) else Decimal(1).scaleb(-p)
    out: list[Decimal] = []
    for a in allowed:
        ac = a.currency
        if cur is None or ac == cur or ac.upper() == cur:
            try:
                out.append(a.amount.quantize(exp, None, _CTX))
            except InvalidOperation:  # absurd magnitude/precision: cannot equal anything we parse
                continue
    return tuple(out) if small else frozenset(out)
