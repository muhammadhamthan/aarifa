# Kairox infrastructure (Terraform, AWS me-central-1)

Single-tenant deployment per client company: **one AWS account per company** (pack, auto) plus **one
shared staging account**, all in a Kairox AWS Organization. Region **me-central-1 (UAE)**, AZ IDs
`mec1-az1` + `mec1-az3`. Terraform >= 1.13 (CI uses 1.15.x), AWS provider `~> 6.0`, S3 backend with the
native lockfile (`use_lockfile = true`, no DynamoDB).

The vendor choices, fallbacks and monthly cost come from the read-only platform docs
(`docs/Arifaa Platform — Platforms, Pricing & Fallbacks/`: `Recommendation.md`, `Fallback plan.md`); the run
order and every env var the code reads are in `docs/DEPLOYMENT.md`. This file is the Terraform detail.

## What runs per company (11 Nov)

| ECS service | Image | Size (prod) | Notes |
|---|---|---|---|
| `api` ×2 | kairox/app `kx api` | 1 vCPU / 2 GB, autoscale 2–4 | Behind ALB + WAF; one task per zone; signs staff tokens (in-house login) |
| `worker` | kairox/app `kx worker` | 1 vCPU / 2 GB, 1–2 | Celery worker (deliveries, ladder steps, follow-ups, event handlers) |
| `beat` | kairox/app `kx beat` | 0.25 vCPU / 0.5 GB, **exactly 1** | Celery Beat; deploys stop-then-start (0 % / 100 %) so two Beats never overlap |
| `voice` | kairox/app `kx voice` | 2 vCPU / 4 GB, 1–2 | LiveKit Agents worker (Deepgram STT, ElevenLabs TTS) |
| `cms` | kairox/cms | 0.5 vCPU / 1 GB | Payload CMS; calls `/webhooks/cms` on publish |
| `embeddings` | Text Embeddings Inference CPU, `Qwen/Qwen3-Embedding-0.6B` | 1 vCPU / 4 GB | Private: `http://embeddings.<company>.<env>.kx.internal` (`/v1/embeddings`) |
| `phoenix` | Arize Phoenix | 0.5 vCPU / 1 GB | Private: AI traces in the `phoenix` database (UI + OTLP on 6006) |
| `otel-collector` | OpenTelemetry Collector contrib | 0.25 vCPU / 0.5 GB | Ships the masked app logs from CloudWatch to Better Stack |
| `migrate` (one-off) | kairox/app `kx migrate` | 0.5 vCPU / 1 GB | Alembic for every database |
| `db-bootstrap` (one-off) | kairox/app script | 0.25 vCPU / 0.5 GB | Roles, databases (incl. `phoenix`), extensions |

Private services are registered in a Cloud Map private DNS namespace `<company>.<env>.kx.internal` and are
reachable only from the app security group (ports 80, 4317–4318, 6006). Third-party images are pinned in
`modules/app_services` `var.images` (Terraform owns them; deploys do not change them).

## Layout

```
infra/
  bootstrap/            state bucket + KMS key, local state, run once per account
  org/                  region-lock SCP (management account)
  envs/
    staging/            shared staging: one RDS with pack_* / auto_* DBs, Fargate Spot, single-AZ, one Redis node
    pack-prod/          Arifaa Pack production   (main.tf identical across *-prod; only tfvars/backend differ)
    auto-prod/          Arifaa Auto production
  modules/
    network             VPC, public/private/data subnets per AZ, NAT, S3 endpoint, flow logs, app SG
                        (+ app-SG-to-itself rules for the private services)
    secrets             platform CMK + Secrets Manager shells (Anthropic, Gemini [optional], Meta WhatsApp,
                        Postmark, Telnyx, Twilio, LiveKit, Deepgram, ElevenLabs, Soniox [optional], Google
                        [optional], Better Stack, Turnstile, CMS) + generated secrets + the staff-login keys
                        (RS256 JWT key via the tls provider, Fernet-format TOTP-seed encryption key)
    database            RDS PostgreSQL 18 (Multi-AZ, CMK, Database Insights — Advanced in prod, 14-day
                        backups, force_ssl), app-role passwords + DSN secrets (cms, crm, automation, agents,
                        phoenix), optional DR backup replication (dr_copy_region)
    cache               ElastiCache for Redis OSS 7.1 (2 nodes in 2 zones in prod, TLS + AUTH,
                        maxmemory-policy noeviction); secret per company with `url` and Celery `broker_url`
    storage             S3 media / quotes / audio (90-day expiry) / exports, optional media CDN (off)
    email               SES v2 identity + DKIM + MAIL FROM, configuration set -> SNS events -> /webhooks/ses
    ecs_cluster         cluster (Container Insights off since 11 Nov), ECR repos kairox/app + kairox/cms
    ecs_service         generic Fargate service or one-off task (roles, logs, autoscaling, circuit breaker,
                        deployment percentages, optional Cloud Map name)
    alb_waf             ALB (HTTPS, host routing), ACM, WAFv2 managed rules + per-IP rate limits (tight limit on
                        /v1/public/ and /v1/auth/), logs
    app_services        one company's runtime: the services above + Parameter Store vendor order + Cloud Map
                        namespace + deploy manifest
    observability       SNS alarm topic (email + optional Better Stack CloudWatch integration), ALB/RDS/ECS
                        alarms, Kairox/QueueLagSeconds and Kairox/VendorSwitch alarms, AWS Budgets
    github_oidc         GitHub OIDC provider + deploy / tf-plan / tf-apply roles (no long-lived keys)
  scripts/
    db_bootstrap.sh     roles + DBs + extensions (citext, pg_trgm, btree_gist, vector) + reporting grants, idempotent
    deploy_company.sh   SSM tag -> db-bootstrap -> kx migrate -> roll api/worker/beat/voice (+cms) -> wait,
                        then wait for embeddings/phoenix/otel-collector
    run_ecs_task.sh / register_task_def.sh   helpers used by deploy.yml
```

The Cognito module was removed on 11 Nov: staff login is in-house (Argon2 passwords, RS256 access tokens,
TOTP). The API publishes its public key at `https://<api host>/.well-known/jwks.json`.

Every resource is tagged `company`, `env`, `owner=kairox`, `cost-center` (provider `default_tags`).

## Parameter Store: vendor order per field

`modules/app_services` seeds `/kx/<env>/<company>/vendors/<field>` (comma-separated, primary first) and then
ignores the value, so ops can reorder without Terraform fighting it (Fallback plan: "the order changes
without a deploy"). The Python tasks may read the prefix; the api task may also write it (admin override).

| Field | Seed value |
|---|---|
| `llm` | `claude` (Gemini only after the suite passes and Arifa agrees) |
| `calls` | `telnyx,twilio` |
| `email` | `ses,postmark` |
| `embeddings` | `qwen3,fulltext` |
| `stt` | `deepgram,soniox` |
| `tts` | `elevenlabs,google` |

## Apply order (first time)

1. **Accounts**: in the Organization create `kx-staging`, `kx-pack-prod`, `kx-auto-prod` (Workloads OU).
2. **bootstrap** in each account (admin creds, local state):
   `terraform -chdir=infra/bootstrap apply -var-file=<account>.tfvars`. Put the bucket name into that
   env's `backend.hcl` and `terraform.tfvars` (`state_bucket_name`).
3. **org** from the management account: set `target_ids` to the Workloads OU, then apply. Leave
   `dr_copy_region` empty unless the client consents in writing; `bedrock_regions` stays empty (no Bedrock).
4. **envs/staging**, then **envs/pack-prod** (and later auto-prod), with admin creds the first time:
   `terraform init -backend-config=backend.hcl && terraform apply`.
   Services are created with **0 tasks** until an image exists (SSM tag `bootstrap`).
5. Copy the `github` output into GitHub (below). Fill the integration secrets
   (`aws secretsmanager put-secret-value --secret-id kx/<env>/<company>/<name> --secret-string '{...}'`).
   The JWT key, the TOTP-seed encryption key, the service token, the CMS webhook secret, the Payload
   secret, the database passwords and the Redis URLs are generated by Terraform.
6. **First deploy**: run `deploy` (staging). It pushes the image, runs `db-bootstrap` and `kx migrate`,
   and registers the task definitions. Then **apply Terraform once more**: it reads the deployed tag
   from SSM and raises the services to their sizes (api 2 tasks, one per AZ; beat exactly 1).
7. Create the first staff users (`kx create-user`, see `docs/DEPLOYMENT.md`), including the break-glass admin.
8. Afterwards: PRs get `terraform plan` comments; merges to `main` apply staging and then prod, gated by
   Environment reviewers. Tags `v*` deploy staging and then pack-prod (with approval).

DNS without Route 53 (the client's DNS): the first apply prints `certificate_validation_records` and
`ses_dns_records`. Ask the client's DNS admin to add them plus CNAMEs `api` / `cms` -> `alb_dns_name`.
Until the certificate is validated, the HTTPS listener cannot be created: apply with
`-target=module.alb.aws_acm_certificate.this` first, then apply in full.

## GitHub setup

- Environments `staging`, `pack-prod`, `auto-prod` (prod ones: required reviewers = Hamthan + 1).
  Variables for each: `AWS_DEPLOY_ROLE_ARN`, `TF_APPLY_ROLE_ARN`, `AWS_ACCOUNT_ID`.
- Repository variables: `TF_PLAN_ROLE_STAGING`, `TF_PLAN_ROLE_PACK_PROD`, `TF_PLAN_ROLE_AUTO_PROD`,
  `STAGING_ACCOUNT_ID`. In staging tfvars set `prod_account_ids` so prod can pull promoted images.
- Python packages are published to **GitHub Packages / GHCR** (CodeArtifact is not in me-central-1).

## Adding a new client

1. Create the AWS account in the Workloads OU (the SCP applies automatically) and run `bootstrap` there.
2. `cp -r infra/envs/pack-prod infra/envs/<code>-prod`, then edit `backend.hcl` (bucket, key) and
   `terraform.tfvars` (company, account_id, vpc_cidr — a new /16, hosts, SES domain, sending address,
   website origins, budget, `github_environment`). `main.tf` stays identical.
3. Add the company to `envs/staging/terraform.tfvars` `companies` (prefix `<code>_`, next Redis DB index;
   the Celery broker uses index + 8).
4. Create GitHub Environment `<code>-prod`, add a job to `deploy.yml` and to `terraform.yml` (copy auto-prod).
5. Apply, fill the secrets, deploy (steps 4–7 above).

## Monthly cost

Use the **Recommendation** in the platform docs, which prices every vendor and AWS from official pages:
about **US$903 / 1,775 / 3,936 a month per company at Light / Normal / Heavy** (VAT excluded). The AWS part
of that table (servers incl. Beat and the OpenTelemetry collector, RDS Multi-AZ, Redis, the Qwen3 container,
Phoenix, Database Insights Advanced, WAF, secrets, network, DNS, PDF rendering, Business Support+) is what
AWS Budgets watches (`monthly_budget_usd`); Claude, Meta, Postmark, Telnyx/Twilio, Deepgram, ElevenLabs,
LiveKit, Better Stack and Vercel bill outside AWS and are covered by the app's daily spend caps.
Staging is not in those totals (about US$310 a month at full size per the Recommendation).

## Manual steps (cannot be automated)

From the Recommendation's "decisions before go-live" and the Fallback plan:

- **Claude API**: open the account in Arifa's name, buy prepaid credit, set a monthly spend cap (USD 500
  maximum on the starting tier); put the key in `kx/<env>/<company>/anthropic`.
- **Meta WhatsApp**: Kairox Tech Provider onboarding, Business verification, a WABA + one number per region,
  message templates (incl. `requirement_summary` with a document header); token, app secret, verify token and
  phone-number id in `kx/<env>/<company>/meta_whatsapp`.
- **SES**: request production access (leave the sandbox) in each prod account, add DKIM/MAIL FROM/DMARC
  records, and confirm the SNS→API HTTPS subscription to `/webhooks/ses` (the API answers
  `SubscriptionConfirmation`).
- **Postmark** (email backup + buyer replies): a server with the outbound stream and an inbound webhook to
  `https://<api host>/webhooks/postmark/inbound` with Basic auth; token and Basic-auth pair in
  `kx/<env>/<company>/postmark`. SES Mail Manager is no longer used (`inbound_mail_enabled = false`).
- **Telnyx** (staff alert calls, primary): a TeXML application, a caller ID, and **live test calls on du and
  Etisalat** before go-live; API key, application id, from-number and webhook public key in
  `kx/<env>/<company>/telnyx`. **Twilio** stays wired as the backup (`kx/<env>/<company>/twilio`).
- **TDRA (UAE)**: no AI call to UAE numbers that did not ask for one; staff alert calls are scripted IVR only.
- **Better Stack**: uptime monitors on `https://<api host>/healthz`, a **heartbeat monitor** for Celery Beat
  (missing 2 minutes = page), the status page, one on-call responder, a log source for the collector. Put the
  Sentry-compatible DSN, the heartbeat URL, the log-source token and ingesting host in
  `kx/<env>/<company>/betterstack`. Optionally subscribe Better Stack's CloudWatch integration URL to the alarm
  topic (`betterstack_alarm_webhook_url`). Arifa must approve Better Stack (EU data location) as a processor.
- **Qwen3 retrieval test**: 50 real buyer questions per language (en / ar / hi / ta / zh / si) against the
  catalogue before go-live; if Qwen3 misses the pass mark, set `embedding_model = "BAAI/bge-m3"`, apply and
  re-index.
- **LiveKit Cloud** project per company (record where audio is processed); Deepgram and ElevenLabs keys;
  Soniox / Google Chirp only if approved.
- **Cloudflare Turnstile**: site key on the website, secret in `kx/<env>/<company>/turnstile`.
- **CMS**: create the Payload API key for the knowledge-base indexer (`kx/<env>/<company>/cms`).
- **Staff users**: created with `kx create-user`; each enrols an authenticator app (TOTP) at first login;
  the break-glass admin's TOTP seed is stored offline.
- **Client consent** (written) before setting `dr_copy_region` (data leaves the UAE).
- **Vendor switch-off test** in staging: switch off each vendor in turn (including Redis, Beat and Better
  Stack) and record the take-over time against the Fallback plan's triggers.

## Notes and open items

- **Image tags to confirm** before the first apply (`modules/app_services` `var.images`): the TEI CPU image
  (`cpu-1.7.2` is the default; Qwen3-Embedding needs TEI 1.7.2 or later), Phoenix (`latest` is a placeholder:
  pin the version you tested) and the collector (`0.120.0`). TEI and Phoenix run on **X86_64** Fargate
  (`var.architectures`) until arm64 images are confirmed; the collector runs on ARM64.
- **Phoenix login** is off (`phoenix_enable_auth = false`): Phoenix is reachable only inside the app security
  group. Turning it on also needs an API key for trace ingestion. Open the UI with ECS Exec port forwarding.
- **Collector config** (`OTEL_COLLECTOR_CONFIG`, generated by Terraform): `awscloudwatch` receiver on the
  api/worker/beat/voice log groups → `batch` → `otlphttp` to Better Stack. PII is masked at the source
  (`kx.core.logging`); the collector does not mask again.
- The app must emit `Kairox/QueueLagSeconds` and `Kairox/VendorSwitch` with a dimension set `[Company]`
  (Embedded Metric Format) for the alarms to match.
- PostgreSQL: `engine_version = "18"` lets RDS choose the default 18.x minor. Check what me-central-1
  offers: `aws rds describe-db-engine-versions --engine postgres --region me-central-1`. Also confirm the
  instance classes (`db.m6g.large`, `cache.t4g.*`) and Redis OSS 7.1 are offered there.
- The RDS master gets `INHERIT, SET` on the app roles. The bootstrap needs this on PG16+ to own/grant.
- App DSNs rely on `rds.force_ssl=1` plus driver TLS negotiation; move to `verify-full` with the RDS CA
  bundle when convenient. The CMS URI uses `sslmode=no-verify` for the same reason.
- `kx api` must listen on `$KX_API_PORT` (8000) and serve `$KX_HEALTH_PATH` (`/healthz`).
- A deploy registers new task-definition revisions outside Terraform. The next apply re-registers an
  identical revision (same image tag from SSM). That is harmless.
- Switching the cache from Valkey to Redis OSS (and the secret name from `.../valkey` to `.../redis`)
  replaces the replication group: nothing durable is lost (due work lives in PostgreSQL).
