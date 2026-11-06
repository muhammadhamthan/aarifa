# Reuse catalogue — what to import, never rewrite

One place per capability. If you need something in this list, import it. If it's missing, add it to the
owning package (and this file), not to your app. Enforced by import-linter (packages never import each other).

## kx.core — shared by every package and every client
| Capability | Import | Notes |
|---|---|---|
| Deploy settings, DSNs | `kx.core.settings`: `get_settings`, `CoreSettings`, `LogicalDb` | env prefix `KX_` |
| DB engines / transactions | `kx.core.db`: `Databases.transaction(LogicalDb.X)`, `.dependency()`, `make_base(schema)` | one engine per logical DB |
| Money (never float) | `kx.core.money`: `d`, `q`, `pct`, `display`, `to_currency`, `MONEY_PLACES`, `UNIT_PRICE_PLACES` | ROUND_HALF_UP |
| IDs, hashing | `kx.core.identity`: `new_id` (UUIDv7), `sha256_hex`, `hmac_sha256_hex`, `constant_time_equals` | |
| Contacts | `normalise_phone` (E.164), `normalise_email`, `normalise_address`, `recipient_hash` | grant check depends on these |
| Priced-content hash | `kx.core.identity.priced_content_sha256(body, pdf_sha)` | CRM issues, automation verifies — one function |
| Business hours / paused timers | `kx.core.timewindow.Window`: `contains`, `next_open`, `add_open_minutes` | 2 h approval timer pauses overnight |
| Events | `kx.core.events`: `Event`, `Outbox.add`, `relay_once`, `inbox_once`, `InMemoryBus`, event names | outbox in same transaction |
| Typed event payloads | `kx.core.event_payloads`: `make_event`, `parse_payload`, 22 payload models (incl. `ChangeRequested`, `ChangeRequestClosed`, `DealAssigned`) | |
| HTTP errors | `kx.core.errors`: `NotFound`, `Conflict`, `Forbidden`, `Unauthorized`, `RuleViolation`, `install_problem_handlers` | RFC 9457 |
| Auth | `kx.core.auth`: `current_principal`, `require_role`, `require_human_mfa`, `Role` | approvals need MFA |
| Logging | `kx.core.logging`: `get_logger`, `configure_logging`, `mask_pii` | PII masked automatically |
| Price safety | `kx.core.price_guard`: `check_prices`, `extract_prices` | contest winner C, fails closed |
| Cross-package contracts | `kx.core.contracts`: `CrmGateway`, `GrantConsumer`, `MessageSender`, DTOs | no cost/markup fields, ever |

## Planned in docs/V1_CHANGE_PLAN.md (do not build twice; add them here when done)

| Capability | Will live in |
|---|---|
| Price-list Excel import/export | `kx.crm.api` + `openpyxl` (WP11) |

## kx.crm — sales CRM (any manufacturer)
| Capability | Import |
|---|---|
| Price list (pure, v1) | `kx.crm.pricing.price_list`: `PriceTier`, `TierPrice`, `resolve_tier`, `min_price_for`, `convert_tier`, `list_line`, `discount_from_list_pct` |
| Negotiation ladder (pure, v1) | `kx.crm.pricing.engine`: `ladder_between`, `evaluate_ladder_counter` (§12.2 rules 1-5) over `kx.crm.pricing.types.LadderState` |
| Price-list I/O | `kx.crm.services.pricing`: `list_pricing_inputs`, `current_tiers`, `tiers_of_version`, `lead_time_text`, `fx_with_date`, `PricingNeeded`, `QuantityOutsideTiers` |
| Pricing maths (pure; cost-based = **v2**) | `kx.crm.pricing.engine`: `unit_cost_per_1000`, `freight_per_1000`, `resolve_band`, `price_line`, `quote_totals`, `concession_ladder`, `evaluate_counter`, `estimate_delivery` |
| Business rules (pure) | `kx.crm.rules` (stage machine, LOST reasons, staleness 3/7 days, qualification fields) |
| Audit + timeline + outbox in one call | `kx.crm.services.journal.record()` |
| Handover after the final quote (AI off, reason `handover`) | `kx.crm.services.deals.hand_over` |
| Who may act on a deal (approve, edit, view; MD takeover read-only) | `kx.crm.services.access`: `ensure_can_approve`, `ensure_can_edit`, `ensure_can_view` |
| Approvals (salesperson, manager any deal, MD on takeover; special price; 120-min escalation) | `kx.crm.services.approvals`: `open_approval`, `decide`, `sweep_overdue`, `initial_role` |
| Edit then approve / revise a quote (special price below the minimum) | `kx.crm.services.quotes.revise_quote` (`QuoteEdits`, `LineEdit`) |
| Change requests (raise, start, revise link, resolve, decline, 120-min sweep) | `kx.crm.services.change_requests`; HTTP `kx.crm.api.change_requests`; contract `CrmGateway.raise_change_request`, `ChangeField`, `ChangeType` |
| Who is alerted | `kx.crm.services.staff`: `approver_for` (owner, MD on takeover, backup when away, else manager), `covering_user`, `backup_of`, `manager_user`, `owner_of`, `staff_contact` |
| Deal assignment (round-robin, rules, away/backup, returning customer), reassign, takeover, hand-back, availability | `kx.crm.services.assignment` (pure: `choose`, `rule_pool`, `available`, `next_in_turn`); HTTP `kx.crm.api.assignment`, `PUT /users/{id}/availability` |
| Services | `kx.crm.services.*`: settings, deals, pricing, price_proposals, quotes, approvals, issuing, grants, negotiation, orders, retention |
| Gateways for other packages | `kx.crm.gateway`: `CrmGatewayImpl`, `GrantConsumerImpl`, `ApprovalStatusReaderImpl` |
| HTTP API | `kx.crm.api.build_router(databases, deps)` |
| Jobs + handlers | `kx.crm.jobs`: `CrmRuntime`, `build_blueprint`, `HANDLERS`, `dispatch` |
| Quote PDF (EN/AR, RTL; delivery time, Incoterm + place, payment terms, FX line, salesperson) and the §11.4 message (signed by the salesperson, "open to discuss" line in the message only) | `kx.crm.pdf`: `QuoteRenderer`, `QuoteDocument`, `SalesContact`, `quote_message`, `open_to_discuss`, `Letterhead`, `S3PdfUploader` |
| AI cover line for the quote message (D9) | `CrmGateway.set_cover_note` → `kx.crm.services.quotes.set_cover_note`; written in `kx.agents.graphs.quotation` (`COVER_NOTE`) |

## kx.automation — channels and jobs
| Capability | Import |
|---|---|
| The only way out | `kx.automation.outbound.OutboundService` (grant, kill switch, suppression, rate limit, 2/week cap, buyer hours) |
| Which number sends (buyer's thread → regional `countries` → default; email = company address) | `kx.automation.repo.channel_account(..., address=, country=)` |
| WhatsApp Cloud API | `kx.automation.channels.whatsapp.WhatsAppClient` (+ signature verify, webhook parsing, media download) |
| Email (SES v2) + inbound MIME | `kx.automation.channels.email.EmailSender`, parsing helpers |
| SNS verification | `kx.automation.channels.sns.SnsVerifier` |
| Staff alert calls (Twilio) | `kx.automation.channels.voice_ivr.TwilioCaller`, `kx.automation.alerts.VoiceAlerts`, `PauseAlerts` |
| §16 reminder ladder (approvals and change requests) | `kx.automation.alerts.ApprovalLadder` over `LadderTicket.for_approval` / `.for_change` |
| Staff notices (§17 "notified", alert hours) | `kx.automation.alerts.notify_staff` |
| Webhooks router | `kx.automation.webhooks.build_router(WebhookDeps)` |
| Jobs | `kx.automation.jobs.build_jobs`, `AutomationRuntime`, `ProcrastinateScheduler` |
| Kill switch / rate limits | `kx.automation.killswitch.KillSwitch`, `kx.automation.ratelimit.TokenBucketLimiter` |
| Price scraping → proposals | `kx.automation.scraping.PriceScraper` |

## kx.agents — AI agent framework
| Capability | Import |
|---|---|
| 12 roles, prompts | `kx.agents.registry`: `AgentRegistry`, `AgentSpec`, `PromptStore`, `DefaultPromptStore`, `ModelTier` |
| Models + fallback | `kx.agents.models`: `ModelRouter`, `AnthropicApiProvider`, `BedrockProvider` |
| Routing (no LLM; handed-over deals: silent, or the fixed acknowledgement after hours) | `kx.agents.orchestrator` |
| Reply guard | `kx.agents.guard` (prices, cost wording, injection, length) |
| Graphs | `kx.agents.graphs.conversation` / `quotation` (interrupt/resume) / `deal` |
| Memory, checkpointer | `kx.agents.memory`: `open_checkpointer`, turn store |
| Hybrid KB search | `kx.agents.knowledge`: `KnowledgeBase`, `KnowledgeIndexer`, `Embedder`, `ContentSource` |
| Voice | `kx.agents.voice`: `VoiceRouter`, `make_livekit_entrypoint` |
| Tracing | `kx.agents.tracing.Tracer` (Langfuse, PII-masked) |
| Entry point | `kx.agents.service.AgentService`, `kx.agents.handlers.AgentHandlers` |
| Test kit | `kx.agents.testing` (fake model, `FakeCrmGateway`, fake embedder), `kx.agents.evals` |
| Change requests from the AI | tool `kx.agents.tools.raise_change_request`; deal graph `intent=change` + `PAYMENT_TOPIC`; phrase `Phrase.CHANGE_REQUEST`, `team_member` |

## Client app (copy per client) — `apps/arifaa-pack`
`company.yaml` (identity), `config.py` (`KX_APP_*`), `adapters.py` (address book, Titan embedder, Payload CMS
source, LiveKit plugins), `container.py` (composition root + `EventBus`), `api.py`, `cli.py`
(`kx api | worker | voice | migrate`), `migrations/` (Alembic, one script dir per DB).

**New client (e.g. Arifaa Auto):** copy `apps/arifaa-pack` → `apps/arifaa-auto`, edit `company.yaml`, prompts
and `crm.settings` seed; add `infra/envs/auto-prod/terraform.tfvars`. No platform code changes.
