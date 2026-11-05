# Delivery — Kairox platform v1 (Arifaa Pack)

> **Update 5 Nov 2026.** Everything below describes the code as delivered on 4 Nov (309 tests). The business flow changed on 5 Nov:
> - the salesperson approves quotes;
> - pricing comes from a manual price list;
> - change requests go to the salesperson;
> - delivery calculation and scraping move to v2.
>
> The code changes are planned in `docs/V1_CHANGE_PLAN.md` (WP1–WP12), and the rules are in `docs/BUSINESS_FLOW_V1.md`. The DDL v1.1 items below are now part of WP1. The client questions are in the *v1 Questions for the Client* Claude Doc (https://claude.ai/artifact/K4UAEoFYiNdcVCDGzTkJdj). The ★ items block development:
> - the salesperson list;
> - the product Excel with tier prices and minimums;
> - a sample quote and payment terms;
> - website content and media;
> - the WhatsApp number(s) per region and the email domain.

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
