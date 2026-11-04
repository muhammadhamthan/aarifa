variable "name" {
  description = "Instance identifier, e.g. kx-pack-prod."
  type        = string
}

variable "env" {
  type = string
}

variable "tenants" {
  description = <<-EOT
    Companies hosted on this instance => database/role name prefix.
    prod:    { pack = "" }                      -> DBs cms, crm, automation, agents; roles crm_app ...
    staging: { pack = "pack_", auto = "auto_" } -> DBs pack_crm, auto_crm ...; roles pack_crm_app ...
  EOT
  type        = map(string)
}

variable "logical_databases" {
  description = "Logical databases per tenant (T19: one server, four databases)."
  type        = list(string)
  default     = ["cms", "crm", "automation", "agents"]
}

variable "engine_version" {
  description = "PostgreSQL version. A major only (\"18\") lets RDS pick the default minor and auto-upgrade minors. Check what me-central-1 offers: aws rds describe-db-engine-versions --engine postgres --region me-central-1 --query \"DBEngineVersions[?starts_with(EngineVersion,'18')].EngineVersion\""
  type        = string
  default     = "18"
}

variable "parameter_group_family" {
  type    = string
  default = "postgres18"
}

variable "instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "allocated_storage_gb" {
  type    = number
  default = 50
}

variable "max_allocated_storage_gb" {
  description = "Storage autoscaling ceiling."
  type        = number
  default     = 200
}

variable "multi_az" {
  type    = bool
  default = true
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "backup_retention_days" {
  type    = number
  default = 14
}

variable "performance_insights_retention_days" {
  description = "7 = free tier."
  type        = number
  default     = 7
}

variable "database_insights_mode" {
  description = "CloudWatch Database Insights mode: standard (free, includes Performance Insights) or advanced."
  type        = string
  default     = "standard"
}

variable "monitoring_interval" {
  description = "Enhanced Monitoring seconds (0 disables)."
  type        = number
  default     = 60
}

variable "subnet_ids" {
  description = "Data-tier subnets (at least two AZs)."
  type        = list(string)
}

variable "vpc_id" {
  type = string
}

variable "client_security_group_ids" {
  description = "Security groups allowed to connect on 5432 (static keys => SG id)."
  type        = map(string)
}

variable "kms_key_arn" {
  type = string
}

variable "dr_copy_region" {
  description = "Region to replicate automated backups to (encrypted). null = OFF. Only with the client's written consent (data stays in the UAE otherwise)."
  type        = string
  default     = null
}

variable "dr_retention_days" {
  type    = number
  default = 7
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "tags" {
  type    = map(string)
  default = {}
}
