# CLAUDE.md: guide for Claude Code in this repo

Kairox platform: a reusable B2B sales system (CRM + automation + AI agents). The first client is Arifa Packing & Packaging LLC (Arifaa Pack, Ajman, UAE). Python 3.14, uv workspace.

## Read first (in this order, only what the task needs)

1. **The SRS** (business rules, read-only): `docs/Arifaa Pack — Detailed Business Flow v1 (SRS basis) (2).md`. Quote its § numbers in commits. **Never edit it.**
2. **The platform docs** (vendors, fallbacks, costs, read-only): `docs/Arifaa Platform — Platforms, Pricing & Fallbacks/` — `Recommendation.md` and `Fallback plan.md` first. **Never edit them.**
3. `docs/V1_CHANGE_PLAN.md`: the 11 Nov work list (WP13–WP29, decisions D17–D40) above the finished 5 Nov plan (WP1–WP12). Log in `docs/V1_PROGRESS.md`; open decisions in `docs/OPEN_QUESTIONS.md`.
4. `CONTRACTS.md`: package boundaries, shared helpers, hard safety rules.
5. `docs/REUSE_CATALOGUE.md`: where each capability lives. Import, never rewrite.
6. Background only if needed: `docs/HANDOFF_CONTEXT.md`, `docs/DELIVERY.md`, `docs/DEPLOYMENT.md`, `docs/CONTESTS.md`, `infra/README.md`.
7. Frontend hand-offs (generated from the code): `docs/frontend/`.

## Layout

- `packages/kx-core-db` → `kx.core`: shared core, contracts, price guard, auth tokens, job store, fallback/circuit breaker.
- `packages/kx-core-crm` → `kx.crm`: the CRM; pricing is in `kx/crm/pricing`; staff login in `kx/crm/services/auth.py`.
- `packages/kx-core-automation` → `kx.automation`: channels (WhatsApp, SES, Postmark; Twilio for calls), opt-out, outbound gate, jobs. (Telnyx is planned, not built — OPEN #55.)
- `packages/kx-agent-framework` → `kx.agents`: LangGraph agents, knowledge search, voice, website sessions.
- `apps/arifaa-pack` → `arifaa_pack`: wiring (`container.py`), Celery app (`celery_app.py`), API, CLI `kx`, Alembic `migrations/`.
- `db/ddl/0*.sql`: the v1 baseline. **Never edit it.** Schema changes are new Alembic revisions (`migrations/<db>/versions/000N_*.py`).
- `infra/`: Terraform. `tests/`: root unit, spec, DB and integration tests. Each package also has its own `tests/`.

## Commands

```bash
uv sync --all-packages --all-extras
make up                                   # local postgres (pgvector) + redis
export KX_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres
make test                                 # or: uv run pytest packages/kx-core-crm/tests -q
make lint                                 # ruff, ruff format --check, mypy --strict, lint-imports
make migrate                              # kx migrate (alembic, every DB)
kx api | kx worker | kx beat | kx voice   # API, Celery worker, the ONE Celery Beat, LiveKit voice worker
uv run pytest tests/spec -m bench --benchmark-enable   # speed checks
```

## Rules

- **Packages never import each other** (import-linter). They cross only through `kx.core.contracts` Protocols and events (`kx.core.events`, `kx.core.event_payloads`).
- **Celery is imported only by the app** (`arifaa_pack`). Packages schedule work through `kx.core.jobs` (PostgreSQL schedule table) and the `TaskScheduler` protocol. Long waits are rows in the table, never Celery ETAs. Every task is idempotent (`acks_late` can redeliver).
- **Money:** Decimal only (`kx.core.money`), never float.
- **IDs:** `kx.core.identity.new_id`; hashes and phone/email normalisation come from `kx.core.identity`.
- **Every state change:** `kx.crm.services.journal.record()` writes audit + timeline + outbox in the same transaction.
- **The backend builds every quote** (§11.1); the AI never calculates or types a price.
- **AI-visible data** (`DealContext` and DTOs) never contains cost, markup, the minimum price or ladder steps.
- **Every AI reply** passes `kx.core.price_guard.check_prices` (it fails closed), whichever model or vendor answered. Priced messages go out only through `OutboundService` with a single-use send grant.
- **Opt-out (§8.1):** one suppression list checked before every message and call; the AI may add a stop, never remove one.
- **Approvals** use `require_human_mfa` (TOTP factor; an email-code login is not enough).
- **Vendors:** call a field's adapter (`llm`, `calls`, `email`, `embeddings` …), never a vendor directly; the order comes from Parameter Store; a switch must be logged and alerted.
- **Style:** `from __future__ import annotations` (but **not** in FastAPI router modules: it breaks dependencies defined inside router factories); `mypy --strict`; ruff line length 110; small functions; docstrings that explain the business reason.
- **Tests:** pure logic gets spec or unit tests (hypothesis where useful); DB behaviour gets `@pytest.mark.db` tests using the root `databases` fixture.
- **Dependencies:** don't add any without asking; everything needed is in `uv.lock`. After any `pyproject.toml` change, run `uv lock` and commit the lock.
- **Commits:** small, one work package (WP) at a time, with the § / D reference.

## Saving tokens

- Read only the files a WP names. Use `grep -n` before opening big files (the SRS is ~1,050 lines: grep its `## ` headings).
- Run the narrowest test target first (one package or one file), then `make test` once at the end.
