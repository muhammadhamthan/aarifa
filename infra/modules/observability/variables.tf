variable "name" {
  type = string
}

variable "alarm_emails" {
  description = "Ops emails subscribed to the alarm topic (each must confirm the subscription)."
  type        = list(string)
}

variable "alarm_webhook_url" {
  description = "Optional HTTPS endpoint (the API's /v1/webhooks/alarms) that relays alarms to WhatsApp."
  type        = string
  default     = null
}

variable "alb_arn_suffix" {
  type = string
}

variable "target_group_arn_suffixes" {
  description = "name => target group arn_suffix."
  type        = map(string)
}

variable "db_instance_ids" {
  type = list(string)
}

variable "db_allocated_storage_gb" {
  type = number
}

variable "ecs_cluster_name" {
  type = string
}

variable "ecs_services" {
  description = "Services to alarm on (CPU/memory), static keys => service name."
  type        = map(string)
  default     = {}
}

variable "companies" {
  description = "Companies emitting the custom queue-lag metric."
  type        = list(string)
}

variable "queue_lag_threshold_seconds" {
  description = "procrastinate oldest-todo-job age (emitted by the worker as Kairox/QueueLagSeconds)."
  type        = number
  default     = 120
}

variable "monthly_budget_usd" {
  type = number
}

variable "budget_emails" {
  type = list(string)
}

variable "kms_key_arn" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
