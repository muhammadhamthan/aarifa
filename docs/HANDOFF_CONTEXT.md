# Arifaa Pack + Arifaa Auto — full handoff context (paste into the next session)

Last updated: Thu 5 Nov 2026. Owner: Hamthan (Team Lead / CTO, Kairox AI Tech, India). Client: Arifa Group (CLIENT-4), UAE.
This file replaces earlier context notes. Where it conflicts with older documents, this file wins.

---------------------------------------------------------------------------------------------------------
## 0. How to start the next session
1. Attach **kairox-platform-v1.zip** (the code; the cloud workspace is temporary and may be wiped).
2. Paste this file (or point to project doc `claude/arifaa-pack-context.md`).
3. Say what you want next, e.g. "review package X", "build the DDL v1.1 migration", "start Arifaa Auto".
4. Credit is limited: ask for targeted work; avoid re-running whole-repo research or multi-agent builds.

---------------------------------------------------------------------------------------------------------
## 1. What we are building
Two client projects on ONE shared, Kairox-owned platform (client gets a licence; Kairox reuses the code).
- **Arifaa Pack** (packaging maker, UAE; buyers worldwide). Deliverables:
  - a 3D, voice-first website (AI speaks first, chat fallback, product pages, SEO);
  - a CRM with 8 deal stages, quote versions, a mandatory approval gate, Team + MD dashboards, and XLSX/CSV/PDF export;
  - 12 AI agents;
  - automation: WhatsApp, email, scripted manager alert calls, follow-ups.
- **Arifaa Auto**: similar scope, details still unknown. Next steps:
  - workshop Tue 10 Nov;
  - requirements signed Sat 14 Nov;
  - backend starts 16 Nov on the shared packages.
- **Deployment model:** single-tenant, a separate AWS account per company, with Pack and Auto data fully separate. A new client = a copy of the app folder plus config; no platform changes.

## 2. Status on 5 Nov 2026 (platform v1 code delivered)
The platform code is built, connected and tested. It is in `kairox-platform-v1.zip`, a uv workspace on Python 3.14 that includes the git history.

| Part | Package | Tests |
|---|---|---|
| Shared core: settings, DB, money, ids, time windows, events/outbox/inbox, typed payloads, errors, auth, logging, contracts, price guard | `kx.core` (packages/kx-core-db) | 117 (unit, spec, DB, integration) |
| CRM: ORM for 45 tables, deals, pricing, price proposals, quotes, two-step approvals, send grants, negotiation, orders, stock, retention, API, jobs, quote PDF (English/Arabic) | `kx.crm` | 39 (13 on Postgres) |
| Automation: WhatsApp Cloud API, SES, SNS, Twilio alert calls, OutboundService, webhooks, jobs, price scraping, kill switch, rate limits | `kx.automation` | 108 |
| AI agents: 12 roles, deterministic router, 3 graphs, reply guard, memory, hybrid knowledge search, voice routing, Langfuse tracing, evals | `kx.agents` | 45 (4 on Postgres) |
| Client app: wires everything together, event bus, API, worker, voice, `kx migrate` | apps/arifaa-pack | 2 whole-app smoke tests |
| Infra: 13 Terraform modules, 3 envs, region-lock SCP, bootstrap, DB bootstrap script, Dockerfile, compose, CI/deploy workflows | infra/, .github/ | linters only |
| DB design: DDL for 3 DBs + 21 SQL rule tests | db/ | T1–T21 pass |

- **Results:** 309 tests pass, 0 skipped. All DB tests ran on real Postgres 16 + pgvector.
- **Quality gates, all clean:** ruff, ruff format, mypy --strict (113 files), import-linter (3/3 contracts).
- **Size:** about 16k lines of Python excluding tests, 288 tracked files.
- **Docs in the repo:**
  - `README.md`
  - `CONTRACTS.md`, read first
  - `docs/DELIVERY.md`, `docs/REUSE_CATALOGUE.md`, `docs/DEPLOYMENT.md`, `docs/CONTESTS.md`
  - `infra/README.md`
  - this file

**NOT yet verified (do first):**
1. `terraform init/validate/plan` (registry was blocked in the sandbox).
2. `docker build` (there was no Docker daemon).
3. Live API calls: Meta WhatsApp, SES, Twilio, Bedrock, Anthropic, Langfuse, Payload CMS, Titan embeddings.
4. Voice: the LiveKit speech plugins (ElevenLabs, Sarvam, Azure, Silero) still need adding to the lock. Only the routing table is tested.

## 3. Key dates (Mon–Sat, 9 h/day; Fri 25 Dec holiday)
| Milestone | Pack | Auto |
|---|---|---|
| Requirements signed | Tue 3 Nov | Sat 14 Nov |
| Staging live (shared) | Wed 4 Nov | Wed 4 Nov |
| Website done | Sat 14 Nov | Sat 5 Dec |
| Backend + CRM screens done | Sat 21 Nov | Sat 12 Dec |
| All agents done | Wed 25 Nov | Sat 12 Dec |
| Feature complete | Sat 28 Nov | Sat 19 Dec |
| Client UAT | 30 Nov–5 Dec | 21–24 Dec |
| Production deploy | Mon 7 Dec | Mon 28 Dec |
| Go-live + handover | Wed 9 Dec | Tue 29 Dec |

Weekly reporting to the CEO is one report covering both projects: the Monday plan by 11 AM and the Friday report by 6 PM.

## 4. Team and workload
**Team:** 10 people at Kairox (India). On these projects: 4 Python devs, 2 frontend, and 1 flex tester.

| Person | Original split (2 Nov) | Suggested now that platform v1 code exists |
|---|---|---|
| **Hamthan** (lead) | DB for both; entire Pack backend incl. automation integration; Auto backend architecture + review; orchestrator + agent framework; infra; security with Ismail; Commercial Approval AI | Review `kx.core`, the quote release path (approvals → issuing → grants → outbound) and `container.py`; run `terraform plan` on staging; DDL v1.1 migration; security review |
| **Ismail** | Auto backend on shared core; test lead for both; shared automation module | Own `kx.automation` (live WhatsApp/SES/Twilio tests, SNS, templates); test lead; then Auto backend |
| **Habib** | Pack agents except orchestrator + Commercial Approval | Own `kx.agents` for Pack: prompts per role and language, live-model evals, KB content indexing, voice worker + LiveKit plugins |
| **Mahalakshmi** | Auto agents likewise | Review `kx.crm` (pricing, negotiation, PDF) now; Auto agents after the 14 Nov sign-off |
| **Rizwan** | Both websites (design + 3D) | Website + "Enter the factory" + LiveKit client; Payload CMS collections (they feed the KB) |
| **Nishath** | CRM frontend for both | CRM screens against `/v1` API (OpenAPI from FastAPI); MFA login (Cognito) |
| **Flex member** (name TBD) | Content entry, daily testing, UAT scripts, user guides | Same, plus eval scenarios in `kx/agents/evals` and the UAT checklist from the business flow below |

Suggested review order (about 1 day per reviewer): CONTRACTS.md → contracts.py → pricing engine + spec tests → price guard + agents guard → approvals/issuing/grants + outbound → container.py → infra README → `terraform plan`.

## 5. Business flow (final, Round 3 answers 4 Nov)
1. **Visitor arrives.** One click on "Enter the factory", then the AI speaks first and drives the site via LiveKit RPC.
   - The site is in English; the AI speaks the visitor's language: en, ar, hi, ta, zh or si.
   - The AI says it is an AI assistant and never pretends to be human, but keeps the conversation natural.
   - A recording notice plays at the start of every voice conversation.
2. **Qualification.** The AI collects product, quantity, delivery country and city, application, needed-by date, and contact name plus phone or email.
   - The phone number is confirmed by sending the Requirement Summary PDF to WhatsApp.
   - That PDF contains no prices; an indicative price range is off by default.
3. **AI drafts the quote** from the cost build-up (§6) at the top of the MD's markup range. Quote validity is 7 days.
4. **Approval 1.**
   - The manager approves; the MD is notified of every quote.
   - The MD must approve if the deal is above a value the MD sets, and can take over any deal at any time.
   - The approval window is 2 hours, counted only 08:00–22:00 UAE time (the timer pauses overnight).
   - Alert ladder: 3 IVR calls 20 minutes apart, then the MD.
   - Approvals need MFA and a human (no service tokens).
   - Once approved, the CRM issues a single-use send grant and the quote is sent.
5. **Negotiation.** The AI negotiates inside the range with no approval needed, on web voice, web chat, WhatsApp, email/text, and calls the buyer starts or asks for. It never negotiates on cold outbound calls.
   - The AI opens at the top of the range and makes at most 3 concessions.
   - Concession 1 can be goodwill; later concessions only for a bigger quantity.
   - Advance payment is always required; it is never a concession.
   - Longer delivery is NEVER offered as a concession.
6. **Below range or concessions used up.** The AI says: "Let me check with our sales manager; you'll hear back as soon as possible, within 60 minutes." It then pauses.
   - The manager and MD are alerted.
   - The manager replies, or sets an approved price and hands the deal back to the AI; the MD may take over.
7. **Buyer agrees → Approval 2.** The MD must confirm the final quote. A second grant is issued and the final quote is sent. The manager then takes over.
8. **Payment and order.**
   - Payment is confirmed outside the system (advance), and the deal moves to WON.
   - An Order record follows: In production → Ready → Shipped → Delivered, updated by the manager.
   - Future: the Pack ERP connects as a plugin to the CRM (the integration seam is designed now).
9. **Follow-ups.** Day 2, 5 and 10 after the quote. Maximum 2 per week, only 09:00–18:00 in the buyer's local time. WhatsApp and text follow-up matter most, because buyers rarely return to the website.
10. **Deal stages:** NEW → QUALIFIED → QUOTATION → NEGOTIATION → READY_TO_CLOSE → PAYMENT → WON / LOST. LOST requires a reason.
11. **Stock:** a simple table of raw materials and finished goods, updated manually in v1; ERP sync later.
12. **Unknown destination port:** quote EXW plus a separate freight line later.
13. **Retention:**
    - audio 90 days;
    - open-lead chats 24 months;
    - won customers 5 years;
    - lost leads anonymised after 12 months;
    - consent log for the lifetime of the relationship plus 5 years.
14. **Law (UAE):** contact only buyers who asked.
    - AI calls to UAE numbers the buyer did not request need TDRA approval and a licensed local line; they stay off until then.
    - WhatsApp voice calls are blocked in the UAE.
    - UAE PDPL does not require data localisation, so encrypted DR backups abroad are allowed, but only with written client consent.

## 6. Pricing
### 6a. Quote pricing (implemented in `kx.crm.pricing.engine`, contest winner A)
All pricing inputs are entered in the CRM by the MD or manager: raw-material rates, production, packaging, freight lanes, FX, margin rules.

**Cost per 1,000 pcs:**
- kg per 1,000 ÷ (yield% / 100) × material rate per kg + production + packaging + freight;
- rounded to 4 decimal places.

**Freight per 1,000:** (container cost + surcharge) × (1 + lane buffer%) ÷ pieces per 40HC × 1,000.

**Price per piece:** cost per 1,000 × (1 + markup%) ÷ 1,000, rounded to 6 decimal places. Line total is rounded to 4 decimal places. VAT is 5% on UAE deliveries.

**Markup is ON COST** (cost 100 at 40% = 140).
- Ranges are set per product category.
- Optional overrides apply per customer type and quantity band.
- The lowest priority number wins; on a tie, the most specific rule wins.

**Concession ladder:** n equal steps from the top of the range to the bottom. The bottom of the range is the floor.

**Worked example:**
- **Inputs:** AED 12/kg, 8.5 kg per 1,000, 92% yield, production 35, packaging 6.
- **Cost:** 151.8696 per 1,000.
- **Ladder** (40–50% range, 3 concessions): 0.227804 → 0.222742 → 0.217680 → 0.212617.
- **Line total:** 50,000 pcs = AED 11,390.20.

The negotiation must use the UNROUNDED unit cost; using the rounded one drifts the opening offer to 0.227805.

**Raw-material prices:**
- Every day the system checks official price websites (scraping where allowed) and creates *proposed* prices.
- The manager reviews and approves them; they are never applied automatically.
- If prices aren't updated, it warns after 3 days and blocks new quotes after 7 days.
- Daily index feeds: LME, resin futures, SCFI, FX.
- Freight per lane carries a war-risk buffer tier; quote validity is 7 days during the Hormuz crisis.

**No ML price prediction:**
- Official sites publish current prices, and freight is driven by war risk.
- Delivery-time prediction IS wanted: v1 is rule-based (production days + stock + supplier lead time + transit + buffer, skipping Sundays), always labelled "estimated, confirmed by our team".
- Every actual outcome is stored as training data for a later ML model.

**Safety:**
- Nothing the AI sees contains cost, markup or floor.
- Every AI reply passes the price guard (contest winner C, fails closed). It only allows approved quote prices and issued counter-offers, and blocks bare prices like "0.21 each".
- Priced messages leave only through OutboundService with a single-use grant bound to the content hash and recipient.

### 6b. Running cost per company per month (estimates; check the AWS Pricing Calculator before quoting)
| Item | Staging (both companies) | Each prod (pack / auto) |
|---|---|---|
| RDS PostgreSQL 18 | t4g.medium single-AZ ≈ $60 | m6g.large Multi-AZ 100 GB ≈ $280 |
| Valkey cache | ≈ $14 | ≈ $55 |
| Fargate ARM64 | Spot ≈ $30 | api×2 + worker + voice + cms ≈ $180 |
| ALB + WAF + NAT | ≈ $90 | ≈ $130 |
| CloudWatch, Secrets, KMS, S3, ECR, SES, Cognito | ≈ $40 | ≈ $70 |
| **AWS total** | **≈ $235** | **≈ $715** |

- **Full budget including LLM, speech, LiveKit Cloud, Twilio, Meta and Vercel**, per company per month:
  - low ~$480;
  - **medium ~$1,570 (recommended ceiling)**;
  - high ~$3,530.
- **Budgets alarm:** prod $1,570, staging $600.

**Cost levers:**
- Haiku for routing and simple turns, Sonnet for sales turns, Opus for evals only.
- Prompt caching on system prompts.
- Scale the voice service to 0 when idle.
- Self-host LiveKit later if it's cheaper.

## 7. Architecture (winner A: modular monolith, plus adopted grafts)
- **Repos:**
  - Backend: `kairox-platform`, a uv workspace with 4 packages and one app per client.
  - Frontend: separate repo, Next.js 16 + React 19 + Payload 3.92.
  - Packages are published to GitHub Packages (CodeArtifact is not in me-central-1).
- **Packages never import each other** (import-linter enforces this). They talk through Protocols in `kx.core.contracts` (CrmGateway, GrantConsumer, MessageSender) and versioned events.
- **One image, 4 ECS Fargate ARM64 services** in me-central-1, zones az1 + az3:
  - `api` ×2 behind ALB + WAF;
  - `worker` (procrastinate);
  - `voice` (LiveKit agent);
  - `cms` (Payload).
  - Plus a one-off `kx migrate` task.
- **Databases (one per application):** one RDS PostgreSQL 18 Multi-AZ instance per company, holding 4 databases (cms, crm, automation, agents).
  - One login role per app; no cross-DB foreign keys (`*_ref uuid` columns instead).
  - The job queue (procrastinate) lives in the automation DB.
  - Redis/Valkey is used only for cache, rate limits and kill switches.
  - Staging is one shared server, with DBs named pack_* and auto_*.
- **Events:**
  - Each DB has an outbox written in the same transaction as the state change.
  - Each package's relay publishes to the app EventBus, which queues one durable procrastinate job per subscribed package.
  - Each handler de-duplicates with its own inbox table.
  - Events use a versioned envelope with typed payloads (`kx.core.event_payloads`).
- **Quote gate across DBs:**
  - At approval the CRM issues a single-use **send grant**, bound to quote version + channel + recipient hash + content hash, with a 48-hour lifetime.
  - Automation consumes it atomically (`crm.consume_send_grant`) before sending.
  - The recipient comes from the contact record, never from the AI.
- **AI (`kx.agents`, LangGraph 1.2.19 / LangChain 1.4.8):**
  - A deterministic router (no LLM) picks one of 3 graphs:
    - **Conversation:** `create_agent` + role middleware + price-guard middleware (regenerate once, then a safe fallback);
    - **Quotation:** StateGraph with `interrupt()`, resumed by approval events;
    - **Deal:** negotiation StateGraph that calls CRM `evaluate_counter`.
  - Postgres checkpointer.
  - Hybrid knowledge search: pgvector + full-text + trigram, fused by RRF in one SQL query; 1024-dim Titan v2 embeddings.
  - Langfuse tracing with PII masking.
  - Models: Claude Sonnet 5.5 / Haiku 5.5 / Opus 5.5 (evals). Bedrock is primary and the Anthropic API is the fallback; Bedrock goes through a subclass using `AsyncAnthropicBedrock`, not yet tested live.
- **Voice:**
  - LiveKit Agents, LiveKit Cloud first.
  - Speech vendor by language: ElevenLabs Scribe v2 + Flash v2.5 (en/ar/zh), Sarvam Bulbul (hi/ta), Azure (si).
  - The manager alert call is a scripted Twilio IVR (no AI); it alerts only — approval happens in the CRM with MFA.
- **Channels:**
  - WhatsApp: Meta Cloud API direct, with Kairox as Tech Provider and a new number per company.
  - Email: SES sending; inbound via SES Mail Manager (UAE listed since Dec 2025; no Terraform resources yet, so set up manually), with Postmark as fallback.
- **Security:**
  - Cognito with TOTP MFA required; groups md/manager/admin/sales.
  - Region-lock SCP (only me-central-1, plus the DR region if consented).
  - WAF; KMS encryption; Secrets Manager.
  - RFC 9457 errors; PII masked in logs.
  - Pending: switch DB TLS to verify-full.
- **CI/CD (GitHub Actions + OIDC):**
  - ruff, format, mypy strict, import-linter, pytest on pgvector Postgres;
  - docker build; terraform fmt/validate/tflint/checkov;
  - deploy: arm64 image → ECR → migrate task → rolling update with circuit breaker; prod needs reviewer approval.
- **Stack (pinned, verified 2–4 Nov):**

| Area | Versions |
|---|---|
| Language / API | Python 3.14.6, FastAPI 0.162, Pydantic 2.14 |
| Database | SQLAlchemy 2.1.6 async + asyncpg 0.32, Alembic 1.20.1, psycopg 3.3.7 |
| Jobs | procrastinate 3.10.0 |
| AI | LangGraph 1.2.19, langchain-core 1.7.4, langchain-anthropic 1.7.9, checkpoint-postgres 3.2.0, Langfuse 5.1 |
| Voice | livekit-agents 1.8.10 |
| Channels / utilities | aioboto3 15.5, twilio 9.12, httpx 0.28, tenacity 9.2, structlog 26.1, orjson 3.13, weasyprint 70 |
| Dev / test | pytest 9.2, hypothesis, ruff 0.17.3, mypy 2.4.1 |
| Infra | Terraform 1.15, AWS provider ~> 6.0 (6.71.0) |

## 8. Research findings (what we checked and decided)
- **Region:**
  - Data in the UAE (me-central-1); app and database in the UAE.
  - Supabase is out (no UAE region).
  - CodeArtifact is not in me-central-1, so GitHub Packages is used.
  - RDS PostgreSQL 18 is available up to minor 18.6, but confirm the instance class in me-central-1.
- **Node.js vs Python:** Python for the whole backend, because LangGraph/LangChain Python is the most mature. TypeScript is used only in the frontend.
- **Queue:** Postgres-only (procrastinate), instead of SQS/EventBridge or Celery. It means fewer moving parts, transactional enqueue, and durable timers.
- **Voice cost:** LiveKit Cloud first (self-host later if cheaper); speech vendor routed by language (see §7). AI cold calls stay off in the UAE until TDRA approval.
- **WhatsApp:** Meta direct is cheaper than Twilio WhatsApp; templates need Meta approval; follow-ups outside the 24-hour window need templates.
- **Pricing feeds:** cost build-up plus daily feeds; no ML price predictor until 12–18 months of own data. Delivery time is rule-based now, ML later.
- **SES Mail Manager:** listed for the Middle East (UAE) since Dec 2025, but has no Terraform resources yet, so it's a manual step with Postmark as fallback.
- **Bedrock:** langchain-anthropic has no Bedrock class, so we wrap the official `AsyncAnthropicBedrock` client. Check model availability in the UAE or use a cross-region inference profile (client consent needed).
- **GitHub Actions versions** (verified Nov 2026): checkout@v6, setup-uv@v8.1.0, configure-aws-credentials@v6, build-push-action@v7, setup-terraform@v4. checkov runs via uvx; trivy-action is avoided.

## 9. Competing builds (multi-agent "fight")
- **Architecture:** 3 designs were scored (A modular monolith, B, C); A won, and the best parts of B and C were adopted.
  - Adopted from B/C: price guard on every channel; grant tied to channel + recipient; MFA-only approvals; 5-minute approval sweep; versioned events; `contact.anonymised` event; kill switches; 48-hour grant lifetime; ordered inbound processing per conversation; price-source health checks; region-lock SCP with exceptions.
- **Pricing engine:**
  - A (tuned Decimal) beat B (integer fractions). Both were exact on 200k cases including half-way ties, and A was faster: 1.6 µs per counter-offer, 2.1 µs per priced line.
- **Price guard:**
  - C (two-stage hybrid) beat A (regex) and B (hand-written lexer).
  - Before hardening C passed 40/40 spec tests and 27/29 hidden cases; after hardening it passes 29/29.
  - Speed: 5.4 ms per 1000 replies, vs 9.4 ms for A.
  - It missed half as many amounts as B in the fuzz test.
  - Hardened rules: a per-piece phrase after a bare number, or any bare 2-decimal number, counts as a price.
- The judges are kept in `candidates/judge_*.py` so the contest can be re-run.

## 10. Open items (prioritised)
**A. Verify before UAT:**
- `terraform plan` on staging;
- `docker build`;
- live channel tests (WhatsApp templates, SES, Twilio UAE number);
- Bedrock model IDs;
- LiveKit plugins plus a voice worker run;
- replace all `000000000000` / `REPLACE` placeholders, the GitHub owner, hostnames, the OU id and alarm emails.

**B. DDL v1.1 migration** (Alembic revision 0002 per DB):
- **automation:**
  - `outbound_messages` add `template_params jsonb`, `contact_ref`, `recipient_hash`, plus statuses bounced/complained;
  - `call_attempts` add UNIQUE(approval_ref, attempt_no).
- **crm:**
  - `contacts` add a generated `recipient_hash` column with an index, and allow source 'phone';
  - `deals.ai_paused_reason` allow 'concessions_exhausted';
  - add seed `quote.vat_pct_domestic=5` (the settings key is `approval.md_value_limit_aed`).
- **agents:**
  - `turns` add `run_id`;
  - `conversations` add `summary_upto_turn`;
  - store recipient hashes for anonymisation.

**C. Code / contracts:**
- grant for formal counter-offer messages;
- `OutboundRequest.thread_ref`;
- `QuoteApproved.language`;
- per-model cost table (`cost_usd` is 0 today);
- load price sources from `crm.price_sources`;
- delete S3 objects on anonymisation;
- add types-PyYAML;
- `Kairox/QueueLagSeconds` metric;
- DB TLS verify-full.

**D. Client answers still needed:**
- N1–N12: material per 1,000 pcs, backlog, supplier lead times, shipping, segments, printing, samples, visit slots, other apps, payment fields, report recipients, product list/Excel.
- Round 2: B1–B13, P-1–P-12.
- Also: the list of official raw-material price websites, the MD value limit, markup ranges per category, letterhead (address, TRN), and DR-backup consent.
- arifapack.com and autotec.ae can't be fetched from Claude's workspace, so the product pages or Excel files must be provided.

**E. Arifaa Auto:** workshop 10 Nov; requirements 14 Nov; copy `apps/arifaa-pack` to `apps/arifaa-auto`; `infra/envs/auto-prod` already exists.

## 11. Documents (claude.ai artifacts)
- Business Flow Draft & Round 3 Questions (4 Nov): https://claude.ai/artifact/JQhPUcZqnwMF9K4ctw5p65
- Research & Business-Flow Questions: https://claude.ai/artifact/Ha3P98JamsbcPXzFw1UZTt
- Round 2 Client Questions & Next Steps: https://claude.ai/artifact/6pVeq7p8jYf8wSxjBC69QU
- Client Questions & Requirements (answered; DO NOT MODIFY): https://claude.ai/artifact/Jbs97ZPYgNxuU8JkHLvEdH
- Two-Project Plan (Nov–Dec 2026): https://claude.ai/artifact/9Hpv3JtDd9pcJpRtLeSQT8
- Backend Architecture Blueprint (2 Nov; partly superseded): https://claude.ai/artifact/72eqQnr6vA8kESDsytKFMR
- Weekly Reports — both projects: https://claude.ai/artifact/GBYp1yXwoEobuqADBoXzPH
- Deployment & Security Plan (older; this file wins on conflicts): https://claude.ai/artifact/R2zLRoP2AD5GbcjmSJviZX
- Project docs in CLIENT-4: `claude/arifaa-pack-context.md` (this file), `claude/arifaa-platform-v1-delivery.md`.
- Outdated (Pack-only plan, 30 Nov handover):
  - Project Timeline: https://claude.ai/artifact/3gqk3PNH6mUNx4r1PCDSRs
  - Team Work Split: https://claude.ai/artifact/BrGNg51jmypWW1UpsuFSjp
  - Weekly Report Templates: https://claude.ai/artifact/B8b3Bz9T38gEoBRBzK4KQp
  - Week 1 Monday Plan: https://claude.ai/artifact/HmFtJwtwmHhD4Qt9D8FZ3C
  - Week 1 Friday Report: https://claude.ai/artifact/1ccihzN1BWz91ft76VQ9BB
  - First draft Delivery Plan: https://claude.ai/artifact/XJ8LDt4wzNLdSDXLPkWDTg
