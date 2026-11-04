"""Neutral judge for price-guard candidates: hidden adversarial cases + same-machine timing + disagreement fuzz."""
import importlib
import random
import subprocess
import sys
import timeit
from decimal import Decimal as D

sys.path.insert(0, "candidates")
from kx.core.contracts import AllowedPrice  # noqa: E402

NAMES = [n for n in ("a", "b", "c") if importlib.util.find_spec(f"price_guard_{n}.price_guard")]
MODS = {n: importlib.import_module(f"price_guard_{n}.price_guard") for n in NAMES}

UNIT = AllowedPrice(amount=D("0.227804"), currency="AED")
TOTAL = AllowedPrice(amount=D("11390.20"), currency="AED")
USD_C = AllowedPrice(amount=D("3100"), currency="USD")
ALLOWED = [UNIT, TOTAL, USD_C]

# (text, expected ok, note) -- safety first: a missed low price is the costly failure
HIDDEN = [
    ("AED0.22/pc", False, "glued unit after slash, price still wrong"),
    ("price: 0.22 per pc", False, "keyword + bare number"),
    ("Our best: AED 0.19!", False, "exclamation"),
    ("AED 0.2278.", True, "sentence full stop"),
    ("AED 0.21 per piece", False, "no-break space"),
    ("2 x 40HC at USD 3,100 each", True, "container count ignored"),
    ("2 x 40HC at USD 2,900 each", False, "lower freight"),
    ("Unit price:\nAED 0.21", False, "newline between keyword and amount"),
    ("total AED 11,390.20 (incl. VAT)", True, "paren after"),
    ("total AED 11,390.19", False, "one fil lower"),
    ("سعر القطعة ٠٫٢١ درهم", False, "Arabic low price"),
    ("سعر القطعة ٠٫٢٣ درهم", True, "Arabic allowed at 2 dp"),
    ("单价 ¥0.21", False, "CNY not allowed"),
    ("We need 120,000 pcs by 2026-12-01", True, "qty + date only"),
    ("Call +971 4 123 4567 for the price", True, "phone near keyword"),
    ("price 0.22 AED", False, "suffix currency"),
    ("aed 0.21", False, "lowercase code"),
    ("Dh0.21", False, "glued Dh"),
    ("I can offer 10% discount", False, "percentage not allowed"),
    ("the rate is 0.23 per piece", True, "rounded unknown currency"),
    ("AED 0.227804 x 50,000 pcs = AED 11,390.20", True, "worked example"),
    ("AED 0.227804 x 50,000 pcs = AED 10,000.00", False, "wrong total"),
    ("Delivery 15/11/2026, 750 ml, 8011 alloy, Q-2026-00012", True, "noise only"),
    ("USD 3100", True, "no separator"),
    ("₹ 8.50/-", False, "INR not allowed"),
    ("Okay, I can do 0.21 each", False, "bare number + 'each' (no keyword)"),
    ("0.21 per piece is my final", False, "bare number + 'per piece'"),
    ("AED 0.22 or 0.21", False, "second amount in an 'or' list"),
    ("AED 0.21-0.23", False, "range with a low end"),
]


def hidden_score(mod):
    fails = []
    for text, exp, note in HIDDEN:
        try:
            got = mod.check_prices(text, ALLOWED, default_currency="AED").ok
        except Exception as e:  # noqa: BLE001
            got = f"EXC {type(e).__name__}"
        if got != exp:
            fails.append((note, text, exp, got))
    return fails


def spec(name):
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/spec/test_spec_price_guard.py", "-q",
                        "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
                       env={**__import__("os").environ, "PYTHONPATH": "candidates",
                            "KX_PRICE_GUARD": f"price_guard_{name}.price_guard"},
                       capture_output=True, text=True)
    return r.stdout.strip().splitlines()[-1]


CORPUS = [
    "Hello Ahmed, the price for 50,000 pcs of the 750 ml container is AED 0.227804 per piece.",
    "الإجمالي ١١٬٣٩٠٫٢٠ درهم شامل الضريبة",
    "We can deliver around 2026-12-03 to Colombo; call +971 50 123 4567.",
    "Unit AED 0.227804, total AED 11,390.20, or AED 0.19 for cash",
    "Thanks for your interest! Our team will get back to you shortly with details.",
] * 200
LONG = " ".join(CORPUS[:5]) * 20  # ~6 KB email-sized reply


def timing(mod):
    allowed = [UNIT, TOTAL]
    f1 = lambda: [mod.check_prices(t, allowed) for t in CORPUS]  # noqa: E731
    f2 = lambda: mod.check_prices(LONG, allowed)  # noqa: E731
    t1 = min(timeit.repeat(f1, number=5, repeat=7)) / 5 * 1000
    t2 = min(timeit.repeat(f2, number=200, repeat=7)) / 200 * 1e6
    return t1, t2


def fuzz(n=30000):
    rnd = random.Random(11)
    parts = ["AED", "aed", "Dhs", "USD", "$", "€", "₹", "Rs.", "price", "total", "rate", "pcs", "ml", "%",
             "x", "per piece", "for", "-", "/", ",", ".", ":", "\n", "40HC", "Q-2026-0001", "+971 50"]
    def num():
        k = rnd.random()
        if k < .3: return f"{rnd.randint(0, 99)}.{rnd.randint(0, 999999):0{rnd.choice([2,4,6])}d}"
        if k < .5: return f"{rnd.randint(1, 999)},{rnd.randint(0, 999):03d}" + (f".{rnd.randint(0,99):02d}" if rnd.random() < .5 else "")
        if k < .6: return f"{rnd.randint(1, 9)}.{rnd.randint(0,999):03d},{rnd.randint(0,99):02d}"
        return str(rnd.randint(0, 100000))
    disagree = []
    for _ in range(n):
        toks = [rnd.choice(parts) if rnd.random() < .6 else num() for _ in range(rnd.randint(2, 7))]
        text = " ".join(toks) if rnd.random() < .7 else "".join(toks)
        res = {}
        for name, m in MODS.items():
            try:
                res[name] = tuple((x.amount, x.currency, x.is_percentage) for x in m.extract_prices(text, "AED"))
            except Exception as e:  # noqa: BLE001
                res[name] = f"EXC {type(e).__name__}"
        if len(set(map(str, res.values()))) > 1:
            disagree.append((text, res))
    return disagree


if __name__ == "__main__":
    for n, m in MODS.items():
        fails = hidden_score(m)
        t1, t2 = timing(m)
        print(f"== {n}: spec [{spec(n)}] hidden {len(HIDDEN)-len(fails)}/{len(HIDDEN)} "
              f"| 1000 msgs {t1:.2f} ms | 6KB email {t2:.0f} us")
        for f in fails:
            print("   FAIL", f)
    d = fuzz()
    print(f"fuzz disagreements: {len(d)} / 30000")
    for text, res in d[:12]:
        print(repr(text), res)
