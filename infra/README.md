# Kairox infrastructure (Terraform, AWS me-central-1)

Single-tenant deployment per client company: **one AWS account per company** (pack, auto) plus **one
shared staging account**, all in a Kairox AWS Organization. Region **me-central-1 (UAE)**, AZ IDs
`mec1-az1` + `mec1-az3`. Terraform >= 1.13 (CI uses 1.15.x), AWS provider `~> 6.0`, S3 backend with the
native lockfile (`use_lockfile = true`, no DynamoDB).

## Layout

```
infra/
  bootstrap/            state bucket + KMS key, local state, run once per account
  org/                  region-lock SCP (management account)
  envs/
    staging/            shared staging: one RDS with pack_* / auto_* DBs, Fargate Spot, single-AZ
    pack-prod/          Arifaa Pack production   (main.tf identical across *-prod; only tfvars/backend differ)
    auto-prod/          Arifaa Auto production
  modules/
    network             VPC, public/private/data subnets per AZ, NAT, S3 endpoint, flow logs, app SG
    secrets             platform CMK + Secrets Manager shells (Meta, Twilio, Anthropic, LiveKit, ElevenLabs,
                        Sarvam, Azure speech, Langfuse, Sentry, Turnstile) + generated secrets
    database            RDS PostgreSQL 18 (Multi-AZ, CMK, PI/Database Insights, 14-day backups, force_ssl),
                        app-role passwords + DSN secrets, optional DR backup replication (dr_copy_region)
    cache               ElastiCache Valkey (TLS + AUTH), URL secret per company
    storage             S3 media / quotes / audio (90-day expiry) / exports, optional media CDN (off)
    email               SES v2 identity + DKIM + MAIL FROM, configuration set -> SNS events; inbound toggle
    auth                Cognito pool, TOTP MFA required, groups md/manager/admin/sales, SPA client (PKCE)
    ecs_cluster         cluster (Container Insights), ECR repos kairox/app + kairox/cms
    ecs_service         generic Fargate ARM64 service or one-off task (roles, logs, autoscaling, circuit breaker)
    alb_waf             ALB (HTTPS, host routing), ACM, WAFv2 managed rules + per-IP rate limits, logs
    app_services        one company's runtime: api / worker / voice / cms + `kx migrate` task + deploy manifest
    observability       SNS alarm topic, ALB/RDS/ECS/queue-lag alarms, AWS Budgets
    github_oidc         GitHub OIDC provider + deploy / tf-plan / tf-apply roles (no long-lived keys)
  scripts/
    db_bootstrap.sh     roles + DBs + extensions (citext, pg_trgm, vector) + reporting grants, idempotent
    deploy_company.sh   SSM tag -> db-bootstrap -> kx migrate -> roll services -> wait
    run_ecs_task.sh / register_task_def.sh   helpers used by deploy.yml
```

Every resource is tagged `company`, `env`, `owner=kairox`, `cost-center` (provider `default_tags`).

## Apply order (first time)

1. **Accounts**: in the Organization create `kx-staging`, `kx-pack-prod`, `kx-auto-prod` (Workloads OU).
2. **bootstrap** in each account (admin creds, local state):
   `terraform -chdir=infra/bootstrap apply -var-file=<account>.tfvars`. Put the bucket name into that
   env's `backend.hcl` and `terraform.tfvars` (`state_bucket_name`).
3. **org** from the management account: set `target_ids` to the Workloads OU, then apply.
   Leave `dr_copy_region` / `bedrock_regions` empty unless the client consents in writing.
4. **envs/staging**, then **envs/pack-prod** (and later auto-prod), with admin creds the first time:
   `terraform init -backend-config=backend.hcl && terraform apply`.
   Services are created with **0 tasks** until an image exists (SSM tag `bootstrap`).
5. Copy the `github` output into GitHub (below). Fill the integration secrets
   (`aws secretsmanager put-secret-value --secret-id kx/<env>/<company>/<name> --secret-string '{...}'`).
6. **First deploy**: run `deploy` (staging). It pushes the image, runs `db-bootstrap` and `kx migrate`,
   and registers the task definitions. Then **apply Terraform once more**: it reads the deployed tag
   from SSM and raises the services to their sizes (api 2 tasks, one per AZ).
7. Afterwards: PRs get `terraform plan` comments; merges to `main` apply staging and then prod, gated by
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
   `terraform.tfvars` (company, account_id, vpc_cidr — a new /16, hosts, SES domain, Cognito prefix,
   budget, `github_environment`). `main.tf` stays identical.
3. Add the company to `envs/staging/terraform.tfvars` `companies` (prefix `<code>_`, next Valkey DB index).
4. Create GitHub Environment `<code>-prod`, add a job to `deploy.yml` and to `terraform.yml` (copy auto-prod).
5. Apply, fill the secrets, deploy (steps 4–6 above).

## Rough monthly cost (USD, me-central-1) — ESTIMATES ONLY

Assumes on-demand pricing about 10–15% above us-east-1. Excludes LLM, speech, LiveKit Cloud, Twilio,
Meta, Vercel and data transfer beyond a few GB. Verify with the AWS Pricing Calculator before quoting.

| Item | staging (both companies) | pack-prod (each prod similar) |
|---|---|---|
| RDS PostgreSQL | t4g.medium single-AZ, 30 GB ≈ $60 | m6g.large Multi-AZ, 100 GB ≈ $280 |
| ElastiCache Valkey | t4g.micro x1 ≈ $14 | t4g.small x2 ≈ $55 |
| Fargate ARM64 | 6 small tasks on Spot ≈ $30 | api 2 + worker 1 + voice 1 (2 vCPU) + cms ≈ $180 |
| ALB + LCU | ≈ $30 | ≈ $30 |
| NAT gateway(s) | 1 ≈ $45 | 2 ≈ $85 |
| WAF (ACL, 7 rules, requests) | ≈ $15 | ≈ $15 |
| CloudWatch (logs 30 d, Container Insights, alarms) | ≈ $15 | ≈ $40 |
| Secrets Manager, KMS, S3, ECR, SES, Cognito | ≈ $25 | ≈ $30 |
| **Total (estimate)** | **≈ $235** | **≈ $715** |

The total fits the 4 Nov "medium" ceiling (~$1,570 per company per month) once AI/voice usage is added.
The Budgets alarm is set to `monthly_budget_usd` (prod 1570, staging 600).

## Manual steps (cannot be automated)

- **SES**: request production access (leave the sandbox) in each prod account, add DKIM/MAIL FROM/DMARC
  records, and confirm the SNS→API HTTPS subscription (the API must answer `SubscriptionConfirmation`).
- **SES Mail Manager (reply capture)**: AWS lists Middle East (UAE) since Dec 2025. Confirm in the console
  on day 1, then set `inbound_mail_enabled = true` and create the ingress point + rule set (write to
  `inbound_bucket_name` with `mail_manager_role_arn`, notify `inbound_topic_arn`) in the console or with
  the `awscc` provider. The AWS provider has no Mail Manager resources. Fallback: Postmark inbound.
- **Meta WhatsApp**: Kairox Tech Provider onboarding, Business verification, a WABA + new number per
  company, message templates; put the token, app secret and verify token in `kx/<env>/<company>/meta_whatsapp`.
- **TDRA (UAE)**: sender/number registration as required, and approval + a licensed local line before any
  AI call to UAE numbers that did not ask for one. Manager alert calls use the scripted Twilio IVR only.
- **Twilio**: buy the manager-alert number (regulatory bundle if needed) and create an API key.
- **LiveKit Cloud** project per company (choose the closest region; record where audio is processed).
- **Cognito**: create staff users (admin-only sign-up); each enrols TOTP on first login.
- **Client consent** (written) before setting `dr_copy_region` or `bedrock_regions` (data leaves the UAE).

## Notes and open items

- PostgreSQL: `engine_version = "18"` lets RDS choose the default 18.x minor. Check what me-central-1
  offers: `aws rds describe-db-engine-versions --engine postgres --region me-central-1`. Also confirm the
  instance classes (`db.m6g.large`, `cache.t4g.*`) and Valkey 8.1 are offered there.
- The RDS master gets `INHERIT, SET` on the app roles. The bootstrap needs this on PG16+ to own/grant.
- App DSNs rely on `rds.force_ssl=1` plus driver TLS negotiation; move to `verify-full` with the RDS CA
  bundle when convenient. The CMS URI uses `sslmode=no-verify` for the same reason.
- `kx api` must listen on `$KX_API_PORT` (8000) and serve `$KX_HEALTH_PATH` (`/healthz`). The worker must
  emit the CloudWatch metric `Kairox/QueueLagSeconds` (dimension `Company`) for the queue-lag alarm.
- A deploy registers new task-definition revisions outside Terraform. The next apply re-registers an
  identical revision (same image tag from SSM). That is harmless.
