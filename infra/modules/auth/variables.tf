variable "name" {
  type = string
}

variable "groups" {
  description = "CRM roles (Role enum in kx.core.auth). Precedence: lower wins when a user is in several."
  type        = map(number)
  default = {
    md      = 1
    admin   = 2
    manager = 3
    sales   = 4
  }
}

variable "callback_urls" {
  description = "CRM (Vercel) OAuth callback URLs."
  type        = list(string)
}

variable "logout_urls" {
  type = list(string)
}

variable "domain_prefix" {
  description = "Cognito managed-login domain prefix (<prefix>.auth.me-central-1.amazoncognito.com)."
  type        = string
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "access_token_minutes" {
  type    = number
  default = 30
}

variable "refresh_token_days" {
  type    = number
  default = 1
}

variable "user_pool_tier" {
  description = "LITE | ESSENTIALS | PLUS (PLUS adds threat protection; ~$0.02/MAU)."
  type        = string
  default     = "ESSENTIALS"
}

variable "tags" {
  type    = map(string)
  default = {}
}
