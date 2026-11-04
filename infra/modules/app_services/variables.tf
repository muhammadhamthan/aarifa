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
  description = "ECR URL of the Python image (one image, three modes)."
  type        = string
}

variable "cms_repository_url" {
  description = "ECR URL of the Payload CMS image (built from apps/arifaa-cms once it exists)."
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
  description = "logical (cms, crm, automation, agents) => role secret ARN from the database module."
  type        = map(string)
}

variable "valkey_secret_arn" {
  type = string
}

variable "integration_secret_arns" {
  description = "short name (meta_whatsapp, twilio ...) => secret ARN for this company."
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

variable "jwt_issuer" {
  type = string
}

variable "jwks_url" {
  type = string
}

variable "jwt_audience" {
  type = string
}

variable "bedrock_region" {
  description = "Region the Bedrock runtime client calls. Anthropic models may only be reachable via cross-region inference profiles (prompts leave the UAE: client consent). null = Bedrock not used (Anthropic API direct)."
  type        = string
  default     = null
}

variable "extra_environment" {
  description = "Additional plain env vars for the Python services."
  type        = map(string)
  default     = {}
}

variable "sizes" {
  description = "Per service: cpu, memory, min, max (desired starts at min)."
  type = map(object({
    cpu    = number
    memory = number
    min    = number
    max    = number
  }))
  default = {
    api    = { cpu = 1024, memory = 2048, min = 2, max = 4 }
    worker = { cpu = 1024, memory = 2048, min = 1, max = 2 }
    voice  = { cpu = 2048, memory = 4096, min = 1, max = 2 }
    cms    = { cpu = 512, memory = 1024, min = 1, max = 1 }
  }
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
