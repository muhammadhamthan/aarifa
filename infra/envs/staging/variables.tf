variable "account_id" {
  type = string
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
  type    = string
  default = "10.40.0.0/16"
}

variable "cost_center" {
  type    = string
  default = "kairox-staging"
}

variable "owner" {
  type    = string
  default = "kairox"
}

variable "dr_copy_region" {
  description = "Always null in staging (synthetic data). Present so the provider alias resolves."
  type        = string
  default     = null
}

variable "base_domain" {
  description = "Staging DNS zone, e.g. staging.kairox.ai. Hosts: <company>-api.<base>, <company>-cms.<base>."
  type        = string
}

variable "route53_zone_id" {
  description = "Zone for base_domain in the staging account (null = add records by hand)."
  type        = string
  default     = null
}

variable "companies" {
  description = "Companies hosted in staging => settings."
  type = map(object({
    db_prefix   = string
    valkey_db   = number
    crm_urls    = list(string)
    ses_domain  = string
    cost_center = string
  }))
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_engine_version" {
  type    = string
  default = "18"
}

variable "service_sizes" {
  type = map(object({
    cpu    = number
    memory = number
    min    = number
    max    = number
  }))
  default = {
    api    = { cpu = 512, memory = 1024, min = 1, max = 1 }
    worker = { cpu = 512, memory = 1024, min = 1, max = 1 }
    voice  = { cpu = 1024, memory = 2048, min = 0, max = 1 }
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
  type    = string
  default = null
}

variable "alarm_emails" {
  type = list(string)
}

variable "monthly_budget_usd" {
  type    = number
  default = 600
}

variable "prod_account_ids" {
  description = "Prod accounts that pull promoted images from the staging registry."
  type        = list(string)
  default     = []
}

variable "github_repository" {
  type = string
}

variable "create_github_oidc_provider" {
  type    = bool
  default = true
}

variable "state_bucket_name" {
  type = string
}
