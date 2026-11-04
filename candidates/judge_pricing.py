import importlib, random, timeit, sys
from decimal import Decimal as D
from fractions import Fraction as F
sys.path.insert(0, "candidates")
from kx.crm.pricing.types import CostInputs, MarginBand, QuoteLineInput, NegotiationState
A = importlib.import_module("pricing_a.engine"); B = importlib.import_module("pricing_b.engine")

def rhu(fr, places):  # exact round-half-up of a Fraction
    s = 10**places; x = fr * s
    n = (abs(x.numerator) * 2 + x.denominator) // (2 * x.denominator)
    return D(n if x >= 0 else -n).scaleb(-places)

def ref_cost(c):
    v = F(c.kg_per_1000) / (F(c.yield_pct) / 100) * F(c.material_rate_per_kg) + F(c.production_per_1000) + F(c.packaging_per_1000) + F(c.freight_per_1000)
    return rhu(v, 4)

rnd = random.Random(7); bad = {"A": 0, "B": 0}; ex = []
def dec(lo, hi, p): return D(rnd.randint(lo * 10**p, hi * 10**p)).scaleb(-p)
for _ in range(200_000):
    c = CostInputs(dec(1, 40, 3), dec(0, 30, 3), D(rnd.choice([50, 60, 75, 80, 90, 92, 95, 96, 100])), dec(0, 80, 2), dec(0, 20, 2))
    r = ref_cost(c)
    for name, m in (("A", A), ("B", B)):
        got = m.unit_cost_per_1000(c)
        if got != r or got.as_tuple().exponent != -4:
            bad[name] += 1
            if len(ex) < 4: ex.append((name, c, got, r))
print("cost mismatches vs exact:", bad, ex[:2])

band = MarginBand(D(40), D(50), 3)
li = QuoteLineInput("x", D(50000), CostInputs(D("12.00"), D("8.5"), D("92"), D("35"), D("6")))
st = NegotiationState(D("0.1518696"), band, 1, D("0.222742"), D(50000))
for name, m in (("A", A), ("B", B)):
    m.concession_ladder.cache_clear() if hasattr(m.concession_ladder, "cache_clear") else None
    for label, fn in (("evaluate_counter", lambda: m.evaluate_counter(st, D("0.215"), D(60000))),
                      ("price_line", lambda: m.price_line(li, band)),
                      ("unit_cost_per_1000", lambda: m.unit_cost_per_1000(li.cost))):
        best = min(timeit.repeat(fn, number=20000, repeat=7)) / 20000 * 1e6
        print(f"{name} {label:20s} {best:.3f} us")
