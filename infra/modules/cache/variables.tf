variable "name" {
  type = string
}

variable "env" {
  type = string
}

variable "tenants" {
  description = "Company => Valkey logical DB index (staging shares one node: pack=0, auto=1)."
  type        = map(number)
}

variable "engine_version" {
  description = "Valkey engine version available in me-central-1 (aws elasticache describe-cache-engine-versions --engine valkey)."
  type        = string
  default     = "8.1"
}

variable "parameter_group_family" {
  type    = string
  default = "valkey8"
}

variable "node_type" {
  type    = string
  default = "cache.t4g.micro"
}

variable "replicas" {
  description = "Read replicas in addition to the primary. 1+ enables Multi-AZ automatic failover."
  type        = number
  default     = 0
}

variable "subnet_ids" {
  type = list(string)
}

variable "vpc_id" {
  type = string
}

variable "client_security_group_ids" {
  type = map(string)
}

variable "kms_key_arn" {
  type = string
}

variable "snapshot_retention_days" {
  description = "Cache holds only rebuildable data (rate limits, kill-switch cache); 0-1 is fine."
  type        = number
  default     = 1
}

variable "tags" {
  type    = map(string)
  default = {}
}
