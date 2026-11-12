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
| Typed event payloads | `kx.core.event_payloads`: `make_event`, `parse_payload`, 23 payload models (incl. `ChangeRequested`, `ChangeRequestClosed`, `DealAssigned`, `DealWon`) | |
| HTTP errors | `kx.core.errors`: `NotFound`, `Conflict`, `Forbidden`, `Unauthorized`, `RuleViolation`, `install_problem_handlers` | RFC 9457 |
| Auth | `kx.core.auth`: `current_principal`, `require_role`, `require_human_mfa`, `Role` | approvals need MFA |
| Logging | `kx.core.logging`: `get_logger`, `configure_logging`, `mask_pii` | PII masked automatically |
| Price safety | `kx.core.price_guard`: `check_prices`, `extract_prices` | contest winner C, fails closed |
| Cross-package contracts | `kx.core.contracts`: `CrmGateway`, `GrantConsumer`, `MessageSender`, `ClickToCall`, `StaffMailer`, DTOs | no cost/markup fields, ever |
| Staff login (D26) | `kx.core.passwords` (Argon2id, TOTP, `SeedBox`), `kx.core.auth.TokenIssuer` (RS256, JWKS) | in-house, no Cognito |
| Background jobs (D25) | `kx.core.jobs`: `JobStore`, `JobRunner`, `JobSpec` (PostgreSQL schedule table; Celery only in the app) | |
| Vendor fallback (D30) | `kx.core.fallback`: `CircuitBreaker`, `FallbackChain`, `AllVendorsFailed` | switch metric `Kairox/VendorSwitch` |
| Spend caps (D34) | `kx.core.spend`: `SpendMeter`, `SpendCaps` | integers in Redis, 80 % / 100 % alerts |
| Kill-switch ladder (D35) | `kx.core.killswitch`: `KillSwitchLadder`, `KillLevel`, `engaged_at` | |
| STOP rules (§8.1) | `kx.core.optout`: `match`, `match_voice`, `confirmation_text` | |
| Signed public tokens | `kx.core.signed`: `sign`, `verify` | website sessions |
| CloudWatch metrics | `kx.core.metrics`: `emit_metric` (EMF) | |

## Planned in docs/V1_CHANGE_PLAN.md (do not build twice; add them here when done)

| Capability | Will live in |
|---|---|

## kx.crm — sales CRM (any manufacturer)
| Capability | Import |
|---|---|
| Price list (pure, v1) | `kx.crm.pricing.price_list`: `PriceTier`, `TierPrice`, `resolve_tier`, `min_price_for`, `convert_tier`, `list_line`, `discount_from_list_pct` |
| Negotiation ladder (pure, v1) | `kx.crm.pricing.engine`: `ladder_between`, `evaluate_ladder_counter` (§12.2 rules 1-5) over `kx.crm.pricing.types.LadderState` |
| Price-list I/O | `kx.crm.services.pricing`: `list_pricing_inputs`, `current_tiers`, `tiers_of_version`, `lead_time_text`, `fx_with_date`, `PricingNeeded`, `QuantityOutsideTiers` |
| Pricing maths (pure; cost-based = **v2**) | `kx.crm.pricing.engine`: `unit_cost_per_1000`, `freight_per_1000`, `resolve_band`, `price_line`, `quote_totals`, `concession_ladder`, `evaluate_counter`, `estimate_delivery` |
| Business rules (pure) | `kx.crm.rules` (stage machine, §15 LOST reasons, `lost_proposal_due`, staleness 3/7 days = v2, qualification fields) |
| Advance → PAYMENT, WON with the re-order date + order, order statuses | `kx.crm.services.orders`: `record_payment`, `mark_won`, `update_order_status` |
| LOST proposal after silence (task, never automatic) | `kx.crm.services.lost_proposals.propose_lost` (daily job `lost_proposals`) |
| Audit + timeline + outbox in one call | `kx.crm.services.journal.record()` |
| Handover after the final quote (AI off, reason `handover`) | `kx.crm.services.deals.hand_over` |
| Who may act on a deal (approve, edit, view; MD takeover read-only) | `kx.crm.services.access`: `ensure_can_approve`, `ensure_can_edit`, `ensure_can_view` |
| Approvals (salesperson, manager any deal, MD on takeover; special price; 120-min escalation) | `kx.crm.services.approvals`: `open_approval`, `decide`, `sweep_overdue`, `initial_role` |
| Edit then approve / revise a quote (special price below the minimum) | `kx.crm.services.quotes.revise_quote` (`QuoteEdits`, `LineEdit`) |
| Change requests (raise, start, revise link, resolve, decline, 120-min sweep) | `kx.crm.services.change_requests`; HTTP `kx.crm.api.change_requests`; contract `CrmGateway.raise_change_request`, `ChangeField`, `ChangeType` |
| Who is alerted | `kx.crm.services.staff`: `approver_for` (owner, MD on takeover, backup when away, else manager), `covering_user`, `backup_of`, `manager_user`, `owner_of`, `staff_contact` |
| Deal assignment (round-robin, rules, away/backup, returning customer), reassign, takeover, hand-back, availability | `kx.crm.services.assignment` (pure: `choose`, `rule_pool`, `available`, `next_in_turn`); HTTP `kx.crm.api.assignment`, `PUT /users/{id}/availability` |
| Staff login, users | `kx.crm.services.auth`; HTTP `kx.crm.api.auth`, `kx.crm.api.users` |
| §14 hand-over: staff reply, click-to-call | `kx.crm.services.handover` (`reply`, `call_buyer`) |
| §8.2 phone status, Requirement Summary | `kx.crm.services.phone`, `kx.crm.services.summary` |
| §7 CMS product mirror | `kx.crm.services.catalogue.mirror_product` |
| D32 WhatsApp quote -> email | `kx.crm.services.issuing.resend_by_email` |
| §20/§21 records, dashboards, exports | `kx.crm.api.records`, `kx.crm.api.reports`, kill switch `kx.crm.api.operations` |
| Services | `kx.crm.services.*`: settings, deals, pricing, price_proposals, quotes, approvals, issuing, grants, negotiation, orders, retention |
| Gateways for other packages | `kx.crm.gateway`: `CrmGatewayImpl`, `GrantConsumerImpl`, `ApprovalStatusReaderImpl` |
| HTTP API | `kx.crm.api.build_router(databases, deps)`: deals, assignment (reassign/takeover/hand-back), quotes + approvals, change requests, screens (inbox, price list, Excel, stock), admin (settings, availability) |
| Price list maintenance (CRUD with audit, Excel validate-then-import as a new version, export) | `kx.crm.services.price_lists` (pure `parse_rows`, `overlap_errors`; `add_tier`, `update_tier`, `delete_tier`, `import_xlsx`, `export_xlsx`) |
| My inbox (§20.2) | `kx.crm.services.inbox.inbox_for` |
| Stock table (§15.5) | `kx.crm.services.orders.set_stock`, `move_stock` |
| Pricing needed (D8) | `kx.crm.services.quotes.pricing_needed` (the gateway calls it when the AI may not price) |
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
| §17 notifications matrix (event → recipients) | `kx.automation.handlers.EventHandlers.registry()`; table test `kx-core-automation/tests/test_notifications_matrix_db.py` |
| Webhooks router | `kx.automation.webhooks.build_router(WebhookDeps)` |
| Jobs | `kx.automation.jobs`: `register_jobs`, `periodic_jobs`, `JobScheduler`, `AutomationRuntime` (scraper only with `features.scraping`, D14) |
| Kill switch / rate limits | `kx.automation.killswitch.KillSwitch` (= `kx.core.killswitch.KillSwitchLadder`), `kx.automation.ratelimit.TokenBucketLimiter` |
| Email chain SES -> Postmark (D33) | `kx.automation.channels.email_chain.EmailChain`, `kx.automation.channels.postmark` (`PostmarkSender`, `parse_inbound`) |
| Click-to-call (§14) | `kx.automation.alerts.CallBridge` |
| CMS publish webhook (§7) | `kx.automation.cms.build_router` |
| STOP flow, unsubscribe link | `kx.automation.optout`, `kx.automation.unsubscribe` |
| Admin: regional numbers, templates | `kx.automation.admin_api.build_router` |
| Price scraping → proposals | `kx.automation.scraping.PriceScraper` |

## kx.agents — AI agent framework
| Capability | Import |
|---|---|
| 12 roles, prompts (`AgentSpec.enabled`; repeat sales off in v1) | `kx.agents.registry`: `AgentRegistry` (`disabled()`), `AgentSpec`, `PromptStore`, `DefaultPromptStore`, `ModelTier` |
| Models + fallback | `kx.agents.models`: `ModelRouter`, `AnthropicApiProvider` (Claude API direct, D27); safe reply + breaker in `AgentService` |
| Routing (no LLM; agreement wording and post-quote change topics → deal graph; payment topics → deal graph in every stage; handed-over deals silent or the after-hours acknowledgement) | `kx.agents.orchestrator` (`PAYMENT_TOPIC`) |
| Reply guard | `kx.agents.guard` (prices, cost wording, injection, length) |
| Graphs | `kx.agents.graphs.conversation` / `quotation` (interrupt/resume) / `deal` |
| Memory, checkpointer | `kx.agents.memory`: `open_checkpointer`, turn store |
| Hybrid KB search | `kx.agents.knowledge`: `KnowledgeBase`, `KnowledgeIndexer`, `Embedder`, `ContentSource` |
| Voice | `kx.agents.voice`: `VoiceRouter`, `make_livekit_entrypoint` |
| Tracing | `kx.agents.tracing.Tracer` (Phoenix over OpenTelemetry, PII-masked, D29) |
| Website conversation API (§6) | `kx.agents.website.build_router(WebsiteDeps)` |
| Run cost table | `kx.agents.runs.PRICES`, `call_cost` |
| Entry point | `kx.agents.service.AgentService`, `kx.agents.handlers.AgentHandlers` |
| Test kit | `kx.agents.testing` (fake model, `FakeCrmGateway`, fake embedder), `kx.agents.evals` (§23 scenarios s01-s10 + trick prompts t01-t07) |
| Change requests from the AI | tool `kx.agents.tools.raise_change_request`; deal graph `intent=change` + `PAYMENT_TOPIC`; phrase `Phrase.CHANGE_REQUEST`, `team_member` |

## Client app (copy per client) — `apps/arifaa-pack`
`company.yaml` (identity), `config.py` (`KX_APP_*`), `adapters.py` (address book, `HttpEmbedder` = Qwen3 in our UAE container (D28), Payload CMS
source, LiveKit plugins), `container.py` (composition root + `EventBus`), `api.py`, `cli.py`
(`kx api | worker | beat | voice | migrate | create-user`), `migrations/` (Alembic, one script dir per DB).

**New client (e.g. Arifaa Auto):** copy `apps/arifaa-pack` → `apps/arifaa-auto`, edit `company.yaml`, prompts
and `crm.settings` seed; add `infra/envs/auto-prod/terraform.tfvars`. No platform code changes.
