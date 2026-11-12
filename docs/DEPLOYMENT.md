# Deployment guide

Full infrastructure detail (modules, manual steps): `infra/README.md`. Vendor choices, fallbacks and monthly cost: the read-only platform docs in `docs/Arifaa Platform — Platforms, Pricing & Fallbacks/` (`Recommendation.md`, `Fallback plan.md`). This page is the run order.

## Topology (per company, AWS me-central-1 / UAE, two zones)

| Runs | Where | What |
|---|---|---|
| `api` ×2 | ECS Fargate ARM, one per zone, behind ALB + WAF | CRM API `/v1`, staff login `/v1/auth`, website API `/v1/public`, webhooks `/webhooks/*`, `/.well-known/jwks.json`, `/healthz` |
| `worker` | ECS Fargate | Celery worker: deliveries, ladder steps, follow-ups, event handlers, ingest jobs (one event loop per process) |
| `beat` | ECS Fargate, **exactly 1 task** | Celery Beat: sweeps `automation.scheduled_jobs` every 30 s, relays the outboxes every 5 s, periodic jobs, heartbeat to Better Stack |
| `voice` | ECS Fargate (can scale to 0) | LiveKit Agents worker: Deepgram STT, ElevenLabs TTS, same graphs and price guard as chat |
| `cms` | ECS Fargate (Node) | Payload CMS (website admin panel), database `cms` |
| `embeddings` | ECS Fargate, 1 vCPU + 4 GB | Qwen3-Embedding-0.6B (Text Embeddings Inference, OpenAI-compatible `/v1/embeddings`), private |
| `phoenix` | ECS Fargate | Arize Phoenix (AI traces stay in the UAE), private |
| `otel-collector` | ECS Fargate | Ships PII-masked app logs and errors to Better Stack |
| `kx migrate` | one-off ECS task | Alembic for every database (idempotent) |
| Website + CRM screens | Vercel Pro | The browser calls the UAE API directly |

Data: **RDS PostgreSQL 18 Multi-AZ** (databases `cms`, `crm`, `automation`, `agents`, `phoenix`; one login role per app; KMS-encrypted; 14-day backups), **ElastiCache Redis OSS** 2 nodes, `maxmemory-policy noeviction` (cache, rate limits, kill switches, spend counters, Celery broker), **S3** (media, quote and summary PDFs, audio with 90-day expiry), **Cloudflare R2** for 3D models and videos, **SES** (send), **Secrets Manager** (keys) and **Parameter Store** (vendor order per field), CloudWatch alarms + Database Insights, AWS Budgets. The org **SCP** blocks every region except me-central-1 (a DR region only with written consent).

Outside AWS (processors Arifa must approve): Anthropic (Claude API), Meta (WhatsApp Cloud API), Postmark, Telnyx, Twilio, Deepgram, ElevenLabs, LiveKit Cloud, Better Stack (EU data location), Vercel, Cloudflare; Google (Gemini, Chirp, Gmail API) only if approved.

## First deploy (once per AWS account)
1. `infra/bootstrap` → state bucket (S3 native lockfile).
2. `infra/org` → region-lock SCP (management account).
3. Fill `infra/envs/<env>/terraform.tfvars` + `backend.hcl` (replace every `000000000000` / `REPLACE`).
4. `terraform init && terraform plan && terraform apply` (services start at 0 tasks — no image yet).
5. `infra/scripts/db_bootstrap.sh` as the RDS master user: creates the databases owned by their roles, extensions (`citext`, `pg_trgm`, `vector`, `btree_gist`), `reporting_ro` read grants. Idempotent.
6. Fill the secrets (below) with `aws secretsmanager put-secret-value`; set the vendor order in Parameter Store if it differs from the defaults.
7. Push a tag → `deploy.yml`: OIDC role → build arm64 image → ECR → run `kx migrate` task → rolling update with circuit-breaker rollback. Staging first; prod needs reviewer approval (GitHub Environments).
8. Second `terraform apply` with real desired counts (`beat` stays at 1).
9. Create the first staff users: `kx create-user --role md --email … --name …` (prints a one-time password; the person enrols an authenticator app at first login). Create the break-glass admin the same way and store its TOTP seed offline.

## Every release
`git tag vX.Y.Z && git push --tags` → CI (ruff, format, mypy strict, import-linter, pytest on pgvector Postgres, docker build, terraform fmt/validate/tflint/checkov) → deploy workflow as above.

## Local development
```bash
uv sync --all-packages --all-extras
cp .env.example .env                  # local values; never commit .env
make up                               # postgres (pgvector, pg18) + redis
kx migrate                            # alembic for every DB (idempotent)
kx api                                # :8000, /healthz, /docs
kx worker                             # Celery worker
kx beat                               # Celery Beat (run exactly one)
KX_TEST_PG_DSN="postgresql://postgres:postgres@localhost:5432/postgres" uv run pytest
```

## Environment variables

Every key, with its default and a comment, is in `.env.example` (generated from the three settings classes, 12 Nov). In AWS they come from Secrets Manager (secrets) and the task definition (the rest).

| Group | Prefix | Keys |
|---|---|---|
| Core | `KX_` | `ENV`, `COMPANY_CODE`, `COMPANY_TIMEZONE`, `BASE_CURRENCY`, `CRM_DSN`, `AUTOMATION_DSN`, `AGENTS_DSN` (`postgresql+asyncpg://`), `REDIS_URL`, `AWS_REGION`, `LOG_LEVEL`, `LOG_JSON`, `SERVICE_TOKEN_SECRET` (signs website session and unsubscribe tokens) |
| Staff login (in-house) | `KX_` | `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_PRIVATE_KEY` (RSA PEM, secret), `JWT_KEY_ID`, `JWKS_URL` (only to verify against another issuer), `AUTH__ENCRYPTION_KEY` (encrypts TOTP seeds, secret), `AUTH__ACCESS_TTL_SECONDS`, `AUTH__REFRESH_TTL_SECONDS`, `AUTH__MAX_FAILED_ATTEMPTS`, `AUTH__LOCKOUT_MINUTES`, `AUTH__CODE_TTL_SECONDS`, `AUTH__COOKIE_SECURE`, `AUTH__COOKIE_DOMAIN` |
| Channels | `KX_AUTO_` | `PUBLIC_BASE_URL`; `WHATSAPP__*` (phone_number_id, access_token, app_secret, verify_token, graph_version, template_language); `SES__*` (from_address, configuration_set, sns_topic_arns); `POSTMARK__*` (server_token, message_stream, inbound_user, inbound_password; Postmark sends from `SES__FROM_ADDRESS`); `TWILIO__*` (account_sid, auth_token, from_number, status_callback_base_url); `WHATSAPP__QUOTE_DELIVERY_MINUTES` (D32, default 10); `ALERTS__*`, `FOLLOWUP__*`, `RATE_LIMIT__*`, `OPTOUT__*`. Telnyx is not wired yet (OPEN_QUESTIONS #55). Vendor order per field comes from Parameter Store (`KX_APP_VENDOR_ORDER_SSM_PREFIX`), not env |
| App | `KX_APP_` | buckets, `CORS_ORIGINS`, `CMS_URL`, `CMS_API_KEY`, `CMS_WEBHOOK_SECRET`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` + `GEMINI_FALLBACK_ENABLED`, `EMBEDDING_URL`, `EMBEDDING_MODEL`, `EMBEDDING_TIMEOUT_SECONDS`, `PHOENIX_COLLECTOR_ENDPOINT`, `PHOENIX_PROJECT`, `SENTRY_DSN` (Better Stack error tracking), `BETTERSTACK_HEARTBEAT_URL`, `TURNSTILE_SECRET_KEY`, `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`, `CELERY_BROKER_URL`, `WORKER_CONCURRENCY`, `VENDOR_ORDER_SSM_PREFIX`, `SPEND_*`, `PDF_RENDERER`, `CHROMIUM_PATH` |
| Voice plugins | (none) | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `DEEPGRAM_API_KEY`, `ELEVEN_API_KEY`, `SONIOX_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS` (read by the LiveKit plugins themselves) |
| Migrations | | `KX_DDL_DIR=/app/db/ddl` (set in the image); optional `KX_MIGRATE_<DB>_DSN` overrides |

## v1 business settings (crm.settings, seeded by migrations)
These are changed in the CRM by the manager, MD or admin, not through env vars.

| Group | Keys and defaults |
|---|---|
| Pricing | `pricing.mode=price_list` |
| Quote defaults | `quote.vat_pct_domestic=5`, `quote.vat_pct_export=0`, `quote.validity_days=7`, `quote.default_incoterm=EXW`, `quote.payment_terms_text`, `quote.currency_by_country={}` |
| Negotiation and change requests | `negotiation.max_steps=3`, `negotiation.free_samples_allowed=false`, `change_request.sla_working_hours=4` |
| Approvals | `approval.reminder_minutes=[30,60,120]`, `approval.md_value_limit_aed=null` (off) |
| Follow-ups and assignment | `followup.lost_after_days=14`, `followup.weekend_days_by_country`, `assignment.rules=[]` |
| Notifications | `notifications.order_status_to_buyer=false` |
| Opt-out (§8.1) | `optout.ask_reason=false` (lawyer first), `optout.live_minutes=10`, `optout.unclear_answer_hours=24` |
| v2 features | **off in v1:** `features.scraping=false`, `features.delivery_calc=false`, `features.news_digest=false` |

**Regional WhatsApp numbers.** Each number is an `automation.channel_accounts` row with `region` and `countries[]`. The Meta access token and app secret come from Secrets Manager. Email stays on one SES identity.

## Manual / external steps before go-live
From the Recommendation checklist and the infra manual steps:
- Arifa approves the processor list (incl. **Better Stack, EU data location**) and consent text in en / ar / hi / ta / zh; says yes or no to **Gemini** as the standby AI; Claude API account in Arifa's name with prepaid credit and a monthly spend cap.
- **Meta:** Tech Provider onboarding, a number per region, and approved templates: every name in `AutomationSettings.alerts`, the follow-ups, `requirement_summary` (document header), the 24-hour re-open template (EN/AR).
- **SES** production access + DKIM/MAIL FROM/DMARC; **Postmark** server (outbound stream + inbound webhook with Basic auth to `/webhooks/postmark/inbound`).
- **Telnyx** TeXML application + caller ID, live test calls on du and Etisalat; **Twilio** stays wired as backup.
- **TDRA:** no AI call to UAE numbers that did not ask for one.
- **LiveKit Cloud** project; Deepgram and ElevenLabs keys (plus Soniox / Google Chirp backups if approved).
- **Better Stack:** uptime monitors for `/healthz`, a heartbeat monitor for Beat (missing 2 min = page), the status page, the on-call responder.
- **Qwen3 retrieval test** (50 questions per language, en / ar / hi / ta / zh / si); switch to bge-m3 and re-index if it misses the pass mark.
- **Haiku 5.5** pass mark on the 50 negotiation scenarios, all five languages.
- Staff users created; each enrols an authenticator app (TOTP).
- Live switch-off test of every vendor in staging (Fallback plan, "Test before go-live").
