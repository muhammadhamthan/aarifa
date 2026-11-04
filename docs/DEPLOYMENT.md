# Deployment guide

Full infrastructure detail (modules, cost table, manual steps): `infra/README.md`. This page is the run order.

## Topology (per company, AWS me-central-1 / UAE, AZs a + c)
- **ECS Fargate (ARM64), one image, `kx` entrypoint:** `api` ×2 (one per AZ, behind ALB + WAF), `worker`
  (procrastinate jobs), `voice` (LiveKit agent worker, can scale to 0), `cms` (Payload, Node), one-off `kx migrate`.
- **RDS PostgreSQL 18 Multi-AZ:** databases `cms`, `crm`, `automation`, `agents`; one login role per app;
  the job queue lives in `automation`. KMS-encrypted, 14-day backups, deletion protection in prod.
- **Valkey** (cache, rate limits, kill switches only), **S3** (media, quote PDFs, audio 90-day expiry, inbound
  mail), **SES**, **Cognito** (staff, MFA required), **Secrets Manager**, CloudWatch alarms, Budgets.
- Website + CRM screens on **Vercel**; the browser calls the UAE API directly.
- Org **SCP** blocks every region except me-central-1 (DR region only if `dr_copy_region` is set with consent).

## First deploy (once per AWS account)
1. `infra/bootstrap` → state bucket (S3 native lockfile).
2. `infra/org` → region-lock SCP (management account).
3. Fill `infra/envs/<env>/terraform.tfvars` + `backend.hcl` (replace every `000000000000` / `REPLACE`).
4. `terraform init && terraform plan && terraform apply` (services start at 0 tasks — no image yet).
5. `infra/scripts/db_bootstrap.sh` as the RDS master user: creates the 4 DBs owned by their roles,
   extensions (`citext`, `pg_trgm`, `vector`), `reporting_ro` read grants. Idempotent.
6. Push a tag → `deploy.yml`: OIDC role → build arm64 image → ECR → run `kx migrate` task → rolling update
   with circuit-breaker rollback. Staging first; prod needs reviewer approval (GitHub Environments).
7. Second `terraform apply` with real desired counts.

## Every release
`git tag vX.Y.Z && git push --tags` → CI (ruff, format, mypy strict, import-linter, pytest on pgvector
Postgres, docker build, terraform fmt/validate/tflint/checkov) → deploy workflow as above.

## Local development
```bash
uv sync --all-packages --all-extras
docker compose up -d                 # postgres (pgvector, pg18) runs db/ddl; valkey
kx migrate                           # alembic + procrastinate schema (idempotent)
kx api                               # :8000, /healthz
kx worker                            # jobs, outbox relays, periodic sweeps
KX_TEST_PG_DSN="postgresql://postgres@localhost:5432/postgres" uv run pytest
```

## Environment variables (all from Secrets Manager / task definition)
- Core `KX_*`: `ENV`, `COMPANY_CODE`, `CRM_DSN`, `AUTOMATION_DSN`, `AGENTS_DSN` (`postgresql+asyncpg://`),
  `REDIS_URL`, `JWT_ISSUER`, `JWT_AUDIENCE`, `JWKS_URL`, `AWS_REGION`.
- Automation `KX_AUTO_*`: `WHATSAPP__*` (phone_number_id, access_token, app_secret, verify_token),
  `SES__*` (from_address, configuration_set, sns_topic_arns), `TWILIO__*`, `PRICE_SOURCES`.
- App `KX_APP_*`: buckets, `CMS_URL`/`CMS_API_KEY`, `ANTHROPIC_API_KEY`, `BEDROCK_REGION` +
  `BEDROCK_MODEL_IDS`, `EMBEDDING_MODEL_ID`/`EMBEDDING_REGION`, `LANGFUSE_*`, `CORS_ORIGINS`.
- Migrations: `KX_DDL_DIR=/app/db/ddl` (set in the image); optional `KX_MIGRATE_<DB>_DSN` overrides.

## Manual / external steps before go-live
SES production access + domain DKIM; Meta Tech Provider + WhatsApp templates approved; Twilio UAE number
(manager alert calls); TDRA approval before any AI call to UAE numbers the buyer didn't request; LiveKit
Cloud project + speech vendor keys (ElevenLabs, Sarvam, Azure); Cognito users for MD / managers with TOTP;
Bedrock model access (check UAE availability or a cross-region inference profile, with client consent).
