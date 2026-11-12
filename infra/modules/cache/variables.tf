variable "name" {
  type = string
}

variable "env" {
  type = string
}

variable "tenants" {
  description = "Company => Redis logical DB index for the cache (staging shares one node: pack=0, auto=1)."
  type        = map(number)
}

variable "broker_db_offset" {
  description = "Celery broker logical DB = tenant index + this offset (pack=8, auto=9 with the default)."
  type        = number
  default     = 8
}

variable "engine_version" {
  description = "Redis OSS engine version offered in me-central-1 (aws elasticache describe-cache-engine-versions --engine redis)."
  type        = string
  default     = "7.1"
}

variable "parameter_group_family" {
  type    = string
  default = "redis7"
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
  description = "Redis holds rebuildable data only (cache, counters, broker messages re-created from PostgreSQL); 0-1 is fine."
  type        = number
  default     = 1
}

variable "tags" {
  type    = map(string)
  default = {}
}
