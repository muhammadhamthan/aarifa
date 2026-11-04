variable "name" {
  description = "Name prefix, e.g. kx-pack-prod."
  type        = string
}

variable "env" {
  description = "staging | prod"
  type        = string
}

variable "companies" {
  description = "Company codes whose integration secrets live in this account (prod: one, staging: pack + auto)."
  type        = list(string)
}

variable "integrations" {
  description = <<-EOT
    Third-party secrets created as empty shells (JSON with placeholder keys). Values are set by a human
    with `aws secretsmanager put-secret-value`; Terraform ignores later changes. Key = secret short name,
    value = list of JSON keys. ECS tasks reference them as "<arn>:<json-key>::".
  EOT
  type        = map(list(string))
  default = {
    meta_whatsapp = ["access_token", "app_secret", "verify_token", "phone_number_id", "waba_id"]
    twilio        = ["account_sid", "api_key_sid", "api_key_secret", "auth_token", "from_number"]
    anthropic     = ["api_key"]
    livekit       = ["url", "api_key", "api_secret"]
    elevenlabs    = ["api_key"]
    sarvam        = ["api_key"]
    azure_speech  = ["key", "region"]
    langfuse      = ["host", "public_key", "secret_key"]
    sentry        = ["dsn"]
    turnstile     = ["secret_key"]
  }
}

variable "generated" {
  description = "Secrets Terraform generates itself (random 48-char value under key `value`)."
  type        = list(string)
  default     = ["service_token", "cms_webhook_hmac", "payload_secret"]
}

variable "kms_decrypt_account_ids" {
  description = "Other accounts allowed kms:Decrypt via ECR (cross-account image pulls from this registry)."
  type        = list(string)
  default     = []
}

variable "recovery_window_days" {
  type    = number
  default = 7
}

variable "kms_deletion_window_days" {
  type    = number
  default = 30
}

variable "tags" {
  type    = map(string)
  default = {}
}
