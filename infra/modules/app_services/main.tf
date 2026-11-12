# One company's runtime (11 Nov topology, docs/DEPLOYMENT.md):
#   Python image, five modes   kx api (x2) | kx worker (Celery) | kx beat (exactly 1) | kx voice | kx migrate
#   Payload CMS image          cms
#   Private third-party        embeddings (Qwen3-Embedding-0.6B on Text Embeddings Inference),
#                              phoenix (Arize Phoenix AI traces), otel-collector (masked logs -> Better Stack)
# The deployed app/cms image tags live in SSM and are written by .github/workflows/deploy.yml; Terraform reads
# them back so applies never roll back a deploy. Third-party images are pinned by var.images (Terraform owns them).
# Private services are reached by DNS inside the VPC: <service>.<company>.<env>.kx.internal (Cloud Map).

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_ssm_parameter" "app_tag" {
  # checkov:skip=CKV2_AWS_34: holds an image tag / resource names, not a secret
  name           = "/kx/${var.env}/${var.company}/image_tag/app"
  type           = "String"
  insecure_value = "bootstrap"
  description    = "Deployed kairox/app tag (written by the deploy workflow)"
  tags           = var.tags

  lifecycle {
    ignore_changes = [insecure_value]
  }
}

resource "aws_ssm_parameter" "cms_tag" {
  # checkov:skip=CKV2_AWS_34: holds an image tag / resource names, not a secret
  name           = "/kx/${var.env}/${var.company}/image_tag/cms"
  type           = "String"
  insecure_value = "bootstrap"
  description    = "Deployed kairox/cms tag (written by the deploy workflow)"
  tags           = var.tags

  lifecycle {
    ignore_changes = [insecure_value]
  }
}

# ---- vendor order per field (Fallback plan: "the order changes without a deploy") ---------------
# The app reads /kx/<env>/<company>/vendors/<field> (comma-separated, first = primary) and caches it
# for about a minute; the admin override writes the same parameter. Terraform only seeds the default.
resource "aws_ssm_parameter" "vendor_order" {
  # checkov:skip=CKV2_AWS_34: vendor names, not a secret
  for_each       = var.vendor_order
  name           = "/kx/${var.env}/${var.company}/vendors/${each.key}"
  type           = "String"
  insecure_value = join(",", each.value)
  description    = "Vendor order for ${each.key} (primary first). Edit to switch vendors without a deploy."
  tags           = var.tags

  lifecycle {
    ignore_changes = [insecure_value]
  }
}

# ---- private DNS for the internal services ---------------------------------------------------
resource "aws_service_discovery_private_dns_namespace" "this" {
  name        = "${var.company}.${var.env}.kx.internal"
  description = "Private service names for ${var.name} (embeddings, phoenix, otel-collector)"
  vpc         = var.vpc_id
  tags        = var.tags
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  app_tag   = aws_ssm_parameter.app_tag.insecure_value
  cms_tag   = aws_ssm_parameter.cms_tag.insecure_value
  app_image = "${var.app_repository_url}:${local.app_tag}"
  cms_image = "${var.cms_repository_url}:${local.cms_tag}"
  # Until the first image is pushed (and db-bootstrap has created the databases), services exist with zero tasks.
  app_ready = local.app_tag != "bootstrap"
  cms_ready = local.cms_tag != "bootstrap" && var.cms_target_group_arn != null

  sizes = merge(local.default_sizes, var.sizes)
  default_sizes = {
    api            = { cpu = 1024, memory = 2048, min = 2, max = 4 }
    worker         = { cpu = 1024, memory = 2048, min = 1, max = 2 }
    beat           = { cpu = 256, memory = 512, min = 1, max = 1 }
    voice          = { cpu = 2048, memory = 4096, min = 1, max = 2 }
    cms            = { cpu = 512, memory = 1024, min = 1, max = 1 }
    embeddings     = { cpu = 1024, memory = 4096, min = 1, max = 1 }
    phoenix        = { cpu = 512, memory = 1024, min = 1, max = 1 }
    otel_collector = { cpu = 256, memory = 512, min = 1, max = 1 }
  }

  namespace       = aws_service_discovery_private_dns_namespace.this.name
  embedding_url   = "http://embeddings.${local.namespace}"
  phoenix_otlp    = "http://phoenix.${local.namespace}:6006/v1/traces"
  vendor_prefix   = "/kx/${var.env}/${var.company}/vendors"
  jwt_issuer      = coalesce(var.jwt_issuer, var.public_api_url)
  from_address    = coalesce(var.email_from_address, "sales@${var.ses_from_domain}")
  python_services = ["api", "worker", "beat", "voice"]

  integ = var.integration_secret_arns
  gen   = var.generated_secret_arns

  # ---- secrets (Secrets Manager valueFrom) ------------------------------------------------------
  # Names match the settings classes (KX_*, KX_AUTO_*, KX_APP_*), .env.example and docs/DEPLOYMENT.md (the app
  # ignores unknown names, so a typo fails silently: keep them in step). Telnyx env is added once Telnyx is wired
  # (OPEN_QUESTIONS #55; its secret shell exists). Vendor SDKs that
  # read their own env (LiveKit plugins: LIVEKIT_*, DEEPGRAM_API_KEY, ELEVEN_API_KEY, SONIOX_API_KEY) get those.
  secrets_db = {
    KX_CRM_DSN               = "${var.db_secret_arns["crm"]}:dsn::"
    KX_AUTOMATION_DSN        = "${var.db_secret_arns["automation"]}:dsn::"
    KX_AGENTS_DSN            = "${var.db_secret_arns["agents"]}:dsn::"
    KX_REDIS_URL             = "${var.redis_secret_arn}:url::"
    KX_APP_CELERY_BROKER_URL = "${var.redis_secret_arn}:broker_url::"
  }
  secrets_common = {
    KX_SERVICE_TOKEN_SECRET  = "${local.gen["service_token"]}:value::"
    KX_APP_SENTRY_DSN        = "${local.integ["betterstack"]}:sentry_dsn::"
    KX_APP_ANTHROPIC_API_KEY = "${local.integ["anthropic"]}:api_key::"
    KX_APP_GEMINI_API_KEY    = "${local.integ["gemini"]}:api_key::"
    KX_APP_CMS_API_KEY       = "${local.integ["cms"]}:api_key::"
  }
  # Staff login (in-house, D26): only the api signs tokens and reads TOTP seeds.
  secrets_auth = {
    KX_JWT_PRIVATE_KEY      = "${var.jwt_secret_arn}:private_key_pem::"
    KX_JWT_KEY_ID           = "${var.jwt_secret_arn}:key_id::"
    KX_AUTH__ENCRYPTION_KEY = "${var.auth_secret_arn}:encryption_key::"
  }
  secrets_channels = {
    KX_AUTO_WHATSAPP__ACCESS_TOKEN     = "${local.integ["meta_whatsapp"]}:access_token::"
    KX_AUTO_WHATSAPP__APP_SECRET       = "${local.integ["meta_whatsapp"]}:app_secret::"
    KX_AUTO_WHATSAPP__VERIFY_TOKEN     = "${local.integ["meta_whatsapp"]}:verify_token::"
    KX_AUTO_WHATSAPP__PHONE_NUMBER_ID  = "${local.integ["meta_whatsapp"]}:phone_number_id::"
    KX_AUTO_POSTMARK__SERVER_TOKEN     = "${local.integ["postmark"]}:server_token::"
    KX_AUTO_POSTMARK__INBOUND_USER     = "${local.integ["postmark"]}:inbound_username::"
    KX_AUTO_POSTMARK__INBOUND_PASSWORD = "${local.integ["postmark"]}:inbound_password::"
    KX_AUTO_TWILIO__ACCOUNT_SID        = "${local.integ["twilio"]}:account_sid::"
    KX_AUTO_TWILIO__AUTH_TOKEN         = "${local.integ["twilio"]}:auth_token::"
    KX_AUTO_TWILIO__FROM_NUMBER        = "${local.integ["twilio"]}:from_number::"
    KX_APP_CMS_WEBHOOK_SECRET          = "${local.gen["cms_webhook_hmac"]}:value::"
    KX_APP_TURNSTILE_SECRET_KEY        = "${local.integ["turnstile"]}:secret_key::"
  }
  # The api issues LiveKit room tokens for the website (KX_APP_LIVEKIT_*).
  secrets_livekit_app = {
    KX_APP_LIVEKIT_URL        = "${local.integ["livekit"]}:url::"
    KX_APP_LIVEKIT_API_KEY    = "${local.integ["livekit"]}:api_key::"
    KX_APP_LIVEKIT_API_SECRET = "${local.integ["livekit"]}:api_secret::"
  }
  # The voice worker: LiveKit Agents + speech plugins read these names themselves.
  secrets_voice_plugins = {
    LIVEKIT_URL                    = "${local.integ["livekit"]}:url::"
    LIVEKIT_API_KEY                = "${local.integ["livekit"]}:api_key::"
    LIVEKIT_API_SECRET             = "${local.integ["livekit"]}:api_secret::"
    DEEPGRAM_API_KEY               = "${local.integ["deepgram"]}:api_key::"
    ELEVEN_API_KEY                 = "${local.integ["elevenlabs"]}:api_key::"
    SONIOX_API_KEY                 = "${local.integ["soniox"]}:api_key::"
    KX_APP_GOOGLE_CREDENTIALS_JSON = "${local.integ["google"]}:credentials_json::"
  }
  # Celery: the worker runs the heartbeat task Beat schedules, so both carry the heartbeat URL.
  secrets_jobs = {
    KX_APP_BETTERSTACK_HEARTBEAT_URL = "${local.integ["betterstack"]}:heartbeat_url::"
  }

  secrets_api    = merge(local.secrets_db, local.secrets_common, local.secrets_auth, local.secrets_channels, local.secrets_livekit_app)
  secrets_worker = merge(local.secrets_db, local.secrets_common, local.secrets_channels, local.secrets_jobs)
  secrets_voicew = merge(local.secrets_db, local.secrets_common, local.secrets_livekit_app, local.secrets_voice_plugins)
  secrets_migrate = merge(local.secrets_db, {
    KX_MIGRATE_CRM_DSN        = "${var.db_secret_arns["crm"]}:migrate_dsn::"
    KX_MIGRATE_AUTOMATION_DSN = "${var.db_secret_arns["automation"]}:migrate_dsn::"
    KX_MIGRATE_AGENTS_DSN     = "${var.db_secret_arns["agents"]}:migrate_dsn::"
  })

  # ---- plain environment for every Python service -------------------------------------------------
  env_python = merge(
    {
      KX_ENV          = var.kx_env
      KX_COMPANY_CODE = var.company
      KX_AWS_REGION   = local.region
      KX_LOG_JSON     = "true"
      KX_S3_BUCKET    = var.bucket_names["quotes"]
      KX_API_PORT     = tostring(var.api_port)
      KX_HEALTH_PATH  = var.api_health_path
      KX_JWT_ISSUER   = local.jwt_issuer
      KX_JWT_AUDIENCE = var.jwt_audience
      AWS_REGION      = local.region

      KX_APP_API_PORT                   = tostring(var.api_port)
      KX_APP_MEDIA_BUCKET               = var.bucket_names["quotes"]
      KX_APP_INBOUND_MAIL_BUCKET        = coalesce(var.inbound_bucket_name, var.bucket_names["quotes"])
      KX_APP_S3_KMS_KEY_ID              = var.kms_key_arn
      KX_APP_CORS_ORIGINS               = jsonencode(var.cors_origins)
      KX_APP_CMS_URL                    = coalesce(var.public_cms_url, var.public_api_url)
      KX_APP_EMBEDDING_URL              = local.embedding_url
      KX_APP_EMBEDDING_MODEL            = var.embedding_model
      KX_APP_EMBEDDING_TIMEOUT_SECONDS  = "2"
      KX_APP_PHOENIX_COLLECTOR_ENDPOINT = local.phoenix_otlp
      KX_APP_PHOENIX_PROJECT            = var.company
      KX_APP_VENDOR_ORDER_SSM_PREFIX    = local.vendor_prefix
      KX_APP_GEMINI_FALLBACK_ENABLED    = "false"
      KX_APP_PDF_RENDERER               = "chromium"
      KX_APP_CHROMIUM_PATH              = "/usr/bin/chromium"
      KX_APP_SENTRY_ENVIRONMENT         = "${var.env}-${var.company}"

      KX_AUTO_PUBLIC_BASE_URL                  = var.public_api_url
      KX_AUTO_SES__FROM_ADDRESS                = local.from_address
      KX_AUTO_SES__CONFIGURATION_SET           = var.ses_configuration_set
      KX_AUTO_SES__SNS_TOPIC_ARNS              = jsonencode(compact([var.ses_events_topic_arn]))
      KX_AUTO_TWILIO__STATUS_CALLBACK_BASE_URL = var.public_api_url
    },
    var.extra_environment,
  )

  bucket_arns = concat(values(var.bucket_arns), var.inbound_bucket_arn == null ? [] : [var.inbound_bucket_arn])

  # Log groups the OpenTelemetry collector ships to Better Stack (masked at the source by kx.core.logging).
  shipped_log_groups = [for s in local.python_services : "/kx/${var.name}-${s}"]
  otel_config = {
    receivers = {
      awscloudwatch = {
        region = local.region
        logs = {
          poll_interval = "1m"
          groups = {
            named = { for g in local.shipped_log_groups : g => {} }
          }
        }
      }
    }
    processors = {
      batch = {}
    }
    exporters = {
      "otlphttp/betterstack" = {
        endpoint = "https://$${env:BETTERSTACK_INGEST_HOST}"
        headers = {
          Authorization = "Bearer $${env:BETTERSTACK_SOURCE_TOKEN}"
        }
      }
    }
    service = {
      pipelines = {
        logs = {
          receivers  = ["awscloudwatch"]
          processors = ["batch"]
          exporters  = ["otlphttp/betterstack"]
        }
      }
    }
  }
}

# ---- task role policy for the Python services ----------------------------------------
data "aws_iam_policy_document" "python" {
  # checkov:skip=CKV_AWS_356: cloudwatch:PutMetricData has no resource ARNs; limited to namespace Kairox
  # checkov:skip=CKV_AWS_111: cloudwatch:PutMetricData has no resource ARNs; limited to namespace Kairox
  statement {
    sid       = "Buckets"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = [for a in local.bucket_arns : "${a}/*"]
  }
  statement {
    sid       = "ListBuckets"
    actions   = ["s3:ListBucket"]
    resources = local.bucket_arns
  }
  statement {
    sid       = "Kms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
  statement {
    sid     = "SesSend"
    actions = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = [
      var.ses_identity_arn,
      "arn:aws:ses:${local.region}:${local.account_id}:configuration-set/${var.ses_configuration_set}",
    ]
  }
  statement {
    sid       = "KairoxMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Kairox"]
    }
  }
  statement {
    sid     = "ReadVendorOrder"
    actions = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = [
      "arn:aws:ssm:${local.region}:${local.account_id}:parameter${local.vendor_prefix}",
      "arn:aws:ssm:${local.region}:${local.account_id}:parameter${local.vendor_prefix}/*",
    ]
  }
}

# The api also hosts the admin override that forces a vendor on or off (Fallback plan "Manual override").
data "aws_iam_policy_document" "api" {
  source_policy_documents = [data.aws_iam_policy_document.python.json]
  statement {
    sid       = "WriteVendorOrder"
    actions   = ["ssm:PutParameter"]
    resources = ["arn:aws:ssm:${local.region}:${local.account_id}:parameter${local.vendor_prefix}/*"]
  }
}

data "aws_iam_policy_document" "cms" {
  statement {
    sid       = "Media"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${var.bucket_arns["media"]}/*"]
  }
  statement {
    sid       = "ListMedia"
    actions   = ["s3:ListBucket"]
    resources = [var.bucket_arns["media"]]
  }
  statement {
    sid       = "Kms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
}

data "aws_iam_policy_document" "otel" {
  # checkov:skip=CKV_AWS_356: logs:DescribeLogGroups has no resource-level permissions
  # checkov:skip=CKV_AWS_111: logs:DescribeLogGroups has no resource-level permissions
  statement {
    sid       = "DescribeLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadAppLogs"
    actions   = ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"]
    resources = flatten([for g in local.shipped_log_groups : ["arn:aws:logs:${local.region}:${local.account_id}:log-group:${g}", "arn:aws:logs:${local.region}:${local.account_id}:log-group:${g}:*"]])
  }
  statement {
    sid       = "Kms"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

# ---- services ---------------------------------------------------------------------------
module "api" {
  source = "../ecs_service"

  name                   = "${var.name}-api"
  cluster_arn            = var.cluster_arn
  cluster_name           = var.cluster_name
  image                  = local.app_image
  command                = ["api"]
  cpu                    = local.sizes["api"].cpu
  memory                 = local.sizes["api"].memory
  port                   = var.api_port
  target_group_arn       = var.api_target_group_arn
  container_health_check = ["CMD", "python", "/app/healthcheck.py"]
  environment            = merge(local.env_python, { KX_PROCESS = "api" })
  secrets                = local.secrets_api
  subnet_ids             = var.subnet_ids
  security_group_ids     = [var.security_group_id]
  desired_count          = local.app_ready ? local.sizes["api"].min : 0
  min_count              = local.app_ready ? local.sizes["api"].min : 0
  max_count              = local.app_ready ? local.sizes["api"].max : 0
  capacity_provider      = var.capacity_provider
  task_role_policy_json  = data.aws_iam_policy_document.api.json
  stop_timeout_seconds   = 30
  kms_key_arn            = var.kms_key_arn
  log_retention_days     = var.log_retention_days
  tags                   = merge(var.tags, { company = var.company, service = "api" })
}

# Celery worker: deliveries, ladder steps, follow-ups, event handlers (acks_late, one event loop per process).
module "worker" {
  source = "../ecs_service"

  name                  = "${var.name}-worker"
  cluster_arn           = var.cluster_arn
  cluster_name          = var.cluster_name
  image                 = local.app_image
  command               = ["worker"]
  cpu                   = local.sizes["worker"].cpu
  memory                = local.sizes["worker"].memory
  environment           = merge(local.env_python, { KX_PROCESS = "worker" })
  secrets               = local.secrets_worker
  subnet_ids            = var.subnet_ids
  security_group_ids    = [var.security_group_id]
  desired_count         = local.app_ready ? local.sizes["worker"].min : 0
  min_count             = local.app_ready ? local.sizes["worker"].min : 0
  max_count             = local.app_ready ? local.sizes["worker"].max : 0
  capacity_provider     = var.capacity_provider
  task_role_policy_json = data.aws_iam_policy_document.python.json
  # Celery warm shutdown: the current task finishes; acks_late redelivers anything cut off.
  stop_timeout_seconds = 120
  kms_key_arn          = var.kms_key_arn
  log_retention_days   = var.log_retention_days
  tags                 = merge(var.tags, { company = var.company, service = "worker" })
}

# Celery Beat: EXACTLY ONE task, never autoscaled; deploys stop the old Beat before starting the new one.
# Missed ticks are harmless: due work waits in automation.scheduled_jobs and runs on the next sweep.
module "beat" {
  source = "../ecs_service"

  name                               = "${var.name}-beat"
  cluster_arn                        = var.cluster_arn
  cluster_name                       = var.cluster_name
  image                              = local.app_image
  command                            = ["beat"]
  cpu                                = local.sizes["beat"].cpu
  memory                             = local.sizes["beat"].memory
  environment                        = merge(local.env_python, { KX_PROCESS = "beat" })
  secrets                            = local.secrets_worker
  subnet_ids                         = var.subnet_ids
  security_group_ids                 = [var.security_group_id]
  desired_count                      = local.app_ready ? 1 : 0
  min_count                          = local.app_ready ? 1 : 0
  max_count                          = local.app_ready ? 1 : 0
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  capacity_provider                  = var.capacity_provider
  task_role_policy_json              = data.aws_iam_policy_document.python.json
  stop_timeout_seconds               = 30
  kms_key_arn                        = var.kms_key_arn
  log_retention_days                 = var.log_retention_days
  tags                               = merge(var.tags, { company = var.company, service = "beat" })
}

module "voice" {
  source = "../ecs_service"

  name                  = "${var.name}-voice"
  cluster_arn           = var.cluster_arn
  cluster_name          = var.cluster_name
  image                 = local.app_image
  command               = ["voice"]
  cpu                   = local.sizes["voice"].cpu
  memory                = local.sizes["voice"].memory
  environment           = merge(local.env_python, { KX_PROCESS = "voice" })
  secrets               = local.secrets_voicew
  subnet_ids            = var.subnet_ids
  security_group_ids    = [var.security_group_id]
  desired_count         = local.app_ready ? local.sizes["voice"].min : 0
  min_count             = local.app_ready ? local.sizes["voice"].min : 0
  max_count             = local.app_ready ? local.sizes["voice"].max : 0
  capacity_provider     = var.capacity_provider
  task_role_policy_json = data.aws_iam_policy_document.python.json
  # LiveKit drains: running calls get time to finish politely.
  stop_timeout_seconds = 120
  kms_key_arn          = var.kms_key_arn
  log_retention_days   = var.log_retention_days
  tags                 = merge(var.tags, { company = var.company, service = "voice" })
}

module "cms" {
  source = "../ecs_service"

  name             = "${var.name}-cms"
  cluster_arn      = var.cluster_arn
  cluster_name     = var.cluster_name
  image            = local.cms_image
  cpu              = local.sizes["cms"].cpu
  memory           = local.sizes["cms"].memory
  port             = var.cms_port
  target_group_arn = var.cms_target_group_arn
  environment = {
    NODE_ENV                  = "production"
    PORT                      = tostring(var.cms_port)
    PAYLOAD_PUBLIC_SERVER_URL = coalesce(var.public_cms_url, var.public_api_url)
    S3_BUCKET                 = var.bucket_names["media"]
    S3_REGION                 = local.region
    # Payload calls this on publish/unpublish (HMAC-signed): AI re-index + CRM product mirror (SRS §7).
    KX_API_HOOK_URL = "${var.public_api_url}/webhooks/cms"
  }
  secrets = {
    DATABASE_URI        = "${var.db_secret_arns["cms"]}:uri::"
    PAYLOAD_SECRET      = "${local.gen["payload_secret"]}:value::"
    KX_CMS_WEBHOOK_HMAC = "${local.gen["cms_webhook_hmac"]}:value::"
  }
  subnet_ids               = var.subnet_ids
  security_group_ids       = [var.security_group_id]
  create_service           = var.cms_target_group_arn != null
  desired_count            = local.cms_ready ? local.sizes["cms"].min : 0
  min_count                = local.cms_ready ? local.sizes["cms"].min : 0
  max_count                = local.cms_ready ? local.sizes["cms"].max : 0
  capacity_provider        = var.capacity_provider
  readonly_root_filesystem = false # Next.js/Payload write caches next to the app
  task_role_policy_json    = data.aws_iam_policy_document.cms.json
  kms_key_arn              = var.kms_key_arn
  log_retention_days       = var.log_retention_days
  tags                     = merge(var.tags, { company = var.company, service = "cms" })
}

# ---- private third-party services ---------------------------------------------------------
# Knowledge search (D28): Qwen3-Embedding-0.6B served by Hugging Face Text Embeddings Inference (CPU),
# OpenAI-compatible POST /v1/embeddings on port 80. Weights are public and downloaded at start (via NAT);
# buyer questions never leave the VPC. 1 vCPU + 4 GB per the Recommendation.
module "embeddings" {
  source = "../ecs_service"

  name                           = "${var.name}-embeddings"
  container_name                 = "embeddings"
  cluster_arn                    = var.cluster_arn
  cluster_name                   = var.cluster_name
  image                          = var.images["embeddings"]
  command                        = ["--model-id", var.embedding_model, "--port", "80"]
  cpu                            = local.sizes["embeddings"].cpu
  memory                         = local.sizes["embeddings"].memory
  cpu_architecture               = var.architectures["embeddings"]
  port                           = 80
  environment                    = { HUGGINGFACE_HUB_CACHE = "/tmp/hf-cache", HF_HOME = "/tmp/hf-home" }
  subnet_ids                     = var.subnet_ids
  security_group_ids             = [var.security_group_id]
  desired_count                  = local.app_ready ? local.sizes["embeddings"].min : 0
  min_count                      = local.app_ready ? local.sizes["embeddings"].min : 0
  max_count                      = local.app_ready ? local.sizes["embeddings"].max : 0
  capacity_provider              = var.capacity_provider
  readonly_root_filesystem       = false # third-party image writes outside /tmp
  service_discovery_namespace_id = aws_service_discovery_private_dns_namespace.this.id
  discovery_name                 = "embeddings"
  stop_timeout_seconds           = 30
  kms_key_arn                    = var.kms_key_arn
  log_retention_days             = var.log_retention_days
  tags                           = merge(var.tags, { company = var.company, service = "embeddings" })
}

# AI tracing (D29): Arize Phoenix in the UAE; UI + OTLP/HTTP on 6006, OTLP/gRPC on 4317; traces stored in
# the company's `phoenix` database. Reachable only inside the app security group (no ALB rule).
module "phoenix" {
  source = "../ecs_service"

  name             = "${var.name}-phoenix"
  container_name   = "phoenix"
  cluster_arn      = var.cluster_arn
  cluster_name     = var.cluster_name
  image            = var.images["phoenix"]
  cpu              = local.sizes["phoenix"].cpu
  memory           = local.sizes["phoenix"].memory
  cpu_architecture = var.architectures["phoenix"]
  port             = 6006
  environment = {
    PHOENIX_PORT              = "6006"
    PHOENIX_GRPC_PORT         = "4317"
    PHOENIX_WORKING_DIR       = "/tmp/phoenix"
    PHOENIX_ENABLE_AUTH       = var.phoenix_enable_auth ? "true" : "false"
    PHOENIX_TELEMETRY_ENABLED = "false"
  }
  secrets = {
    PHOENIX_SQL_DATABASE_URL = "${var.db_secret_arns["phoenix"]}:url::"
  }
  subnet_ids                     = var.subnet_ids
  security_group_ids             = [var.security_group_id]
  desired_count                  = local.app_ready ? local.sizes["phoenix"].min : 0
  min_count                      = local.app_ready ? local.sizes["phoenix"].min : 0
  max_count                      = local.app_ready ? local.sizes["phoenix"].max : 0
  capacity_provider              = var.capacity_provider
  readonly_root_filesystem       = false # third-party image writes outside /tmp
  service_discovery_namespace_id = aws_service_discovery_private_dns_namespace.this.id
  discovery_name                 = "phoenix"
  stop_timeout_seconds           = 30
  kms_key_arn                    = var.kms_key_arn
  log_retention_days             = var.log_retention_days
  tags                           = merge(var.tags, { company = var.company, service = "phoenix" })
}

# Monitoring (D29): ships the (already PII-masked) app logs from CloudWatch to Better Stack. Raw logs stay
# in CloudWatch, which is also the backup path when Better Stack ingestion fails (Fallback plan).
module "otel_collector" {
  source = "../ecs_service"

  name             = "${var.name}-otel-collector"
  container_name   = "otel-collector"
  cluster_arn      = var.cluster_arn
  cluster_name     = var.cluster_name
  image            = var.images["otel_collector"]
  command          = ["--config=env:OTEL_COLLECTOR_CONFIG"]
  cpu              = local.sizes["otel_collector"].cpu
  memory           = local.sizes["otel_collector"].memory
  cpu_architecture = var.architectures["otel_collector"]
  environment = {
    OTEL_COLLECTOR_CONFIG = yamlencode(local.otel_config)
  }
  secrets = {
    BETTERSTACK_SOURCE_TOKEN = "${local.integ["betterstack"]}:log_source_token::"
    BETTERSTACK_INGEST_HOST  = "${local.integ["betterstack"]}:ingest_host::"
  }
  subnet_ids                     = var.subnet_ids
  security_group_ids             = [var.security_group_id]
  desired_count                  = local.app_ready ? local.sizes["otel_collector"].min : 0
  min_count                      = local.app_ready ? local.sizes["otel_collector"].min : 0
  max_count                      = local.app_ready ? local.sizes["otel_collector"].max : 0
  capacity_provider              = var.capacity_provider
  task_role_policy_json          = data.aws_iam_policy_document.otel.json
  service_discovery_namespace_id = aws_service_discovery_private_dns_namespace.this.id
  discovery_name                 = "otel-collector"
  stop_timeout_seconds           = 30
  kms_key_arn                    = var.kms_key_arn
  log_retention_days             = var.log_retention_days
  tags                           = merge(var.tags, { company = var.company, service = "otel-collector" })
}

# ---- one-off: kx migrate ------------------------------------------------------------------
module "migrate" {
  source = "../ecs_service"

  name                   = "${var.name}-migrate"
  cluster_arn            = var.cluster_arn
  cluster_name           = var.cluster_name
  image                  = local.app_image
  command                = ["migrate"]
  cpu                    = 512
  memory                 = 1024
  environment            = merge(local.env_python, { KX_PROCESS = "migrate" })
  secrets                = local.secrets_migrate
  subnet_ids             = var.subnet_ids
  security_group_ids     = [var.security_group_id]
  create_service         = false
  enable_execute_command = false
  kms_key_arn            = var.kms_key_arn
  log_retention_days     = var.log_retention_days
  tags                   = merge(var.tags, { company = var.company, service = "migrate" })
}

# ---- deploy manifest: the deploy workflow reads this instead of hard-coding names ----------
locals {
  deploy_manifest = {
    company = var.company
    cluster = var.cluster_name
    # Services that run the app image and are rolled on every deploy (Beat rolls 0/100: no overlap).
    services = compact([module.api.service_name, module.worker.service_name, module.beat.service_name, module.voice.service_name])
    # Third-party images pinned in Terraform (var.images); deploy only waits for them to be stable.
    internal_services = compact([module.embeddings.service_name, module.phoenix.service_name, module.otel_collector.service_name])
    cms_service       = module.cms.service_name
    migrate_family    = module.migrate.task_definition_family
    subnets           = var.subnet_ids
    security_group    = var.security_group_id
    app_tag_param     = aws_ssm_parameter.app_tag.name
    cms_tag_param     = aws_ssm_parameter.cms_tag.name
    app_repository    = var.app_repository_url
    cms_repository    = var.cms_repository_url
    db_bootstrap      = var.db_bootstrap_family
  }
}

resource "aws_ssm_parameter" "deploy" {
  # checkov:skip=CKV2_AWS_34: holds an image tag / resource names, not a secret
  name           = "/kx/${var.env}/${var.company}/deploy"
  type           = "String"
  insecure_value = jsonencode(local.deploy_manifest)
  description    = "Deploy manifest for .github/workflows/deploy.yml"
  tags           = var.tags
}
