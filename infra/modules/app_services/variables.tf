variable "name" {
  description = "Prefix for this company's services, e.g. kx-pack-prod or kx-staging-pack."
  type        = string
}

variable "env" {
  description = "staging | prod (secret / SSM path segment)."
  type        = string
}

variable "kx_env" {
  description = "KX_ENV value: staging | production."
  type        = string
}

variable "company" {
  type = string
}

variable "cluster_arn" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "app_repository_url" {
  description = "ECR URL of the Python image (one image: api | worker | beat | voice | migrate)."
  type        = string
}

variable "cms_repository_url" {
  description = "ECR URL of the Payload CMS image (built from apps/arifaa-cms once it exists)."
  type        = string
}

variable "vpc_id" {
  description = "VPC of the private Cloud Map namespace <company>.<env>.kx.internal (embeddings, phoenix, otel-collector)."
  type        = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_id" {
  type = string
}

variable "api_target_group_arn" {
  type = string
}

variable "cms_target_group_arn" {
  type    = string
  default = null
}

variable "api_port" {
  type    = number
  default = 8000
}

variable "api_health_path" {
  type    = string
  default = "/healthz"
}

variable "cms_port" {
  type    = number
  default = 3000
}

variable "public_api_url" {
  description = "e.g. https://api.arifapack.com"
  type        = string
}

variable "public_cms_url" {
  type    = string
  default = null
}

variable "db_secret_arns" {
  description = "logical (cms, crm, automation, agents, phoenix) => role secret ARN from the database module."
  type        = map(string)
}

variable "redis_secret_arn" {
  description = "Secret with JSON keys url (KX_REDIS_URL) and broker_url (KX_APP_CELERY_BROKER_URL) from the cache module."
  type        = string
}

variable "jwt_secret_arn" {
  description = "Staff-login RS256 key secret (private_key_pem, key_id) from the secrets module."
  type        = string
}

variable "auth_secret_arn" {
  description = "Staff-login encryption key secret (encryption_key) from the secrets module."
  type        = string
}

variable "integration_secret_arns" {
  description = "short name (anthropic, meta_whatsapp, postmark, telnyx, twilio, livekit, deepgram, elevenlabs, betterstack ...) => secret ARN for this company."
  type        = map(string)
}

variable "generated_secret_arns" {
  description = "short name (service_token, cms_webhook_hmac, payload_secret) => secret ARN."
  type        = map(string)
}

variable "bucket_names" {
  type = map(string)
}

variable "bucket_arns" {
  type = map(string)
}

variable "inbound_bucket_arn" {
  description = "SES Mail Manager inbound bucket (optional; buyer replies arrive through Postmark since 11 Nov)."
  type        = string
  default     = null
}

variable "inbound_bucket_name" {
  type    = string
  default = null
}

variable "ses_identity_arn" {
  type = string
}

variable "ses_configuration_set" {
  type = string
}

variable "ses_from_domain" {
  type = string
}

variable "ses_events_topic_arn" {
  description = "SNS topic of SES delivery/bounce/complaint events (only this topic may feed /webhooks/ses)."
  type        = string
  default     = null
}

variable "email_from_address" {
  description = "The one company sending address (SES and the Postmark backup). null = sales@<ses_from_domain>."
  type        = string
  default     = null
}

variable "jwt_issuer" {
  description = "Issuer of the in-house staff access tokens (null = public_api_url)."
  type        = string
  default     = null
}

variable "jwt_audience" {
  type    = string
  default = "kx-crm"
}

variable "cors_origins" {
  description = "Browser origins allowed to call the API (CRM screens, website), sent as KX_APP_CORS_ORIGINS (JSON list)."
  type        = list(string)
  default     = []
}

variable "embedding_model" {
  description = "Model served by the embeddings service (Platform contest R4 winner; runner-up BAAI/bge-m3)."
  type        = string
  default     = "Qwen/Qwen3-Embedding-0.6B"
}

variable "vendor_order" {
  description = "Seed value of /kx/<env>/<company>/vendors/<field> (primary first). Terraform ignores later edits. The app reads `email` today; calls stay Twilio-only until Telnyx is wired (OPEN_QUESTIONS #55)."
  type        = map(list(string))
  default = {
    llm        = ["claude"]
    calls      = ["twilio"]
    email      = ["ses", "postmark"]
    embeddings = ["qwen3", "fulltext"]
    stt        = ["deepgram", "soniox"]
    tts        = ["elevenlabs", "google"]
  }
}

variable "images" {
  description = <<-EOT
    Third-party images (pinned; Terraform owns them, deploys do not touch them). CONFIRM each tag before the
    first apply and bump deliberately:
      embeddings     Text Embeddings Inference CPU image with Qwen3-Embedding support (>= 1.7.2)
      phoenix        Arize Phoenix server (replace "latest" with the version you tested)
      otel_collector OpenTelemetry Collector contrib (needs the awscloudwatch receiver + otlphttp exporter)
  EOT
  type        = map(string)
  default = {
    embeddings     = "ghcr.io/huggingface/text-embeddings-inference:cpu-1.7.2"
    phoenix        = "arizephoenix/phoenix:latest"
    otel_collector = "otel/opentelemetry-collector-contrib:0.120.0"
  }
}

variable "architectures" {
  description = "Fargate CPU architecture per third-party service. X86_64 where an arm64 image is not confirmed."
  type        = map(string)
  default = {
    embeddings     = "X86_64"
    phoenix        = "X86_64"
    otel_collector = "ARM64"
  }
}

variable "phoenix_enable_auth" {
  description = "Phoenix login (PHOENIX_ENABLE_AUTH). Off by default: Phoenix is reachable only inside the app security group; turning it on also needs an API key for trace ingestion."
  type        = bool
  default     = false
}

variable "extra_environment" {
  description = "Additional plain env vars for the Python services."
  type        = map(string)
  default     = {}
}

variable "sizes" {
  description = "Per service: cpu, memory, min, max (desired starts at min). Merged over the module defaults (api, worker, beat, voice, cms, embeddings, phoenix, otel_collector); beat always runs exactly 1."
  type = map(object({
    cpu    = number
    memory = number
    min    = number
    max    = number
  }))
  default = {}
}

variable "capacity_provider" {
  type    = string
  default = "FARGATE"
}

variable "kms_key_arn" {
  type = string
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "db_bootstrap_family" {
  description = "Task family of the instance-level db-bootstrap task (run by deploy before migrate)."
  type        = string
}
