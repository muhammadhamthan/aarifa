# Arifaa Pack + Arifaa Auto — full handoff context (paste into the next session)

Last updated: Thu 5 Nov 2026. Owner: Hamthan (Team Lead / CTO, Kairox AI Tech, India). Client: Arifa Group (CLIENT-4), UAE.
This file replaces earlier context notes. Where it conflicts with older documents, this file wins.

---------------------------------------------------------------------------------------------------------
## ⚠ 5 Nov changes. These OVERRIDE sections 5 and 6a below; the code still follows the old rules.
- **Approvals:**
  - The **assigned salesperson** approves both the 1st quote and the final quote.
  - The MD is not in the approval path; the MD can take over any deal.
  - Several salespersons each handle their own deals.
- **v1 pricing = price list.** The client's manager or IT role enters, per product, a unit price per quantity tier (e.g. 1 unit = X, 100 units = Y).
  - The AI quotes at the top and negotiates **price only**, down to the minimum the manager set.
  - Every quote says the price is open to discussion; the wording is still to agree.
- **Quote fields:**
  - product full details, quantity, unit price and total;
  - delivery time, set or confirmed by the salesperson;
  - payment terms and validity.
  - Fields are filled by the assigned salesperson; the price comes from the list.
- **Product or delivery-time change requests:** the AI promises nothing. The salesperson is alerted immediately, and the CRM shows what the buyer asked next to the current values. The salesperson edits, approves and resends.
- **CMS** = the website admin panel: text, photos, videos, products, 3D labels, without a developer.
- **Moved to v2:**
  - delivery-time calculation and prediction;
  - raw-material scraping, cost build-up and auto price range;
  - ERP plugin (stock, production status, stock-based quoting);
  - re-order follow-up.
- **Code impact:** about 1 week for one backend developer (estimate).
  - Approvals change from manager/MD to salesperson.
  - Pricing changes from cost + markup to price-list tiers.
  - New: change request record, deal assignment, salesperson inbox.
  - Scraper and delivery estimate are switched off by flag.
- **Docs:**
  - **DETAILED business flow v1 (SRS basis, the master doc):** https://claude.ai/artifact/MbcE3qg4MEGYvcKFybcix5
  - Hamthan's 5 Nov answers built into it:
    - no second approval check, but the manager is notified when the buyer pushes below the minimum;
    - there is no "sales manager" role, only a manager;
    - assignment is round-robin, with optional language/country matching; the manager or MD can reassign;
    - email comes from one company address, and WhatsApp uses one number per region, signed by the salesperson;
    - after the final quote the salesperson is alerted and the manager notified; payment is handled by humans only;
    - quantity, delivery or spec changes alert the salesperson;
    - the news digest is v2;
    - the expected re-order date is stored in v1.
  - short flow summary: https://claude.ai/artifact/9hRXK9QW2vNKuShYNBbWUo
  - questions for Hamthan: https://claude.ai/artifact/R3s1AshbNiwwTqwYx2coPy
  - questions for the client: https://claude.ai/artifact/K4UAEoFYiNdcVCDGzTkJdj

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

## 2. Status on 6 Nov 2026 (platform v1 code follows the 5 Nov flow)
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

- **Results (6 Nov, 5 Nov flow):** 502 tests pass, 0 skipped (309 on 4 Nov). All DB tests ran on real Postgres 18 + pgvector. WP1–WP12 of `docs/V1_CHANGE_PLAN.md` are done; see `docs/V1_PROGRESS.md` and `docs/OPEN_QUESTIONS.md`.
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

## 5. Business flow (5 Nov, current)

The full detail, with 26 sections and 10 worked scenarios, is in **`docs/BUSINESS_FLOW_V1.md`**, exported from the Claude Doc https://claude.ai/artifact/MbcE3qg4MEGYvcKFybcix5, which also has 3 diagrams.

**In short:**
1. Enquiry on web voice/chat, WhatsApp or email.
2. The AI qualifies the buyer and sends a Requirement Summary (no prices).
3. Round-robin assignment to a salesperson.
4. The AI drafts the quote from the price list (tier list price).
5. **Approval 1 by the salesperson.**
6. The quote is sent from the regional WhatsApp number or company email, signed by the salesperson, with a natural "open to discuss" line in the message.
7. The AI negotiates **price only**, inside the tier range.
8. Other requests become change requests to the salesperson.
9. Agreement leads to the final quote and **Approval 2 by the salesperson**. The salesperson is then alerted and the manager notified.
10. Payment is handled by humans only, then WON (with the expected re-order date) and order tracking.
11. Follow-ups on day 2/5/10.

## 6. Pricing

### 6a. Quote pricing in v1 = manual price list

- **Price list:** per product and quantity tier, a list price (top) and a minimum (floor), entered by the client's manager (with Excel import). An optional minimum % per category; a per-product value overrides it.
- **Example (illustrative):** 50,000 pcs at AED 0.25 = AED 12,500 + 5% VAT (UAE) = AED 13,125.
- **Ladder:** 0.2500 → 0.2433 → 0.2367 → 0.2300 (3 equal steps; steps 2–3 for more quantity).
- **Below the minimum:**
  - the first ask gets the next step;
  - an insisted ask pauses the AI, alerts the salesperson and notifies the manager;
  - a special price needs the manager.
- **v2:** the cost build-up engine (§6a of the 4 Nov version, still in `kx.crm.pricing.engine`) with daily raw-material feeds sets the ranges automatically.

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
