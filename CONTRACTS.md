# Kairox Platform — package contracts

Read this before writing code in any package. It answers three questions: **who owns what**, **what each package may import**, and **which shared helper already does what you are about to write**.

## 1. Packages and ownership

| Package (dist name → import) | Owns | May import | Must never |
|---|---|---|---|
| `kx-core-db` → `kx.core` | settings, DB engines/sessions, money, ids, time windows, events/outbox/inbox, errors, auth, logging, **contracts** (DTOs + Protocols), price guard | stdlib, pydantic, sqlalchemy, fastapi, structlog, orjson, phonenumbers, pyjwt | import any other `kx.*` package |
| `kx-core-crm` → `kx.crm` | `crm` DB: deals, quotes, approvals, send grants, pricing engine, settings, price proposals, orders, stock, retention | `kx.core` | import `kx.automation` / `kx.agents` |
| `kx-core-automation` → `kx.automation` | `automation` DB: channels (WhatsApp, SES, Twilio IVR), OutboundService, webhooks, procrastinate jobs, scrapers | `kx.core` | import `kx.crm` / `kx.agents` |
| `kx-agent-framework` → `kx.agents` | `agents` DB: AgentSpec registry, orchestrator, graphs, tools, guard middleware, memory, KB search, voice routing | `kx.core` | import `kx.crm` / `kx.automation` |
| `arifaa-pack` → `arifaa_pack` | client wiring: api/worker/voice entrypoints, `company.yaml`, prompts, migrations | everything | be imported by platform packages |

Enforced by import-linter (`uv run lint-imports`). Packages talk to each other only through:
1. **Protocols in `kx.core.contracts`** (`CrmGateway`, `GrantConsumer`, `MessageSender`) — implemented by one package, injected by the app.
2. **Events** (`kx.core.events` catalogue) written to the owning DB's outbox in the same transaction as the state change.

No cross-database foreign keys: reference other DBs with `*_ref uuid` columns.

## 2. Shared helpers — use these, never re-implement

| Need | Use |
|---|---|
| Settings / DSNs | `kx.core.settings.get_settings()`, `CoreSettings`, `LogicalDb` |
| Engine, session, transaction | `kx.core.db.Databases` → `.transaction(LogicalDb.X)`, `.dependency(LogicalDb.X)`; ORM bases via `make_base("crm")` |
| Money | `kx.core.money`: `d`, `q`, `pct`, `display`, `to_currency`, `MONEY_PLACES` (4 dp), `UNIT_PRICE_PLACES` (6 dp). **Never float.** |
| Ids, hashes | `kx.core.identity`: `new_id` (UUIDv7), `sha256_hex`, `hmac_sha256_hex`, `constant_time_equals` |
| Phone / email / recipient | `normalise_phone` (E.164, default AE), `normalise_email`, `normalise_address`, `recipient_hash(channel, address)` |
| Business hours, approval timer | `kx.core.timewindow.Window` (`contains`, `next_open`, `add_open_minutes`) |
| Events | `Event`, `Outbox.add`, `relay_once`, `inbox_once`, `InMemoryBus`, event-name constants |
| Errors → HTTP | `DomainError` subclasses (`NotFound`, `Conflict`, `Forbidden`, `Unauthorized`, `RuleViolation`) + `install_problem_handlers(app)` |
| Auth | `current_principal`, `require_role(...)`, `require_human_mfa(...)` (approvals), `Role`, `Principal` |
| Logging | `get_logger(__name__)`, `configure_logging`, `mask_pii`; PII is masked by a processor — don't hand-mask |
| Price safety | `kx.core.price_guard.check_prices(text, allowed, default_currency=..., allowed_percentages=...)` |
| Pricing maths | `kx.crm.pricing.engine` (`unit_cost_per_1000`, `freight_per_1000`, `resolve_band`, `price_line`, `quote_totals`, `concession_ladder`, `evaluate_counter`, `estimate_delivery`) |
| HTTP client | one shared `httpx.AsyncClient` per process (created in app lifespan, injected) |
| Retries | `tenacity` (`AsyncRetrying`, exponential + jitter); never hand-rolled loops |
| JSON | `orjson` |

## 3. Hard rules (business safety)

- **Nothing reaching the AI may contain cost, markup, margin range or floor.** `kx.core.contracts` DTOs are the only shapes agents see.
- **Priced messages** (`PRICED_KINDS`) leave only via `MessageSender.send`, which must call `GrantConsumer.consume_grant(...)` first; a `False` means do not send. The recipient comes from the contact record, never from the AI.
- **Human approvals** require `require_human_mfa(Role.MANAGER | Role.MD)`; final quotes require MD.
- Every AI reply passes `check_prices` against `DealContext.allowed_prices` before it is sent or spoken.
- Advance payment is always required; longer delivery is never a concession (DB CHECK enforces).
- Manager alerts only 08:00–22:00 Asia/Dubai; the 2 h approval window pauses outside (`Window.add_open_minutes`).
- Follow-ups day 2/5/10, max 2 per week, 09:00–18:00 buyer local time.

## 4. Code standards

- Python 3.14, `from __future__ import annotations`, full type hints, `mypy --strict` clean, `ruff` clean (config in root `pyproject.toml`).
- Async everywhere on I/O paths (SQLAlchemy 2.1 async + asyncpg; psycopg 3 only for procrastinate and LangGraph checkpointer).
- Pure logic in plain functions over frozen dataclasses / pydantic models; I/O in services; FastAPI routers thin.
- Tests: `tests/` inside each package, `pytest` + `hypothesis`; DB tests marked `@pytest.mark.db` use the root `databases` fixture.
- Do not add dependencies without the lead: everything allowed is already in `uv.lock`.
