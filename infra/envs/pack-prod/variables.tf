variable "company" {
  description = "Company code (KX_COMPANY_CODE), e.g. pack, auto."
  type        = string
}

variable "account_id" {
  description = "AWS account of this company's production (guards against applying to the wrong account)."
  type        = string
}

variable "region" {
  type    = string
  default = "me-central-1"
}

variable "az_ids" {
  type    = list(string)
  default = ["mec1-az1", "mec1-az3"]
}

variable "vpc_cidr" {
  type = string
}

variable "cost_center" {
  type = string
}

variable "owner" {
  type    = string
  default = "kairox"
}

# ---- DNS / hostnames --------------------------------------------------------------
variable "api_host" {
  description = "e.g. api.arifapack.com (Vercel website + CRM call this directly)."
  type        = string
}

variable "cms_host" {
  description = "e.g. cms.arifapack.com (Payload admin)."
  type        = string
}

variable "route53_zone_id" {
  description = "Hosted zone in this account, or null when the client's DNS admin adds records by hand."
  type        = string
  default     = null
}

variable "ses_domain" {
  type = string
}

variable "inbound_mail_enabled" {
  type    = bool
  default = false
}

variable "crm_urls" {
  description = "CRM origins on Vercel, e.g. [\"https://crm.arifapack.com\"]."
  type        = list(string)
}

variable "website_urls" {
  description = "Website origins on Vercel that call /v1/public (chat, voice token), e.g. [\"https://www.arifapack.com\"]."
  type        = list(string)
  default     = []
}

variable "email_from_address" {
  description = "The one company sending address (SES + the Postmark backup). null = sales@<ses_domain>."
  type        = string
  default     = null
}

# ---- sizes ----------------------------------------------------------------------
variable "db_instance_class" {
  type    = string
  default = "db.m6g.large"
}

variable "db_engine_version" {
  type    = string
  default = "18"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 100
}

variable "dr_copy_region" {
  description = "OFF (null) unless the client consents in writing to encrypted backups outside the UAE."
  type        = string
  default     = null
}

variable "cache_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "database_insights_mode" {
  description = "advanced = CloudWatch Database Insights Advanced (slow queries, locks; Recommendation ~US$18/month)."
  type        = string
  default     = "advanced"
}

variable "service_sizes" {
  type = map(object({
    cpu    = number
    memory = number
    min    = number
    max    = number
  }))
  # beat always runs exactly 1 task (min/max ignored); embeddings 1 vCPU + 4 GB per the Recommendation.
  default = {
    api            = { cpu = 1024, memory = 2048, min = 2, max = 4 }
    worker         = { cpu = 1024, memory = 2048, min = 1, max = 2 }
    beat           = { cpu = 256, memory = 512, min = 1, max = 1 }
    voice          = { cpu = 2048, memory = 4096, min = 1, max = 2 }
    cms            = { cpu = 512, memory = 1024, min = 1, max = 1 }
    embeddings     = { cpu = 1024, memory = 4096, min = 1, max = 1 }
    phoenix        = { cpu = 512, memory = 1024, min = 1, max = 1 }
    otel_collector = { cpu = 256, memory = 512, min = 1, max = 1 }
  }
}

variable "api_health_path" {
  type    = string
  default = "/healthz"
}

variable "cms_health_path" {
  type    = string
  default = "/admin"
}

# ---- ops ----------------------------------------------------------------------------
variable "alarm_emails" {
  type = list(string)
}

variable "monthly_budget_usd" {
  description = "AWS Budgets alarm (AWS bill only: Claude, Meta, Postmark, Telnyx, LiveKit, Better Stack, Vercel bill outside AWS)."
  type        = number
}

variable "betterstack_alarm_webhook_url" {
  description = "Optional: Better Stack CloudWatch-integration URL subscribed to the alarm topic (incidents from alarms). Email stays the backup path."
  type        = string
  default     = null
}

variable "github_repository" {
  type = string
}

variable "github_environment" {
  description = "GitHub Environment that deploys here (pack-prod / auto-prod)."
  type        = string
}

variable "create_github_oidc_provider" {
  type    = bool
  default = true
}

variable "staging_account_id" {
  description = "Staging account: source registry for image promotion."
  type        = string
}

variable "state_bucket_name" {
  type = string
}
