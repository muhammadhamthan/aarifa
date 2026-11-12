# Kairox Platform — package contracts

Read this before writing code in any package. It answers three questions: **who owns what**, **what each package may import**, and **which shared helper already does what you are about to write**.

Business rules come from the SRS (`docs/Arifaa Pack — Detailed Business Flow v1 (SRS basis) (2).md`); vendors and fallbacks from `docs/Arifaa Platform — Platforms, Pricing & Fallbacks/`. Both are read-only.

## 1. Packages and ownership

| Package (dist name → import) | Owns | May import | Must never |
|---|---|---|---|
| `kx-core-db` → `kx.core` | settings, DB engines/sessions, money, ids, time windows, events/outbox/inbox, **job store**, errors, **auth (token issue + verify)**, logging, metrics, **fallback/circuit breaker**, spend caps, **contracts** (DTOs + Protocols), price guard | stdlib, pydantic, sqlalchemy, fastapi, structlog, orjson, phonenumbers, pyjwt, cryptography, redis | import any other `kx.*` package |
| `kx-core-crm` → `kx.crm` | `crm` DB: deals, quotes, approvals, send grants, pricing engine, settings, price proposals, orders, stock, retention, **staff users + login**, catalogue mirror, Requirement Summary | `kx.core` | import `kx.automation` / `kx.agents` |
| `kx-core-automation` → `kx.automation` | `automation` DB: channels (WhatsApp, SES, Postmark; Twilio for calls; Telnyx planned, OPEN #55), OutboundService, **opt-out**, webhooks, job bodies + the schedule table, scrapers | `kx.core` | import `kx.crm` / `kx.agents` |
| `kx-agent-framework` → `kx.agents` | `agents` DB: AgentSpec registry, orchestrator, graphs, tools, guard middleware, memory, KB search, voice routing, **website sessions** | `kx.core` | import `kx.crm` / `kx.automation` |
| `arifaa-pack` → `arifaa_pack` | client wiring: api / worker / beat / voice entrypoints, **the Celery app**, vendor adapters, `company.yaml`, prompts, migrations | everything | be imported by platform packages |

Enforced by import-linter (`uv run lint-imports`). Packages talk to each other only through:
1. **Protocols in `kx.core.contracts`** (`CrmGateway`, `GrantConsumer`, `MessageSender`, `StaffMailer`, …): implemented by one package, injected by the app.
2. **Events** (`kx.core.events` catalogue) written to the owning DB's outbox in the same transaction as the state change.

No cross-database foreign keys: reference other DBs with `*_ref uuid` columns. **Celery is imported only by `arifaa_pack`**: packages never see it.

## 2. Shared helpers — use these, never re-implement

| Need | Use |
|---|---|
| Settings / DSNs | `kx.core.settings.get_settings()`, `CoreSettings`, `LogicalDb` |
| Engine, session, transaction | `kx.core.db.Databases` → `.transaction(LogicalDb.X)`, `.dependency(LogicalDb.X)`; ORM bases via `make_base("crm")` |
| Money | `kx.core.money`: `d`, `q`, `pct`, `display`, `to_currency`, `MONEY_PLACES` (4 dp), `UNIT_PRICE_PLACES` (6 dp). **Never float.** |
| Ids, hashes | `kx.core.identity`: `new_id` (UUIDv7), `sha256_hex`, `hmac_sha256_hex`, `constant_time_equals` |
| Phone / email / recipient | `normalise_phone` (E.164, default AE), `phone_kind` (mobile / landline / invalid), `normalise_email`, `normalise_address`, `recipient_hash(channel, address)` |
| Business hours, approval timer | `kx.core.timewindow.Window` (`contains`, `next_open`, `add_open_minutes`) |
| Events | `Event`, `Outbox.add`, `relay_once`, `inbox_once`, `InMemoryBus`, event-name constants |
| Background work, timers | `kx.core.jobs`: `JobStore` (`enqueue`, `replace`, `pull_forward`, `cancel`, `claim`, `finish`, `fail`, `due`, `recover`), `JobSpec`, `Kicker` protocol (the app's Celery sender) |
| Vendor fallback | `kx.core.fallback`: `CircuitBreaker`, `FallbackChain`, the bare `VendorSwitch` metric; the Parameter Store order is `arifaa_pack.adapters.VendorOrder` (the app owns Parameter Store) |
| Daily spend caps | `kx.core.spend.SpendMeter` (Redis counters; 80 % alert, 100 % degrade) |
| Metrics | `kx.core.metrics.emit_metric` (CloudWatch Embedded Metric Format log line) |
| Errors → HTTP | `DomainError` subclasses (`NotFound`, `Conflict`, `Forbidden`, `Unauthorized`, `RuleViolation`, `TooManyRequests`) + `install_problem_handlers(app)` |
| Auth | `current_principal`, `require_role(...)`, `require_human_mfa(...)` (approvals), `Role`, `Principal`, `TokenIssuer` (RS256 access tokens), `TokenIssuer.jwks()` (served at `/.well-known/jwks.json`) |
| Passwords, one-time codes | `kx.core.passwords`: `hash_password` / `verify_password` (Argon2id), `totp_now` / `verify_totp` (RFC 6238), `new_numeric_code` |
| Logging | `get_logger(__name__)`, `configure_logging`, `mask_pii`; PII is masked by a processor — don't hand-mask |
| Price safety | `kx.core.price_guard.check_prices(text, allowed, default_currency=..., allowed_percentages=...)` |
| Pricing maths | v1: `kx.crm.pricing.price_list` (`resolve_tier`, `min_price_for`, `convert_tier`, `list_line`) and `kx.crm.pricing.engine` (`ladder_between`, `evaluate_ladder_counter`, `quote_totals`); v2 (off): `unit_cost_per_1000`, `freight_per_1000`, `resolve_band`, `price_line`, `concession_ladder`, `evaluate_counter`, `estimate_delivery` |
| HTTP client | one shared `httpx.AsyncClient` per process (created in app lifespan, injected) |
| Retries | `tenacity` (`AsyncRetrying`, exponential + jitter); never hand-rolled loops |
| JSON | `orjson` |

## 3. Hard rules (business safety)

**Status (11 Nov):** the 5 Nov rules below are built (WP1–WP12). The 11 Nov rules (marked **[11 Nov]**) are the WP13–WP29 work list in `docs/V1_CHANGE_PLAN.md`. The 4 Nov cost-based pricing remains as v2 behind `pricing.mode='cost'`.

- **Nothing reaching the AI may contain cost, markup, the minimum price, the ladder steps or other customers' data.** `kx.core.contracts` DTOs are the only shapes agents see.
- **[11 Nov] The backend builds every quote** (§11.1, D17): tier, list price, FX, VAT, specs, delivery time, terms, version number and PDF come from fixed rules over the catalogue and price list. The AI collects the inputs and writes only the covering message. A line the system cannot price stays empty and becomes a Pricing-needed task.
- **Priced messages** (`PRICED_KINDS`) leave only via `MessageSender.send`, which must call `GrantConsumer.consume_grant(...)` first; `False` means do not send. The recipient comes from the contact record, never from the AI. **[11 Nov]** A fallback to another channel (D32) needs its own grant for that channel, issued by the CRM for the same approved version.
- **Approvals:**
  - the deal's assigned **salesperson** (role `sales`) approves the 1st quote, every revision and the final quote;
  - the **manager** may approve any deal;
  - the **MD** only deals it has taken over (the owner is read-only meanwhile);
  - all approvals need `require_human_mfa` — **[11 Nov]** the TOTP factor (D26; an email-code login is not MFA for approvals, see OPEN_QUESTIONS #49);
  - a price **below the tier minimum** needs a manager-approved `special_price` approval.
- **Pricing v1 = price list:**
  - per product and quantity tier, a list price (the AI quotes it) and a minimum (the AI never goes below it);
  - at most 3 equal concession steps; steps 2–3 only for a bigger quantity;
  - cost/markup pricing, scraping and the staleness block are v2 and off by flag.
- **The AI negotiates price only.** Product, spec, printing, quantity outside the tiers, delivery, samples and payment all become a **change request** to the salesperson. Payment is never discussed by the AI.
- **Every AI reply passes `check_prices`** against `DealContext.allowed_prices` (the approved quote plus the ladder steps for that deal) before it is sent or spoken — **[11 Nov]** whichever model answered (Claude, a tested backup, or the safe reply). A backup that fails the guard twice gives the safe reply and a hand-over.
- **[11 Nov] Opt-out (§8.1, D18):** rules match first; one suppression list (scopes `all`, `whatsapp`, `email`, `phone`) is checked before every message and every call; queued follow-ups, reminders and templates are cancelled; exactly one plain confirmation (no apology, button, question, offer or promotion) within 5 minutes; the AI may **add** a stop, never remove one; every decision is logged permanently.
- **[11 Nov] Phone confirmation (§8.2, D19):** never an OTP, never "verify your identity" or "for security", never a test call. WhatsApp quotes go only to numbers that are Reachable or better.
- **Advance payment is always required** (the salesperson confirms it; **[11 Nov]** its details are optional, D20). Longer delivery is never a concession (a DB CHECK enforces it).
- **Staff alerts:**
  - only 08:00–22:00 Asia/Dubai;
  - reminders at 30 min (WhatsApp), 60 min (call) and 120 min (escalate to the manager);
  - timers pause outside those hours (`Window.add_open_minutes`).
- **Buyer follow-ups:**
  - day 2/5/10, at most 2 per week;
  - 09:00–18:00 buyer local time, on the buyer country's working days;
  - stop on reply or opt-out;
  - text only: no calls to buyers unless they asked (§16); **no cold AI calls**, ever.
- **Handover:** after final approval, the salesperson is alerted and the manager notified, and the AI stops replying on that deal. **[11 Nov]** The salesperson continues on the same company number from the CRM inbox (D21).
- **[11 Nov] Fallbacks (D30):** call a field's adapter, never a vendor. The price guard, approval gate, consent and quiet hours run after every switch. A price turn falls back only to a model that passed the negotiation suite (none yet: D27). No job runs twice with effect: every task checks an idempotency key before sending a message or placing a call. Spend caps per company per day (D34). Kill-switch ladder (D35).

**Counter-offer verdicts (WP2):** `CounterVerdictKind.REQUOTE` (§12.2 rule 5: the quantity moved into another tier; the CRM drafts a revised quote for the salesperson and the AI gets no price, `offer_unit_price=None`) and `CHANGE_REQUEST` (quantity below the MOQ or outside the tiers, or a hand-priced item: a human answers); `CounterVerdict.requires_approval`. `DealContext.allowed_prices` = the issued quote plus the ladder steps actually offered; steps not yet offered and the minimum never reach the AI.

**Approval events (WP3):** `QuoteSubmitted.required_role` adds `sales`; `kind` adds `special_price`; `approver` is the deal's salesperson (the MD on a takeover, the manager for a special price) and `escalate_to` is the manager; `due_at` is the 120-minute escalation counted in alert hours. `ApprovalOverdue.escalate_to` is the manager. Grant `policy` adds `sales`.

**Change requests (WP4):** events `change_request.raised.v1` (`ChangeRequested`: deal, card, type, `assigned_to`, `escalate_to`, `sla_due_at`, `remind_at`, `escalate_at`) and `change_request.closed.v1` (`ChangeRequestClosed`); `CrmGateway.raise_change_request(deal_id, type, buyer_text, fields, actor) -> UUID` with `ChangeType` and `ChangeField`; `DealContext.salesperson_name` and `change_request_hours`. `QuoteSubmitted.remind_at` carries the §16 reminder times; automation's `ApprovalStatusReader.is_pending` answers for approvals and change requests.

**Assignment (WP5):** event `deal.assigned.v1` (`DealAssigned`: `assigned_to`, `previous`, `notify`, `reason` round_robin | rule | returning_customer | backup | manager | reassigned | takeover | hand_back). `ChangeRequested.alert_ref` identifies the current reminder ladder; a reassignment issues a new one.

**Sending (WP6):** `OutboundRequest.buyer_country` and `QuoteApproved.buyer_country` / `language` pick the regional WhatsApp number; `CrmGateway.set_cover_note(deal_id, note, actor)` stores the AI's "open to discuss" line on the quote waiting for approval (message only, hashed into the grant with the rest of the body).

**Handover (WP7):** `QuoteApproved.salesperson` / `manager` are set for the final quote only (automation alerts the salesperson and notifies the manager); `DealContext.ai_paused_reason` and `team_available` let the orchestrator answer a handed-over deal with the fixed out-of-hours acknowledgement only (`Route.action = "acknowledge"`).

**v2 off (WP9):** `DeliveryEstimateView` dates are optional and `lead_time_text` carries the v1 answer (the product's default delivery time, D10) while `features.delivery_calc` is off.

**AI behaviour (WP10):** `QuoteView.delivery_time_text` / `incoterm` let the AI confirm the agreed terms back (§14.2); the agreed total (CRM price × agreed quantity) is the only new amount it may state.

**Notifications (WP12):** event `deal.won.v1` (`DealWon`); `NegotiationPaused.notify` (the manager; `alert` is the salesperson); `OrderStatusChanged.notify_buyer` + buyer address (setting `notifications.order_status_to_buyer`); `MessageReceived.opted_out` (automation sends the single confirmation the suppression lets through).

**11 Nov contracts (WP14–WP25)** are listed in `docs/REUSE_CATALOGUE.md` as they land: `consent.opted_out.v1` (`OptedOut`), `deal.requirement_summary.v1`, `MessageStatus.contact_ref/deal_ref/channel/kind`, `catalogue.product_published.v1`, `conversation.summary_updated.v1`, `auth.break_glass_used.v1`, `CrmGateway.record_opt_out`, `StaffMailer`.

## 4. Code standards

- Python 3.14, `from __future__ import annotations` (not in FastAPI router modules), full type hints, `mypy --strict` clean, `ruff` clean (config in root `pyproject.toml`).
- Async everywhere on I/O paths (SQLAlchemy 2.1 async + asyncpg; psycopg 3 only for migrations and the LangGraph checkpointer). Celery tasks are sync shells that run one coroutine on the worker process's own event loop.
- Pure logic in plain functions over frozen dataclasses / pydantic models; I/O in services; FastAPI routers thin.
- Tests: `tests/` inside each package, `pytest` + `hypothesis`; DB tests marked `@pytest.mark.db` use the root `databases` fixture.
- Do not add dependencies without the lead: everything allowed is already in `uv.lock`.
