# CLAUDE.md: guide for Claude Code in this repo

Kairox platform: a reusable B2B sales system (CRM + automation + AI agents). The first client is Arifa Packing & Packaging LLC (Arifaa Pack, Ajman, UAE). Python 3.14, uv workspace.

## Read first (in this order, only what the task needs)

1. `docs/V1_CHANGE_PLAN.md`: the 5 Nov work list (WP1–WP12, **done** 6 Nov; log in `docs/V1_PROGRESS.md`, open decisions in `docs/OPEN_QUESTIONS.md`).
2. `docs/BUSINESS_FLOW_V1.md`: the business rules (SRS source). Quote its § numbers in commits.
3. `CONTRACTS.md`: package boundaries, shared helpers, hard safety rules.
4. `docs/REUSE_CATALOGUE.md`: where each capability lives. Import, never rewrite.
5. Background only if needed: `docs/HANDOFF_CONTEXT.md`, `docs/DELIVERY.md`, `docs/DEPLOYMENT.md`, `docs/CONTESTS.md`.

## Layout

- `packages/kx-core-db` → `kx.core`: shared core, contracts, price guard.
- `packages/kx-core-crm` → `kx.crm`: the CRM; pricing is in `kx/crm/pricing`.
- `packages/kx-core-automation` → `kx.automation`: channels and jobs.
- `packages/kx-agent-framework` → `kx.agents`: LangGraph agents.
- `apps/arifaa-pack` → `arifaa_pack`: wiring (`container.py`), API, CLI `kx`, Alembic `migrations/`.
- `db/ddl/0*.sql`: the v1 baseline. **Never edit it.** Schema changes are new Alembic revisions (`migrations/<db>/versions/000N_*.py`).
- `infra/`: Terraform. `tests/`: root unit, spec, DB and integration tests. Each package also has its own `tests/`.

## Commands

```bash
uv sync --all-packages --all-extras
make up                                   # local postgres (pgvector) + valkey
export KX_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres
make test                                 # or: uv run pytest packages/kx-core-crm/tests -q
make lint                                 # ruff, ruff format --check, mypy --strict, lint-imports
make migrate                              # kx migrate (alembic + procrastinate schema)
uv run pytest tests/spec -m bench --benchmark-enable   # speed checks
```

## Rules

- **Packages never import each other** (import-linter). They cross only through `kx.core.contracts` Protocols and events (`kx.core.events`, `kx.core.event_payloads`).
- **Money:** Decimal only (`kx.core.money`), never float.
- **IDs:** `kx.core.identity.new_id`; hashes and phone/email normalisation come from `kx.core.identity`.
- **Every state change:** `kx.crm.services.journal.record()` writes audit + timeline + outbox in the same transaction.
- **AI-visible data** (`DealContext` and DTOs) never contains cost, markup, the minimum price or ladder steps.
- **Every AI reply** passes `kx.core.price_guard.check_prices` (it fails closed). Priced messages go out only through `OutboundService` with a single-use send grant.
- **Approvals** use `require_human_mfa`.
- **Style:** `from __future__ import annotations` (but **not** in FastAPI router modules: it breaks dependencies defined inside router factories); `mypy --strict`; ruff line length 110; small functions; docstrings that explain the business reason.
- **Tests:** pure logic gets spec or unit tests (hypothesis where useful); DB behaviour gets `@pytest.mark.db` tests using the root `databases` fixture.
- **Dependencies:** don't add any without asking; everything needed is in `uv.lock`. After any `pyproject.toml` change, run `uv lock` and commit the lock.
- **Commits:** small, one work package (WP) at a time, with the § reference.

## Saving tokens

- Read only the files a WP names. Use `grep -n` before opening big files.
- Run the narrowest test target first (one package or one file), then `make test` once at the end.
