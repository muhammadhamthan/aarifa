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

variable "cognito_domain_prefix" {
  type = string
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

variable "service_sizes" {
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

variable "api_health_path" {
  type    = string
  default = "/healthz"
}

variable "cms_health_path" {
  type    = string
  default = "/admin"
}

variable "bedrock_region" {
  description = "null = Bedrock not used (Anthropic API direct, key in Secrets Manager)."
  type        = string
  default     = null
}

# ---- ops ----------------------------------------------------------------------------
variable "alarm_emails" {
  type = list(string)
}

variable "monthly_budget_usd" {
  type = number
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
