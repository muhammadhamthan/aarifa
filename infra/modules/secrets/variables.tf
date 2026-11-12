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
    Vendors follow docs/Arifaa Platform - Platforms, Pricing & Fallbacks/Recommendation.md (11 Nov).
    Optional backups (gemini, soniox, google) stay "CHANGE_ME" until Arifa approves them; the app keeps
    them switched off until then.
  EOT
  type        = map(list(string))
  default = {
    # Claude API (Sonnet 5.5 money turns, Haiku 5.5 the rest)
    anthropic = ["api_key"]
    # optional AI backup: off until the 50-scenario suite passes and Arifa agrees
    gemini        = ["api_key"]
    meta_whatsapp = ["access_token", "app_secret", "verify_token", "phone_number_id", "waba_id"]
    # SES send backup + buyer replies (inbound webhook Basic auth)
    postmark = ["server_token", "inbound_username", "inbound_password"]
    # staff alert calls: Telnyx primary (TeXML application), Twilio backup
    telnyx = ["api_key", "application_id", "from_number", "public_key"]
    twilio = ["account_sid", "auth_token", "from_number"]
    # website voice: LiveKit Cloud, Deepgram STT, ElevenLabs TTS; Soniox / Google Chirp optional backups
    livekit     = ["url", "api_key", "api_secret"]
    deepgram    = ["api_key"]
    elevenlabs  = ["api_key"]
    soniox      = ["api_key"]
    google      = ["credentials_json"]
    betterstack = ["sentry_dsn", "heartbeat_url", "log_source_token", "ingest_host"]
    turnstile   = ["secret_key"]
    # Payload API key used by the knowledge-base indexer
    cms = ["api_key"]
  }
}

variable "generated" {
  description = "Secrets Terraform generates itself (random 48-char value under key `value`)."
  type        = list(string)
  default     = ["service_token", "cms_webhook_hmac", "payload_secret"]
}

variable "jwt_key_id_prefix" {
  description = "Staff login signs RS256 access tokens; the key id (kid) is <prefix>-<company>-<n>. Rotate by tainting tls_private_key.jwt and bumping jwt_key_generation."
  type        = string
  default     = "kx"
}

variable "jwt_key_generation" {
  description = "Bump together with a taint of tls_private_key.jwt to publish a new kid."
  type        = number
  default     = 1
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
