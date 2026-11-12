variable "name" {
  description = "Service / task family name, e.g. kx-pack-prod-api."
  type        = string
}

variable "container_name" {
  type    = string
  default = "app"
}

variable "cluster_arn" {
  type = string
}

variable "cluster_name" {
  type = string
}

variable "image" {
  description = "Full image reference (registry/repo:tag)."
  type        = string
}

variable "command" {
  description = "Container command (the image ENTRYPOINT is `kx`, so [\"api\"] runs `kx api`)."
  type        = list(string)
  default     = null
}

variable "entry_point" {
  description = "Override the image ENTRYPOINT (db-bootstrap uses the psql script)."
  type        = list(string)
  default     = null
}

variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

variable "cpu_architecture" {
  type    = string
  default = "ARM64"
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "secrets" {
  description = "Env var => Secrets Manager valueFrom (\"<secret-arn>:<json-key>::\" or a plain ARN)."
  type        = map(string)
  default     = {}
}

variable "port" {
  description = "Container port (null = no port mapping, e.g. worker / voice / one-off tasks)."
  type        = number
  default     = null
}

variable "container_health_check" {
  description = "ECS container health check command (CMD-SHELL form), null = none."
  type        = list(string)
  default     = null
}

variable "target_group_arn" {
  type    = string
  default = null
}

variable "health_check_grace_seconds" {
  type    = number
  default = 60
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}

variable "create_service" {
  description = "false = only the task definition (one-off tasks: migrate, db-bootstrap)."
  type        = bool
  default     = true
}

variable "desired_count" {
  description = "Initial desired count. Afterwards min_count/max_count bound it (desired_count is ignored after creation so autoscaling is not reset by applies)."
  type        = number
  default     = 1
}

variable "min_count" {
  type    = number
  default = 1
}

variable "max_count" {
  description = "Set max_count = 0 to scale a service to zero (e.g. voice)."
  type        = number
  default     = 1
}

variable "cpu_target_percent" {
  type    = number
  default = 60
}

variable "capacity_provider" {
  description = "FARGATE or FARGATE_SPOT (staging)."
  type        = string
  default     = "FARGATE"
}

variable "readonly_root_filesystem" {
  description = "Mounts a writable /tmp volume when true."
  type        = bool
  default     = true
}

variable "stop_timeout_seconds" {
  description = "SIGTERM -> SIGKILL grace (workers finish their current job; voice ends calls politely)."
  type        = number
  default     = 60
}

variable "deployment_minimum_healthy_percent" {
  description = "100 for request-serving services; 0 for the single Celery Beat (no overlap during a deploy)."
  type        = number
  default     = 100
}

variable "deployment_maximum_percent" {
  description = "200 for most services; 100 for the single Celery Beat."
  type        = number
  default     = 200
}

variable "service_discovery_namespace_id" {
  description = "Cloud Map private DNS namespace id; set to register the tasks as <discovery_name>.<namespace> (private services)."
  type        = string
  default     = null
}

variable "discovery_name" {
  description = "DNS label in the namespace (default: container_name)."
  type        = string
  default     = null
}

variable "enable_execute_command" {
  type    = bool
  default = true
}

variable "task_role_policy_json" {
  description = "Extra IAM policy for the task role (S3, SES, SSM vendor order ...)."
  type        = string
  default     = null
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
