variable "name" {
  type = string
}

variable "ecr_repositories" {
  description = "ECR repositories in this account (images are pushed by the GitHub OIDC deploy role)."
  type        = list(string)
  default     = ["kairox/app", "kairox/cms"]
}

variable "pull_account_ids" {
  description = "Other Kairox accounts allowed to pull (staging registry -> prod accounts, for image promotion)."
  type        = list(string)
  default     = []
}

variable "keep_images" {
  description = "Tagged images kept per repository."
  type        = number
  default     = 30
}

variable "container_insights" {
  description = "enabled | enhanced | disabled"
  type        = string
  default     = "enhanced"
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
