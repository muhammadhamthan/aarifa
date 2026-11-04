variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "app_security_group_id" {
  description = "Task security group that receives ALB traffic on the target ports."
  type        = string
}

variable "domain_name" {
  description = "Primary certificate name, e.g. api.arifapack.com."
  type        = string
}

variable "subject_alternative_names" {
  type    = list(string)
  default = []
}

variable "route53_zone_id" {
  description = "Zone for ACM validation + alias records. null = DNS is managed by the client: apply once with -target=module.<alb>.aws_acm_certificate.this, create the validation CNAMEs from the output, then apply again."
  type        = string
  default     = null
}

variable "targets" {
  description = "Host-routed target groups: key => { host, port, health_path }."
  type = map(object({
    host        = string
    port        = number
    health_path = string
    priority    = number
  }))
}

variable "idle_timeout_seconds" {
  description = "High enough for SSE chat streams."
  type        = number
  default     = 300
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "rate_limit_per_5min" {
  description = "Requests per 5 minutes per client IP across the whole ALB."
  type        = number
  default     = 2000
}

variable "ai_rate_limit_per_5min" {
  description = "Tighter limit for the expensive public AI endpoints (chat, voice token)."
  type        = number
  default     = 300
}

variable "ai_path_prefixes" {
  type    = list(string)
  default = ["/v1/voice/token", "/v1/conversations"]
}

variable "blocked_country_codes" {
  description = "Optional geo block list (ISO alpha-2). Empty = none (buyers are worldwide)."
  type        = list(string)
  default     = []
}

variable "kms_key_arn" {
  description = "For the WAF log group."
  type        = string
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "tags" {
  type    = map(string)
  default = {}
}
