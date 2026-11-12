# Delivery — Kairox platform v1 (Arifaa Pack)

> **Update 6 Nov 2026: the code now follows the 5 Nov business flow** (the SRS; `BUSINESS_FLOW_V1.md` was later renamed to the current SRS file). All twelve work
> packages of `docs/V1_CHANGE_PLAN.md` are done (one commit each, `WP1` ... `WP12`), plus end-to-end tests of the ten
> §23 scenarios. **502 tests pass, 0 skipped** (309 before), `make lint` clean. Decisions and gaps for the client or
> the lead: `docs/OPEN_QUESTIONS.md`. Work log with tests per WP and benchmarks: `docs/V1_PROGRESS.md`.
>
> | WP | What changed |
> |---|---|
> | 1 | Alembic `*_0002` per DB (+ `crm_0003`-`0008` later): salesperson/special-price approvals, price-list tiers (no-overlap constraint), change requests, assignment fields, takeover, handover, quote delivery/terms/FX, re-order date, indexed contact hashes, regional WhatsApp accounts, v1 settings. Test DBs are built through the migrations. |
> | 2 | Price-list pricing (`kx.crm.pricing.price_list`, `ladder_between`, `evaluate_ladder_counter`): list price per tier, minimum (tier or category %), FX at the printed rate, VAT 5 % / 0 %, 3-step ladder, §12.2 rules 1-5; cost pricing kept for v2 (`pricing.mode='cost'`). |
> | 3 | The deal's salesperson approves every version; the manager any deal; the MD only on takeover (owner read-only); manager-only special price below the minimum; escalation to the manager at 120 alert-hour minutes. |
> | 4 | Change requests end to end (CRM card + inbox API, AI tool and deal-graph path, fixed "within 4 working hours" reply, §16 reminder ladder for approvals and cards). |
> | 5 | Round-robin assignment with language/country/category rules, away + backup, returning customer, reassign, MD takeover / hand-back; pending items follow the person responsible. |
> | 6 | Regional WhatsApp number by thread / country / default; message signed by the salesperson with the AI's "open to discuss" line (message only); PDF shows delivery time, Incoterm + place, payment terms, FX line, salesperson (EN/AR RTL). |
> | 7 | Final approval: final quote sent, salesperson alerted, manager notified, AI off (`handover`); after-hours buyer messages get only the fixed acknowledgement. |
> | 8 | Advance → PAYMENT; WON needs the advance and the expected re-order date; §15 LOST reasons; daily LOST proposal task after silence (never automatic). |
> | 9 | v2 off by flags, code kept: scraper not scheduled, no staleness block in price-list mode, delivery answer = product default, repeat-sales agent disabled. |
> | 10 | Prompts/policy per §12.2/§13/§18, fixed phrases, agreement confirms the terms, router sends change and payment topics to the deal graph; evals = §23 scenarios 1-10 + trick prompts. |
> | 11 | Screens API: inbox, price-list CRUD (audited), Excel import (validated, all-or-nothing) / export, stock table, settings for the manager; unpriceable quotes → Pricing-needed task; manual quote for custom items. |
> | 12 | §17 matrix: every row mapped to an event and a handler, table-driven test. |
>
> **Not verified live (needs real accounts / the client):** Meta WhatsApp templates (all §17 names in `AutomationSettings.alerts` must be approved, EN/AR), SES sending, Twilio alert calls, Bedrock/Anthropic models with the new prompts (evals ran on the scripted model only), the WeasyPrint PDF layout with the client's letterhead, the Excel template with the client's real price list, the regional numbers list, staff alert-channel preferences, Terraform/ECS deploy of the new revisions. The buyer's Requirement Summary PDF (§6.5, §17 row 1) is **not built**.
>
> The 4 Nov description below is kept for history (counts and rules there are the 4 Nov ones).

Date: 4 Nov 2026. Repo: `kairox-platform` (uv workspace, Python 3.14). Git history included.

## What is built
| Part | Where | Tests |
|---|---|---|
| Shared core: settings, DB, money, ids, windows, events/outbox/inbox, payloads, errors, auth, logging, contracts, **price guard** | `packages/kx-core-db` (`kx.core`) | 117 (root `tests/`: unit, spec, DB, integration) |
| CRM: 45-table ORM, deals, pricing, price proposals, quotes, two-step approvals, send grants, negotiation, orders, retention, API, jobs, EN/AR quote PDF | `packages/kx-core-crm` (`kx.crm`) | 39 (13 on Postgres) |
| Automation: WhatsApp, SES, SNS, Twilio alert calls, OutboundService, webhooks, jobs, scraping, kill switch, rate limits | `packages/kx-core-automation` (`kx.automation`) | 108 |
| AI agents: 12 roles, orchestrator, 3 graphs, guard, memory, hybrid KB, voice routing, tracing, evals | `packages/kx-agent-framework` (`kx.agents`) | 45 (4 on Postgres) |
| Client app: composition root, event bus, API, worker, voice, `kx migrate` | `apps/arifaa-pack` | 2 whole-app smoke tests |
| Infra: 13 Terraform modules, 3 envs, SCP, bootstrap, DB bootstrap, Dockerfile, compose, CI/CD | `infra/`, `.github/`, `Dockerfile` | linters (see below) |
| DB design: DDL + 21 SQL rule tests | `db/` | T1–T21 pass |

**Result: 309 tests pass, 0 skipped** (all DB tests on real PostgreSQL 16 + pgvector).
Gates: ruff clean, ruff format clean (158 files), mypy `--strict` clean (113 files), import-linter 3/3 contracts kept.

## Competing builds (details: `docs/CONTESTS.md`)
- Pricing engine — **A (tuned Decimal)**: exact on 200k cases, 1.6 µs per counter-offer. Its cost-based functions become v2; v1 adds price-list tiers + `ladder_between` (WP2).
- Price guard — **C (two-stage hybrid)**, hardened to fail closed: 40/40 spec + 29/29 hidden, ~5.4 ms / 1000 replies.

## Verified vs not verified (honest list)
Verified here: everything above, migrations twice on fresh DBs, API boot with all packages wired, event fan-out
to durable jobs, DB bootstrap script (6 runs), actionlint, hadolint, shellcheck, checkov.

**Not run in this sandbox** (blocked downloads / no daemon / no accounts) — run these first:
1. `terraform init / validate / plan` for each env (registry blocked here).
2. `docker build` (no Docker daemon).
3. Live calls: Meta WhatsApp, SES, Twilio, Bedrock, Anthropic API, Langfuse, Payload CMS, Titan embeddings.
4. Voice worker: LiveKit speech plugins (`livekit-plugins-elevenlabs/-sarvam/-azure/-silero`) are not in the
   lock yet; the routing table is tested, the live worker is not.

## Open items for the team (prioritised)
**DDL v1.1 migration** (add as Alembic revision 0002 per DB):
- `automation.outbound_messages`: add `template_params jsonb`, `contact_ref uuid`, `recipient_hash text`;
  allow statuses `bounced`, `complained`.
- `automation.call_attempts`: `UNIQUE (approval_ref, attempt_no)` (advisory lock used today).
- `crm.contacts`: generated `recipient_hash` column + index (lookup is a scan today); `source` add `'phone'`.
- `crm.deals.ai_paused_reason`: add `'concessions_exhausted'` (stored as `'exception'` today).
- `crm.settings` seed: `quote.vat_pct_domestic = 5` (code defaults to 5).
- `agents.turns.run_id`; `agents.conversations.summary_upto_turn`; recipient hashes for anonymisation.

**Contracts / code:**
- Counter-offers sent as a formal priced message need a grant method that takes the body (AI chat replies are
  already protected by the guard + `allowed_prices`).
- Add `thread_ref` to `OutboundRequest`; `language` to `QuoteApproved`; per-model cost table (`cost_usd` = 0 now).
- Price-source config duplicates `crm.price_sources`: load it from the CRM at worker start.
- Anonymisation blanks S3 keys in DB, not the S3 objects: add a delete on `MediaStore` or lifecycle rule.
- Add `types-PyYAML` to dev deps (one `type: ignore` today); worker metric `Kairox/QueueLagSeconds`.
- DB TLS: switch DSNs to `verify-full` with the RDS CA bundle.

**Client answers still needed (from N12, B1–B13, P-1–P-12):** official raw-material price sites, MD value limit,
markup ranges per category, product catalogue/Excel, letterhead (address, TRN).

## Where to start reviewing (suggested order, ~1 day)
1. `CONTRACTS.md` → `packages/kx-core-db/src/kx/core/contracts.py` (the rules every package follows).
2. `kx/crm/pricing/engine.py` + `tests/spec/` (the money).
3. `kx/core/price_guard.py` + `kx/agents/guard.py` (what stops the AI leaking prices).
4. `kx/crm/services/approvals.py`, `issuing.py`, `grants.py` + `kx/automation/outbound.py` (quote release path).
5. `apps/arifaa-pack/src/arifaa_pack/container.py` (how it all connects).
6. `infra/README.md`, then `terraform plan` on staging.
