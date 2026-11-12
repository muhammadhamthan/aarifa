# Competing builds — results

Each hot-path module was built several times by independent agents using a different technique, judged by the
same spec suite (`tests/spec/`), then by a hidden adversarial set and a same-machine timing run.
Judges are kept in `candidates/judge_*.py` so the contest can be re-run when the code changes.

## 1. Pricing engine (`kx.crm.pricing.engine`) — winner: A

| | A — tuned `Decimal` | B — integer fractions |
|---|---|---|
| Spec tests | 28/28 | 28/28 |
| Exact vs `Fraction` reference (200,000 random costs, incl. half-way ties) | 0 mismatches | 0 mismatches |
| `evaluate_counter` | **1.57 µs** | 1.76 µs |
| `price_line` | **2.05 µs** | 2.91 µs |
| `unit_cost_per_1000` | **0.55 µs** | 1.05 µs |

Why A: equally exact, faster everywhere, and simpler to review. A rearranges each formula so only one division
is inexact (34-digit context), which is why it survives the half-way cases that broke a naive Decimal version.

Spec decision recorded from the contest: `NegotiationState.unit_cost` must be the **unrounded** cost/1000 ÷ 1000.
Using the 6-dp display value moves the opening offer from 0.227804 to 0.227805.

## 2. Price guard (`kx.core.price_guard`) — winner: C, then hardened

| | A — master regex | B — hand-written lexer | C — two-stage hybrid |
|---|---|---|---|
| Spec tests | 40/40 | 40/40 | 40/40 |
| Hidden cases (before hardening) | 28/29 | 27/29 | 27/29 |
| 1000 mixed messages | 9.38 ms | 5.60 ms | **5.84 ms** |
| 6 KB email | 623 µs | 598 µs | **539 µs** |
| Fuzz: amounts missed that another candidate caught | **1,991** | 4,159 | 2,171 |

Why C: nearly as fast as B but far safer (B missed twice as many amounts), and close to A on safety at ~40% of A's time.

Hardening added after the contest (all three missed these):
- a bare number followed by a per-piece phrase is a price: "0.21 each", "0.21 per piece", "8.50/-";
- any bare number written with 2+ decimals is a price: "I could go to 0.21".

After hardening: 40/40 spec, **29/29 hidden**, 5.4 ms per 1000 messages. These cases are now permanent tests in
`tests/spec/test_spec_price_guard.py::test_fail_closed_cases`.

Design choice: the guard **fails closed**. A false alarm costs one AI rewrite; a leaked low price costs margin.

Known limits (by design, all err towards blocking): "AED 1 250" (space thousands) reads as AED 1; a bare
quantity right after "price" with an unlisted noun ("price for 2 crates") is checked as a price.

## 3. Rules for all code (from the brief)

- No helper is written twice: `CONTRACTS.md` §2 lists the one place each helper lives.
- Packages never import each other; import-linter enforces it in CI.

## 4. After the 5 Nov flow change

- **Pricing engine A stays.** Its cost functions (`unit_cost_per_1000`, `freight_per_1000`, `resolve_band`, the cost-based ladder) are kept for **v2** automatic pricing.
- v1 adds a pure price-list layer (`resolve_tier`, `ladder_between`, a ladder-based `evaluate_counter` with `low_ask_count`) in WP2. Re-run `candidates/judge_pricing.py`-style exactness checks on the new functions, and add the §11–12 numbers as spec tests.
- **Price guard C is unchanged.** `allowed_prices` becomes the approved quote prices plus the ladder steps from the price list.

## 5. Platform contest (7–11 Nov)

The vendor and platform contest (rounds 1–4: AI brain, voice, telephony, WhatsApp, email, database, cache,
embeddings, monitoring, login, background jobs) is recorded in the read-only platform docs:
`docs/Arifaa Platform — Platforms, Pricing & Fallbacks/Platform contest.md`, with the prices it used in
`Official sources.md`. Outcomes that change this code base (11 Nov):

| Field | Winner | Runner-up / backup | Code |
|---|---|---|---|
| Embeddings | Qwen3-Embedding-0.6B in a UAE container (28/30) | BAAI/bge-m3 (26/30) | `arifaa_pack.adapters.HttpEmbedder` (WP23) |
| Background jobs | Celery 5.7 + Celery Beat (accepted with conditions) | Procrastinate (previous) | `kx.core.jobs`, `arifaa_pack.celery_app` (WP20) |
| Staff login | In-house, TOTP first (accepted with conditions) | Amazon Cognito (previous) | `kx.crm.services.auth`, `kx.core.auth` (WP21) |
| Monitoring | Better Stack for uptime, errors, logs, on-call | CloudWatch alarms + SNS email | `arifaa_pack.observability` (WP24) |
| AI brain | Claude mix: Sonnet 5.5 on money turns, Haiku 5.5 on the rest (24/30) | Gemini backups only after the suite passes and Arifa agrees | `kx.agents.models` (WP22) |
| Alert calls | Telnyx after live du/Etisalat tests (2–1) | Twilio | `kx.automation.channels.telnyx` (WP25) |
| AI tracing | Arize Phoenix self-hosted in the UAE (3–0) | CloudWatch logs | `kx.agents.tracing` (WP24) |
