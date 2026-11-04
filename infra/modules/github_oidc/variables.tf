variable "name" {
  description = "Role name prefix, e.g. kx-pack-prod."
  type        = string
}

variable "repository" {
  description = "GitHub owner/repo allowed to assume the roles."
  type        = string
}

variable "environments" {
  description = "GitHub Environments that deploy into this account (staging account: [\"staging\"])."
  type        = list(string)
}

variable "create_oidc_provider" {
  description = "false if the account already has token.actions.githubusercontent.com."
  type        = bool
  default     = true
}

variable "ecr_repository_arns" {
  type = list(string)
}

variable "ecs_cluster_arn" {
  type = string
}

variable "passable_role_prefix" {
  description = "Deploy may pass ECS task/execution roles whose names start with this."
  type        = string
}

variable "ssm_image_tag_prefix" {
  description = "SSM path prefix of the deployed-image-tag parameters, e.g. /kx/prod."
  type        = string
}

variable "promotion_source_account_id" {
  description = "Staging account whose ECR images prod deploys copy (cross-account pull). null in staging."
  type        = string
  default     = null
}

variable "state_bucket_name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
