variable "name" {
  type = string
}

variable "domain" {
  description = "Sending domain verified in SES, e.g. arifapack.com (staging: staging.kairox.ai subdomain)."
  type        = string
}

variable "mail_from_subdomain" {
  description = "Custom MAIL FROM (bounce) subdomain, e.g. \"mail\" -> mail.arifapack.com. null = amazonses.com."
  type        = string
  default     = "mail"
}

variable "route53_zone_id" {
  description = "If the domain's zone is in this account, DKIM/MAIL FROM records are created; otherwise they are output for the client's DNS admin."
  type        = string
  default     = null
}

variable "event_webhook_url" {
  description = "HTTPS endpoint (API) subscribed to SES delivery/bounce/complaint events, e.g. https://api.arifapack.com/v1/webhooks/ses. null = no subscription yet."
  type        = string
  default     = null
}

variable "inbound_mail_enabled" {
  description = <<-EOT
    Reply capture. Classic SES receiving is not offered in me-central-1, but SES Mail Manager is
    (Middle East (UAE) since Dec 2025). This toggle creates the inbound bucket, SNS topic and the IAM
    role Mail Manager writes with. The ingress point / rule set are NOT managed here: the AWS provider
    has no Mail Manager resources at the time of writing - create them per infra/README.md (or with
    the awscc provider) and confirm availability on day 1. Fallback: Postmark inbound webhook.
  EOT
  type        = bool
  default     = false
}

variable "inbound_retention_days" {
  description = "Raw inbound MIME kept this long (the parsed message lives in the automation DB)."
  type        = number
  default     = 30
}

variable "kms_key_arn" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
