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
| Pricing maths | v1: `kx.crm.pricing.price_list` (`resolve_tier`, `min_price_for`, `convert_tier`, `list_line`) and `kx.crm.pricing.engine` (`ladder_between`, `evaluate_ladder_counter`, `quote_totals`); v2 (off): `unit_cost_per_1000`, `freight_per_1000`, `resolve_band`, `price_line`, `concession_ladder`, `evaluate_counter`, `estimate_delivery` |
| HTTP client | one shared `httpx.AsyncClient` per process (created in app lifespan, injected) |
| Retries | `tenacity` (`AsyncRetrying`, exponential + jitter); never hand-rolled loops |
| JSON | `orjson` |

## 3. Hard rules (business safety): 5 Nov flow, see docs/BUSINESS_FLOW_V1.md

**Status:** the code still implements the 4 Nov rules (MD approvals, cost-based pricing). `docs/V1_CHANGE_PLAN.md` lists the changes needed to reach these rules.

- **Nothing reaching the AI may contain cost, markup, the minimum price, the ladder steps or other customers' data.** `kx.core.contracts` DTOs are the only shapes agents see.
- **Priced messages** (`PRICED_KINDS`) leave only via `MessageSender.send`, which must call `GrantConsumer.consume_grant(...)` first; `False` means do not send. The recipient comes from the contact record, never from the AI.
- **Approvals:**
  - the deal's assigned **salesperson** (role `sales`) approves the 1st quote, every revision and the final quote;
  - the **manager** may approve any deal;
  - the **MD** only deals it has taken over (the owner is read-only meanwhile);
  - all approvals need `require_human_mfa`;
  - a price **below the tier minimum** needs a manager-approved `special_price` approval.
- **Pricing v1 = price list:**
  - per product and quantity tier, a list price (the AI quotes it) and a minimum (the AI never goes below it);
  - at most 3 equal concession steps; steps 2–3 only for a bigger quantity;
  - cost/markup pricing, scraping and the staleness block are v2 and off by flag.
- **The AI negotiates price only.** Product, spec, printing, quantity outside the tiers, delivery, samples and payment all become a **change request** to the salesperson. Payment is never discussed by the AI.
- **Every AI reply passes `check_prices`** against `DealContext.allowed_prices` (the approved quote plus the ladder steps for that deal) before it is sent or spoken.
- **Advance payment is always required.** Longer delivery is never a concession (a DB CHECK enforces it).
- **Staff alerts:**
  - only 08:00–22:00 Asia/Dubai;
  - reminders at 30 min (WhatsApp), 60 min (call) and 120 min (escalate to the manager);
  - timers pause outside those hours (`Window.add_open_minutes`).
- **Buyer follow-ups:**
  - day 2/5/10, at most 2 per week;
  - 09:00–18:00 buyer local time, on the buyer country's working days;
  - stop on reply or opt-out.
- **Handover:** after final approval, the salesperson is alerted and the manager notified, and the AI stops replying on that deal.

**Counter-offer verdicts (WP2):** `CounterVerdictKind.REQUOTE` (§12.2 rule 5: the quantity moved into another tier; the CRM drafts a revised quote for the salesperson and the AI gets no price, `offer_unit_price=None`) and `CHANGE_REQUEST` (quantity below the MOQ or outside the tiers, or a hand-priced item: a human answers); `CounterVerdict.requires_approval`. `DealContext.allowed_prices` = the issued quote plus the ladder steps actually offered; steps not yet offered and the minimum never reach the AI.

**Approval events (WP3):** `QuoteSubmitted.required_role` adds `sales`; `kind` adds `special_price`; `approver` is the deal's salesperson (the MD on a takeover, the manager for a special price) and `escalate_to` is the manager; `due_at` is the 120-minute escalation counted in alert hours. `ApprovalOverdue.escalate_to` is the manager. Grant `policy` adds `sales`.

**Change requests (WP4):** events `change_request.raised.v1` (`ChangeRequested`: deal, card, type, `assigned_to`, `escalate_to`, `sla_due_at`, `remind_at`, `escalate_at`) and `change_request.closed.v1` (`ChangeRequestClosed`); `CrmGateway.raise_change_request(deal_id, type, buyer_text, fields, actor) -> UUID` with `ChangeType` and `ChangeField`; `DealContext.salesperson_name` and `change_request_hours`. `QuoteSubmitted.remind_at` carries the §16 reminder times; automation's `ApprovalStatusReader.is_pending` answers for approvals and change requests.

**Assignment (WP5):** event `deal.assigned.v1` (`DealAssigned`: `assigned_to`, `previous`, `notify`, `reason` round_robin | rule | returning_customer | backup | manager | reassigned | takeover | hand_back). `ChangeRequested.alert_ref` identifies the current reminder ladder; a reassignment issues a new one.

**Sending (WP6):** `OutboundRequest.buyer_country` and `QuoteApproved.buyer_country` / `language` pick the regional WhatsApp number; `CrmGateway.set_cover_note(deal_id, note, actor)` stores the AI's "open to discuss" line on the quote waiting for approval (message only, hashed into the grant with the rest of the body).

**Handover (WP7):** `QuoteApproved.salesperson` / `manager` are set for the final quote only (automation alerts the salesperson and notifies the manager); `DealContext.ai_paused_reason` and `team_available` let the orchestrator answer a handed-over deal with the fixed out-of-hours acknowledgement only (`Route.action = "acknowledge"`).

**v2 off (WP9):** `DeliveryEstimateView` dates are optional and `lead_time_text` carries the v1 answer (the product's default delivery time, D10) while `features.delivery_calc` is off.

## 4. Code standards

- Python 3.14, `from __future__ import annotations`, full type hints, `mypy --strict` clean, `ruff` clean (config in root `pyproject.toml`).
- Async everywhere on I/O paths (SQLAlchemy 2.1 async + asyncpg; psycopg 3 only for procrastinate and LangGraph checkpointer).
- Pure logic in plain functions over frozen dataclasses / pydantic models; I/O in services; FastAPI routers thin.
- Tests: `tests/` inside each package, `pytest` + `hypothesis`; DB tests marked `@pytest.mark.db` use the root `databases` fixture.
- Do not add dependencies without the lead: everything allowed is already in `uv.lock`.
