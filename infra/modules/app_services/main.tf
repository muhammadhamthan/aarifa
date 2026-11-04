# One company's runtime: the Python image in three modes (`kx api`, `kx worker`, `kx voice`), the
# Payload CMS image, and the one-off `kx migrate` task. The deployed image tag lives in SSM and is
# written by .github/workflows/deploy.yml; Terraform reads it back so applies never roll back a deploy.

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

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region

  app_tag   = aws_ssm_parameter.app_tag.insecure_value
  cms_tag   = aws_ssm_parameter.cms_tag.insecure_value
  app_image = "${var.app_repository_url}:${local.app_tag}"
  cms_image = "${var.cms_repository_url}:${local.cms_tag}"
  # Until the first image is pushed, services exist with zero tasks.
  app_ready = local.app_tag != "bootstrap"
  cms_ready = local.cms_tag != "bootstrap" && var.cms_target_group_arn != null

  integ = var.integration_secret_arns
  gen   = var.generated_secret_arns

  # Env var names: SDK-native where the SDK reads them itself (LiveKit plugins, Langfuse, Sentry,
  # Anthropic, Twilio), KX_* otherwise.
  secrets_db = {
    KX_CRM_DSN        = "${var.db_secret_arns["crm"]}:dsn::"
    KX_AUTOMATION_DSN = "${var.db_secret_arns["automation"]}:dsn::"
    KX_AGENTS_DSN     = "${var.db_secret_arns["agents"]}:dsn::"
    KX_REDIS_URL      = "${var.valkey_secret_arn}:url::"
  }
  secrets_common = {
    KX_SERVICE_TOKEN_SECRET = "${local.gen["service_token"]}:value::"
    SENTRY_DSN              = "${local.integ["sentry"]}:dsn::"
    LANGFUSE_HOST           = "${local.integ["langfuse"]}:host::"
    LANGFUSE_PUBLIC_KEY     = "${local.integ["langfuse"]}:public_key::"
    LANGFUSE_SECRET_KEY     = "${local.integ["langfuse"]}:secret_key::"
    ANTHROPIC_API_KEY       = "${local.integ["anthropic"]}:api_key::"
  }
  secrets_channels = {
    KX_META_ACCESS_TOKEN    = "${local.integ["meta_whatsapp"]}:access_token::"
    KX_META_APP_SECRET      = "${local.integ["meta_whatsapp"]}:app_secret::"
    KX_META_VERIFY_TOKEN    = "${local.integ["meta_whatsapp"]}:verify_token::"
    KX_META_PHONE_NUMBER_ID = "${local.integ["meta_whatsapp"]}:phone_number_id::"
    KX_META_WABA_ID         = "${local.integ["meta_whatsapp"]}:waba_id::"
    TWILIO_ACCOUNT_SID      = "${local.integ["twilio"]}:account_sid::"
    TWILIO_API_KEY          = "${local.integ["twilio"]}:api_key_sid::"
    TWILIO_API_SECRET       = "${local.integ["twilio"]}:api_key_secret::"
    TWILIO_AUTH_TOKEN       = "${local.integ["twilio"]}:auth_token::"
    KX_TWILIO_FROM_NUMBER   = "${local.integ["twilio"]}:from_number::"
    KX_CMS_WEBHOOK_HMAC     = "${local.gen["cms_webhook_hmac"]}:value::"
    KX_TURNSTILE_SECRET     = "${local.integ["turnstile"]}:secret_key::"
  }
  secrets_voice = {
    LIVEKIT_URL         = "${local.integ["livekit"]}:url::"
    LIVEKIT_API_KEY     = "${local.integ["livekit"]}:api_key::"
    LIVEKIT_API_SECRET  = "${local.integ["livekit"]}:api_secret::"
    ELEVEN_API_KEY      = "${local.integ["elevenlabs"]}:api_key::"
    SARVAM_API_KEY      = "${local.integ["sarvam"]}:api_key::"
    AZURE_SPEECH_KEY    = "${local.integ["azure_speech"]}:key::"
    AZURE_SPEECH_REGION = "${local.integ["azure_speech"]}:region::"
  }
  # api issues LiveKit tokens, so it needs the LiveKit key too.
  secrets_api    = merge(local.secrets_db, local.secrets_common, local.secrets_channels, local.secrets_voice)
  secrets_worker = merge(local.secrets_db, local.secrets_common, local.secrets_channels)
  secrets_voicew = merge(local.secrets_db, local.secrets_common, local.secrets_voice)
  secrets_migrate = merge(local.secrets_db, {
    KX_MIGRATE_CRM_DSN        = "${var.db_secret_arns["crm"]}:migrate_dsn::"
    KX_MIGRATE_AUTOMATION_DSN = "${var.db_secret_arns["automation"]}:migrate_dsn::"
    KX_MIGRATE_AGENTS_DSN     = "${var.db_secret_arns["agents"]}:migrate_dsn::"
  })

  env_python = merge(
    {
      KX_ENV                   = var.kx_env
      KX_COMPANY_CODE          = var.company
      KX_AWS_REGION            = local.region
      KX_LOG_JSON              = "true"
      KX_JWT_ISSUER            = var.jwt_issuer
      KX_JWKS_URL              = var.jwks_url
      KX_JWT_AUDIENCE          = var.jwt_audience
      KX_S3_BUCKET             = var.bucket_names["quotes"]
      KX_S3_BUCKET_MEDIA       = var.bucket_names["media"]
      KX_S3_BUCKET_QUOTES      = var.bucket_names["quotes"]
      KX_S3_BUCKET_AUDIO       = var.bucket_names["audio"]
      KX_S3_BUCKET_EXPORTS     = var.bucket_names["exports"]
      KX_SES_CONFIGURATION_SET = var.ses_configuration_set
      KX_SES_FROM_DOMAIN       = var.ses_from_domain
      KX_PUBLIC_API_URL        = var.public_api_url
      KX_API_PORT              = tostring(var.api_port)
      KX_HEALTH_PATH           = var.api_health_path
      KX_BEDROCK_REGION        = coalesce(var.bedrock_region, "disabled")
      AWS_REGION               = local.region
      SENTRY_ENVIRONMENT       = "${var.env}-${var.company}"
      OTEL_SERVICE_NAME_PREFIX = var.name
    },
    var.extra_environment,
  )

  bucket_arns = concat(values(var.bucket_arns), var.inbound_bucket_arn == null ? [] : [var.inbound_bucket_arn])
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
    sid       = "QueueLagMetric"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Kairox"]
    }
  }
  dynamic "statement" {
    for_each = var.bedrock_region == null ? [] : [1]
    content {
      sid     = "BedrockAnthropic"
      actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      # Cross-region inference profiles route to foundation models in other regions.
      resources = [
        "arn:aws:bedrock:*::foundation-model/anthropic.*",
        "arn:aws:bedrock:*:${local.account_id}:inference-profile/*",
      ]
    }
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

# ---- services ---------------------------------------------------------------------------
module "api" {
  source = "../ecs_service"

  name                   = "${var.name}-api"
  cluster_arn            = var.cluster_arn
  cluster_name           = var.cluster_name
  image                  = local.app_image
  command                = ["api"]
  cpu                    = var.sizes["api"].cpu
  memory                 = var.sizes["api"].memory
  port                   = var.api_port
  target_group_arn       = var.api_target_group_arn
  container_health_check = ["CMD", "python", "/app/healthcheck.py"]
  environment            = merge(local.env_python, { KX_PROCESS = "api" })
  secrets                = local.secrets_api
  subnet_ids             = var.subnet_ids
  security_group_ids     = [var.security_group_id]
  desired_count          = local.app_ready ? var.sizes["api"].min : 0
  min_count              = local.app_ready ? var.sizes["api"].min : 0
  max_count              = local.app_ready ? var.sizes["api"].max : 0
  capacity_provider      = var.capacity_provider
  task_role_policy_json  = data.aws_iam_policy_document.python.json
  stop_timeout_seconds   = 30
  kms_key_arn            = var.kms_key_arn
  log_retention_days     = var.log_retention_days
  tags                   = merge(var.tags, { company = var.company, service = "api" })
}

module "worker" {
  source = "../ecs_service"

  name                  = "${var.name}-worker"
  cluster_arn           = var.cluster_arn
  cluster_name          = var.cluster_name
  image                 = local.app_image
  command               = ["worker"]
  cpu                   = var.sizes["worker"].cpu
  memory                = var.sizes["worker"].memory
  environment           = merge(local.env_python, { KX_PROCESS = "worker" })
  secrets               = local.secrets_worker
  subnet_ids            = var.subnet_ids
  security_group_ids    = [var.security_group_id]
  desired_count         = local.app_ready ? var.sizes["worker"].min : 0
  min_count             = local.app_ready ? var.sizes["worker"].min : 0
  max_count             = local.app_ready ? var.sizes["worker"].max : 0
  capacity_provider     = var.capacity_provider
  task_role_policy_json = data.aws_iam_policy_document.python.json
  stop_timeout_seconds  = 120
  kms_key_arn           = var.kms_key_arn
  log_retention_days    = var.log_retention_days
  tags                  = merge(var.tags, { company = var.company, service = "worker" })
}

module "voice" {
  source = "../ecs_service"

  name                  = "${var.name}-voice"
  cluster_arn           = var.cluster_arn
  cluster_name          = var.cluster_name
  image                 = local.app_image
  command               = ["voice"]
  cpu                   = var.sizes["voice"].cpu
  memory                = var.sizes["voice"].memory
  environment           = merge(local.env_python, { KX_PROCESS = "voice" })
  secrets               = local.secrets_voicew
  subnet_ids            = var.subnet_ids
  security_group_ids    = [var.security_group_id]
  desired_count         = local.app_ready ? var.sizes["voice"].min : 0
  min_count             = local.app_ready ? var.sizes["voice"].min : 0
  max_count             = local.app_ready ? var.sizes["voice"].max : 0
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
  cpu              = var.sizes["cms"].cpu
  memory           = var.sizes["cms"].memory
  port             = var.cms_port
  target_group_arn = var.cms_target_group_arn
  environment = {
    NODE_ENV                  = "production"
    PORT                      = tostring(var.cms_port)
    PAYLOAD_PUBLIC_SERVER_URL = coalesce(var.public_cms_url, var.public_api_url)
    S3_BUCKET                 = var.bucket_names["media"]
    S3_REGION                 = local.region
    KX_API_HOOK_URL           = "${var.public_api_url}/hooks/cms"
  }
  secrets = {
    DATABASE_URI        = "${var.db_secret_arns["cms"]}:uri::"
    PAYLOAD_SECRET      = "${local.gen["payload_secret"]}:value::"
    KX_CMS_WEBHOOK_HMAC = "${local.gen["cms_webhook_hmac"]}:value::"
  }
  subnet_ids               = var.subnet_ids
  security_group_ids       = [var.security_group_id]
  create_service           = var.cms_target_group_arn != null
  desired_count            = local.cms_ready ? var.sizes["cms"].min : 0
  min_count                = local.cms_ready ? var.sizes["cms"].min : 0
  max_count                = local.cms_ready ? var.sizes["cms"].max : 0
  capacity_provider        = var.capacity_provider
  readonly_root_filesystem = false # Next.js/Payload write caches next to the app
  task_role_policy_json    = data.aws_iam_policy_document.cms.json
  kms_key_arn              = var.kms_key_arn
  log_retention_days       = var.log_retention_days
  tags                     = merge(var.tags, { company = var.company, service = "cms" })
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
    company        = var.company
    cluster        = var.cluster_name
    services       = compact([module.api.service_name, module.worker.service_name, module.voice.service_name])
    cms_service    = module.cms.service_name
    migrate_family = module.migrate.task_definition_family
    subnets        = var.subnet_ids
    security_group = var.security_group_id
    app_tag_param  = aws_ssm_parameter.app_tag.name
    cms_tag_param  = aws_ssm_parameter.cms_tag.name
    app_repository = var.app_repository_url
    cms_repository = var.cms_repository_url
    db_bootstrap   = var.db_bootstrap_family
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
